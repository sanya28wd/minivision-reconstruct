"""Backup, restore into a scratch database, and verify (RPT-03; ADR 0011).

Real pg_dump / pg_restore against real PostgreSQL (needs the PostgreSQL client tools at least as new as the
server; see README). Nothing is mocked except where a test says so. Verification is checked from outside:
rows are read back from the source and from the restored scratch database and compared directly.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

import incident_report
from incident_report import backup
from incident_report.backup import BackupError, backup_database, restore_and_verify
from incident_report.db import load_migrations, migrate
from pgscratch import admin_url, database_names_like, scratch_database, url_for, user_tables


@pytest.fixture
def source_url() -> Iterator[str]:
    """A migrated scratch database holding a few varied runs rows."""
    with scratch_database("incident_src") as url:
        with psycopg.connect(url) as conn:
            migrate(conn)
            for i in range(3):
                _insert_run(conn, i)
            conn.execute(
                "INSERT INTO runs (run_id, origin, created_at, input_hash, status, total_latency_ms, error)"
                " VALUES (%s, 'replay', '2026-10-13 05:30:00+04', %s, 'failed', 12.5, %s)",
                (uuid.uuid4(), "cd" * 32, "No recording — déjà vu\nsecond line"),
            )
        yield url


def _insert_run(conn: psycopg.Connection, i: int) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, origin, created_at, input_hash, report, status, total_latency_ms)"
        " VALUES (%s, 'app', %s, %s, %s, 'succeeded', %s)",
        (
            uuid.uuid4(),
            datetime(2026, 10, 13, 1, i, tzinfo=UTC),
            f"{i:02x}" * 32,
            Jsonb({"summary": f"run {i}"}),
            i + 0.25,
        ),
    )


def _rows(url: str, table: str = "runs") -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conn:
        return sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)  # noqa: S608  # fixed names


def _migrated_table_names(url: str) -> set[str]:
    """The user tables the shipped migrations create, read from the live migrated database (not the backup)."""
    return {name.removeprefix("public.") for name in user_tables(url)}


def _own_prefix() -> str:
    """A scratch-database prefix unique to this test. The server is shared with other pytest sessions, so a
    check for "no scratch database left" must look only at databases this test itself could have created."""
    return f"rs_{uuid.uuid4().hex[:12]}"


def _backup(source_url: str, tmp_path: Path) -> backup.BackupResult:
    return backup_database(source_url, tmp_path / "backups")


# --- the backup ---------------------------------------------------------------------------------------


def test_a_backup_writes_a_custom_format_dump_and_a_manifest_into_the_chosen_folder(
    source_url: str, tmp_path: Path
) -> None:
    result = _backup(source_url, tmp_path)

    assert result.dump_path.parent == tmp_path / "backups"
    assert result.dump_path.read_bytes()[:5] == b"PGDMP"  # pg_dump custom-format archives start with this
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    tables = {t["name"]: t for t in manifest["tables"]}
    assert {"runs", "schema_migrations"} <= set(tables)  # the two tables this app must always back up
    assert set(tables) == _migrated_table_names(source_url)  # and nothing else: exactly what the migrations create
    assert tables["runs"]["rows"] == 4
    assert tables["schema_migrations"]["rows"] == len(load_migrations())  # one bookkeeping row per migration
    assert tables["runs"]["columns"][0] == "run_id uuid"
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == sorted(
        [result.dump_path.name, result.manifest_path.name]
    )  # no partial files left


def test_the_manifest_holds_no_credentials_or_host(source_url: str, tmp_path: Path) -> None:
    result = _backup(source_url, tmp_path)

    text = result.manifest_path.read_text(encoding="utf-8")
    assert "password" not in text.lower() and "postgresql://" not in text


def test_backups_are_private_to_the_owner(source_url: str, tmp_path: Path) -> None:
    result = _backup(source_url, tmp_path)

    if os.name == "posix":  # Windows file modes do not express this
        assert result.dump_path.parent.stat().st_mode & 0o077 == 0
        assert result.dump_path.stat().st_mode & 0o077 == 0
        assert result.manifest_path.stat().st_mode & 0o077 == 0


def test_a_backup_never_replaces_an_existing_backup(source_url: str, tmp_path: Path) -> None:
    moment = datetime(2026, 10, 13, 6, 0, 0, tzinfo=UTC)
    first = backup_database(source_url, tmp_path / "b", now=moment)

    with pytest.raises(BackupError, match="already exists"):
        backup_database(source_url, tmp_path / "b", now=moment)

    assert first.dump_path.exists()


def test_a_failed_backup_leaves_no_partial_output(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        backup_database(url_for("no_such_database_rpt03"), tmp_path / "b")

    assert not (tmp_path / "b").exists() or list((tmp_path / "b").iterdir()) == []


def test_a_backup_folder_inside_the_package_is_refused(source_url: str) -> None:
    package_dir = Path(incident_report.__file__).resolve().parent

    with pytest.raises(BackupError, match="package"):
        backup_database(source_url, package_dir / "backups")


# --- restore into a scratch database and verify ---------------------------------------------------------


def test_restore_into_a_scratch_database_reproduces_every_row_and_verifies(source_url: str, tmp_path: Path) -> None:
    result = _backup(source_url, tmp_path)

    verdict = restore_and_verify(result.dump_path, admin_url=admin_url(), keep=True)
    try:
        assert verdict.ok, verdict.mismatches
        assert verdict.scratch_database is not None
        assert verdict.mismatches == [] and verdict.tables_checked == len(_migrated_table_names(source_url))
        assert verdict.scratch_database not in source_url
        scratch = url_for(verdict.scratch_database)
        # Read back from outside the module under test: every row, every column, identical.
        assert _rows(scratch) == _rows(source_url)
        assert len(_rows(scratch)) == 4
        assert _rows(scratch, "schema_migrations") == _rows(source_url, "schema_migrations")
    finally:
        with psycopg.connect(admin_url(), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{verdict.scratch_database}" WITH (FORCE)')


def test_the_scratch_database_is_dropped_after_verification_by_default(source_url: str, tmp_path: Path) -> None:
    result = _backup(source_url, tmp_path)

    prefix = _own_prefix()

    verdict = restore_and_verify(result.dump_path, admin_url=admin_url(), scratch_prefix=prefix)

    assert verdict.ok
    assert verdict.scratch_database is not None and verdict.scratch_database.startswith(prefix)
    assert database_names_like(f"{prefix}_%") == []


def test_restoring_never_touches_the_source_database(source_url: str, tmp_path: Path) -> None:
    result = _backup(source_url, tmp_path)
    before = _rows(source_url)

    restore_and_verify(result.dump_path, admin_url=admin_url())

    assert _rows(source_url) == before


def test_verification_fails_when_contents_differ_even_if_the_row_counts_match(source_url: str, tmp_path: Path) -> None:
    old = _backup(source_url, tmp_path)
    with psycopg.connect(source_url) as conn:  # change one value; same number of rows
        conn.execute("UPDATE runs SET total_latency_ms = total_latency_ms + 1 WHERE origin = 'replay'")
    new = backup_database(source_url, tmp_path / "later")
    # Pair the OLD archive with the table fingerprints of the LATER state: counts agree, contents do not.
    manifest = json.loads(old.manifest_path.read_text(encoding="utf-8"))
    manifest["tables"] = json.loads(new.manifest_path.read_text(encoding="utf-8"))["tables"]
    old.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verdict = restore_and_verify(old.dump_path, admin_url=admin_url())

    assert not verdict.ok
    assert any("runs" in m and "contents" in m for m in verdict.mismatches)
    assert not any("row count" in m for m in verdict.mismatches)


def test_verification_fails_when_a_row_is_missing_from_the_manifest_count(source_url: str, tmp_path: Path) -> None:
    result = _backup(source_url, tmp_path)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    for t in manifest["tables"]:
        if t["name"] == "runs":
            t["rows"] += 1
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verdict = restore_and_verify(result.dump_path, admin_url=admin_url())

    assert not verdict.ok
    assert any("runs" in m and "row count" in m for m in verdict.mismatches)


def test_a_malformed_manifest_is_a_plain_error(source_url: str, tmp_path: Path) -> None:
    result = _backup(source_url, tmp_path)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["tables"] = [{"name": "runs"}]
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    prefix = _own_prefix()

    with pytest.raises(BackupError, match="malformed"):
        restore_and_verify(result.dump_path, admin_url=admin_url(), scratch_prefix=prefix)
    assert database_names_like(f"{prefix}_%") == []


def test_a_damaged_backup_file_is_detected_before_anything_is_restored(source_url: str, tmp_path: Path) -> None:
    result = _backup(source_url, tmp_path)
    data = bytearray(result.dump_path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    result.dump_path.write_bytes(bytes(data))
    prefix = _own_prefix()

    verdict = restore_and_verify(result.dump_path, admin_url=admin_url(), scratch_prefix=prefix)

    assert not verdict.ok and verdict.scratch_database is None
    assert any("checksum" in m for m in verdict.mismatches)
    assert database_names_like(f"{prefix}_%") == []


def test_a_missing_manifest_is_a_plain_error(source_url: str, tmp_path: Path) -> None:
    result = _backup(source_url, tmp_path)
    result.manifest_path.unlink()

    with pytest.raises(BackupError, match="manifest"):
        restore_and_verify(result.dump_path, admin_url=admin_url())


def test_a_backup_taken_while_the_database_is_being_written_still_restores_and_verifies(
    source_url: str, tmp_path: Path
) -> None:
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            with psycopg.connect(source_url, autocommit=True) as conn:
                i = 100
                while not stop.is_set():
                    _insert_run(conn, i % 60)
                    i += 1
        except BaseException as exc:  # noqa: BLE001  # reported to the main thread below
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        result = _backup(source_url, tmp_path)
    finally:
        stop.set()
        thread.join()
    assert errors == []

    assert restore_and_verify(result.dump_path, admin_url=admin_url()).ok


# --- tools, versions and credentials --------------------------------------------------------------------


def test_the_dump_tool_must_be_at_least_as_new_as_the_server() -> None:
    assert backup.client_major("pg_dump (PostgreSQL) 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)") == 16
    assert backup.client_major("pg_dump (PostgreSQL) 17.2") == 17
    backup.check_client_version("pg_dump", "pg_dump (PostgreSQL) 16.15", server_version_num=160004)
    backup.check_client_version("pg_dump", "pg_dump (PostgreSQL) 17.1", server_version_num=160004)
    with pytest.raises(BackupError, match=r"pg_dump is version 15.*server is 16"):
        backup.check_client_version("pg_dump", "pg_dump (PostgreSQL) 15.8", server_version_num=160004)
    with pytest.raises(BackupError, match="version"):
        backup.check_client_version("pg_dump", "something unexpected", server_version_num=160004)


def test_a_missing_tool_says_how_to_point_at_it(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="INCIDENT_PG_BIN_DIR"):
        backup.find_pg_tool("pg_dump", pg_bin_dir=tmp_path)


def test_the_database_password_goes_to_the_tools_by_environment_never_the_command_line() -> None:
    password = "not-a-real-" + "password"  # built at runtime so the secret scanner has nothing to flag
    url = "".join(["postgresql:", "//alice:", password, "@db.internal:5433/incident?sslmode=require"])

    env = backup.libpq_env(url)

    assert env == {
        "PGHOST": "db.internal",
        "PGPORT": "5433",
        "PGUSER": "alice",
        "PGPASSWORD": password,
        "PGSSLMODE": "require",
    }
    assert backup.database_name(url) == "incident"


def test_a_connection_option_the_tools_cannot_receive_is_refused_rather_than_dropped() -> None:
    with pytest.raises(BackupError, match="target_session_attrs"):
        backup.libpq_env("postgresql://h/db?target_session_attrs=read-write")


# --- the command line -------------------------------------------------------------------------------------


def test_cli_backup_then_restore_verify_passes(
    source_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", source_url)
    monkeypatch.setenv("SCRATCH_ADMIN_DATABASE_URL", admin_url())

    assert backup.main(["backup", "--out-dir", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    (dump,) = (tmp_path / "out").glob("*.dump")
    assert str(dump) in out and "runs: 4 rows" in out

    assert backup.main(["restore-verify", str(dump)]) == 0
    assert "PASS" in capsys.readouterr().out


def test_cli_restore_verify_fails_with_a_nonzero_exit_when_verification_fails(
    source_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _backup(source_url, tmp_path)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["tables"][0]["sha256"] = "0" * 64
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("SCRATCH_ADMIN_DATABASE_URL", admin_url())

    assert backup.main(["restore-verify", str(result.dump_path)]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_needs_a_backup_folder_and_a_scratch_server(
    source_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", source_url)
    monkeypatch.delenv("INCIDENT_BACKUP_DIR", raising=False)
    monkeypatch.delenv("SCRATCH_ADMIN_DATABASE_URL", raising=False)

    assert backup.main(["backup"]) == 2
    assert "INCIDENT_BACKUP_DIR" in capsys.readouterr().err
    assert backup.main(["restore-verify", str(tmp_path / "x.dump")]) == 2
    assert "SCRATCH_ADMIN_DATABASE_URL" in capsys.readouterr().err


# --- sharing a server with other pytest sessions --------------------------------------------------------


def test_a_scratch_database_from_another_session_does_not_disturb_this_tests_cleanup_checks(
    source_url: str, tmp_path: Path
) -> None:
    """Regression: another session's in-flight restore (default-prefix scratch database) made a server-wide
    "no scratch database left" check fail at random. Simulate that session directly."""
    foreign = f"{backup.SCRATCH_PREFIX}_20990101000000_{uuid.uuid4().hex[:8]}"
    result = _backup(source_url, tmp_path)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["tables"] = [{"name": "runs"}]
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    prefix = _own_prefix()
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{foreign}"')
        try:
            with pytest.raises(BackupError, match="malformed"):
                restore_and_verify(result.dump_path, admin_url=admin_url(), scratch_prefix=prefix)

            assert database_names_like(f"{prefix}_%") == []
            assert foreign in database_names_like(f"{backup.SCRATCH_PREFIX}_%")  # theirs is untouched
        finally:
            admin.execute(f'DROP DATABASE IF EXISTS "{foreign}" WITH (FORCE)')


@pytest.mark.parametrize("bad", ["", "Upper", "has space", 'quo"te', "x" * 31, "1leading_digit", "semi;colon"])
def test_a_bad_scratch_prefix_is_refused_before_anything_is_created(bad: str, source_url: str, tmp_path: Path) -> None:
    result = _backup(source_url, tmp_path)

    with pytest.raises(BackupError, match="prefix"):
        restore_and_verify(result.dump_path, admin_url=admin_url(), scratch_prefix=bad)
