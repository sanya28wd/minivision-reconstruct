"""New workspace DDL reverses cleanly and protects every history operation."""

import uuid

import psycopg
import pytest
from psycopg import sql

from evidence_import_fixtures import FIXTURES
from incident_report.db import load_migrations
from incident_report.evidence_import.persistence import import_bundle


def test_0022_up_down_up_and_append_only(database_url: str) -> None:
    migration = next(m for m in load_migrations() if m.name.startswith("0022_"))
    schema = "mig03_" + uuid.uuid4().hex
    with psycopg.connect(database_url) as conn:
        imported = import_bundle(FIXTURES / "mig02-relative", conn)
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute(migration.up_sql)
        conn.execute(migration.down_sql)
        conn.execute(migration.up_sql)
        incident = uuid.uuid4()
        old, new = uuid.uuid4(), uuid.uuid4()
        for identifier in (old, new):
            conn.execute(
                "INSERT INTO incident_evidence_imports(link_id,incident_id,import_id,view_sha256,source_view_id,producer_case_id,attached_by) VALUES (%s,%s,%s,%s,'front','case','synthetic')",
                (identifier, incident, imported.import_id, "a" * 64 if identifier == old else "b" * 64),
            )
        conn.execute(
            "INSERT INTO incident_evidence_supersessions(incident_id,old_link_id,new_link_id) VALUES (%s,%s,%s)",
            (incident, old, new),
        )
        conn.execute(
            "INSERT INTO track_party_mappings(incident_id,import_id,track_ref,view_sha256,action,reviewer,reason) VALUES (%s,%s,'synthetic','view','unmap','synthetic','synthetic')",
            (incident, imported.import_id),
        )
        conn.execute(
            "INSERT INTO view_time_alignments(incident_id,view_sha256,reference_view_sha256,offset_seconds,uncertainty_seconds,reviewer,reason) VALUES (%s,'view','view',0,1,'synthetic','synthetic')",
            (incident,),
        )
        for table in (
            "incident_evidence_imports",
            "incident_evidence_supersessions",
            "track_party_mappings",
            "view_time_alignments",
        ):
            for query in ("UPDATE {} SET incident_id=incident_id", "DELETE FROM {}", "TRUNCATE {} CASCADE"):
                with pytest.raises(psycopg.Error), conn.transaction():
                    conn.execute(sql.SQL(query).format(sql.Identifier(table)))
        # Existing app/benchmark reset convention (0016) must stay usable, without cascading into history.
        with conn.transaction(force_rollback=True):
            conn.execute("TRUNCATE public.runs")
        conn.execute(migration.down_sql)
        conn.execute(sql.SQL("DROP SCHEMA {}").format(sql.Identifier(schema)))
