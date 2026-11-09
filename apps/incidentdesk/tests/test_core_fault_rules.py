"""RPT-18 behavior at Seam 3: typed evidence, never description parsing."""

from typing import Any

import pytest

from incident_report.facts import CitedFact
from incident_report.rules import FaultStatus, Observed, PartySituation, Situation, evaluate_situation

A = "white SUV"
B = "red sedan"
CONTACT = CitedFact(description="contact", view_id="exterior", timestamp_seconds=3.0, confidence=0.9)


def observed(value: Any, field: str) -> Any:
    return Observed(
        value=value, evidence=(CitedFact(description=field, view_id="exterior", timestamp_seconds=2.0, confidence=0.9),)
    )


def signal_situation(first: str, second: str) -> Situation:
    parties = []
    for name, other, signal, relation in ((A, B, first, "B_from_A_left"), (B, A, second, "B_from_A_right")):
        parties.append(
            PartySituation(
                description=name,
                party_type=observed("car", "party_type"),
                movement=observed("straight", "movement"),
                approach_control_faced=observed("signal", "approach_control_faced"),
                signal_state_when_crossing_stop_line=observed(signal, "signal_state_when_crossing_stop_line"),
                relative_position_of_other={other: observed(relation, "relative_position_of_other")},
            )
        )
    return Situation(
        parties=tuple(parties),
        collisions=(CONTACT,),
        conflict_zone_type=observed("signalised_junction", "conflict_zone_type"),
        officer_directing_traffic=observed("no", "officer_directing_traffic"),
    )


@pytest.mark.parametrize(
    "first,second,expected",
    [
        ("red", "green", (FaultStatus.AT_FAULT, FaultStatus.NOT_AT_FAULT)),
        ("green", "red", (FaultStatus.NOT_AT_FAULT, FaultStatus.AT_FAULT)),
    ],
)
def test_signal_crossing_on_red_is_cited(first: str, second: str, expected: tuple[FaultStatus, ...]) -> None:
    faults = evaluate_situation(signal_situation(first, second))
    assert tuple(f.status for f in faults) == expected
    for fault in faults:
        assert fault.article == "MR 130/1997 Art. 25(1)"
        assert "unofficial translation" in fault.reason
        assert CONTACT in fault.cited_facts
        assert any(e.description == "signal_state_when_crossing_stop_line" for e in fault.cited_facts)


@pytest.mark.parametrize("missing", ["unknown", "not_visible"])
def test_signal_unknown_never_blames_a_party(missing: str) -> None:
    faults = evaluate_situation(signal_situation("red", missing))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all("signal_state_when_crossing_stop_line" in f.reason for f in faults)


def test_two_red_crossings_keep_both_breaches_as_findings() -> None:
    faults = evaluate_situation(signal_situation("red", "red"))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    for fault in faults:
        assert "both" in fault.reason.lower()
        assert fault.findings
        assert fault.findings[0].party_description == fault.party_description
        assert fault.findings[0].cited_facts


@pytest.mark.parametrize(
    "name",
    [
        "lateral_move_into_other_party_path",
        "lateral_move_start_t",
        "contact_t",
        "leading_vehicle_action",
        "visible_vehicle_markings",
    ],
)
def test_p11_p12_stay_unknown_in_overview_2_adapter(name: str) -> None:
    from incident_report.facts import OverviewFacts, PartyFact
    from incident_report.rules import situation_from_overview

    facts = OverviewFacts(
        parties=[PartyFact(description=A, view_id="exterior", timestamp_seconds=0.0, confidence=0.9)],
        events=[],
        candidate_collisions=[CONTACT],
        behaviour_observations=[],
        location_conditions=[],
        structured_facts=[],
    )
    party = situation_from_overview(facts).parties[0]
    field = getattr(party, name)
    assert not field if name == "leading_vehicle_action" else not field.is_known


