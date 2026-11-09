"""RPT-30 # synthetic-fixture: driver observations, never accuracy evidence."""

import json
from typing import Any

import pytest

from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import parse_overview


def payload(*observations: tuple[str, float, float]) -> dict[str, Any]:
    return {
        "parties": [{"description": "white Toyota sedan", "timestamp_seconds": 0.0, "confidence": 0.9}],
        "events": [],
        "candidate_collisions": [],
        "behaviour_observations": [],
        "location_conditions": [],
        "structured_facts": [],
        "text_readings": [],
        "driver_observations": [
            dict(kind=k, subject="white Toyota sedan", start_seconds=s, end_seconds=e, confidence=0.9)
            for k, s, e in observations
        ],
    }


def test_interior_observations_required_and_cited() -> None:
    facts = parse_overview(
        json.dumps(payload(("phone_in_hand", 1.0, 2.0))), "inside", prompt_version="overview-4", view_role="interior"
    )
    assert facts.driver_observations[0].view_id == "inside"
    assert facts.driver_observations[0].end_seconds == 2.0
    missing = payload()
    del missing["driver_observations"]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(missing), "inside", prompt_version="overview-4", view_role="interior")


@pytest.mark.parametrize(
    "observations,expected",
    [
        ([("phone_in_hand", 1.0, 2.0)], "Entertainment"),
        ([("phone_on_mount", 0.0, 8.0), ("looking_at_phone", 1.0, 1.5)], "GPS"),
        ([("looking_at_phone", 1.0, 4.0)], "Entertainment"),
        ([("tapping_phone", 1.0, 1.1), ("tapping_phone", 2.0, 2.1), ("tapping_phone", 3.0, 3.1)], "Entertainment"),
        ([("smiling_at_phone", 1.0, 2.0)], "Cannot Determine"),
        ([("phone_on_mount", 1.0, 2.0)], "Cannot Determine"),
    ],
)
def test_phone_classification(observations: list[tuple[str, float, float]], expected: str) -> None:
    from incident_report.rules.driver import classify_driver_observations

    facts = parse_overview(
        json.dumps(payload(*observations)), "inside", prompt_version="overview-4", view_role="interior"
    )
    result = classify_driver_observations(facts)[0]
    assert result.phone_use == expected
    assert result.cited_facts
    assert "project assumption, to be validated" in result.reason


@pytest.mark.parametrize("kind,key", [("phone_in_hand", "mobile_phone_distraction"), ("seatbelt_not_worn", "seatbelt")])
def test_standalone_driver_fines_use_existing_rows(kind: str, key: str) -> None:
    from incident_report.agreement import agree_no_collision_violations
    from incident_report.fines import FinesTable
    from incident_report.rules import evaluate_fault

    facts = parse_overview(
        json.dumps(payload((kind, 1.0, 2.0))), "inside", prompt_version="overview-4", view_role="interior"
    )
    violations, _ = agree_no_collision_violations([facts], [evaluate_fault(facts)])
    assert len(violations) == 1
    row = FinesTable.default().get(key)
    assert violations[0].item_number == row.item_number
    assert violations[0].status == row.status
    assert violations[0].cited_facts[0].view_id == "inside"
    assert all(p.status.value == "Cannot Determine" for p in evaluate_fault(facts))


def test_previous_contract_fingerprints_unchanged() -> None:
    import hashlib
    from pathlib import Path

    from incident_report.ai.gemini import PROMPT_VERSION, GeminiSettings, overview_request_hash, prompt_for_version
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.domain import Role
    from incident_report.facts import LEGACY_OVERVIEW_SCHEMA, OVERVIEW_3_SCHEMA, OVERVIEW_4_SCHEMA, OVERVIEW_SCHEMA

    baseline = json.loads(Path(__file__).with_name("phone_legacy_baseline.json").read_text())
    for version, schema in zip(
        ("overview-1", "overview-2", "overview-3"),
        (LEGACY_OVERVIEW_SCHEMA, OVERVIEW_SCHEMA, OVERVIEW_3_SCHEMA),
        strict=True,
    ):
        req = AIRequest(input_hash="a" * 64, prompt_version=version, views=(AIViewRef(Role.INTERIOR, "b" * 64, 20.0),))
        assert hashlib.sha256(prompt_for_version(version).encode()).hexdigest() == baseline[version]["prompt"]
        assert hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest() == baseline[version]["schema"]
        assert overview_request_hash(req, GeminiSettings(model="gemini-2.5-pro")) == baseline[version]["request"]
    assert PROMPT_VERSION == "overview-2"
    assert "driver_observations" in OVERVIEW_4_SCHEMA["required"]
    assert "maxItems" not in json.dumps(OVERVIEW_4_SCHEMA)


