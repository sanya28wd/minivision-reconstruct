"""RPT-32 synthetic-fixture: observations and rules, no live AI or accuracy claims."""

import json
from typing import Any

import pytest

from incident_report.ai.gemini import parse_overview


def payload(kind: str = "eye_closure", start: float | None = 1.0, end: float | None = 4.2) -> dict[str, Any]:
    return {
        "parties": [{"description": "white Toyota sedan", "timestamp_seconds": 0.0, "confidence": 0.9}],
        "events": [],
        "candidate_collisions": [],
        "behaviour_observations": [],
        "location_conditions": [],
        "structured_facts": [],
        "text_readings": [],
        "driver_observations": [],
        "collision_observations": [],
        "drowsiness_observations": [
            {
                "kind": kind,
                "subject": "white Toyota sedan",
                "timestamp_seconds": 2.0,
                "start_seconds": start,
                "end_seconds": end,
                "confidence": 0.9,
            }
        ],
    }


def test_cited_eye_closure_duration_is_computed_and_never_fined() -> None:
    from incident_report.agreement import agree_no_collision_violations
    from incident_report.rules import evaluate_fault
    from incident_report.rules.drowsiness import assess_drowsiness

    facts = parse_overview(json.dumps(payload()), "inside", prompt_version="overview-7", view_role="interior")
    assessment = assess_drowsiness(facts)[0]
    assert assessment.duration_seconds == pytest.approx(3.2)
    assert assessment.label == "Severe eye closure"
    assert assessment.causal_role == "Contributing Factor"
    assert {f.timestamp_seconds for f in assessment.cited_facts} == {1.0, 2.0, 4.2}
    assert {f.view_id for f in assessment.cited_facts} == {"inside"}
    assert "project assumption, to be validated" in assessment.reason
    faults = evaluate_fault(facts)
    assert faults[0].findings[0].rule_id == "driver_drowsiness"
    violations, _ = agree_no_collision_violations([facts], [faults])
    assert violations == ()


def test_cause_requires_same_view_cited_contact_during_complete_eye_closure() -> None:
    from incident_report.rules import evaluate_fault
    from incident_report.rules.drowsiness import assess_drowsiness

    data = payload()
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
    facts = parse_overview(json.dumps(data), "inside", prompt_version="overview-7", view_role="interior")
    a = assess_drowsiness(facts)[0]
    assert a.causal_role == "Cause"
    assert any(f.timestamp_seconds == 3.0 for f in a.cited_facts)
    assert evaluate_fault(facts)[0].findings[0].classification == "Finding"
    assert evaluate_fault(facts)[0].status.value == "Cannot Determine"
    other = facts.collision_observations[0].model_copy(update={"view_id": "other"})
    assert (
        assess_drowsiness(facts.model_copy(update={"collision_observations": [other]}))[0].causal_role
        == "Contributing Factor"
    )
    after = facts.collision_observations[0].model_copy(update={"timestamp_seconds": 5.0})
    assert (
        assess_drowsiness(facts.model_copy(update={"collision_observations": [after]}))[0].causal_role
        == "Contributing Factor"
    )
    assert (
        assess_drowsiness(facts.model_copy(update={"candidate_collisions": []}))[0].causal_role == "Contributing Factor"
    )


@pytest.mark.parametrize("start,end", [(None, 4.0), (1.0, None), (None, None)])
def test_missing_endpoint_cannot_determine_duration(start: float | None, end: float | None) -> None:
    from incident_report.rules.drowsiness import assess_drowsiness

    facts = parse_overview(
        json.dumps(payload(start=start, end=end)), "inside", prompt_version="overview-7", view_role="interior"
    )
    a = assess_drowsiness(facts)[0]
    assert a.duration_seconds is None
    assert a.label == "Cannot Determine"
    assert "duration: Cannot Determine" in a.reason
    assert a.causal_role == "Cannot Determine"


