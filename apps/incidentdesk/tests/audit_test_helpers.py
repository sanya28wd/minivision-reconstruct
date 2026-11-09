"""TEST-ONLY helper that wipes tables including audit_log. Production code must never do this.

Why it exists: audit_log has a statement-level BEFORE TRUNCATE trigger (migration 0006) so that nobody
can wipe the log. Tests still need a clean table, so this helper steps around that one trigger for a
single transaction.

How it works, and the source: the PostgreSQL docs say "Simply enabled triggers (the default) will fire when
the replication role is origin (the default) or local", and that session_replication_role = replica
therefore skips them (https://www.postgresql.org/docs/16/sql-altertable.html and
https://www.postgresql.org/docs/16/runtime-config-client.html). Only a superuser (or a role with SET
privilege on the setting) may change it. `SET LOCAL` limits the change to this transaction, so no other
statement or connection is affected. tests/test_audit.py proves the helper really skips the trigger and that
src/ never contains this bypass.
"""

from __future__ import annotations

import psycopg
from psycopg import sql


def unsafe_test_only_truncate(database_url: str, *tables: str) -> None:
    """Truncate the named tables, skipping audit_log's TRUNCATE guard, inside one transaction."""
    if not tables:
        raise ValueError("name at least one table")
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(sql.SQL("TRUNCATE {}").format(sql.SQL(", ").join(sql.Identifier(t) for t in tables)))
