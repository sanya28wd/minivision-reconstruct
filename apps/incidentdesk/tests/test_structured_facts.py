import json
from pathlib import Path
from typing import Any

import pytest

from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import parse_overview
from incident_report.rules.adapter import situation_from_overview


def payload(value: str = "red") -> dict[str, Any]:
    return {
        "parties": [{"description": "white SUV", "timestamp_seconds": 0.0, "confidence": 0.9}],
        "events": [],
        "candidate_collisions": [],
        "behaviour_observations": [],
        "location_conditions": [],
        "structured_facts": [
            {
                "subject": "white SUV",
                "field": "signal_state_when_crossing_stop_line",
                "value": value,
                "other_party": "",
                "timestamp_seconds": 1.0,
                "confidence": 0.9,
            }
        ],
    }


def test_structured_signal_roundtrip_and_adapter() -> None:
    facts = parse_overview(json.dumps(payload()), "view-a", prompt_version="overview-2")
    assert facts.structured_facts[0].view_id == "view-a"
    assert type(facts).model_validate_json(facts.model_dump_json()) == facts
    signal = situation_from_overview(facts).parties[0].signal_state_when_crossing_stop_line
    assert signal.value == "red"
    assert signal.evidence[0].timestamp_seconds == 1.0


@pytest.mark.parametrize("value", ["purple", "main road"])
def test_invalid_structured_value_rejected(value: str) -> None:
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(payload(value)), "view-a", prompt_version="overview-2")


# Independent expected vocabulary from research section 5 (IDs below).
FIELD_EXAMPLES = {
    "conflict_zone_type": ("scene", "roundabout_unsignalised"),  # S2
    "officer_directing_traffic": ("scene", "no"),  # S3
    "posted_speed_limit_kmh": ("scene", "80"),  # S4
    "visibility_conditions": ("scene", "rain_or_fog_or_dust"),  # S6
    "party_type": ("white SUV", "car"),  # P1
    "motion_state_before_contact": ("white SUV", "stopped_in_lane"),  # P2
    "movement": ("white SUV", "left_turn"),  # P3
    "approach_control_faced": ("white SUV", "give_way_sign_or_line"),  # P4
    "signal_state_when_crossing_stop_line": ("white SUV", "green_arrow"),  # P5
    "relative_position_of_other": ("white SUV", "B_from_A_left"),  # P6
    "roundabout_entry_state": ("white SUV", "entering_from_approach"),  # P7
    "lane_line_crossed_before_contact": ("white SUV", "solid_white"),  # P9
    "signal_given": ("white SUV", "left_indicator"),  # P10
    "entry_origin": ("white SUV", "car_park_or_driveway"),  # P14
    "emergency_lights": ("white SUV", "yes"),  # P19, split visible/audible observations
    "emergency_siren": ("white SUV", "unknown"),  # P19
    "impact_zone_on_party": ("white SUV", "rear_left"),  # P20
    "pedestrian_crossing_place": ("white SUV", "marked_crossing"),  # P21
    "pedestrian_signal_state": ("white SUV", "flashing_green"),  # P22
    "pedestrian_started_before_vehicle_arrived": ("white SUV", "yes"),  # P23
}


def field_payload(field: str, value: str) -> dict[str, Any]:
    data = payload(value)
    data["parties"].append({"description": "blue object", "timestamp_seconds": 0.0, "confidence": 0.9})
    data["structured_facts"][0].update(
        field=field,
        subject=FIELD_EXAMPLES[field][0],
        other_party="blue object" if field == "relative_position_of_other" else "",
    )
    return data


@pytest.mark.parametrize("field", FIELD_EXAMPLES)
def test_every_field_maps_with_citation_and_allows_unknown(field: str) -> None:
    for value in (FIELD_EXAMPLES[field][1], "unknown"):
        facts = parse_overview(json.dumps(field_payload(field, value)), "view-a", prompt_version="overview-2")
        situation = situation_from_overview(facts)
        owner = situation if FIELD_EXAMPLES[field][0] == "scene" else situation.parties[0]
        observation = getattr(owner, field)
        if field == "relative_position_of_other":
            observation = observation["blue object"]
        assert observation.value == (80 if field == "posted_speed_limit_kmh" and value == "80" else value)
        assert [(e.view_id, e.timestamp_seconds) for e in observation.evidence] == [("view-a", 1.0)]
        assert situation.parties[0].road_class.value == "unknown"


@pytest.mark.parametrize("field", FIELD_EXAMPLES)
def test_every_field_rejects_values_outside_its_vocabulary(field: str) -> None:
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(field_payload(field, "invented")), "view-a", prompt_version="overview-2")