@pytest.mark.parametrize("role", ["exterior", "unknown", None])
def test_parser_rejects_driver_observations_outside_interior(role: str | None) -> None:
    with pytest.raises(AIConnectionError):
        parse_overview(
            json.dumps(payload(("phone_in_hand", 1.0, 2.0))), "view", prompt_version="overview-4", view_role=role
        )


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "Entertainment"},
        {"subject": "Vehicle 1"},
        {"subject": "blue van"},
        {"confidence": float("nan")},
        {"start_seconds": -1.0},
        {"end_seconds": 0.0},
        {"view_id": "other"},
        {"verdict": "GPS"},
    ],
)
def test_parser_rejects_invalid_observation(change: dict[str, Any]) -> None:
    data = payload(("phone_in_hand", 1.0, 2.0))
    data["driver_observations"][0].update(change)
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "inside", prompt_version="overview-4", view_role="interior")


def test_l0_checks_role_duration_and_confidence() -> None:
    from incident_report.ai.interface import AIViewRef
    from incident_report.domain import Role
    from incident_report.facts import validate_citations

    facts = parse_overview(
        json.dumps(payload(("phone_in_hand", 1.0, 20.0))), "inside", prompt_version="overview-4", view_role="interior"
    )
    assert validate_citations(facts, [AIViewRef(Role.INTERIOR, "inside", 20.0)]).is_valid
    assert not validate_citations(facts, [AIViewRef(Role.INTERIOR, "inside", 19.0)]).is_valid
    assert not validate_citations(facts, [AIViewRef(Role.EXTERIOR, "inside", 20.0)]).is_valid
    assert not validate_citations(facts, ["inside"]).is_valid
    low = facts.model_copy(
        update={"driver_observations": [facts.driver_observations[0].model_copy(update={"confidence": 0.1})]}
    )
    assert not validate_citations(low, [AIViewRef(Role.INTERIOR, "inside", 20.0)]).is_valid


def test_look_intervals_merge_only_without_gaps_and_never_across_views() -> None:
    from incident_report.rules.driver import classify_driver_observations

    def facts_for(*items: tuple[str, float, float]) -> Any:
        return parse_overview(json.dumps(payload(*items)), "inside", prompt_version="overview-4", view_role="interior")

    contiguous = facts_for(("looking_at_phone", 1.0, 2.5), ("looking_at_phone", 2.5, 4.0))
    assert classify_driver_observations(contiguous)[0].phone_use == "Entertainment"
    gapped = facts_for(("looking_at_phone", 1.0, 2.4), ("looking_at_phone", 2.5, 4.0))
    assert classify_driver_observations(gapped)[0].phone_use == "Cannot Determine"
    other = gapped.driver_observations[1].model_copy(update={"view_id": "other", "start_seconds": 2.4})
    split = gapped.model_copy(update={"driver_observations": [gapped.driver_observations[0], other]})
    assert classify_driver_observations(split)[0].phone_use == "Cannot Determine"


def test_tapping_window_boundary_duplicates_and_custom_settings() -> None:
    from incident_report.config import PhoneUseSettings
    from incident_report.rules.driver import classify_driver_observations

    data = payload(("tapping_phone", 1.0, 1.1), ("tapping_phone", 3.0, 3.1), ("tapping_phone", 6.0, 6.1))
    facts = parse_overview(json.dumps(data), "inside", prompt_version="overview-4", view_role="interior")
    assert classify_driver_observations(facts)[0].phone_use == "Entertainment"
    assert (
        classify_driver_observations(facts, PhoneUseSettings(tapping_window_seconds=4.9))[0].phone_use
        == "Cannot Determine"
    )
    duplicate = facts.model_copy(update={"driver_observations": [facts.driver_observations[0]] * 3})
    assert classify_driver_observations(duplicate)[0].phone_use == "Cannot Determine"


@pytest.mark.parametrize(
    "kind",
    [
        "phone_on_mount",
        "looking_at_phone",
        "tapping_phone",
        "smiling_at_phone",
        "seatbelt_not_visible",
        "seatbelt_worn",
    ],
)
def test_other_observations_never_produce_fines(kind: str) -> None:
    from incident_report.agreement import agree_no_collision_violations
    from incident_report.rules import evaluate_fault

    facts = parse_overview(
        json.dumps(payload((kind, 1.0, 2.0))), "inside", prompt_version="overview-4", view_role="interior"
    )
    assert agree_no_collision_violations([facts], [evaluate_fault(facts)])[0] == ()