def test_p11_timestamps_do_not_encode_a_sudden_threshold() -> None:
    party = PartySituation(
        description=A,
        lateral_move_into_other_party_path=observed("yes", "P11"),
        lateral_move_start_t=observed(1.0, "move start"),
        contact_t=observed(1.001, "contact"),
    )
    assert party.contact_t.value == 1.001
    assert "sudden" not in PartySituation.model_fields


def changed_party(situation: Situation, index: int, **changes: Any) -> Situation:
    parties = list(situation.parties)
    parties[index] = PartySituation.model_validate({**parties[index].model_dump(), **changes})
    return situation.model_copy(update={"parties": tuple(parties)})


def junction_situation() -> Situation:
    scene = signal_situation("not_applicable", "not_applicable")
    for i in range(2):
        scene = changed_party(
            scene,
            i,
            approach_control_faced=observed("none_confirmed", "approach_control_faced"),
            entry_origin=observed("public_road_same_road", "entry_origin"),
            roundabout_entry_state=observed("not_in_roundabout", "roundabout_entry_state"),
        )
    return scene.model_copy(update={"conflict_zone_type": observed("unsignalised_junction", "conflict_zone_type")})


@pytest.mark.parametrize(
    "scenario,at_fault,article",
    [
        ("uncontrolled", 0, "Art. 6"),
        ("stop", 0, "MR 130/1997 Art. 1(3), 27"),
        ("give_way", 1, "MR 130/1997 Art. 1(3), 27"),
        ("entry", 0, "MR 130/1997 Art. 43/60"),
        ("roundabout", 1, "Art. 6"),
    ],
)
def test_typed_junction_families(scenario: str, at_fault: int, article: str) -> None:
    scene = junction_situation()
    if scenario in {"stop", "give_way"}:
        scene = changed_party(
            scene,
            at_fault,
            approach_control_faced=observed(
                "stop_sign_or_line" if scenario == "stop" else "give_way_sign_or_line", "approach_control_faced"
            ),
        )
    elif scenario == "entry":
        scene = changed_party(
            scene,
            0,
            movement=observed("entering_from_side_road", "movement"),
            entry_origin=observed("side_road", "entry_origin"),
        )
    elif scenario == "roundabout":
        scene = scene.model_copy(
            update={"conflict_zone_type": observed("roundabout_unsignalised", "conflict_zone_type")}
        )
        scene = changed_party(
            scene,
            0,
            movement=observed("circulating_roundabout", "movement"),
            roundabout_entry_state=observed("already_inside_circulating", "roundabout_entry_state"),
        )
        scene = changed_party(
            scene,
            1,
            movement=observed("entering_roundabout", "movement"),
            roundabout_entry_state=observed("entering_from_approach", "roundabout_entry_state"),
        )
    if scenario == "roundabout":
        scene = changed_party(
            scene, 0, relative_position_of_other={B: observed("B_from_A_right", "relative_position_of_other")}
        )
        scene = changed_party(
            scene, 1, relative_position_of_other={A: observed("B_from_A_left", "relative_position_of_other")}
        )
    faults = evaluate_situation(scene)
    assert faults[at_fault].status == FaultStatus.AT_FAULT
    assert faults[1 - at_fault].status == FaultStatus.NOT_AT_FAULT
    assert all(f.article == article for f in faults)
    assert all(CONTACT in f.cited_facts for f in faults)


@pytest.mark.parametrize("control", ["unknown", "not_in_view", "officer"])
def test_art_6_needs_both_approaches_confirmed(control: str) -> None:
    scene = changed_party(junction_situation(), 0, approach_control_faced=observed(control, "approach_control_faced"))
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all("approach_control_faced" in f.reason for f in faults)