@pytest.mark.parametrize("field", ["behaviour_observations", "location_conditions", "structured_facts"])
def test_overview_2_requires_all_arrays_but_legacy_recordings_load(field: str) -> None:
    data = payload()
    del data[field]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "view-a", prompt_version="overview-2")
    legacy = {"parties": data["parties"], "events": [], "candidate_collisions": []}
    assert parse_overview(json.dumps(legacy), "view-a").structured_facts == []


@pytest.mark.parametrize(
    "change",
    [
        {"field": "road_class"},
        {"subject": "red object"},
        {"other_party": "red object"},
        {"field": "relative_position_of_other", "other_party": ""},
        {"subject": "scene"},
        {"view_id": "wrong-view"},
        {"timestamp_seconds": -1.0},
    ],
)
def test_invalid_field_subject_pair_or_citation_rejected(change: dict[str, Any]) -> None:
    data = payload()
    data["structured_facts"][0].update(change)
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "view-a", prompt_version="overview-2")


def test_interior_view_cannot_supply_road_facts() -> None:
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(payload()), "view-a", prompt_version="overview-2", view_role="interior")


def test_party_name_uses_only_visible_words() -> None:
    data = payload()
    data["parties"][0]["description"] = "red Cannot Determine Cannot Determine"
    data["structured_facts"] = []
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "view-a", prompt_version="overview-2")
    assert parse_overview(json.dumps(data), "view-a", prompt_version="overview-1")


@pytest.mark.parametrize("other_value,expected", [("green", "unknown"), ("red", "red"), ("unknown", "red")])
def test_view_reconciliation_keeps_all_citations(other_value: str, expected: str) -> None:
    from incident_report.facts import OverviewFacts

    first = parse_overview(json.dumps(payload()), "view-a", prompt_version="overview-2")
    second = parse_overview(json.dumps(payload(other_value)), "view-b", prompt_version="overview-2")
    combined = OverviewFacts.model_validate(
        {
            **first.model_dump(),
            "structured_facts": [
                *(f.model_dump() for f in first.structured_facts),
                *(f.model_dump() for f in second.structured_facts),
            ],
        }
    )
    situation = situation_from_overview(combined)
    observed = situation.parties[0].signal_state_when_crossing_stop_line
    assert observed.value == expected
    assert [e.view_id for e in observed.evidence] == ["view-a", "view-b"]
    assert bool(situation.contradictions) == (other_value == "green")


def test_prompt_only_requests_observations_and_schema_is_flat() -> None:
    from incident_report.ai.gemini import PROMPT
    from incident_report.facts import OVERVIEW_SCHEMA

    assert "Never state fault, priority, road rank, legal road class, main road, equal in rank" in PROMPT
    assert "unknown is always allowed" in PROMPT
    assert "Interior views must return an empty structured_facts array" in PROMPT
    properties = OVERVIEW_SCHEMA["properties"]["structured_facts"]["items"]["properties"]
    assert set(properties) == {"subject", "field", "value", "other_party", "timestamp_seconds", "confidence"}
    assert set(properties["field"]["enum"]) == set(FIELD_EXAMPLES)
    assert all(prop["type"] in {"STRING", "NUMBER"} for prop in properties.values())
    assert "maxItems" not in json.dumps(OVERVIEW_SCHEMA)


def test_enum_values_trace_to_research_rows() -> None:
    from incident_report.facts import FIELD_VALUES

    research = (Path(__file__).parents[1] / "docs/research/UAE_ROAD_STRUCTURE_AND_PRIORITY.md").read_text(
        encoding="utf-8"
    )
    for field, values in FIELD_VALUES.items():
        if field in {"emergency_lights", "emergency_siren"}:
            assert values == ("yes", "no", "unknown")  # P19 split; not a new legal category
            continue
        row = next(line for line in research.splitlines() if f"`{field}`" in line and line.startswith("|"))
        allowed = row.split("|")[3]
        assert tuple(part.strip() for part in allowed.split("/")) == values


@pytest.mark.parametrize(
    "change",
    [
        {"field": "priority"},
        {"value": "purple"},
        {"subject": "unseen car"},
        {"field": "relative_position_of_other", "other_party": ""},
    ],
)
def test_invalid_structured_output_uses_bounded_repair(tmp_path: Path, change: dict[str, Any]) -> None:
    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.domain import Role
    from incident_report.hashing import incident_input_hash, sha256_hex
    from test_live_gemini import PUBLIC_VIDEO, FakeTransport

    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    request = AIRequest(
        incident_input_hash([(Role.EXTERIOR, digest)]), "overview-2", (AIViewRef(Role.EXTERIOR, digest),)
    )
    bad = payload()
    bad["structured_facts"][0].update(change)
    transport = FakeTransport([json.dumps(bad), json.dumps(payload())])
    connection = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(video, True), transport=transport
    )
    response = connection.analyse(request)
    assert response.facts is not None
    assert response.facts.structured_facts[0].value == "red"
    assert [call.prompt_version for call in response.calls] == ["overview-2", "overview-2-repair"]
    assert "previous response had citation errors" in transport.calls[1][0]


