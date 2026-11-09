"""Logical backups of the PostgreSQL database, and a verified restore into a scratch database (ADR 0011).

`backup`: pg_dump in custom format, taken from one exported snapshot, plus a manifest holding, for every
table, its column list, row count and a SHA-256 of its contents, computed from the SAME snapshot (so a
backup taken while the app is writing is still consistent). The manifest also records the dump file's own
SHA-256.

`restore-verify`: checks the dump file against the manifest, restores it with pg_restore into a brand-new
scratch database (never into an existing one), recomputes the same per-table fingerprints there, compares,
and drops the scratch database. It exits non-zero on any difference.

This proves a restore works on PostgreSQL we can reach. The Cloud SQL automated backups, point-in-time
recovery and the cloud restore rehearsal are PENDING-OWNER (docs/STORAGE_AND_BACKUPS.md).

pg_dump and pg_restore are the PostgreSQL client tools; they must be at least as new as the server (checked
here). Passwords reach them through the environment (PGPASSWORD), never the command line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import incident_report
from incident_report.config import ConfigError, Settings

MANIFEST_FORMAT = 1
_PACKAGE_DIR = Path(incident_report.__file__).resolve().parent

# libpq connection keyword -> the environment variable libpq reads for it. The variable names were checked
# against the strings in the installed libpq (libpq.so.5, PostgreSQL 16.15) and match the libpq documentation.
_KEYWORD_TO_ENV = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "passfile": "PGPASSFILE",
    "options": "PGOPTIONS",
}

# Make row text identical on the source and on the restored copy, whatever each session defaults to.
_SESSION_SETTINGS = (
    "SET TIME ZONE 'UTC'",
    "SET DateStyle = 'ISO, MDY'",
    "SET IntervalStyle = 'postgres'",
    "SET extra_float_digits = 3",
    "SET bytea_output = 'hex'",
)

_LIST_TABLES = """
    SELECT n.nspname, c.relname, c.oid
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition
      AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%%'
    ORDER BY n.nspname, c.relname"""

_LIST_COLUMNS = """
    SELECT a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
    FROM pg_attribute a WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum"""


class BackupError(RuntimeError):
    """A backup or restore could not be done. The message is plain and safe to show (no passwords)."""


@dataclass(frozen=True)
class BackupResult:
    dump_path: Path
    manifest_path: Path
    manifest: dict[str, Any]


@dataclass
class VerifyResult:
    ok: bool
    scratch_database: str | None
    tables_checked: int = 0
    mismatches: list[str] = field(default_factory=list)
    report: list[str] = field(default_factory=list)  # one human line per table checked


# --- connection details and tools --------------------------------------------------------------------


def libpq_env(database_url: str) -> dict[str, str]:
    """Connection details for pg_dump / pg_restore as PG* environment variables (database name excluded)."""
    env: dict[str, str] = {}
    for key, value in conninfo_to_dict(database_url).items():
        if key == "dbname":
            continue
        if key not in _KEYWORD_TO_ENV:
            raise BackupError(
                f"The connection option {key!r} in the database URL cannot be passed to pg_dump/pg_restore; remove it."
            )
        env[_KEYWORD_TO_ENV[key]] = str(value)
    return env


def database_name(database_url: str) -> str | None:
    name = conninfo_to_dict(database_url).get("dbname")
    return str(name) if name else None


def find_pg_tool(name: str, pg_bin_dir: Path | None = None) -> Path:
    folder = pg_bin_dir or (Path(os.environ["INCIDENT_PG_BIN_DIR"]) if os.environ.get("INCIDENT_PG_BIN_DIR") else None)
    if folder is not None:
        for candidate in (folder / name, folder / f"{name}.exe"):
            if candidate.is_file():
                return candidate
    else:
        found = shutil.which(name)
        if found:
            return Path(found)
    raise BackupError(
        f"Cannot find {name}. Install the PostgreSQL client tools (the same major version as the server or newer) "
        "and put their bin folder on PATH, or set INCIDENT_PG_BIN_DIR to it."
    )


def client_major(version_output: str) -> int | None:
    match = re.search(r"\(PostgreSQL\)\s+(\d+)", version_output)
    return int(match.group(1)) if match else None


def check_client_version(tool: str, version_output: str, *, server_version_num: int) -> None:
    """pg_dump and pg_restore must not be older than the server's major version."""
    major = client_major(version_output)
    if major is None:
        raise BackupError(f"Cannot read the version of {tool} from {version_output.strip()!r}.")
    server_major = server_version_num // 10000
    if major < server_major:
        raise BackupError(
            f"{tool} is version {major} but the server is {server_major}. "
            f"Install PostgreSQL client tools {server_major} or newer (or set INCIDENT_PG_BIN_DIR)."
        )