def lane_situation() -> Situation:
    scene = junction_situation().model_copy(update={"conflict_zone_type": observed("mid_block", "conflict_zone_type")})
    scene = changed_party(
        scene,
        0,
        movement=observed("lane_change_right", "movement"),
        lateral_move_into_other_party_path=observed("yes", "lateral_move_into_other_party_path"),
        lateral_move_start_t=observed(1.0, "lateral_move_start_t"),
        contact_t=observed(3.0, "contact_t"),
        lane_line_crossed_before_contact=observed("broken_white", "lane_line_crossed_before_contact"),
        signal_given=observed("right_indicator", "signal_given"),
        relative_position_of_other={B: observed("B_alongside_adjacent_lane", "relative_position_of_other")},
    )
    return changed_party(
        scene, 1, relative_position_of_other={A: observed("B_alongside_adjacent_lane", "relative_position_of_other")}
    )


def rear_situation(action: str = "none_visible") -> Situation:
    scene = junction_situation().model_copy(update={"conflict_zone_type": observed("mid_block", "conflict_zone_type")})
    scene = changed_party(
        scene,
        0,
        motion_state_before_contact=observed("moving", "motion_state_before_contact"),
        lateral_move_into_other_party_path=observed("no", "lateral_move_into_other_party_path"),
        impact_zone_on_party=observed("front", "impact_zone_on_party"),
        relative_position_of_other={B: observed("B_ahead_same_direction", "relative_position_of_other")},
    )
    return changed_party(
        scene,
        1,
        motion_state_before_contact=observed("moving", "motion_state_before_contact"),
        lateral_move_into_other_party_path=observed("no", "lateral_move_into_other_party_path"),
        impact_zone_on_party=observed("rear", "impact_zone_on_party"),
        leading_vehicle_action={A: observed(action, "leading_vehicle_action")},
        relative_position_of_other={A: observed("B_behind", "relative_position_of_other")},
    )


@pytest.mark.parametrize(
    "scene,article", [(lane_situation, "MR 130/1997 Art. 58-59"), (rear_situation, "MR 130/1997 Art. 50")]
)
def test_lane_change_and_rear_end_need_causal_observations(scene: Any, article: str) -> None:
    faults = evaluate_situation(scene())
    assert tuple(f.status for f in faults) == (FaultStatus.AT_FAULT, FaultStatus.NOT_AT_FAULT)
    assert all(f.article == article for f in faults)
    assert all("unofficial translation" in f.reason for f in faults)


@pytest.mark.parametrize(
    "field",
    [
        "lateral_move_into_other_party_path",
        "lateral_move_start_t",
        "contact_t",
        "lane_line_crossed_before_contact",
        "signal_given",
    ],
)
def test_lane_change_each_missing_field_is_named(field: str) -> None:
    value = None if field in {"lateral_move_start_t", "contact_t"} else "unknown"
    faults = evaluate_situation(changed_party(lane_situation(), 0, **{field: observed(value, field)}))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all(field in f.reason for f in faults)


@pytest.mark.parametrize("action", ["unknown", "reversed", "changed_lane_into_path_within_window", "braked_visible"])
def test_rear_end_leader_exceptions_never_automatically_blame_follower(action: str) -> None:
    faults = evaluate_situation(rear_situation(action))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all("leading_vehicle_action" in f.reason for f in faults)


@pytest.mark.parametrize(
    "field", ["motion_state_before_contact", "lateral_move_into_other_party_path", "impact_zone_on_party"]
)
@pytest.mark.parametrize("index", [0, 1])
def test_rear_end_each_party_unknown_is_named(field: str, index: int) -> None:
    faults = evaluate_situation(changed_party(rear_situation(), index, **{field: observed("unknown", field)}))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all(field in f.reason for f in faults)