@pytest.mark.parametrize(
    "duration,label",
    [
        (1.5, "Eye closure below microsleep threshold"),
        (1.6, "Microsleep"),
        (3.0, "Microsleep"),
        (3.1, "Severe eye closure"),
    ],
)
def test_thresholds_use_observed_interval_without_summing_gaps(duration: float, label: str) -> None:
    from incident_report.rules.drowsiness import assess_drowsiness

    data = payload(start=1.0, end=1.0 + duration)
    data["drowsiness_observations"][0]["timestamp_seconds"] = 1.0
    facts = parse_overview(json.dumps(data), "inside", prompt_version="overview-7", view_role="interior")
    assert assess_drowsiness(facts)[0].label == label
    assert assess_drowsiness(facts)[0].duration_seconds == pytest.approx(duration)


@pytest.mark.parametrize("kind", ["yawning", "head_nodding", "eye_rubbing", "blank_stare", "lane_drift"])
def test_each_observable_cue_is_cited_without_claiming_duration_or_cause(kind: str) -> None:
    from incident_report.rules.drowsiness import assess_drowsiness

    role = "exterior" if kind == "lane_drift" else "interior"
    facts = parse_overview(json.dumps(payload(kind)), "view", prompt_version="overview-7", view_role=role)
    a = assess_drowsiness(facts)[0]
    assert a.duration_seconds is None
    assert a.causal_role == ("Cannot Determine" if kind == "lane_drift" else "Contributing Factor")
    assert all(f.view_id == "view" for f in a.cited_facts)
    if kind == "lane_drift":
        assert a.label == "Cannot Determine"


@pytest.mark.parametrize("kind,role", [("eye_closure", "exterior"), ("lane_drift", "interior"), ("yawning", None)])
def test_roles_cannot_be_mixed(kind: str, role: str | None) -> None:
    from incident_report.ai.errors import AIConnectionError

    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(payload(kind)), "view", prompt_version="overview-7", view_role=role)


@pytest.mark.parametrize(
    "change",
    [
        {"start_seconds": 3.0},
        {"end_seconds": 1.0},
        {"confidence": 1.1},
        {"start_seconds": -1.0},
        {"end_seconds": float("nan")},
        {"subject": "V1"},
        {"view_id": "other"},
        {"duration_seconds": 20.0},
        {"kind": "drowsy"},
    ],
)
def test_invalid_or_model_guessed_evidence_is_rejected(change: dict[str, Any]) -> None:
    from incident_report.ai.errors import AIConnectionError

    data = payload()
    data["drowsiness_observations"][0].update(change)
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "inside", prompt_version="overview-7", view_role="interior")


@pytest.mark.parametrize(
    "field",
    ["drowsiness_observations", "collision_observations", "driver_observations", "text_readings", "structured_facts"],
)
def test_overview7_requires_all_inherited_arrays(field: str) -> None:
    from incident_report.ai.errors import AIConnectionError

    data = payload()
    del data[field]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "inside", prompt_version="overview-7", view_role="interior")


def test_overview7_transport_schema_bounded_repair_and_replay(tmp_path: Any) -> None:
    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo, prompt_for_version
    from incident_report.ai.google_video import GoogleVideoTransport
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.ai.overview_replay import ReplayOverviewAIConnection
    from incident_report.domain import Role
    from incident_report.facts import OVERVIEW_7_SCHEMA
    from incident_report.hashing import incident_input_hash, sha256_hex
    from test_google_video_transport import FakeClient
    from test_live_gemini import PUBLIC_VIDEO, FakeTransport

    video = tmp_path / "synthetic.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    req = AIRequest(incident_input_hash([(Role.INTERIOR, digest)]), "overview-7", (AIViewRef(Role.INTERIOR, digest),))
    settings = GeminiSettings(model="gemini-3.8-flash")
    invalid = payload()
    del invalid["drowsiness_observations"]
    response = GeminiAIConnection(
        settings,
        PublicResearchVideo(video, True),
        transport=FakeTransport([json.dumps(invalid), json.dumps(payload())]),
    ).analyse(req)
    assert response.facts and response.facts.drowsiness_observations
    assert len(response.calls) == 2
    assert ReplayOverviewAIConnection(settings, response.calls).analyse(req).facts == response.facts
    client = FakeClient()
    GoogleVideoTransport(client).generate(PublicResearchVideo(video, True), prompt_for_version("overview-7"), settings)
    assert client.generated["config"]["response_schema"] == OVERVIEW_7_SCHEMA
    assert "maxItems" not in json.dumps(OVERVIEW_7_SCHEMA)


