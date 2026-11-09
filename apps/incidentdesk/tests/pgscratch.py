"""Helpers for tests that need their own throwaway PostgreSQL database (RPT-03).

Like conftest.py's `database_url`, they use the server named by TEST_DATABASE_URL and fail with a plain
message when it cannot be reached; they never skip.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from conftest import DEFAULT_TEST_DATABASE_URL


def admin_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def url_for(database: str) -> str:
    info = conninfo_to_dict(admin_url())
    info["dbname"] = database
    return make_conninfo(**info)  # type: ignore[arg-type]  # psycopg stubs type kwargs as str


@contextmanager
def scratch_database(prefix: str = "incident_scratch") -> Iterator[str]:
    """Create an empty database, yield its URL, drop it afterwards."""
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(admin_url(), autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.fail(f"Cannot reach PostgreSQL with TEST_DATABASE_URL={admin_url()!r}. {exc}")
    with admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield url_for(name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def database_names_like(pattern: str) -> list[str]:
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        return [r[0] for r in admin.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (pattern,))]


def schema_snapshot(url: str) -> list[str]:
    """Everything that defines the schema, as sorted text lines (compare two snapshots for equality).

    Covers tables, columns (type, default, nullability), constraints, indexes, triggers, functions, sequences,
    views and extensions in every non-system schema. The migration bookkeeping table is excluded because it
    legitimately differs by timestamps.
    """
    queries = {
        "column": """
            SELECT table_schema || '.' || table_name || ' ' || column_name || ' ' || data_type || ' '
                   || coalesce(column_default, '-') || ' ' || is_nullable
            FROM information_schema.columns
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
              AND table_name <> 'schema_migrations'""",
        "constraint": """
            SELECT conrelid::regclass::text || ' ' || conname || ' ' || pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE connamespace NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
              AND conrelid::regclass::text <> 'schema_migrations'""",
        "index": """
            SELECT indexdef FROM pg_indexes
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND tablename <> 'schema_migrations'""",
        "trigger": """
            SELECT pg_get_triggerdef(oid) FROM pg_trigger
            WHERE NOT tgisinternal AND tgrelid::regclass::text <> 'schema_migrations'""",
        "function": """
            SELECT pg_get_functiondef(p.oid) FROM pg_proc p
            WHERE pronamespace NOT IN ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)""",
        "sequence": """
            SELECT schemaname || '.' || sequencename FROM pg_sequences""",
        "view": """
            SELECT schemaname || '.' || viewname || ' ' || definition FROM pg_views
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')""",
        "extension": "SELECT extname FROM pg_extension WHERE extname <> 'plpgsql'",
    }
    lines: list[str] = []
    with psycopg.connect(url) as conn:
        for kind, query in queries.items():
            lines += [f"{kind}: {row[0]}" for row in conn.execute(query)]
    return sorted(lines)


def user_tables(url: str) -> list[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT schemaname || '.' || tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1"
        )
        return [r[0] for r in rows]