def pedestrian_situation(
    limit: int | str = 30, place: str = "marked_crossing", signal: str = "no_pedestrian_signal"
) -> Situation:
    scene = junction_situation().model_copy(
        update={
            "conflict_zone_type": observed("pedestrian_crossing", "conflict_zone_type"),
            "posted_speed_limit_kmh": observed(limit, "posted_speed_limit_kmh"),
        }
    )
    return changed_party(
        scene,
        0,
        party_type=observed("pedestrian", "party_type"),
        movement=observed("crossing_on_foot", "movement"),
        pedestrian_crossing_place=observed(place, "pedestrian_crossing_place"),
        pedestrian_signal_state=observed(signal, "pedestrian_signal_state"),
        pedestrian_started_before_vehicle_arrived=observed("yes", "pedestrian_started_before_vehicle_arrived"),
    )


@pytest.mark.parametrize(
    "limit,place,signal,expected",
    [
        (30, "marked_crossing", "no_pedestrian_signal", (FaultStatus.NOT_AT_FAULT, FaultStatus.AT_FAULT)),
        (30, "signalised_crossing", "green", (FaultStatus.NOT_AT_FAULT, FaultStatus.AT_FAULT)),
        (100, "outside_any_crossing", "no_pedestrian_signal", (FaultStatus.AT_FAULT, FaultStatus.NOT_AT_FAULT)),
        (80, "marked_crossing", "no_pedestrian_signal", (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE)),
        (
            80,
            "outside_any_crossing",
            "no_pedestrian_signal",
            (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE),
        ),
        (100, "marked_crossing", "no_pedestrian_signal", (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE)),
        (30, "signalised_crossing", "red", (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE)),
        (30, "signalised_crossing", "flashing_green", (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE)),
    ],
)
def test_pedestrian_boundaries_and_signals(
    limit: int, place: str, signal: str, expected: tuple[FaultStatus, ...]
) -> None:
    faults = evaluate_situation(pedestrian_situation(limit, place, signal))
    assert tuple(f.status for f in faults) == expected
    if limit == 80:
        assert all("exactly 80" in f.reason for f in faults)


@pytest.mark.parametrize(
    "field", ["pedestrian_crossing_place", "pedestrian_signal_state", "pedestrian_started_before_vehicle_arrived"]
)
def test_each_unknown_pedestrian_field_is_named(field: str) -> None:
    faults = evaluate_situation(changed_party(pedestrian_situation(), 0, **{field: observed("unknown", field)}))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all(field in f.reason for f in faults)


def test_pedestrian_unknown_posted_limit_is_named() -> None:
    faults = evaluate_situation(pedestrian_situation("unknown"))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all("posted_speed_limit_kmh" in f.reason for f in faults)


def test_all_active_rules_are_explicitly_mapped_to_fines() -> None:
    from incident_report.rules import ALL_RULES
    from incident_report.rules.models import Violation
    from incident_report.rules.mr130_1997 import MR_RULES
    from incident_report.rules.violations import RULE_ID_TO_FINE_KEY, NoFixedItem, map_violation

    assert set(MR_RULES) <= set(ALL_RULES)
    assert all(rule.rule_id in RULE_ID_TO_FINE_KEY for rule in ALL_RULES)
    for rule in ALL_RULES:
        violation = Violation(rule_id=rule.rule_id, party_description=A, cited_facts=(CONTACT,))
        if rule.rule_id == "mr130-1997-art-58-59-lane-change":
            assert RULE_ID_TO_FINE_KEY[rule.rule_id] is NoFixedItem.OFFICER_SELECTION
            with pytest.raises(ValueError, match="No fixed schedule item"):
                map_violation(violation)
        else:
            assert map_violation(violation).item_number
    expected = ["33a", "70", "53", NoFixedItem.OFFICER_SELECTION, "52"]
    actual = []
    for rule in MR_RULES:
        entry = RULE_ID_TO_FINE_KEY[rule.rule_id]
        actual.append(
            entry
            if isinstance(entry, NoFixedItem)
            else map_violation(Violation(rule_id=rule.rule_id, party_description=A, cited_facts=(CONTACT,))).item_number
        )
    assert actual == expected