def test_structured_citations_are_checked_by_l0() -> None:
    from incident_report.ai.interface import AIViewRef
    from incident_report.domain import Role
    from incident_report.facts import validate_citations

    facts = parse_overview(json.dumps(payload()), "view-a", prompt_version="overview-2")
    assert not validate_citations(facts, ["view-b"]).is_valid
    assert not validate_citations(facts, ["view-a"], view_durations={"view-a": 0.5}).is_valid
    assert not validate_citations(facts, [AIViewRef(Role.INTERIOR, "view-a")]).is_valid


def test_replay_fixture_retains_structured_facts(run_log: Any) -> None:
    from replay_harness import discover_replay_cases, run_replay_case

    case = next(c for c in discover_replay_cases() if c.case_id == "case_06_structured_overview_2")
    report = run_replay_case(case, run_log)
    assert report.provenance == "synthetic-fixture"
    assert report.facts is not None
    assert report.facts.structured_facts[0].value == "straight"
    assert report.facts.structured_facts[0].view_id == case.views[0].sha256


@pytest.mark.parametrize("field", FIELD_EXAMPLES)
def test_all_research_enum_values_parse(field: str) -> None:
    from incident_report.facts import FIELD_VALUES

    values = FIELD_VALUES.get(field, ("0", "80", "120", "not_visible", "unknown"))
    for value in values:
        facts = parse_overview(json.dumps(field_payload(field, value)), "view-a", prompt_version="overview-2")
        assert facts.structured_facts[0].value == value


def test_overview_2_sdk_receives_flat_required_schema(tmp_path: Path) -> None:
    from incident_report.ai.gemini import PROMPT, GeminiSettings, PublicResearchVideo
    from incident_report.ai.google_video import GoogleVideoTransport
    from incident_report.facts import OVERVIEW_SCHEMA
    from test_google_video_transport import FakeClient

    video = tmp_path / "public.mp4"
    video.write_bytes(b"synthetic transport fixture")
    client = FakeClient()
    GoogleVideoTransport(client).generate(
        PublicResearchVideo(video, True), PROMPT, GeminiSettings(model="gemini-3.8-flash")
    )
    assert client.generated["config"]["response_schema"] == OVERVIEW_SCHEMA
    assert set(OVERVIEW_SCHEMA["required"]) == {
        "parties",
        "events",
        "candidate_collisions",
        "behaviour_observations",
        "location_conditions",
        "structured_facts",
    }


def test_dense_consolidation_keeps_structured_observations() -> None:
    from incident_report.ai.dense import consolidate_facts

    first = parse_overview(json.dumps(payload()), "view-a", prompt_version="overview-2")
    dense = parse_overview(json.dumps(payload()), "view-a", prompt_version="overview-2")
    consolidated = consolidate_facts(first, [dense])
    assert consolidated.structured_facts == first.structured_facts + dense.structured_facts


@pytest.mark.parametrize("second_value, expected", [("green", "unknown"), ("red", "red")])
def test_multiview_report_carries_facts_into_reconciliation(
    run_log: Any, tmp_path: Path, second_value: str, expected: str
) -> None:
    # synthetic-fixture
    from conftest import make_view
    from incident_report.domain import Origin, Role
    from incident_report.entry import analyse_incident
    from incident_report.hashing import sha256_hex
    from incident_report.storage import LocalEvidenceStore
    from test_new_incident_screen import SpyAIConnection
    from video_fixtures import synthetic_video

    videos = [synthetic_video(b"structured-front"), synthetic_video(b"structured-rear")]
    hashes = [sha256_hex(v) for v in videos]
    observations = [
        parse_overview(json.dumps(payload(value)), digest, prompt_version="overview-2")
        for value, digest in zip(("red", second_value), hashes, strict=True)
    ]
    connection = SpyAIConnection(per_view_facts=dict(zip(hashes, observations, strict=True)))
    report = analyse_incident(
        [make_view(v, Role.EXTERIOR) for v in videos],
        Origin.APP,
        ai=connection,
        run_log=run_log,
        prompt_version="overview-2",
        evidence=LocalEvidenceStore(tmp_path / "evidence"),
    )
    assert report.is_valid
    assert report.facts is not None
    assert len(report.facts.structured_facts) == 2
    situation = situation_from_overview(report.facts)
    assert len(situation.parties) == 1
    signal = situation.parties[0].signal_state_when_crossing_stop_line
    assert signal.value == expected
    assert [e.view_id for e in signal.evidence] == hashes
    assert bool(situation.contradictions) == (second_value == "green")
    assert report.contradictions == situation.contradictions
    assert type(report).model_validate_json(report.model_dump_json()).contradictions == report.contradictions


