"""Synthetic cross-view workflow tests; no fusion accuracy claim."""

import json

import pytest

from incident_report.ai.gemini import parse_overview
from incident_report.facts import OverviewFacts
from incident_report.rules.adapter import situation_from_overview
from test_structured_facts import payload


def test_contradiction_carries_both_readings_and_independent_clocks() -> None:
    # synthetic-fixture
    first = parse_overview(json.dumps(payload("red")), "front", prompt_version="overview-2")
    other = payload("green")
    other["structured_facts"][0]["timestamp_seconds"] = 41.0
    second = parse_overview(json.dumps(other), "rear", prompt_version="overview-2")
    combined = OverviewFacts(
        parties=first.parties + second.parties,
        structured_facts=first.structured_facts + second.structured_facts,
        events=[],
        candidate_collisions=[],
        behaviour_observations=[],
        location_conditions=[],
    )
    scene = situation_from_overview(combined)
    assert scene.parties[0].signal_state_when_crossing_stop_line.value == "unknown"
    contradiction = scene.contradictions[0]
    assert contradiction.subject == "white SUV"
    assert contradiction.field == "signal_state_when_crossing_stop_line"
    assert [(r.value, r.view_id, r.timestamp_seconds) for r in contradiction.readings] == [
        ("red", "front", 1.0),
        ("green", "rear", 41.0),
    ]


@pytest.mark.parametrize("second_value", ["green", "red", "unknown", "not_visible"])
def test_canonical_party_matching_and_no_view_vote(second_value: str) -> None:
    # synthetic-fixture: aliases match solely through existing typed party keys.
    first = parse_overview(json.dumps(payload("red")), "front", prompt_version="overview-2")
    second = parse_overview(json.dumps(payload(second_value)), "rear", prompt_version="overview-2")
    first_party = first.parties[0].model_copy(update={"color": "white", "make_model": "Toyota", "body_type": "SUV"})
    second_party = second.parties[0].model_copy(
        update={"description": "white Toyota SUV", "color": "white", "make_model": "Toyota", "body_type": "SUV"}
    )
    second_reading = second.structured_facts[0].model_copy(
        update={"subject": second_party.description, "confidence": 1.0, "timestamp_seconds": 51.0}
    )
    combined = first.model_copy(
        update={
            "parties": [first_party, second_party],
            "structured_facts": [*first.structured_facts, *first.structured_facts, second_reading],
        }
    )
    scene = situation_from_overview(combined)
    assert len(scene.parties) == 1
    assert scene.parties[0].signal_state_when_crossing_stop_line.value == (
        "unknown" if second_value == "green" else "red"
    )
    assert bool(scene.contradictions) == (second_value == "green")


def test_report_lists_escaped_contradictions_and_rules_abstain() -> None:
    # synthetic-fixture: the two cameras disagree at unrelated clock times.
    from incident_report.rules import FaultStatus
    from incident_report.web.templating import build_environment
    from test_no_incident_factors import A, facts, report_for

    first = facts(collision=True)
    readings = [
        r for r in first.structured_facts if r.subject == A and r.field == "signal_state_when_crossing_stop_line"
    ]
    second = readings[0].model_copy(update={"value": "green", "view_id": "rear<script>", "timestamp_seconds": 51.0})
    combined = first.model_copy(update={"structured_facts": [*first.structured_facts, second]})
    report = report_for(combined)
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in report.parties)
    assert report.violations == ()
    assert len(report.contradictions) == 1
    html = build_environment().get_template("report.html").render(report=report)
    assert '<section aria-label="Contradictions">' in html
    assert "Cannot Determine" in html
    assert "red" in html and "green" in html
    assert "road @ 1.0s" in html and "rear&lt;script&gt; @ 51.0s" in html
    assert "rear<script>" not in html


