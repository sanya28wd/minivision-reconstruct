"""MIG-03 carry-overs at the public import boundary."""

from pathlib import Path

import pytest

from evidence_import_fixtures import FIXTURES, synthetic_bundle
from incident_report.evidence_import import inspect_bundle
from incident_report.evidence_import.payloads import SequenceFields


@pytest.mark.parametrize("name", [p.name for p in FIXTURES.glob("mig02-*")])
def test_real_sequence_windows_are_source_intervals(name: str) -> None:
    result = inspect_bundle(FIXTURES / name)
    rows = [r for r in result.records if isinstance(r.data, SequenceFields)]
    assert rows
    assert all(not r.needs_review for r in rows)
    assert all(isinstance(r.data, SequenceFields) and r.data.covering_frames is not None for r in rows)


def test_window_without_a_frame_is_quarantined(tmp_path: Path) -> None:
    from typing import Any

    def change(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        for name, rows in documents.items():
            if name.endswith("/sequence.json"):
                rows[0]["entries"][0]["time_window"] = [0.001, 0.039]

    result = inspect_bundle(synthetic_bundle(tmp_path, transform=change))
    record = next(r for r in result.records if isinstance(r.data, SequenceFields) and r.data.rank == 1)
    assert isinstance(record.data, SequenceFields)
    assert record.data.time_window == (0.001, 0.039)
    assert record.data.covering_frames is None and record.needs_review
    assert any("window contains no source frame" in issue for issue in result.issues)


@pytest.mark.parametrize(
    "code",
    [
        "no_incident_candidates",
        "calibration_failed",
        "calibration_unavailable",
        "source_timestamps_unavailable",
        "source_timestamps_incomplete",
        "upstream_artifacts_missing",
    ],
)
def test_missing_input_codes_are_structured(tmp_path: Path, code: str) -> None:
    from typing import Any

    if code == "no_incident_candidates":
        root = FIXTURES / "mig02-001-06-normal-crossing"
    else:

        def change(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
            for name, rows in documents.items():
                if code == "calibration_failed" and name.endswith("/calibration.json"):
                    rows[0]["validation_passed"] = False
                if name.endswith("/source_frames.jsonl"):
                    if code == "source_timestamps_unavailable":
                        rows.clear()
                    if code == "source_timestamps_incomplete":
                        rows.pop()

        root = synthetic_bundle(tmp_path, transform=change)
        if code == "calibration_unavailable":
            next(root.rglob("calibration.json")).unlink()
        if code == "upstream_artifacts_missing":
            next(root.rglob("movement/mig02-movement/manifest.json")).unlink()
    result = inspect_bundle(root)
    assert code in result.issue_codes
    assert code in result.missing_inputs
    assert isinstance(result.missing_inputs, tuple)
    from incident_report.evidence_import.models import ImportDecision

    reworded = ImportDecision.model_validate(
        {**result.model_dump(), "missing_inputs": (), "issues": ["Different wording" for _ in result.issues]}
    )
    assert reworded.missing_inputs == result.missing_inputs


def test_stored_mig02_snapshot_without_codes_loads(database_url: str) -> None:
    import hashlib
    import json

    import psycopg

    from incident_report.evidence_import.models import ImportDecision
    from incident_report.evidence_import.persistence import load_import

    snapshot = json.loads(inspect_bundle(FIXTURES / "mig02-relative").model_dump_json())
    snapshot["importer_version"] = "1.2.0"
    # Historical importer versions had different content IDs; do not shadow a current fixture import.
    snapshot["import_id"] = hashlib.sha256(b"synthetic MIG-02 1.2.0 snapshot").hexdigest()
    snapshot.pop("issue_codes")
    snapshot.pop("missing_inputs")
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO evidence_imports(import_id,importer_version,provenance_state,decision) VALUES (%s,%s,%s,%s::jsonb)",
            (snapshot["import_id"], "1.2.0", snapshot["provenance_state"], json.dumps(snapshot)),
        )
        loaded = load_import(snapshot["import_id"], conn)
    assert isinstance(loaded, ImportDecision) and loaded.importer_version == "1.2.0"
    assert loaded.records and loaded.issue_codes == () and loaded.missing_inputs == ()


@pytest.mark.parametrize("window", [[2.0, 1.0], [1.0], [1.0, 2.0, 3.0]])
def test_malformed_window_quarantines_only_its_row(tmp_path: Path, window: list[float]) -> None:
    from typing import Any

    def change(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        for name, rows in documents.items():
            if name.endswith("/sequence.json"):
                rows[0]["entries"][0]["time_window"] = window

    result = inspect_bundle(synthetic_bundle(tmp_path, transform=change))
    assert result.provenance_state != "rejected"
    rows = [r for r in result.records if isinstance(r.data, SequenceFields)]
    invalid = next(r for r in rows if isinstance(r.data, SequenceFields) and r.data.rank == 1)
    assert invalid.needs_review and invalid.original["time_window"] == window
    assert isinstance(invalid.data, SequenceFields) and invalid.data.covering_frames is None
    assert any(not r.needs_review for r in rows if isinstance(r.data, SequenceFields) and r.data.rank != 1)
    assert any("invalid source-time interval" in issue for issue in result.issues)