@pytest.mark.parametrize("name", ["red Cannot Determine object", "CANNOT DETERMINE SUV", "white cAnNoT dEtErMiNe"])
def test_cannot_determine_party_name_is_rejected_and_repaired(tmp_path: Path, name: str) -> None:
    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.domain import Role
    from incident_report.hashing import incident_input_hash, sha256_hex
    from test_live_gemini import PUBLIC_VIDEO, FakeTransport

    bad = payload()
    bad["parties"][0]["description"] = name
    bad["structured_facts"][0]["subject"] = name
    # Match subject to isolate the party-name error from subject validation.
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(bad), "view-a", prompt_version="overview-2")
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    request = AIRequest(
        incident_input_hash([(Role.EXTERIOR, digest)]), "overview-2", (AIViewRef(Role.EXTERIOR, digest),)
    )
    transport = FakeTransport([json.dumps(bad), json.dumps(payload())])
    connection = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(video, True), transport=transport
    )
    response = connection.analyse(request)
    assert response.facts is not None
    assert response.facts.parties[0].description == "white SUV"
    assert [c.prompt_version for c in response.calls] == ["overview-2", "overview-2-repair"]
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "field,seen,abstention",
    [
        ("signal_state_when_crossing_stop_line", "red", "not_visible"),
        ("signal_state_when_crossing_stop_line", "red", "unknown"),
        ("approach_control_faced", "stop_sign_or_line", "not_in_view"),
    ],
)
def test_unseen_view_abstains_and_keeps_both_citations(field: str, seen: str, abstention: str) -> None:
    from incident_report.facts import OverviewFacts

    first = parse_overview(json.dumps(field_payload(field, seen)), "view-a", prompt_version="overview-2")
    second = parse_overview(json.dumps(field_payload(field, abstention)), "view-b", prompt_version="overview-2")
    combined = OverviewFacts.model_validate(
        {
            **first.model_dump(),
            "structured_facts": [
                *(f.model_dump() for f in first.structured_facts),
                *(f.model_dump() for f in second.structured_facts),
            ],
        }
    )
    situation = situation_from_overview(combined)
    observed = getattr(situation.parties[0], field)
    assert observed.value == seen
    assert [e.view_id for e in observed.evidence] == ["view-a", "view-b"]
    assert situation.contradictions == ()
    # Without a seen value these markers supply no positive reading.
    unseen = situation_from_overview(second)
    assert getattr(unseen.parties[0], field).value == "unknown"
    assert getattr(unseen.parties[0], field).evidence[0].view_id == "view-b"
    assert unseen.contradictions == ()


@pytest.mark.parametrize(
    "field,first_value,second_value",
    [
        ("signal_state_when_crossing_stop_line", "red", "green"),
        ("signal_given", "none_visible", "left_indicator"),
        ("approach_control_faced", "none_confirmed", "stop_sign_or_line"),
    ],
)
def test_different_seen_readings_still_contradict(field: str, first_value: str, second_value: str) -> None:
    # synthetic-fixture
    from incident_report.facts import OverviewFacts

    first = parse_overview(json.dumps(field_payload(field, first_value)), "view-a", prompt_version="overview-2")
    second = parse_overview(json.dumps(field_payload(field, second_value)), "view-b", prompt_version="overview-2")
    combined = OverviewFacts.model_validate(
        {
            **first.model_dump(),
            "structured_facts": [
                *(f.model_dump() for f in first.structured_facts),
                *(f.model_dump() for f in second.structured_facts),
            ],
        }
    )
    situation = situation_from_overview(combined)
    observed = getattr(situation.parties[0], field)
    assert observed.value == "unknown"
    assert [e.view_id for e in observed.evidence] == ["view-a", "view-b"]
    assert len(situation.contradictions) == 1
    contradiction = situation.contradictions[0]
    assert (contradiction.subject, contradiction.field) == ("white SUV", field)
    assert [(r.view_id, r.value) for r in contradiction.readings] == [("view-a", first_value), ("view-b", second_value)]
    # none_visible and none_confirmed are positive readings on their own.
    assert getattr(situation_from_overview(first).parties[0], field).value == first_value
