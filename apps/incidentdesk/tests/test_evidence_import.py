"""Local evidence boundary: hostile bundles are recorded, never executed."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from incident_report.evidence_import import inspect_bundle
from incident_report.evidence_import.models import MAX_RECORD_BYTES

FIXTURES = Path(__file__).resolve().parents[3] / "contracts/evidence-v1/fixtures"


def test_unknown_contract_is_a_recorded_rejection(tmp_path: Path) -> None:
    (tmp_path / "bundle.json").write_text('{"contract_version":"999"}', encoding="utf-8")
    decision = inspect_bundle(tmp_path)
    assert decision.provenance_state == "rejected"
    assert decision.issues
    assert decision.records == ()


def bundle(tmp_path: Path, **changes: object) -> Path:
    content = b'{"schema_version":"1.0","case_id":"case-a","artifacts":[]}'
    (tmp_path / "manifest.json").write_bytes(content)
    envelope: dict[str, Any] = {
        "contract_version": "1.0",
        "producer": "minivision-reconstruct",
        "producer_revision": "c66000af37099e39920ff0e8ac621ce5be09090d",
        "case_id": "case-a",
        "created_at": "2026-10-30T00:00:00Z",
        "processing_policy": "local_only",
        "config_identifiers": [],
        "model_identifiers": [],
        "artifacts": {"manifest.json": hashlib.sha256(content).hexdigest()},
        "sources": [],
    }
    envelope.update(changes)
    (tmp_path / "bundle.json").write_text(json.dumps(envelope), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("path", ["../escape.json", "/absolute", "C:/file", "a\\b", "a/../b"])
def test_unsafe_paths_are_rejected_before_reading(tmp_path: Path, path: str) -> None:
    result = inspect_bundle(bundle(tmp_path, artifacts={path: "0" * 64}))
    assert result.provenance_state == "rejected"


def test_hash_mismatch_is_recorded(tmp_path: Path) -> None:
    root = bundle(tmp_path)
    (root / "manifest.json").write_text("{}", encoding="utf-8")
    assert inspect_bundle(root).provenance_state == "rejected"


def test_missing_source_is_incomplete_never_no_incident(tmp_path: Path) -> None:
    result = inspect_bundle(bundle(tmp_path))
    assert result.provenance_state == "incomplete_provenance"
    assert result.issues


def test_malformed_json_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "bundle.json").write_text("{bad", encoding="utf-8")
    assert inspect_bundle(tmp_path).provenance_state == "rejected"


def test_real_producer_bytes_states_and_reviews_survive() -> None:
    root = FIXTURES / "mig02-001-07-occluded-impact"
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    result = inspect_bundle(root)
    assert result.provenance_state == "verified"  # JSON-only: replay/CSV deliberately absent.
    assert result.records
    assert not any(record.affirmative or record.officer_approved for record in result.records)
    assert "lost" in {r.original.get("state") for r in result.records if r.kind == "track_reference"}
    overlays = [r for r in result.records if r.kind == "review_overlay"]
    assert overlays
    assert any(r.needs_review for r in overlays)  # +.001 correction has no source timestamp.
    for path, digest in before.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_real_empty_candidates_are_missing_input() -> None:
    result = inspect_bundle(FIXTURES / "mig02-001-06-normal-crossing")
    assert "missing_input: no incident candidates" in result.issues
    assert result.provenance_state == "verified"


def test_network_path_is_rejected_without_filesystem_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object) -> bool:
        raise AssertionError("network filesystem was probed")

    monkeypatch.setattr(Path, "is_dir", forbidden)
    assert inspect_bundle(Path("//server/share/bundle")).provenance_state == "rejected"


def test_oversized_json_record_is_rejected(tmp_path: Path) -> None:
    root = bundle(tmp_path)
    content = json.dumps({"text": "x" * MAX_RECORD_BYTES}).encode()
    (root / "manifest.json").write_bytes(content)
    document = json.loads((root / "bundle.json").read_bytes())
    document["artifacts"]["manifest.json"] = hashlib.sha256(content).hexdigest()
    (root / "bundle.json").write_text(json.dumps(document), encoding="utf-8")
    assert inspect_bundle(root).provenance_state == "rejected"


def test_jsonl_record_budget_is_enforced_before_parsing_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from incident_report.evidence_import import reader

    root = bundle(tmp_path)
    content = b"{}\n" * 1000
    (root / "data.jsonl").write_bytes(content)
    document = json.loads((root / "bundle.json").read_bytes())
    document["artifacts"]["data.jsonl"] = hashlib.sha256(content).hexdigest()
    (root / "bundle.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(reader, "MAX_RECORDS", 10)
    calls = 0
    original = reader.parse_record

    def counting(content: bytes) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return original(content)

    monkeypatch.setattr(reader, "parse_record", counting)
    assert inspect_bundle(root).provenance_state == "rejected"
    assert calls <= 12  # envelope + at most ten records


def test_complete_bundle_is_verified_but_never_officer_approved(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    result = inspect_bundle(synthetic_bundle(tmp_path))
    assert result.provenance_state == "verified", result.issues
    assert result.structurally_accepted_records
    assert all(not r.officer_approved and not r.affirmative and r.rule_source is None for r in result.records)


def test_rejected_parent_keeps_unreviewed_assessment_nonaffirmative(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    result = inspect_bundle(synthetic_bundle(tmp_path, rejected=True))
    assessments = [r for r in result.records if r.kind == "assessment"]
    assert assessments, result.issues
    assert assessments[0].parent_status == "REJECTED_BY_REVIEWER"
    assert assessments[0].producer_status == "PROPOSED_BY_SYSTEM"
    assert not assessments[0].affirmative


def test_unresolved_modified_time_never_reuses_original_frame(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    result = inspect_bundle(synthetic_bundle(tmp_path, modified=True))
    overlay = next(r for r in result.records if r.kind == "review_overlay")
    original = next(r for r in result.records if r.kind == "event_proposal")
    assert original.source_frame is not None
    assert overlay.needs_review
    assert overlay.source_frame is None and overlay.source_time is None
    assert overlay.effective is not None and overlay.effective["peak_time"] != original.source_time


def test_two_views_never_share_track_identity(tmp_path: Path) -> None:
    from evidence_import_fixtures import two_view_bundle
    from incident_report.evidence_import.payloads import TrackFields

    result = inspect_bundle(two_view_bundle(tmp_path))
    assert result.provenance_state == "verified", result.issues
    tracks = [r for r in result.records if isinstance(r.data, TrackFields)]
    assert tracks and result.envelope is not None and result.envelope.case_id == "shared-case"
    first = {r.track_refs[0] for r in tracks if r.view_id == "first-view"}
    second = {r.track_refs[0] for r in tracks if r.view_id == "second-view"}
    assert first and second and first.isdisjoint(second)
    for view in ("first-view", "second-view"):
        fields = [r.data for r in tracks if r.view_id == view and isinstance(r.data, TrackFields)]
        assert {f.case_id for f in fields} == {"shared-case"}
        assert {f.track_id for f in fields} == {1, 2}
        assert {f.tracking_run for f in fields} == {"staged-tracks"}
        assert {f.revision for f in fields} == {"automatic"}
    assert len({s.original_video_sha256 for s in result.envelope.sources}) == 2
    # Rename only the view: producer case/run/revision/track IDs and bytes remain fixed.
    envelope_path = tmp_path / "bundle.json"
    envelope = json.loads(envelope_path.read_bytes())
    envelope["sources"][0]["view_id"] = "renamed-first-view"
    envelope_path.write_text(json.dumps(envelope))
    renamed = inspect_bundle(tmp_path)
    assert renamed.provenance_state == "verified", renamed.issues
    renamed_first = {
        r.track_refs[0] for r in renamed.records if r.kind == "track_reference" and r.view_id == "renamed-first-view"
    }
    unchanged_second = {
        r.track_refs[0] for r in renamed.records if r.kind == "track_reference" and r.view_id == "second-view"
    }
    assert first.isdisjoint(renamed_first) and renamed_first
    assert second == unchanged_second


def test_import_has_no_socket_or_ai_import(tmp_path: Path) -> None:
    import subprocess
    import sys

    from evidence_import_fixtures import synthetic_bundle

    root = synthetic_bundle(tmp_path)
    script = """