def test_analysis_preserves_events_through_views_dense_and_report_reload(run_log: Any, tmp_path: Any) -> None:
    from conftest import make_view
    from incident_report.ai.dense import consolidate_facts
    from incident_report.domain import Origin, Report, Role
    from incident_report.entry import analyse_incident
    from incident_report.hashing import sha256_hex
    from incident_report.storage import LocalEvidenceStore
    from test_new_incident_screen import SpyAIConnection
    from video_fixtures import synthetic_video

    video = synthetic_video(b"drowsiness")
    digest = sha256_hex(video)
    data = payload(start=0.0, end=0.2)
    data["drowsiness_observations"][0]["timestamp_seconds"] = 0.1
    facts = parse_overview(json.dumps(data), digest, prompt_version="overview-7", view_role="interior")
    report = analyse_incident(
        [make_view(video, Role.INTERIOR)],
        Origin.APP,
        ai=SpyAIConnection(per_view_facts={digest: facts}),
        run_log=run_log,
        prompt_version="overview-7",
        evidence=LocalEvidenceStore(tmp_path / "evidence"),
    )
    assert report.is_valid
    assert report.facts and report.facts.drowsiness_observations == facts.drowsiness_observations
    assert Report.model_validate_json(report.model_dump_json()).facts == report.facts
    assert report.violations == ()
    assert consolidate_facts(facts, []).drowsiness_observations == facts.drowsiness_observations


def test_legacy_prompt_schema_request_hashes_and_default_stay_pinned() -> None:
    import hashlib
    from pathlib import Path

    import incident_report.facts as contracts
    from incident_report.ai.gemini import PROMPT_VERSION, GeminiSettings, overview_request_hash, prompt_for_version
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.domain import Role

    baseline = json.loads(Path(__file__).with_name("drowsiness_legacy_baseline.json").read_text())
    schemas = [
        "LEGACY_OVERVIEW_SCHEMA",
        "OVERVIEW_SCHEMA",
        "OVERVIEW_3_SCHEMA",
        "OVERVIEW_4_SCHEMA",
        "OVERVIEW_5_SCHEMA",
        "OVERVIEW_6_SCHEMA",
    ]
    for i, name in enumerate(schemas, 1):
        version = f"overview-{i}"
        request = AIRequest(
            input_hash="a" * 64, prompt_version=version, views=(AIViewRef(Role.EXTERIOR, "b" * 64, 20.0),)
        )
        assert hashlib.sha256(prompt_for_version(version).encode()).hexdigest() == baseline[version]["prompt"]
        assert (
            hashlib.sha256(json.dumps(getattr(contracts, name), sort_keys=True).encode()).hexdigest()
            == baseline[version]["schema"]
        )
        assert overview_request_hash(request, GeminiSettings(model="gemini-2.5-pro")) == baseline[version]["request"]
    assert PROMPT_VERSION == "overview-2"


def test_endpoint_citations_are_validated_against_duration_and_view_role() -> None:
    from types import SimpleNamespace

    from incident_report.facts import validate_citations

    facts = parse_overview(json.dumps(payload()), "inside", prompt_version="overview-7", view_role="interior")
    view = SimpleNamespace(sha256="inside", role="interior", duration_seconds=3.0)
    assert not validate_citations(facts, [view]).is_valid
    view.duration_seconds = 10.0
    assert validate_citations(facts, [view]).is_valid
    view.role = "exterior"
    assert not validate_citations(facts, [view]).is_valid