def test_unsourced_rule_is_rejected() -> None:
    from incident_report.rules import Rule

    with pytest.raises(ValueError, match="legal source"):
        Rule(rule_id="unsourced", legal_source="", article="", title="unverified", description="unknown", source_url="")


@pytest.mark.parametrize(
    "markings,lights,siren,expected",
    [
        ("police", "yes", "yes", (FaultStatus.NOT_AT_FAULT, FaultStatus.AT_FAULT)),
        ("police", "yes", "unknown", (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE)),
        ("ambulance", "yes", "yes", (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE)),
        ("civil_defence", "yes", "yes", (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE)),
        ("other_official_marked", "yes", "yes", (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE)),
        ("unknown", "yes", "yes", (FaultStatus.CANNOT_DETERMINE, FaultStatus.CANNOT_DETERMINE)),
    ],
)
def test_emergency_markings_at_signals(
    markings: str, lights: str, siren: str, expected: tuple[FaultStatus, ...]
) -> None:
    scene = changed_party(
        signal_situation("red", "green"),
        0,
        party_type=observed("emergency_or_official_marked", "party_type"),
        visible_vehicle_markings=observed(markings, "visible_vehicle_markings"),
        emergency_lights=observed(lights, "emergency_lights"),
        emergency_siren=observed(siren, "emergency_siren"),
    )
    faults = evaluate_situation(scene)
    assert tuple(f.status for f in faults) == expected
    if siren == "unknown":
        assert all("emergency_siren" in f.reason for f in faults)
    if markings in {"ambulance", "civil_defence"}:
        assert all("on duty" in f.reason for f in faults)


def test_red_observation_after_contact_is_only_a_contributing_factor() -> None:
    scene = signal_situation("red", "green")
    late = CitedFact(description="signal after contact", view_id="exterior", timestamp_seconds=4.0, confidence=0.9)
    scene = changed_party(scene, 0, signal_state_when_crossing_stop_line=Observed(value="red", evidence=(late,)))
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert faults[0].findings[0].classification == "Contributing Factor"
    assert late in faults[0].findings[0].cited_facts


def test_known_breaches_survive_unresolved_applicability() -> None:
    scene = changed_party(signal_situation("red", "red"), 1, movement=observed("unknown", "movement"))
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all(f.findings for f in faults)
    assert all("movement" in f.reason for f in faults)


def test_none_visible_indicator_never_proves_non_use() -> None:
    scene = changed_party(
        lane_situation(),
        0,
        signal_given=observed("none_visible", "signal_given"),
        lateral_move_into_other_party_path=observed("no", "lateral_move_into_other_party_path"),
    )
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert faults[0].findings == ()  # Research section 5.4: none_visible never proves non-use.
    from incident_report.rules.violations import violations_from_faults

    assert violations_from_faults(faults) == ()


def test_uncovered_counterparty_turn_does_not_become_green() -> None:
    scene = changed_party(signal_situation("red", "green"), 1, movement=observed("left_turn", "movement"))
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert faults[0].findings
    assert all("movement" in f.reason for f in faults)


def test_different_view_clock_cannot_establish_causation() -> None:
    reading = CitedFact(description="red at crossing", view_id="other-clock", timestamp_seconds=1.0, confidence=0.9)
    scene = changed_party(
        signal_situation("red", "green"),
        0,
        signal_state_when_crossing_stop_line=Observed(value="red", evidence=(reading,)),
    )
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert faults[0].findings[0].classification == "Contributing Factor"


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("field", ["party_type", "movement", "approach_control_faced", "relative_position_of_other"])
def test_signal_each_party_unknown_is_named(index: int, field: str) -> None:
    value = {} if field == "relative_position_of_other" else observed("unknown", field)
    faults = evaluate_situation(changed_party(signal_situation("red", "green"), index, **{field: value}))
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all(field in f.reason for f in faults)