import socket, sys
class RefuseAI:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('incident_report.ai','minivision_reconstruct')):
            raise AssertionError(fullname)
sys.meta_path.insert(0, RefuseAI())
def forbidden(*a, **kw): raise AssertionError('network call')
socket.socket = forbidden
from pathlib import Path
from incident_report.evidence_import import inspect_bundle
result = inspect_bundle(Path(sys.argv[1]))
assert result.provenance_state == 'verified', result.issues
"""
    completed = subprocess.run([sys.executable, "-c", script, str(root)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_missing_upstream_keeps_proposals_quarantined(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    root = synthetic_bundle(tmp_path)
    reviewed = next(root.rglob("reviewed/revision-003/reviewed_events.jsonl"))
    reviewed.unlink()
    result = inspect_bundle(root)
    assert result.provenance_state == "incomplete_provenance", result.issues
    assert any(r.kind == "event_proposal" for r in result.records)
    assert result.quarantined_records and not result.structurally_accepted_records


def test_missing_review_revision_keeps_effective_values_untrusted(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    root = synthetic_bundle(tmp_path)
    revision = next(root.rglob("reviews/revision-003/revision.json"))
    revision.unlink()
    result = inspect_bundle(root)
    assert result.provenance_state == "incomplete_provenance", result.issues
    assert any(r.kind == "event_proposal" for r in result.records)
    assert not result.structurally_accepted_records


def test_latest_empty_review_snapshot_does_not_inherit_old_decisions(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    def clear_latest(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        for name, rows in documents.items():
            if "reviews/revision-003/revision.json" in name:
                rows[0]["document"]["decisions"] = []
            if "reviewed/revision-003/reviewed_events.jsonl" in name:
                for row in rows:
                    row["status"] = "PROPOSED_BY_SYSTEM"
                    row["decision"] = None
            if "reviewed/revision-003/reviewed_assessments.jsonl" in name:
                for row in rows:
                    row["event_status"] = "PROPOSED_BY_SYSTEM"

    result = inspect_bundle(synthetic_bundle(tmp_path, transform=clear_latest))
    assert result.provenance_state == "verified", result.issues
    assert not any(r.kind == "review_overlay" for r in result.records)


def test_missing_signal_frames_quarantine_the_bundle(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    def invalid_signal(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        for name, rows in documents.items():
            if name.endswith("/events.jsonl") and "/incident/" in name:
                for row in rows:
                    row["signals"][0]["source_frames"] = [999999]
            if "reviewed_events.jsonl" in name:
                for row in rows:
                    row["proposal"]["signals"][0]["source_frames"] = [999999]

    result = inspect_bundle(synthetic_bundle(tmp_path, transform=invalid_signal))
    assert result.provenance_state == "incomplete_provenance", result.issues
    assert "supporting evidence frame unresolved" in result.issues
    assert result.records and not result.structurally_accepted_records


def test_root_junction_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = bundle(tmp_path)
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda p: p == root or original(p))
    assert inspect_bundle(root).provenance_state == "rejected"


def test_real_relative_units_and_gap_states_round_trip() -> None:
    root = FIXTURES / "mig02-relative"
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    result = inspect_bundle(root)
    assert result.provenance_state == "verified", result.issues
    states = {r.original.get("state") for r in result.records if r.kind == "track_reference"}
    assert {"lost", "interpolated"} <= states
    assert {r.original["unit"] for r in result.records if r.kind == "geometry"} == {"relative_units"}
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in before.items())


def test_failed_calibration_is_missing_input(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    def failed(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        for name, rows in documents.items():
            if name.endswith("/calibration.json"):
                rows[0]["validation_passed"] = False

    result = inspect_bundle(synthetic_bundle(tmp_path, transform=failed))
    assert result.provenance_state == "incomplete_provenance", result.issues
    assert "missing_input: failed calibration" in result.issues
    assert not result.structurally_accepted_records


def test_unknown_transform_is_rejected(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    def transform(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        envelope["sources"][0]["transformations"] = [{"operation": "crop", "x": 200}]

    assert inspect_bundle(synthetic_bundle(tmp_path, transform=transform)).provenance_state == "rejected"


@pytest.mark.parametrize("field", ["reconstruction_manifest", "movement_manifest", "incident_manifest"])
def test_missing_manifest_is_incomplete_with_available_records(tmp_path: Path, field: str) -> None:
    from evidence_import_fixtures import synthetic_bundle

    root = synthetic_bundle(tmp_path)
    envelope = json.loads((root / "bundle.json").read_bytes())
    (root / envelope["sources"][0][field]).unlink()
    result = inspect_bundle(root)
    assert result.provenance_state == "incomplete_provenance", result.issues
    assert any(r.kind == "event_proposal" for r in result.records)
    assert result.quarantined_records and not result.structurally_accepted_records


def test_review_chain_cannot_skip_revisions(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    def skip_revision(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        oldest = next(rows[0] for name, rows in documents.items() if "reviews/revision-001/revision.json" in name)
        latest = next(rows[0] for name, rows in documents.items() if "reviews/revision-003/revision.json" in name)
        latest["previous_sha256"] = oldest["previous_sha256"]

    result = inspect_bundle(synthetic_bundle(tmp_path, transform=skip_revision))
    assert result.provenance_state == "rejected"


@pytest.mark.parametrize("name", ["unknown-version", "path-traversal", "hash-mismatch", "malformed-jsonl"])
def test_committed_handmade_negative_fixtures(name: str) -> None:
    assert inspect_bundle(FIXTURES / "negative" / name).provenance_state == "rejected"


def test_all_committed_fixture_bytes_match_frozen_hashes() -> None:
    expected = json.loads((FIXTURES / "SHA256SUMS.json").read_bytes())
    assert expected
    assert all(
        hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest for name, digest in expected.items()
    )


def test_movement_events_are_normalized_with_source_citations(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    result = inspect_bundle(synthetic_bundle(tmp_path))
    events = [r for r in result.records if r.kind == "event_proposal" and "/movement/" in r.artifact_path]
    assert events
    assert all(r.source_frame is not None and r.timestamp_provenance is not None for r in events)


def test_case_identity_never_collides(tmp_path: Path) -> None:
    from evidence_import_fixtures import synthetic_bundle

    first = inspect_bundle(synthetic_bundle(tmp_path / "a"))

    def change_case(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        envelope["case_id"] = "different-case"

        def change(value: Any) -> None:
            if isinstance(value, dict):
                if "case_id" in value:
                    value["case_id"] = "different-case"
                for nested in value.values():
                    change(nested)
            elif isinstance(value, list):
                for nested in value:
                    change(nested)

        change(documents)

    second = inspect_bundle(synthetic_bundle(tmp_path / "b", transform=change_case))
    first_refs = {r.track_refs[0] for r in first.records if r.kind == "track_reference"}
    second_refs = {r.track_refs[0] for r in second.records if r.kind == "track_reference"}
    assert first_refs and second_refs and first_refs.isdisjoint(second_refs), second.issues


def test_real_producer_revision_contains_all_three_review_actions() -> None:
    root = FIXTURES / "mig02-multi-review"
    revision = next(root.rglob("reviews/revision-001/revision.json"))
    document = json.loads(revision.read_bytes())
    assert {d["action"] for d in document["document"]["decisions"]} == {"confirm", "reject", "modify"}
    result = inspect_bundle(root)
    assert result.provenance_state == "verified", result.issues
    assert sum(r.kind == "review_overlay" for r in result.records) == 3
