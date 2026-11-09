"""Round 3 source-linked sequence, multi-view identity and review diagnostics."""

from pathlib import Path

import pytest

from evidence_import_fixtures import FIXTURES
from incident_report.evidence_import import inspect_bundle
from incident_report.evidence_import.payloads import EventFields, SequenceFields


@pytest.mark.parametrize("name", [p.name for p in FIXTURES.glob("mig02-*")])
def test_sequence_peaks_match_their_events(name: str) -> None:
    result = inspect_bundle(FIXTURES / name)
    events = {
        r.data.event_id: r.data
        for r in result.records
        if isinstance(r.data, EventFields) and "/movement/" in r.artifact_path
    }
    sequences = [r for r in result.records if isinstance(r.data, SequenceFields)]
    assert sequences
    for record in sequences:
        assert isinstance(record.data, SequenceFields)
        assert record.data.peak_time == events[record.data.event_id].peak_time
        assert record.data.peak_frame == events[record.data.event_id].peak_frame


def test_unresolvable_sequence_times_stay_unknown(tmp_path: Path) -> None:
    from typing import Any

    from evidence_import_fixtures import synthetic_bundle

    def change(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        for name, rows in documents.items():
            if name.endswith("/sequence.json"):
                rows[0]["entries"][0]["peak_time"] = 0.001
                rows[0]["entries"][0]["time_window"] = [0.001, 0.04]

    result = inspect_bundle(synthetic_bundle(tmp_path, transform=change))
    record = next(r for r in result.records if isinstance(r.data, SequenceFields) and r.data.rank == 1)
    assert isinstance(record.data, SequenceFields)
    assert record.data.peak_time is None and record.data.peak_frame is None
    assert record.data.time_window == (0.001, 0.04)
    assert record.original["peak_time"] == 0.001
    assert record.needs_review and record.record_id in result.quarantined_records


def test_missing_inputs_are_explicit_even_with_verified_provenance() -> None:
    result = inspect_bundle(FIXTURES / "mig02-001-06-normal-crossing")
    assert result.provenance_state == "verified"
    assert "no_incident_candidates" in result.missing_inputs


def test_failed_calibration_has_a_typed_missing_input(tmp_path: Path) -> None:
    from typing import Any

    from evidence_import_fixtures import synthetic_bundle

    def fail(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        for name, rows in documents.items():
            if name.endswith("/calibration.json"):
                rows[0]["validation_passed"] = False

    result = inspect_bundle(synthetic_bundle(tmp_path, transform=fail))
    assert "calibration_failed" in result.missing_inputs


def test_off_table_review_issue_names_target_and_time(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle
    from incident_report.evidence_import.payloads import ReviewFields

    result = inspect_bundle(synthetic_bundle(tmp_path, modified=True))
    overlay = next(r for r in result.records if isinstance(r.data, ReviewFields))
    assert isinstance(overlay.data, ReviewFields)
    assert overlay.needs_review and overlay.source_time is None and overlay.source_frame is None
    assert any(
        overlay.data.target_id in issue and repr(overlay.data.modified_peak_time) in issue for issue in result.issues
    )
    assert result.provenance_state == "verified"
    assert overlay.record_id in result.quarantined_records