@pytest.mark.parametrize("field", ["officer_directing_traffic", "conflict_zone_type"])
def test_signal_each_unknown_scene_field_is_named(field: str) -> None:
    scene = signal_situation("red", "green").model_copy(update={field: observed("unknown", field)})
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all(field in f.reason for f in faults)


@pytest.mark.parametrize("start", [1.0, 2.999999])
def test_lane_change_causation_has_no_suddenness_cutoff(start: float) -> None:
    scene = changed_party(lane_situation(), 0, lateral_move_start_t=observed(start, "lateral_move_start_t"))
    assert evaluate_situation(scene)[0].status == FaultStatus.AT_FAULT


def test_two_causal_lane_intrusions_preserve_each_breach() -> None:
    scene = changed_party(
        lane_situation(),
        1,
        movement=observed("lane_change_left", "movement"),
        lateral_move_into_other_party_path=observed("yes", "lateral_move_into_other_party_path"),
        lateral_move_start_t=observed(1.5, "lateral_move_start_t"),
        contact_t=observed(3.0, "contact_t"),
        lane_line_crossed_before_contact=observed("broken_white", "lane_line_crossed_before_contact"),
        signal_given=observed("left_indicator", "signal_given"),
    )
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all(f.findings and f.findings[0].classification == "Finding" for f in faults)
    from incident_report.rules.violations import violations_from_faults

    assert violations_from_faults(faults) == ()


@pytest.mark.parametrize("action", ["stopped_in_traffic", "stopped_at_signal"])
def test_rear_end_justified_stops_keep_the_following_duty(action: str) -> None:
    scene = changed_party(
        rear_situation(action),
        1,
        motion_state_before_contact=observed("stopped_in_lane", "motion_state_before_contact"),
    )
    assert evaluate_situation(scene)[0].status == FaultStatus.AT_FAULT


def test_pedestrian_vehicle_signal_unknown_is_named() -> None:
    scene = changed_party(
        pedestrian_situation(30, "signalised_crossing", "green"),
        1,
        approach_control_faced=observed("signal", "approach_control_faced"),
        signal_state_when_crossing_stop_line=observed("unknown", "signal_state_when_crossing_stop_line"),
    )
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all("signal_state_when_crossing_stop_line" in f.reason for f in faults)


def test_rear_end_does_not_hide_two_red_crossings() -> None:
    scene = rear_situation().model_copy(
        update={"conflict_zone_type": observed("signalised_junction", "conflict_zone_type")}
    )
    for i in range(2):
        scene = changed_party(
            scene,
            i,
            approach_control_faced=observed("signal", "approach_control_faced"),
            signal_state_when_crossing_stop_line=observed("red", "signal_state_when_crossing_stop_line"),
        )
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all(any(f.article == "MR 130/1997 Art. 25(1)" for f in party.findings) for party in faults)


def test_pedestrian_breach_does_not_hide_driver_red_crossing() -> None:
    scene = changed_party(
        pedestrian_situation(100, "outside_any_crossing", "no_pedestrian_signal"),
        1,
        approach_control_faced=observed("signal", "approach_control_faced"),
        signal_state_when_crossing_stop_line=observed("red", "signal_state_when_crossing_stop_line"),
    )
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all(f.findings for f in faults)
    assert faults[1].findings[0].article == "MR 130/1997 Art. 25(1)"


@pytest.mark.parametrize("markings", ["ambulance", "civil_defence"])
def test_lights_off_do_not_disprove_ambulance_or_fire_duty(markings: str) -> None:
    scene = changed_party(
        signal_situation("green", "red"),
        0,
        party_type=observed("emergency_or_official_marked", "party_type"),
        visible_vehicle_markings=observed(markings, "visible_vehicle_markings"),
        emergency_lights=observed("no", "emergency_lights"),
    )
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all("on duty" in f.reason for f in faults)


