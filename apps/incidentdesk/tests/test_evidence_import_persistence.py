"""Immutable local import persistence and migration safeguards."""

from pathlib import Path

import psycopg
import pytest

from evidence_import_fixtures import synthetic_bundle
from incident_report.evidence_import.models import ImportDecision, LegacyImportSnapshot
from incident_report.evidence_import.persistence import import_bundle, load_import


def test_reimport_and_restart_preserve_one_immutable_import(database_url: str, tmp_path: Path) -> None:
    root = synthetic_bundle(tmp_path)
    with psycopg.connect(database_url) as conn:
        first = import_bundle(root, conn)
        second = import_bundle(root, conn)
        assert first.import_id == second.import_id
        assert conn.execute(
            "SELECT count(*) FROM evidence_imports WHERE import_id=%s", (first.import_id,)
        ).fetchone() == (1,)
    with psycopg.connect(database_url) as conn:
        restored = load_import(first.import_id, conn)
        assert isinstance(restored, ImportDecision)
        assert restored.model_dump() == first.model_dump()
        assert restored.raw_artifacts == first.raw_artifacts
        assert conn.execute(
            "SELECT count(*) FROM evidence_import_records WHERE import_id=%s", (first.import_id,)
        ).fetchone() == (len(first.records),)


@pytest.mark.parametrize("table", ["evidence_imports", "evidence_import_records", "evidence_import_artifacts"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_import_tables_block_all_mutation(database_url: str, tmp_path: Path, table: str, operation: str) -> None:
    from psycopg import sql

    with psycopg.connect(database_url) as conn:
        import_bundle(synthetic_bundle(tmp_path), conn)
    statement = {
        "UPDATE": "UPDATE {} SET import_id=import_id",
        "DELETE": "DELETE FROM {}",
        "TRUNCATE": "TRUNCATE {} CASCADE",
    }[operation]
    with psycopg.connect(database_url) as conn, pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute(sql.SQL(statement).format(sql.Identifier(table)))


def test_new_review_is_a_separate_import(database_url: str, tmp_path: Path) -> None:
    first_root = synthetic_bundle(tmp_path / "first")
    second_root = synthetic_bundle(tmp_path / "second", rejected=True)
    with psycopg.connect(database_url) as conn:
        first = import_bundle(first_root, conn)
        second = import_bundle(second_root, conn)
        assert first.import_id != second.import_id
        assert load_import(first.import_id, conn) is not None


def test_rejected_bundle_is_persisted_for_audit(database_url: str, tmp_path: Path) -> None:
    (tmp_path / "bundle.json").write_text("{bad", encoding="utf-8")
    with psycopg.connect(database_url) as conn:
        result = import_bundle(tmp_path, conn)
        assert result.provenance_state == "rejected"
        restored = load_import(result.import_id, conn)
        assert isinstance(restored, ImportDecision) and restored.raw_artifacts["bundle.json"] == b"{bad"


def test_json_null_text_is_auditable_rejection(database_url: str, tmp_path: Path) -> None:
    import json

    root = synthetic_bundle(tmp_path)
    envelope = json.loads((root / "bundle.json").read_bytes())
    envelope["created_at"] = "bad\u0000text"
    (root / "bundle.json").write_text(json.dumps(envelope), encoding="utf-8")
    with psycopg.connect(database_url) as conn:
        result = import_bundle(root, conn)
        assert result.provenance_state == "rejected"
        assert load_import(result.import_id, conn) is not None


def test_unreadable_attempts_do_not_collapse(database_url: str, tmp_path: Path) -> None:
    with psycopg.connect(database_url) as conn:
        first = import_bundle(tmp_path / "absent-a", conn)
        second = import_bundle(tmp_path / "absent-b", conn)
        assert first.import_id != second.import_id
        assert load_import(first.import_id, conn) is not None
        assert load_import(second.import_id, conn) is not None


def test_real_relative_fixture_survives_database_roundtrip(database_url: str) -> None:
    from evidence_import_fixtures import FIXTURES

    root = FIXTURES / "mig02-relative"
    with psycopg.connect(database_url) as conn:
        original = import_bundle(root, conn)
    with psycopg.connect(database_url) as conn:
        restored = load_import(original.import_id, conn)
        assert isinstance(restored, ImportDecision) and restored.records == original.records
        assert {"lost", "interpolated"} <= {
            r.original.get("state") for r in restored.records if r.kind == "track_reference"
        }
        assert {r.original["unit"] for r in restored.records if r.kind == "geometry"} == {"relative_units"}
        assert restored.raw_artifacts == original.raw_artifacts


def test_failed_reads_share_content_blob(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from incident_report.evidence_import import reader

    def fail(directory: Path, files: dict[str, bytes]) -> None:
        files["partial.json"] = b'{"partial":true}'
        raise reader.InvalidBundle("simulated interrupted read")

    monkeypatch.setattr(reader, "read_files", fail)
    with psycopg.connect(database_url) as conn:
        first = import_bundle(tmp_path, conn)
        second = import_bundle(tmp_path, conn)
        assert first.import_id != second.import_id
        assert conn.execute(
            "SELECT count(*) FROM evidence_import_failed_blobs WHERE content=%s", (b'{"partial":true}',)
        ).fetchone() == (1,)
        restored_first = load_import(first.import_id, conn)
        restored_second = load_import(second.import_id, conn)
        assert restored_first is not None and restored_first.raw_artifacts == first.raw_artifacts
        assert restored_second is not None and restored_second.raw_artifacts == second.raw_artifacts


@pytest.mark.parametrize("table", ["evidence_import_failed_blobs", "evidence_import_failed_artifacts"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_failed_blob_store_is_immutable(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str, operation: str
) -> None:
    from psycopg import sql

    from incident_report.evidence_import import reader

    def fail(directory: Path, files: dict[str, bytes]) -> None:
        files["partial.json"] = b"{}"
        raise reader.InvalidBundle("read interrupted")

    monkeypatch.setattr(reader, "read_files", fail)
    with psycopg.connect(database_url) as conn:
        import_bundle(tmp_path, conn)
    template = {"UPDATE": "UPDATE {} SET sha256=sha256", "DELETE": "DELETE FROM {}", "TRUNCATE": "TRUNCATE {} CASCADE"}[
        operation
    ]
    with psycopg.connect(database_url) as conn, pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        conn.execute(sql.SQL(template).format(sql.Identifier(table)))


def test_r1_import_remains_readable_after_upgrade(database_url: str) -> None:
    import json

    # Exact R1 stored shape: the old acceptance name and no typed data field.
    from typing import Any

    snapshot: dict[str, Any] = {
        "import_id": "a" * 64,
        "importer_version": "1.0.0",
        "provenance_state": "rejected",
        "issues": ["original rejection"],
        "records": [
            {
                "record_id": "legacy-record",
                "kind": "source_binding",
                "view_id": "camera",
                "artifact_path": "manifest.json",
                "original": {},
                "effective": None,
                "source_frame": None,
                "source_time": None,
                "timestamp_provenance": None,
                "track_refs": [],
                "producer_status": None,
                "parent_status": None,
                "needs_review": True,
                "affirmative": False,
                "officer_approved": False,
                "rule_source": None,
            }
        ],
        "accepted_records": [],
        "quarantined_records": [],
        "source_hashes": {},
        "envelope": None,
    }
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO evidence_imports (import_id,importer_version,provenance_state,decision,raw_envelope) VALUES (%s,%s,%s,%s::jsonb,%s)",
            ("a" * 64, "1.0.0", "rejected", json.dumps(snapshot), b"original envelope"),
        )
        restored = load_import("a" * 64, conn)
        assert isinstance(restored, LegacyImportSnapshot)
        assert restored.original_decision == snapshot
        assert restored.raw_envelope == b"original envelope"
        assert conn.execute("SELECT decision FROM evidence_imports WHERE import_id=%s", ("a" * 64,)).fetchone() == (
            snapshot,
        )