def test_incomplete_closure_and_lane_drift_abstain_without_positive_factors() -> None:
    from incident_report.rules import evaluate_fault

    for data, role in [(payload(start=None), "interior"), (payload("lane_drift"), "exterior")]:
        facts = parse_overview(json.dumps(data), "view", prompt_version="overview-7", view_role=role)
        party = evaluate_fault(facts)[0]
        assert party.findings == ()
        assert "Cannot Determine" in party.reason
        assert any(f.view_id == "view" and f.timestamp_seconds == 2.0 for f in party.cited_facts)


def test_multiple_drowsiness_cues_survive_contributing_agreement() -> None:
    from incident_report.agreement import apply_contributing_agreement
    from incident_report.rules import evaluate_fault

    data = payload()
    data["drowsiness_observations"].append({**data["drowsiness_observations"][0], "kind": "yawning"})
    facts = parse_overview(json.dumps(data), "inside", prompt_version="overview-7", view_role="interior")
    faults = evaluate_fault(facts)
    agreed = apply_contributing_agreement(faults, [faults])
    assert "Severe eye closure" in " ".join(f.reason for f in agreed[0].findings)
    assert "yawning" in " ".join(f.reason for f in agreed[0].findings)


@pytest.mark.parametrize("kind", ["eye_closure", "yawning", "head_nodding", "eye_rubbing", "blank_stare", "lane_drift"])
@pytest.mark.parametrize("contact", [False, True])
def test_drowsiness_alone_never_generates_any_fine(kind: str, contact: bool) -> None:
    from incident_report.agreement import agree_no_collision_violations
    from incident_report.rules import evaluate_fault
    from incident_report.rules.adapter import situation_from_overview
    from incident_report.rules.violations import evaluate_violations, violations_from_faults

    data = payload(kind)
    if contact:
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
    facts = parse_overview(
        json.dumps(data),
        "view",
        prompt_version="overview-7",
        view_role="exterior" if kind == "lane_drift" else "interior",
    )
    faults = evaluate_fault(facts)
    assert evaluate_violations(situation_from_overview(facts)) == ()
    assert violations_from_faults(faults) == ()
    assert agree_no_collision_violations([facts], [faults])[0] == ()


def test_drowsiness_added_by_consolidation_is_retained_without_claiming_overview_agreement() -> None:
    from incident_report.agreement import apply_contributing_agreement
    from incident_report.rules import evaluate_fault

    data = payload()
    overview = parse_overview(json.dumps(data), "inside", prompt_version="overview-7", view_role="interior")
    data["drowsiness_observations"].append({**data["drowsiness_observations"][0], "kind": "yawning"})
    final = parse_overview(json.dumps(data), "inside", prompt_version="overview-7", view_role="interior")
    agreed = apply_contributing_agreement(evaluate_fault(final), [evaluate_fault(overview)])
    assert "yawning" in agreed[0].findings[0].reason
    assert agreed[0].findings[0].agreement_status == "not_measured"


def test_drowsiness_timeline_escapes_subject_and_labels_missing_endpoints() -> None:
    from incident_report.web.templating import build_environment
    from test_report_parity import full_report

    report = full_report()
    assert report.facts
    observation = report.facts.drowsiness_observations[0].model_copy(
        update={
            "subject": "<script>alert(1)</script> white sedan",
            "start_seconds": None,
            "end_seconds": None,
        }
    )
    report = report.model_copy(
        update={"facts": report.facts.model_copy(update={"drowsiness_observations": [observation]})}
    )
    html = build_environment().get_template("report.html").render(report=report)
    assert "Drowsiness observation timeline" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Start: Cannot Determine" in html
    assert "End: Cannot Determine" in html
