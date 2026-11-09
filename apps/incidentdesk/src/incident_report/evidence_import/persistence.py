"""Persist an inspection using the app's existing local PostgreSQL connection.

The importer owns no connection setup, credentials, or outbound client. Callers supply
an already-open local DB connection. Serialization occurs before insertion; immutable
rows and per-import locks prevent partial or concurrent duplicate imports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import ImportDecision, LegacyImportSnapshot
from .reader import inspect_bundle


def load_import(import_id: str, connection: Any) -> ImportDecision | LegacyImportSnapshot | None:
    row = connection.execute(
        "SELECT decision, raw_envelope FROM evidence_imports WHERE import_id=%s", (import_id,)
    ).fetchone()
    if row is None:
        return None
    raw = {
        name: bytes(content)
        for name, content in connection.execute(
            "SELECT artifact_path, content FROM evidence_import_artifacts WHERE import_id=%s", (import_id,)
        )
    }
    raw.update(
        {
            name: bytes(content)
            for name, content in connection.execute(
                "SELECT a.artifact_path,b.content FROM evidence_import_failed_artifacts a JOIN evidence_import_failed_blobs b USING (sha256) WHERE a.import_id=%s",
                (import_id,),
            )
        }
    )
    if row[0].get("importer_version") == "1.0.0":
        return LegacyImportSnapshot(
            import_id=import_id,
            importer_version="1.0.0",
            original_decision=row[0],
            raw_artifacts=raw,
            raw_envelope=None if row[1] is None else bytes(row[1]),
        )
    return ImportDecision.model_validate({**row[0], "raw_artifacts": raw})


def import_bundle(directory: Path, connection: Any) -> ImportDecision:
    """Inspect and atomically append; byte-identical content returns its existing decision."""
    return persist_inspection(inspect_bundle(directory), connection)


def persist_inspection(decision: ImportDecision, connection: Any) -> ImportDecision:
    """Append the inspected snapshot, allowing callers to validate it before writes."""
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (int(decision.import_id[:15], 16),))
        existing = load_import(decision.import_id, connection)
        if isinstance(existing, LegacyImportSnapshot):
            raise ValueError("current content ID collides with a historical import")
        if existing is not None:
            return existing
        connection.execute(
            "INSERT INTO evidence_imports (import_id,importer_version,provenance_state,decision,raw_envelope) VALUES (%s,%s,%s,%s::jsonb,%s)",
            (
                decision.import_id,
                decision.importer_version,
                decision.provenance_state,
                decision.model_dump_json(),
                decision.raw_artifacts.get("bundle.json") if decision.read_complete else None,
            ),
        )
        for name, content in decision.raw_artifacts.items():
            if not decision.read_complete:
                connection.execute(
                    "INSERT INTO evidence_import_failed_blobs (sha256,content) VALUES (%s,%s) ON CONFLICT (sha256) DO NOTHING",
                    (decision.source_hashes[name], content),
                )
                connection.execute(
                    "INSERT INTO evidence_import_failed_artifacts (import_id,artifact_path,sha256) VALUES (%s,%s,%s)",
                    (decision.import_id, name, decision.source_hashes[name]),
                )
                continue
            connection.execute(
                "INSERT INTO evidence_import_artifacts (import_id,artifact_path,sha256,content) VALUES (%s,%s,%s,%s)",
                (decision.import_id, name, decision.source_hashes[name], content),
            )
        for record in decision.records:
            connection.execute(
                "INSERT INTO evidence_import_records (import_id,record_id,kind,view_id,record) VALUES (%s,%s,%s,%s,%s::jsonb)",
                (decision.import_id, record.record_id, record.kind, record.view_id, record.model_dump_json()),
            )
    return decision