def test_distinct_canonical_parties_do_not_contradict_even_with_same_description() -> None:
    # synthetic-fixture
    first = parse_overview(json.dumps(payload("red")), "front", prompt_version="overview-2")
    second = parse_overview(json.dumps(payload("green")), "rear", prompt_version="overview-2")
    first_party = first.parties[0].model_copy(update={"color": "white", "make_model": "Toyota", "body_type": "SUV"})
    second_party = second.parties[0].model_copy(update={"color": "white", "make_model": "Nissan", "body_type": "SUV"})
    combined = first.model_copy(
        update={
            "parties": [first_party, second_party],
            "structured_facts": first.structured_facts + second.structured_facts,
        }
    )
    scene = situation_from_overview(combined)
    assert len(scene.parties) == 2
    assert [p.signal_state_when_crossing_stop_line.value for p in scene.parties] == ["red", "green"]
    assert scene.contradictions == ()


@pytest.mark.parametrize("second_value", ["signalised_junction", "roundabout_unsignalised"])
def test_scene_readings_ignore_camera_clock_offsets(second_value: str) -> None:
    # synthetic-fixture
    from test_no_incident_factors import facts

    first = facts()
    original = next(r for r in first.structured_facts if r.field == "conflict_zone_type")
    second = original.model_copy(update={"value": second_value, "view_id": "rear", "timestamp_seconds": 51.0})
    scene = situation_from_overview(first.model_copy(update={"structured_facts": [original, second]}))
    assert scene.conflict_zone_type.value == ("unknown" if second_value == "roundabout_unsignalised" else second_value)
    assert bool(scene.contradictions) == (second_value == "roundabout_unsignalised")


def test_pair_readings_match_both_canonical_parties() -> None:
    # synthetic-fixture
    from test_no_incident_factors import A, B, facts

    first = facts()
    second_parties = [
        p.model_copy(update={"view_id": "rear", "description": p.description.upper()}) for p in first.parties
    ]
    original = next(r for r in first.structured_facts if r.subject == A and r.field == "relative_position_of_other")
    second = original.model_copy(
        update={"subject": A.upper(), "other_party": B.upper(), "view_id": "rear", "value": "B_from_A_right"}
    )
    scene = situation_from_overview(
        first.model_copy(update={"parties": first.parties + second_parties, "structured_facts": [original, second]})
    )
    assert len(scene.parties) == 2
    assert scene.parties[0].relative_position_of_other[B].value == "unknown"
    assert len(scene.contradictions) == 1
    assert scene.contradictions[0].other_party == B


def test_single_camera_changes_are_unknown_but_not_cross_view_contradictions() -> None:
    # synthetic-fixture: a traffic light may change during one clip.
    first = parse_overview(json.dumps(payload("red")), "front", prompt_version="overview-2")
    later = first.structured_facts[0].model_copy(update={"value": "green", "timestamp_seconds": 10.0})
    scene = situation_from_overview(first.model_copy(update={"structured_facts": [*first.structured_facts, later]}))
    assert scene.parties[0].signal_state_when_crossing_stop_line.value == "unknown"
    assert scene.contradictions == ()


def test_distinct_same_camera_parties_with_equal_keys_remain_separate() -> None:
    # synthetic-fixture: the mandated key keeps the make, not the model.
    first = parse_overview(json.dumps(payload("red")), "front", prompt_version="overview-2")
    names = ["white Toyota Corolla sedan", "white Toyota Camry sedan"]
    parties = [first.parties[0].model_copy(update={"description": name}) for name in names]
    readings = [
        first.structured_facts[0].model_copy(update={"subject": name, "value": value})
        for name, value in zip(names, ["red", "green"], strict=True)
    ]
    scene = situation_from_overview(first.model_copy(update={"parties": parties, "structured_facts": readings}))
    assert [p.description for p in scene.parties] == names
    assert [p.signal_state_when_crossing_stop_line.value for p in scene.parties] == ["red", "green"]
    assert scene.contradictions == ()