def test_driver_fine_vote_majority_split_and_invalid_abstention() -> None:
    from incident_report.agreement import agree_no_collision_violations
    from incident_report.rules import evaluate_fault

    yes = parse_overview(
        json.dumps(payload(("phone_in_hand", 1.0, 2.0))), "inside", prompt_version="overview-4", view_role="interior"
    )
    no = parse_overview(
        json.dumps(payload(("phone_on_mount", 1.0, 2.0))), "inside", prompt_version="overview-4", view_role="interior"
    )
    votes, _ = agree_no_collision_violations(
        [no, yes, yes], [evaluate_fault(no), evaluate_fault(yes), evaluate_fault(yes)]
    )
    assert len(votes) == 1
    assert votes[0].agreement_status == "flagged"
    assert votes[0].cited_facts[0].description.endswith("(1s)")
    assert agree_no_collision_violations([yes, no], [evaluate_fault(yes), evaluate_fault(no)])[0] == ()
    assert agree_no_collision_violations([yes, yes, yes], [evaluate_fault(yes), (), ()])[0] == ()


def test_collision_retains_driver_findings_without_changing_fault() -> None:
    from incident_report.agreement import agree_no_collision_violations
    from incident_report.rules import evaluate_fault

    facts = parse_overview(
        json.dumps(payload(("phone_in_hand", 1.0, 2.0), ("seatbelt_not_worn", 1.0, 2.0))),
        "inside",
        prompt_version="overview-4",
        view_role="interior",
    )
    facts = facts.model_copy(update={"candidate_collisions": [facts.parties[0]]})
    before = evaluate_fault(facts.model_copy(update={"driver_observations": []}))
    after = evaluate_fault(facts)
    assert [(p.status, p.rule_id, p.color) for p in before] == [(p.status, p.rule_id, p.color) for p in after]
    assert len(after[0].findings) == 2
    assert all(f.classification == "Contributing Factor" for f in after[0].findings)
    assert len(agree_no_collision_violations([facts], [after])[0]) == 2


def test_report_preserves_driver_evidence_and_escapes_text(run_log: Any, tmp_path: Any) -> None:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from conftest import make_view
    from incident_report.domain import Origin, Report, Role
    from incident_report.entry import analyse_incident
    from incident_report.hashing import sha256_hex
    from incident_report.storage import LocalEvidenceStore
    from test_new_incident_screen import SpyAIConnection
    from video_fixtures import synthetic_video

    videos = [synthetic_video(b"driver-interior"), synthetic_video(b"driver-exterior")]
    hashes = [sha256_hex(v) for v in videos]
    data = payload(("phone_in_hand", 0.0, 0.2), ("seatbelt_not_worn", 0.0, 0.2), ("smiling_at_phone", 0.0, 0.2))
    data["parties"][0]["description"] = "<script>alert(1)</script> white sedan"
    for o in data["driver_observations"]:
        o["subject"] = data["parties"][0]["description"]
    inside = parse_overview(json.dumps(data), hashes[0], prompt_version="overview-4", view_role="interior")
    outside = parse_overview(json.dumps(payload()), hashes[1], prompt_version="overview-4", view_role="exterior")
    report = analyse_incident(
        [make_view(videos[0], Role.INTERIOR), make_view(videos[1], Role.EXTERIOR)],
        Origin.APP,
        ai=SpyAIConnection(per_view_facts={hashes[0]: inside, hashes[1]: outside}),
        run_log=run_log,
        prompt_version="overview-4",
        evidence=LocalEvidenceStore(tmp_path / "evidence"),
    )
    assert report.is_valid
    assert report.facts and len(report.facts.driver_observations) == 3
    assert len(report.violations) == 2
    assert any(v.is_provisional for v in report.violations)
    assert report.incident_status == "No collision"
    assert report.driver_assessments and report.driver_assessments[0].phone_use == "Entertainment"
    assert "smiling_at_phone" in report.driver_assessments[0].cited_facts[-1].description
    assert Report.model_validate_json(report.model_dump_json()).driver_assessments == report.driver_assessments
    env = Environment(loader=FileSystemLoader("src/incident_report/web/templates"), autoescape=select_autoescape())
    html = env.get_template("report.html").render(report=report, officer_question=None)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "0.2s" in html and hashes[0] in html


@pytest.mark.parametrize("worn_start,expected", [(5.0, 1), (2.0, 1), (1.5, 0)])
def test_later_belt_wearing_does_not_erase_earlier_visible_breach(worn_start: float, expected: int) -> None:
    from incident_report.agreement import agree_no_collision_violations
    from incident_report.rules import evaluate_fault

    facts = parse_overview(
        json.dumps(payload(("seatbelt_not_worn", 1.0, 2.0), ("seatbelt_worn", worn_start, worn_start + 1.0))),
        "inside",
        prompt_version="overview-4",
        view_role="interior",
    )
    result, _ = agree_no_collision_violations([facts], [evaluate_fault(facts)])
    assert len(result) == expected