def _tool_for(name: str, server_version_num: int, pg_bin_dir: Path | None) -> Path:
    tool = find_pg_tool(name, pg_bin_dir)
    result = subprocess.run([str(tool), "--version"], capture_output=True, text=True, check=False)  # noqa: S603
    check_client_version(name, result.stdout, server_version_num=server_version_num)
    return tool


def _run(args: list[str], connection_env: dict[str, str], what: str) -> None:
    result = subprocess.run(  # noqa: S603  # fixed argument list, no shell; the executable was located above
        args, env={**os.environ, **connection_env}, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise BackupError(f"{what} failed (exit {result.returncode}): {result.stderr.strip()}")


# --- fingerprints --------------------------------------------------------------------------------------


def _use_session_settings(conn: psycopg.Connection) -> None:
    for statement in _SESSION_SETTINGS:
        conn.execute(statement)


def table_fingerprints(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per user table: schema, name, column list, row count and a SHA-256 of its contents.

    The hash covers every row as PostgreSQL's own text form of the row, sorted bytewise (COLLATE "C") so
    physical order and collation do not matter, with each row length-prefixed so row boundaries are
    unambiguous. Rows are streamed from the server, never all held in memory.
    """
    tables = conn.execute(_LIST_TABLES).fetchall()
    fingerprints: list[dict[str, Any]] = []
    for schema, name, oid in tables:
        columns = [r[0] for r in conn.execute(_LIST_COLUMNS, (oid,))]
        query = sql.SQL('SELECT r FROM (SELECT t::text AS r FROM {} t) s ORDER BY r COLLATE "C"').format(
            sql.Identifier(schema, name)
        )
        digest = hashlib.sha256()
        rows = 0
        with conn.cursor(name=f"fp_{uuid.uuid4().hex}") as cursor:
            cursor.execute(query)
            for (text,) in cursor:
                data = text.encode("utf-8")
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
                rows += 1
        fingerprints.append(
            {"schema": schema, "name": name, "columns": columns, "rows": rows, "sha256": digest.hexdigest()}
        )
    return fingerprints


def compare_fingerprints(expected: Sequence[Mapping[str, Any]], actual: Sequence[Mapping[str, Any]]) -> list[str]:
    """Human-readable differences between the backup's fingerprints (expected) and the restored ones."""
    want = {f"{t['schema']}.{t['name']}": t for t in expected}
    got = {f"{t['schema']}.{t['name']}": t for t in actual}
    problems: list[str] = []
    for label in sorted(want.keys() - got.keys()):
        problems.append(f"{label}: table is missing after the restore")
    for label in sorted(got.keys() - want.keys()):
        problems.append(f"{label}: table appeared after the restore but is not in the backup manifest")
    for label in sorted(want.keys() & got.keys()):
        w, g = want[label], got[label]
        if w["columns"] != g["columns"]:
            problems.append(f"{label}: columns differ ({w['columns']} in the backup, {g['columns']} after the restore)")
        if w["rows"] != g["rows"]:
            problems.append(f"{label}: row count is {w['rows']} in the backup but {g['rows']} after the restore")
        if w["sha256"] != g["sha256"]:
            problems.append(f"{label}: contents differ (checksum {w['sha256'][:12]} vs {g['sha256'][:12]})")
    return problems


# --- backup --------------------------------------------------------------------------------------------


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def manifest_path_for(dump_path: Path) -> Path:
    return dump_path.with_name(dump_path.name + ".manifest.json")


def backup_database(
    database_url: str, out_dir: Path, *, now: datetime | None = None, pg_bin_dir: Path | None = None
) -> BackupResult:
    """Dump the database to `out_dir` (custom format) with a manifest. Never leaves partial output."""
    out_dir = out_dir.resolve()
    if out_dir == _PACKAGE_DIR or _PACKAGE_DIR in out_dir.parents:
        raise BackupError("The backup folder must not be inside the app's package directory.")
    env = libpq_env(database_url)
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        conn = psycopg.connect(database_url)
    except psycopg.Error as exc:
        raise BackupError(f"Cannot connect to the database to back it up: {exc}") from None
    with conn:
        name = conn.info.dbname
        pg_dump = _tool_for("pg_dump", conn.info.server_version, pg_bin_dir)
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", name)
        dump_path = out_dir / f"{safe_name}-{stamp:%Y%m%dT%H%M%S%f}Z.dump"
        manifest_path = manifest_path_for(dump_path)
        if dump_path.exists() or manifest_path.exists():
            raise BackupError(f"{dump_path.name} already exists; backups are never replaced.")
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = dump_path.with_name(dump_path.name + ".partial")
        try:
            # One snapshot for both the dump and the fingerprints, so they describe the same moment.
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            with conn.transaction():
                snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()
                assert snapshot is not None  # noqa: S101  # pg_export_snapshot always returns a row
                _use_session_settings(conn)
                _run(
                    [
                        str(pg_dump),
                        "--format=custom",
                        "--no-owner",
                        "--no-acl",
                        "--no-password",
                        f"--snapshot={snapshot[0]}",
                        f"--file={partial}",
                        f"--dbname={name}",
                    ],
                    env,
                    "pg_dump",
                )
                tables = table_fingerprints(conn)
            partial.chmod(0o600)
            digest, size = _sha256_file(partial)
            manifest: dict[str, Any] = {
                "format": MANIFEST_FORMAT,
                "created_at": stamp.isoformat(),
                "source_database": name,
                "server_version": conn.info.parameter_status("server_version"),
                "pg_dump_version": client_major_text(pg_dump),
                "dump_file": dump_path.name,
                "dump_sha256": digest,
                "dump_bytes": size,
                "tables": tables,
            }
            manifest_partial = manifest_path.with_name(manifest_path.name + ".partial")
            manifest_partial.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            manifest_partial.chmod(0o600)
            os.replace(partial, dump_path)
            os.replace(manifest_partial, manifest_path)
        except BaseException:
            for leftover in (
                partial,
                dump_path,
                manifest_path,
                manifest_path.with_name(manifest_path.name + ".partial"),
            ):
                leftover.unlink(missing_ok=True)
            raise
    return BackupResult(dump_path, manifest_path, manifest)


def client_major_text(tool: Path) -> str:
    result = subprocess.run([str(tool), "--version"], capture_output=True, text=True, check=False)  # noqa: S603
    return result.stdout.strip()


# --- restore and verify -----------------------------------------------------------------------------------


def _load_manifest(dump_path: Path) -> dict[str, Any]:
    path = manifest_path_for(dump_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BackupError(f"The manifest {path.name} is missing next to the backup; cannot verify a restore.") from None
    except (OSError, ValueError) as exc:
        raise BackupError(f"The manifest {path.name} cannot be read: {exc}") from None
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT or "tables" not in manifest:
        raise BackupError(f"The manifest {path.name} is not a format-{MANIFEST_FORMAT} backup manifest.")
    return manifest


SCRATCH_PREFIX = "restore_scratch"
_SCRATCH_PREFIX_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,29}")  # leaves room inside PostgreSQL's 63-byte name limit


def restore_and_verify(
    dump_path: Path,
    *,
    admin_url: str,
    keep: bool = False,
    pg_bin_dir: Path | None = None,
    scratch_prefix: str = SCRATCH_PREFIX,
) -> VerifyResult:
    """Restore the dump into a NEW scratch database on the server `admin_url` points at, and verify it.

    `admin_url` must belong to a role that may CREATE DATABASE; only its server and credentials are used.
    The scratch database is dropped afterwards unless `keep` is true. `scratch_prefix` starts its name; callers
    that share a server with other sessions (the tests) pass a unique one so their databases never mix.
    """
    if not _SCRATCH_PREFIX_PATTERN.fullmatch(scratch_prefix):
        raise BackupError("The scratch database prefix must be lowercase letters, digits and underscores (max 30).")
    manifest = _load_manifest(dump_path)
    if not dump_path.is_file():
        raise BackupError(f"The backup file {dump_path} does not exist.")
    digest, _ = _sha256_file(dump_path)
    if digest != manifest.get("dump_sha256"):
        return VerifyResult(
            ok=False,
            scratch_database=None,
            mismatches=["the backup file's checksum does not match its manifest (damaged or replaced); not restored"],
        )
    env = libpq_env(admin_url)
    try:
        admin = psycopg.connect(admin_url, autocommit=True)
    except psycopg.Error as exc:
        raise BackupError(f"Cannot connect to the scratch server: {exc}") from None
    with admin:
        pg_restore = _tool_for("pg_restore", admin.info.server_version, pg_bin_dir)
        scratch = f"{scratch_prefix}_{datetime.now(UTC):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}"
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(scratch)))
        try:
            _run(
                [
                    str(pg_restore),
                    "--no-owner",
                    "--no-acl",
                    "--no-password",
                    "--exit-on-error",
                    "--single-transaction",
                    f"--dbname={scratch}",
                    str(dump_path),
                ],
                env,
                "pg_restore",
            )
            info = conninfo_to_dict(admin_url)
            info["dbname"] = scratch
            with psycopg.connect(make_conninfo(**info)) as conn:  # type: ignore[arg-type]
                conn.read_only = True
                _use_session_settings(conn)
                restored = table_fingerprints(conn)
        finally:
            if not keep:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch)))
    try:
        mismatches = compare_fingerprints(manifest["tables"], restored)
    except (KeyError, TypeError):
        raise BackupError(
            f"The manifest {manifest_path_for(dump_path).name} is malformed (a table entry is incomplete)."
        ) from None
    report = [f"{t['schema']}.{t['name']}: {t['rows']} rows" for t in restored]
    return VerifyResult(
        ok=not mismatches, scratch_database=scratch, tables_checked=len(restored), mismatches=mismatches, report=report
    )


