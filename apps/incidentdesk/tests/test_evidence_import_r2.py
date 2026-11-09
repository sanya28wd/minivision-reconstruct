"""Round 2 compatibility with unchanged producer exports."""

from pathlib import Path

import pytest

from evidence_import_fixtures import FIXTURES
from incident_report.evidence_import import inspect_bundle


@pytest.mark.parametrize("name", [p.name for p in FIXTURES.glob("mig02-*")])
def test_original_frame_times_resolve_without_rewriting(name: str) -> None:
    result = inspect_bundle(FIXTURES / name)
    assert "source frame/time unresolved" not in result.issues
    for record in result.records:
        if record.kind == "track_reference":
            assert record.source_time is not None


@pytest.mark.parametrize("name", [p.name for p in FIXTURES.glob("mig02-*")])
def test_real_export_is_verified(name: str) -> None:
    result = inspect_bundle(FIXTURES / name)
    assert result.provenance_state == "verified", result.issues
    assert result.not_supplied_derived


def test_duplicate_video_binding_is_rejected(tmp_path: Path) -> None:
    import json
    from copy import deepcopy

    from evidence_import_fixtures import synthetic_bundle

    root = synthetic_bundle(tmp_path)
    envelope = json.loads((root / "bundle.json").read_bytes())
    second = deepcopy(envelope["sources"][0])
    second["view_id"] = "other-view"
    envelope["sources"].append(second)
    (root / "bundle.json").write_text(json.dumps(envelope))
    assert inspect_bundle(root).provenance_state == "rejected"


def test_typed_contract_exposes_geometry_and_evidence() -> None:
    result = inspect_bundle(FIXTURES / "mig02-relative")
    geometry = next(r for r in result.records if r.kind == "geometry")
    from incident_report.evidence_import.payloads import EventFields, GeometryFields

    assert isinstance(geometry.data, GeometryFields)
    assert geometry.data.unit == "relative_units"
    assert geometry.data.calibration_mode == "non_metric"
    event = next(r for r in result.records if r.kind == "event_proposal")
    assert isinstance(event.data, EventFields)
    assert event.data.event_id == event.original["event_id"]
    assert event.data.start_frame >= 0


def test_importer_version_changes_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from incident_report.evidence_import import reader

    before = inspect_bundle(FIXTURES / "mig02-relative")
    monkeypatch.setattr(reader, "IMPORTER_VERSION", "test-next")
    after = inspect_bundle(FIXTURES / "mig02-relative")
    assert before.import_id != after.import_id
    assert after.importer_version == "test-next"


def test_scene_unit_disagreement_is_flagged(tmp_path: Path) -> None:
    from typing import Any

    from evidence_import_fixtures import synthetic_bundle

    def mismatch(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        for name, rows in documents.items():
            if name.endswith("/scene.json"):
                rows[0]["display_unit"] = "relative_units"

    result = inspect_bundle(synthetic_bundle(tmp_path, transform=mismatch))
    assert "scene display unit mismatch" in result.issues


def test_typed_event_times_are_canonical() -> None:
    from incident_report.evidence_import.payloads import EventFields, SourceFields

    result = inspect_bundle(FIXTURES / "mig02-001-01-rear-end")
    for record in result.records:
        if isinstance(record.data, EventFields) and record.source_time is not None:
            assert record.data.peak_time == record.source_time
        if isinstance(record.data, SourceFields):
            assert record.data.frame_origin == 0
            assert record.data.video_metadata.width == 640


def test_relative_uncertainty_is_available_without_position_uncertainty(tmp_path: Path) -> None:
    from typing import Any

    from evidence_import_fixtures import synthetic_bundle
    from incident_report.evidence_import.payloads import GeometryFields

    def change(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        for name, rows in documents.items():
            if name.endswith("/smoothed.jsonl"):
                rows[0]["relative_uncertainty"] = 0.125
                rows[0]["position_uncertainty"] = None

    result = inspect_bundle(synthetic_bundle(tmp_path, transform=change))
    assert any(
        isinstance(r.data, GeometryFields)
        and r.data.position_uncertainty is None
        and r.data.relative_uncertainty == 0.125
        for r in result.records
    )


def test_malformed_derived_hash_is_rejected(tmp_path: Path) -> None:
    import hashlib
    import json
    import shutil

    root = tmp_path / "bundle"
    shutil.copytree(FIXTURES / "mig02-relative", root)
    manifest = next(root.rglob("week8-incident-relative/manifest.json"))
    document = json.loads(manifest.read_bytes())
    next(a for a in document["artifacts"] if a["filename"] == "replay.mp4")["sha256"] = "bad"
    manifest.write_text(json.dumps(document))
    envelope = json.loads((root / "bundle.json").read_bytes())
    envelope["artifacts"][manifest.relative_to(root).as_posix()] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (root / "bundle.json").write_text(json.dumps(envelope))
    assert inspect_bundle(root).provenance_state == "rejected"


def test_envelope_can_list_omitted_derived_artifacts(tmp_path: Path) -> None:
    import json
    import shutil

    root = tmp_path / "bundle"
    shutil.copytree(FIXTURES / "mig02-relative", root)
    envelope = json.loads((root / "bundle.json").read_bytes())
    for manifest in root.rglob("manifest.json"):
        document = json.loads(manifest.read_bytes())
        for artifact in document.get("artifacts", []):
            path = manifest.parent / artifact["filename"]
            if not path.exists() and artifact["filename"] in ("replay.mp4", "trajectories.csv"):
                envelope["artifacts"][path.relative_to(root).as_posix()] = artifact["sha256"]
    (root / "bundle.json").write_text(json.dumps(envelope))
    assert inspect_bundle(root).provenance_state == "verified"