def test_missing_look_duration_is_unknown() -> None:
    from incident_report.rules.driver import classify_driver_observations

    facts = parse_overview(
        json.dumps(payload(("seatbelt_not_visible", 1.0, 2.0))),
        "inside",
        prompt_version="overview-4",
        view_role="interior",
    )
    assert "Cannot Determine (no look evidence)" in classify_driver_observations(facts)[0].reason


def test_report_labels_each_run_when_classification_disagrees_with_majority_fine(run_log: Any, tmp_path: Any) -> None:
    from conftest import make_view
    from incident_report.agreement import AgreementSettings
    from incident_report.ai.interface import AIResponse
    from incident_report.domain import Origin, Role
    from incident_report.entry import analyse_incident
    from incident_report.hashing import sha256_hex
    from incident_report.storage import LocalEvidenceStore
    from test_agreement_signal import FakeMultiRunAI
    from video_fixtures import synthetic_video

    video = synthetic_video(b"phone-vote-disagreement")
    digest = sha256_hex(video)
    mounted = parse_overview(
        json.dumps(payload(("phone_on_mount", 0.0, 0.3), ("looking_at_phone", 0.1, 0.2))),
        digest,
        prompt_version="overview-4",
        view_role="interior",
    )
    held = parse_overview(
        json.dumps(payload(("phone_in_hand", 0.0, 0.2))), digest, prompt_version="overview-4", view_role="interior"
    )
    ai = FakeMultiRunAI([AIResponse("synthetic-fixture", "synthetic-fixture", f) for f in [mounted, held, held]])
    report = analyse_incident(
        [make_view(video, Role.INTERIOR)],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        prompt_version="overview-4",
        agreement_settings=AgreementSettings(n_runs=3),
        evidence=LocalEvidenceStore(tmp_path / "evidence"),
    )
    assert len(report.violations) == 1
    assert report.violations[0].agreement_status == "flagged"
    assert [a.phone_use for a in report.driver_assessments] == ["GPS", "Entertainment", "Entertainment"]
    assert all(f"Overview run {i + 1} of 3" in a.reason for i, a in enumerate(report.driver_assessments))
    assert all("classification agreement not measured" in a.reason for a in report.driver_assessments)


def test_overview4_replay_and_bounded_repair_keep_driver_evidence(tmp_path: Any) -> None:
    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.ai.overview_replay import ReplayOverviewAIConnection
    from incident_report.domain import Role
    from incident_report.hashing import incident_input_hash, sha256_hex
    from test_live_gemini import PUBLIC_VIDEO, FakeTransport

    video = tmp_path / "synthetic.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    settings = GeminiSettings(model="gemini-3.8-flash")
    request = AIRequest(
        incident_input_hash([(Role.INTERIOR, digest)]), "overview-4", (AIViewRef(Role.INTERIOR, digest),)
    )
    data = payload(("phone_in_hand", 1.0, 2.0))
    transport = FakeTransport(
        [json.dumps(payload()).replace('"driver_observations": []', '"unexpected": []'), json.dumps(data)]
    )
    response = GeminiAIConnection(settings, PublicResearchVideo(video, True), transport=transport).analyse(request)
    assert response.facts and response.facts.driver_observations
    assert [c.prompt_version for c in response.calls] == ["overview-4", "overview-4-repair"]
    replay = ReplayOverviewAIConnection(settings, response.calls).analyse(request)
    assert replay.facts == response.facts


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_phone_thresholds_refuse_nonpositive_or_nonfinite_values(value: str) -> None:
    from incident_report.config import ConfigError, PhoneUseSettings

    with pytest.raises(ConfigError):
        PhoneUseSettings.from_env({"INCIDENT_PHONE_CONTINUOUS_LOOK_SECONDS": value})


def test_phone_thresholds_load_from_environment() -> None:
    from incident_report.config import PhoneUseSettings

    settings = PhoneUseSettings.from_env(
        {
            "INCIDENT_PHONE_TAPPING_COUNT": "4",
            "INCIDENT_PHONE_TAPPING_WINDOW_SECONDS": "6",
            "INCIDENT_PHONE_CONTINUOUS_LOOK_SECONDS": "2",
        }
    )
    assert (settings.tapping_count, settings.tapping_window_seconds, settings.continuous_look_seconds) == (4, 6.0, 2.0)
