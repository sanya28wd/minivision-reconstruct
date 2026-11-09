"""RPT-32 round 2 synthetic-fixture integration with final report guardrails."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import make_view
from incident_report.ai.dense import DenseSettings
from incident_report.ai.gemini import parse_overview
from incident_report.domain import Origin, Role
from incident_report.entry import analyse_incident
from incident_report.guardrails import validate_report
from incident_report.hashing import sha256_hex
from incident_report.storage import LocalEvidenceStore
from test_drowsiness import payload
from test_new_incident_screen import SpyAIConnection


@pytest.fixture
def interior_video(tmp_path: Path) -> bytes:
    # Five seconds of generated black frames, not footage or accuracy evidence.
    path = tmp_path / "synthetic-interior.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=10",
            "-t",
            "5",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return path.read_bytes()


def round2_report(video: bytes, kind: str, run_log: Any, tmp_path: Path) -> Any:
    data = payload(kind, 0.0, 0.3) if kind == "yawning" else payload()
    if kind == "eye_closure":
        data["candidate_collisions"] = [
            {"description": "white Toyota sedan contacts barrier", "timestamp_seconds": 3.0, "confidence": 0.9}
        ]
        data["collision_observations"] = [
            {
                "kind": "contact_other",
                "subject": "white Toyota sedan",
                "other_party": "barrier",
                "injury_grade": "unknown",
                "timestamp_seconds": 3.0,
                "confidence": 0.9,
            }
        ]
    else:
        data["drowsiness_observations"][0]["timestamp_seconds"] = 0.1
    digest = sha256_hex(video)
    facts = parse_overview(json.dumps(data), digest, prompt_version="overview-7", view_role="interior")
    return analyse_incident(
        [make_view(video, Role.INTERIOR)],
        Origin.APP,
        ai=SpyAIConnection(per_view_facts={digest: facts}),
        run_log=run_log,
        prompt_version="overview-7",
        evidence=LocalEvidenceStore(tmp_path / "evidence"),
        dense_settings=DenseSettings(model="gemini-3.8-flash"),
    )


@pytest.mark.parametrize("kind", ["yawning", "eye_closure"])
def test_project_drowsiness_survives_final_guards_without_a_violation(
    interior_video: bytes,
    run_log: Any,
    tmp_path: Path,
    kind: str,
) -> None:
    report = round2_report(interior_video, kind, run_log, tmp_path)
    assert report.is_valid, report.validation_errors
    assert report.parties[0].findings[0].rule_id == "driver_drowsiness"
    assert report.violations == ()
    assert report.guardrail_results["legal_sources"] == ()
    assert report.guardrail_results["citations"] == ()
    if kind == "eye_closure":
        assert "Possible cause:" in report.parties[0].findings[0].reason
        assert report.parties[0].findings[0].classification == "Finding"
    stored = run_log.get(report.run_id).report
    assert stored is not None
    assert stored.model_dump() == report.model_dump()
    assert stored == report, {
        name: (type(getattr(stored, name)).__name__, type(getattr(report, name)).__name__)
        for name in type(report).model_fields
        if getattr(stored, name) != getattr(report, name)
    }


@pytest.mark.parametrize(
    "change",
    [
        {"article": "invented"},
        {"legal_source": "invented"},
        {"source_url": "invented"},
        {"aed": 1},
        {"black_points": 0},
        {"impound_days": 1},
        {"rule_id": "driver_fatigue_x"},
    ],
)
def test_project_observation_allowlist_rejects_tampering(
    interior_video: bytes,
    run_log: Any,
    tmp_path: Path,
    change: dict[str, Any],
) -> None:
    report = round2_report(interior_video, "yawning", run_log, tmp_path)
    assert report.is_valid, report.validation_errors
    party = report.parties[0]
    bad = party.findings[0].model_copy(update=change)
    checked = validate_report(report.model_copy(update={"parties": (party.model_copy(update={"findings": (bad,)}),)}))
    assert not checked.is_valid
    assert checked.guardrail_results["legal_sources"]


def test_allowlisted_project_finding_still_requires_valid_citations(
    interior_video: bytes,
    run_log: Any,
    tmp_path: Path,
) -> None:
    report = round2_report(interior_video, "yawning", run_log, tmp_path)
    assert report.is_valid, report.validation_errors
    party = report.parties[0]
    finding = party.findings[0]
    bad_citation = finding.cited_facts[0].model_copy(update={"view_id": "missing-view"})
    bad = finding.model_copy(update={"cited_facts": (bad_citation,)})
    checked = validate_report(report.model_copy(update={"parties": (party.model_copy(update={"findings": (bad,)}),)}))
    assert not checked.is_valid
    assert checked.guardrail_results["citations"]


def test_complete_short_eye_closure_is_known_and_does_not_change_party_reason() -> None:
    from incident_report.rules import evaluate_fault
    from incident_report.rules.drowsiness import assess_drowsiness

    data = payload(start=0.0, end=0.3)
    data["drowsiness_observations"][0]["timestamp_seconds"] = 0.1
    facts = parse_overview(json.dumps(data), "inside", prompt_version="overview-7", view_role="interior")
    a = assess_drowsiness(facts)[0]
    assert a.label == "Eye closure below microsleep threshold"
    assert a.duration_seconds == pytest.approx(0.3)
    assert a.causal_role is None
    before = evaluate_fault(facts.model_copy(update={"drowsiness_observations": []}))[0]
    after = evaluate_fault(facts)[0]
    assert after.findings == ()
    assert after.reason == before.reason
