#!/usr/bin/env python3
"""Migration-immutability guard (retro guard after RPT-14).

Reads src/incident_report/migrations/*.sql (not *.down.sql). For every table that has a trigger which
fires BEFORE UPDATE and/or DELETE (the way an append-only table is made immutable), the same table must
also have a `BEFORE TRUNCATE ... FOR EACH STATEMENT` trigger.

Why: the RPT-14 bug. PostgreSQL row-level triggers never fire on TRUNCATE (CREATE TRIGGER docs: TRUNCATE
triggers are allowed "only FOR EACH STATEMENT"), so an "immutable" log could still be wiped with one
statement. audit_log has both triggers; this keeps the next immutable table from forgetting the second.

The files are scanned in name order, with `--` comments removed. A table whose trigger is dropped later
(`DROP TRIGGER ... ON table`) stops counting for that trigger name.

Usage (same in CI and locally):
    python scripts/guard_migration_immutability.py [--repo .] [--migrations DIR] [--base ...]
`--base`, `--head` and `--fallback-ref` are accepted for a uniform command line; the whole current set of
migrations is always checked.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from guardlib import EXIT_ERROR, EXIT_FAILED, EXIT_OK, add_common_arguments

MIGRATIONS_DIR = "src/incident_report/migrations"

_CREATE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+(?P<name>\"?\w+\"?)\s+"
    r"(?P<timing>BEFORE|AFTER|INSTEAD\s+OF)\s+(?P<events>.*?)\s+ON\s+(?P<table>[\w.\"]+)(?P<rest>[^;]*);",
    re.IGNORECASE | re.DOTALL,
)
_DROP = re.compile(r"DROP\s+TRIGGER\s+(?:IF\s+EXISTS\s+)?(?P<name>\"?\w+\"?)\s+ON\s+(?P<table>[\w.\"]+)", re.IGNORECASE)


def _clean(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _norm(identifier: str) -> str:
    return identifier.replace('"', "").lower()


def scan(sql_files: list[Path]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (tables with a blocking update/delete trigger, tables with a before-truncate statement trigger)."""
    mutation: dict[tuple[str, str], str] = {}  # (table, trigger name) -> table
    truncate: dict[tuple[str, str], str] = {}
    sources: dict[str, set[str]] = {}
    for path in sorted(sql_files):
        sql = _clean(path.read_text(encoding="utf-8"))
        events_seen: list[tuple[int, str, tuple[str, str], str]] = []
        for m in _CREATE.finditer(sql):
            key = (_norm(m["table"]), _norm(m["name"]))
            events = re.sub(r"\s+", " ", m["events"].upper())
            is_before = m["timing"].upper() == "BEFORE"
            statement_level = re.search(r"FOR\s+EACH\s+STATEMENT", m["rest"], re.IGNORECASE) is not None
            if is_before and re.search(r"\b(UPDATE|DELETE)\b", events):
                events_seen.append((m.start(), "mut", key, path.name))
            if is_before and "TRUNCATE" in events and statement_level:
                events_seen.append((m.start(), "trunc", key, path.name))
        for m in _DROP.finditer(sql):
            events_seen.append((m.start(), "drop", (_norm(m["table"]), _norm(m["name"])), path.name))
        for _, kind, key, fname in sorted(events_seen):
            if kind == "mut":
                mutation[key] = key[0]
                sources.setdefault(key[0], set()).add(fname)
            elif kind == "trunc":
                truncate[key] = key[0]
            else:
                mutation.pop(key, None)
                truncate.pop(key, None)
    mut_tables: dict[str, set[str]] = {}
    for table in mutation.values():
        mut_tables[table] = sources.get(table, set())
    trunc_tables: dict[str, set[str]] = {t: set() for t in truncate.values()}
    return mut_tables, trunc_tables


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_arguments(parser, approval=False)
    parser.add_argument("--migrations", default=None, help=f"migrations folder (default: <repo>/{MIGRATIONS_DIR})")
    args = parser.parse_args(argv)
    folder = Path(args.migrations) if args.migrations else Path(args.repo) / MIGRATIONS_DIR

    files = [p for p in folder.glob("*.sql") if not p.name.endswith(".down.sql")] if folder.is_dir() else []
    if not files:
        print(f"ERROR migration-immutability guard found no migrations in {folder}", file=sys.stderr)
        return EXIT_ERROR
    try:
        mut_tables, trunc_tables = scan(files)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ERROR migration-immutability guard could not read a migration: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"migration-immutability guard: checked {len(files)} migration(s), {len(mut_tables)} immutable table(s)")
    missing = sorted(t for t in mut_tables if t not in trunc_tables)
    for table in missing:
        where = ", ".join(sorted(mut_tables[table]))
        print(
            f"MISSING {table} ({where}) blocks UPDATE/DELETE but has no BEFORE TRUNCATE ... FOR EACH STATEMENT trigger"
        )
    if missing:
        print(
            f"FAIL {len(missing)} table(s) can still be wiped with TRUNCATE: row triggers do not fire on it. "
            "Add `CREATE TRIGGER ... BEFORE TRUNCATE ON <table> FOR EACH STATEMENT EXECUTE FUNCTION ...`."
        )
        return EXIT_FAILED
    print("OK every immutable table also blocks TRUNCATE.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