@pytest.mark.parametrize("scenario", ["entry", "roundabout"])
@pytest.mark.parametrize("field", ["approach_control_faced", "officer_directing_traffic"])
@pytest.mark.parametrize("view,timestamp", [("exterior", 4.0), ("other-clock", 1.0)])
def test_dispatch_evidence_needs_the_contact_clock(scenario: str, field: str, view: str, timestamp: float) -> None:
    scene = junction_situation()
    if scenario == "entry":
        scene = changed_party(
            scene,
            0,
            movement=observed("entering_from_side_road", "movement"),
            entry_origin=observed("side_road", "entry_origin"),
        )
    else:
        scene = scene.model_copy(
            update={"conflict_zone_type": observed("roundabout_unsignalised", "conflict_zone_type")}
        )
        scene = changed_party(
            scene,
            0,
            movement=observed("entering_roundabout", "movement"),
            roundabout_entry_state=observed("entering_from_approach", "roundabout_entry_state"),
        )
        scene = changed_party(
            scene,
            1,
            movement=observed("circulating_roundabout", "movement"),
            roundabout_entry_state=observed("already_inside_circulating", "roundabout_entry_state"),
        )
    citation = CitedFact(description=field, view_id=view, timestamp_seconds=timestamp, confidence=0.9)
    if field == "officer_directing_traffic":
        scene = scene.model_copy(update={field: Observed(value="no", evidence=(citation,))})
    else:
        scene = changed_party(scene, 0, **{field: Observed(value="none_confirmed", evidence=(citation,))})
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all(f.classification == "Contributing Factor" for p in faults for f in p.findings)


def test_mixed_control_rear_end_keeps_red_leader_breach() -> None:
    scene = rear_situation().model_copy(
        update={"conflict_zone_type": observed("signalised_junction", "conflict_zone_type")}
    )
    scene = changed_party(
        scene,
        1,
        approach_control_faced=observed("signal", "approach_control_faced"),
        signal_state_when_crossing_stop_line=observed("red", "signal_state_when_crossing_stop_line"),
    )
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert any(f.article == "MR 130/1997 Art. 25(1)" for f in faults[1].findings)
    assert all("approach_control_faced" in f.reason for f in faults)


def test_rear_end_does_not_bypass_unresolved_officer_control() -> None:
    scene = changed_party(rear_situation(), 0, approach_control_faced=observed("officer", "approach_control_faced"))
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all("approach_control_faced" in f.reason for f in faults)


def test_emergency_warnings_without_readable_markings_do_not_prove_normal_priority() -> None:
    scene = changed_party(
        signal_situation("red", "green"),
        0,
        emergency_lights=observed("yes", "emergency_lights"),
        emergency_siren=observed("yes", "emergency_siren"),
    )
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all("visible_vehicle_markings" in f.reason for f in faults)


@pytest.mark.parametrize("movement", ["left_turn", "u_turn", "entering_from_side_road"])
@pytest.mark.parametrize("party_index", [0, 1])
def test_rear_geometry_does_not_resolve_turn_or_entry_duties(movement: str, party_index: int) -> None:
    scene = changed_party(rear_situation(), party_index, movement=observed(movement, "movement"))
    faults = evaluate_situation(scene)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert all("movement" in f.reason for f in faults)
    assert any(f.article == "MR 130/1997 Art. 50" for f in faults[0].findings)


def test_lane_change_fault_has_no_fixed_schedule_item() -> None:
    from incident_report.rules.violations import violations_from_faults

    faults = evaluate_situation(lane_situation())
    assert faults[0].status == FaultStatus.AT_FAULT
    assert violations_from_faults(faults) == ()
    finding = faults[0].findings[0]
    assert finding.article == "MR 130/1997 Art. 58-59"
    assert "unofficial translation" in finding.legal_source
    assert (
        "applicable schedule item for the officer to select: Item 29 (sudden deviation), 83 (no indicator) or 86 (compulsory lane); not determined from video"
        in finding.reason
    )