# --- command line -----------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m incident_report.backup", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("backup", "dump DATABASE_URL to a folder"), ("restore-verify", "restore and verify")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--pg-bin-dir", type=Path, help="folder with pg_dump/pg_restore (default: PATH)")
        if name == "backup":
            command.add_argument("--out-dir", type=Path, help="folder for the backup (default: INCIDENT_BACKUP_DIR)")
        else:
            command.add_argument("dump", type=Path, help="a .dump file made by the backup command")
            command.add_argument("--keep", action="store_true", help="keep the scratch database for inspection")
    return parser


def main(argv: Sequence[str]) -> int:
    """Return 0 on success, 1 when the backup or verification failed, 2 for missing configuration."""
    args = _parser().parse_args(list(argv))
    try:
        if args.command == "backup":
            out = args.out_dir or (
                Path(os.environ["INCIDENT_BACKUP_DIR"]) if os.environ.get("INCIDENT_BACKUP_DIR") else None
            )
            if out is None:
                print("Give --out-dir or set INCIDENT_BACKUP_DIR (a folder outside the repository).", file=sys.stderr)
                return 2
            settings = Settings.from_env()
            print(f"environment: {settings.environment.value}")
            result = backup_database(settings.database_url, out, pg_bin_dir=args.pg_bin_dir)
            print(f"backup:   {result.dump_path}")
            print(f"manifest: {result.manifest_path}")
            for t in result.manifest["tables"]:
                print(f"  {t['schema']}.{t['name']}: {t['rows']} rows")
            return 0
        admin_url = os.environ.get("SCRATCH_ADMIN_DATABASE_URL", "").strip()
        if not admin_url:
            print(
                "Set SCRATCH_ADMIN_DATABASE_URL to a PostgreSQL server where a scratch database may be created.",
                file=sys.stderr,
            )
            return 2
        verdict = restore_and_verify(args.dump, admin_url=admin_url, keep=args.keep, pg_bin_dir=args.pg_bin_dir)
    except (BackupError, ConfigError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    for line in verdict.report:
        print(f"  {line}")
    if verdict.ok:
        kept = f" (kept as {verdict.scratch_database})" if args.keep else ""
        print(
            f"PASS: restored into scratch database {verdict.scratch_database}{kept}; {verdict.tables_checked} table(s) match the backup."
        )
        return 0
    for problem in verdict.mismatches:
        print(f"  MISMATCH {problem}")
    print("FAIL: the restored copy does not match the backup manifest.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
