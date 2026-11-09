"""PostgreSQL access: a small reversible migration runner (plain SQL, PostgreSQL only; ADR 0011).

Every migration is a pair of files in `incident_report/migrations/`:
    NNNN_name.sql        the up (forward) migration
    NNNN_name.down.sql   its exact undo
`NNNN` is a four-digit number, unique and increasing; `name` is lowercase letters, digits and underscores.
Applied migrations are recorded in `schema_migrations` by their up file name. Each `migrate` or `rollback`
runs in ONE transaction (PostgreSQL DDL is transactional), so a failure leaves the database as it was.
A down migration usually destroys data (dropping a table); take a backup first.

Commands: `python -m incident_report.db migrate | status | rollback (--steps N | --to TARGET) --yes`.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable

import psycopg

from incident_report.config import ConfigError, Settings

# Arbitrary constant key so two processes migrating at once take turns.
_MIGRATION_LOCK_KEY = 7_450_001

_UP_NAME = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")
_DOWN_SUFFIX = ".down.sql"


class MigrationError(RuntimeError):
    """The migration files or the request are not usable. The message is plain."""


@dataclass(frozen=True)
class Migration:
    name: str  # the up file name; this is what schema_migrations records
    up_sql: str
    down_sql: str


def load_migrations(source: Traversable | None = None) -> list[Migration]:
    """Read and validate every migration pair, ordered by number. Raises MigrationError if any is unusable."""
    folder = source if source is not None else resources.files("incident_report.migrations")
    ups: dict[str, Traversable] = {}
    downs: dict[str, Traversable] = {}
    for f in folder.iterdir():
        if not f.name.endswith(".sql"):
            continue
        if f.name.endswith(_DOWN_SUFFIX):
            downs[f.name.removesuffix(_DOWN_SUFFIX) + ".sql"] = f
        elif _UP_NAME.fullmatch(f.name):
            ups[f.name] = f
        else:
            raise MigrationError(
                f"Migration file name {f.name!r} is not allowed: use NNNN_lowercase_name.sql (and .down.sql)."
            )
    for up_name in sorted(downs.keys() - ups.keys()):
        raise MigrationError(f"{up_name.removesuffix('.sql')}{_DOWN_SUFFIX} has no matching up migration.")
    numbers: dict[str, str] = {}
    result: list[Migration] = []
    for up_name in sorted(ups):
        number = up_name[:4]
        if number in numbers:
            raise MigrationError(f"Migrations {numbers[number]} and {up_name} share the number {number}.")
        numbers[number] = up_name
        if up_name not in downs:
            raise MigrationError(
                f"Migration {up_name} has no down file ({up_name.removesuffix('.sql')}{_DOWN_SUFFIX})."
            )
        down_sql = downs[up_name].read_text(encoding="utf-8")
        if not down_sql.strip():
            raise MigrationError(f"The down file for {up_name} is empty.")
        result.append(Migration(up_name, ups[up_name].read_text(encoding="utf-8"), down_sql))
    return result


def _lock_and_applied(conn: psycopg.Connection) -> list[str]:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    return [row[0] for row in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]


def migrate(conn: psycopg.Connection, *, source: Traversable | None = None) -> list[str]:
    """Apply every migration not yet applied, in number order. Returns the names applied."""
    migrations = load_migrations(source)
    applied_now: list[str] = []
    with conn.transaction():
        done = set(_lock_and_applied(conn))
        for m in migrations:
            if m.name in done:
                continue
            conn.execute(m.up_sql)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (m.name,))
            applied_now.append(m.name)
    return applied_now


def rollback(
    conn: psycopg.Connection,
    *,
    steps: int | None = None,
    to: str | None = None,
    source: Traversable | None = None,
) -> list[str]:
    """Undo applied migrations, newest first. Give exactly one of:

    steps: how many of the newest applied migrations to undo (at least 1, at most the number applied).
    to:    "base" (undo everything) or a migration name / number ("0002") that must stay applied.
    Returns the names rolled back. Nothing changes if anything is wrong.
    """
    if (steps is None) == (to is None):
        raise MigrationError("Give exactly one of steps or to.")
    migrations = {m.name: m for m in load_migrations(source)}
    with conn.transaction():
        applied = _lock_and_applied(conn)
        if steps is not None:
            if steps < 1:
                raise MigrationError("Rolling back needs steps of at least 1.")
            if steps > len(applied):
                raise MigrationError(f"Cannot roll back {steps} steps: only {len(applied)} migration(s) are applied.")
            targets = applied[len(applied) - steps :]
        elif to == "base":
            targets = applied
        else:
            keep = [n for n in applied if to in (n, n.removesuffix(".sql"), n[:4])]
            if len(keep) != 1:
                raise MigrationError(f"{to!r} is not an applied migration (applied: {', '.join(applied) or 'none'}).")
            targets = applied[applied.index(keep[0]) + 1 :]
        missing = [n for n in targets if n not in migrations]
        if missing:
            raise MigrationError(f"Cannot roll back {', '.join(missing)}: its migration files are not present.")
        rolled_back: list[str] = []
        for name in reversed(targets):
            conn.execute(migrations[name].down_sql)
            conn.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))
            rolled_back.append(name)
    return rolled_back


def status(conn: psycopg.Connection, *, source: Traversable | None = None) -> list[tuple[str, bool]]:
    """(migration name, applied?) for every migration file, in number order."""
    migrations = load_migrations(source)
    with conn.transaction():
        done = set(_lock_and_applied(conn))
    return [(m.name, m.name in done) for m in migrations]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m incident_report.db", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply every pending migration")
    commands.add_parser("status", help="list migrations and whether each is applied")
    undo = commands.add_parser("rollback", help="undo applied migrations (destroys data; back up first)")
    undo.add_argument("--steps", type=int, help="undo the newest N applied migrations")
    undo.add_argument("--to", help='undo everything after this migration (name or number), or "base" for everything')
    undo.add_argument("--yes", action="store_true", help="confirm that you want to run the down migrations")
    return parser


def main(argv: Sequence[str]) -> int:
    """Command line entry point (reads DATABASE_URL). Returns 0 on success, 1 on failure, 2 on bad usage."""
    args = _parser().parse_args(list(argv))
    if args.command == "rollback":
        if (args.steps is None) == (args.to is None):
            print("rollback needs exactly one of --steps or --to.", file=sys.stderr)
            return 2
        if not args.yes:
            print(
                "rollback runs down migrations, which delete data. Take a backup, then repeat with --yes.",
                file=sys.stderr,
            )
            return 2
    try:
        settings = Settings.from_env()
        print(f"environment: {settings.environment.value}")
        with psycopg.connect(settings.database_url) as conn:
            if args.command == "migrate":
                applied = migrate(conn)
                print("applied: " + (", ".join(applied) if applied else "nothing (already up to date)"))
            elif args.command == "status":
                for name, done in status(conn):
                    print(f"[{'applied' if done else 'pending'}] {name}")
            else:
                undone = rollback(conn, steps=args.steps, to=args.to)
                print("rolled back: " + (", ".join(undone) if undone else "nothing"))
    except (ConfigError, MigrationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
