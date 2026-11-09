"""Spec 003 ticket 9 at Seam 3: cited typed facts -> fault and official items."""

from collections.abc import Callable

import pytest

from incident_report.facts import OverviewFacts
from incident_report.rules import FaultStatus, Situation, evaluate_situation, situation_from_overview
from incident_report.rules.violations import violations_from_faults
from incident_report.runlog import RunLog
from test_core_fault_rules import (
    A,
    B,
    changed_party,
    junction_situation,
    lane_situation,
    observed,
    rear_situation,
    signal_situation,
)


@pytest.mark.parametrize(
    "kind,item,aed",
    [("truck_or_heavy", "6", 3000), ("motorcycle", "33b", 1000), ("bus", None, None)],
)
def test_red_light_duty_and_fine_follow_vehicle_class(kind: str, item: str | None, aed: int | None) -> None:
    scene = changed_party(signal_situation("red", "green"), 0, party_type=observed(kind, "party_type"))
    faults = evaluate_situation(scene)
    assert faults[0].status == FaultStatus.AT_FAULT
    assert faults[1].status == FaultStatus.NOT_AT_FAULT
    fines = violations_from_faults(faults, party_types={p.description: p.party_type.value for p in scene.parties})
    if item is None:
        assert fines == ()
        assert "no verified item for this vehicle type; officer selects" in faults[0].reason
    else:
        assert len(fines) == 1
        assert (fines[0].item_number, fines[0].aed) == (item, aed)
        if item == "6":
            assert "Licence suspended 1 year" in (fines[0].notes or "")


def oncoming_scene(*, signals: bool = False) -> Situation:
    scene = signal_situation("green", "green") if signals else junction_situation()
    scene = changed_party(scene, 0, movement=observed("left_turn", "movement"))
    for i, other in ((0, B), (1, A)):
        scene = changed_party(
            scene, i, relative_position_of_other={other: observed("B_ahead_oncoming", "relative_position_of_other")}
        )
    return scene


@pytest.mark.parametrize("signals", [False, True])
def test_oncoming_left_turn_has_owner_mapped_item_and_translation_warning(signals: bool) -> None:
    faults = evaluate_situation(oncoming_scene(signals=signals))
    assert [p.status for p in faults] == [FaultStatus.AT_FAULT, FaultStatus.NOT_AT_FAULT]
    finding = faults[0].findings[0]
    assert "59(5)" in finding.article and "59(2)" in finding.article
    assert "فيتركها تمر أولاً" in finding.reason
    assert "not certified" in finding.reason
    assert (
        "item mapped by the owner (spec 003 decision 12); the item wording does not name this breach" in finding.reason
    )
    fines = violations_from_faults(faults)
    assert [(f.item_number, f.aed, f.black_points) for f in fines] == [("99b", 500, 4)]
    assert "item mapped by the owner" in (fines[0].notes or "")


@pytest.mark.parametrize("kind,item", [("truck_or_heavy", "6"), ("motorcycle", "33b"), ("bus", None)])
def test_production_standalone_mapping_preserves_typed_class(kind: str, item: str | None) -> None:
    from incident_report.agreement import agree_no_collision_violations
    from incident_report.rules.engine import evaluate_fault
    from test_collision_pair_selection import A as SUBJECT
    from test_collision_pair_selection import sf, signal_facts

    facts = signal_facts((A, B), [], [])
    facts = facts.model_copy(
        update={
            "structured_facts": [
                f for f in facts.structured_facts if not (f.subject == SUBJECT and f.field == "party_type")
            ]
            + [sf(SUBJECT, "party_type", kind)]
        }
    )
    fines, _ = agree_no_collision_violations([facts], [evaluate_fault(facts)])
    assert [f.item_number for f in fines] == ([] if item is None else [item])


def with_contact_visibility(scene: Situation, *, assessment: bool = False, view: str = "exterior") -> Situation:
    from incident_report.facts import CollisionObservation, IncidentAssessment

    contact = CollisionObservation(
        kind="vehicle_contact_vehicle",
        subject=A,
        other_party=B,
        injury_grade="unknown",
        view_id=view,
        timestamp_seconds=3.0,
        confidence=0.9,
        visibility="possible_obscured",
        source_part="detail_window",
        part_time_seconds=1.0,
    )
    assessments = (
        [
            IncidentAssessment(
                view_id=view,
                assessment_note="A possible touch behind an obstruction",
                camera_role="fixed_camera",
                contact_state="contact_possible_obscured",
                contact_interval_start_seconds=2.9,
                contact_interval_end_seconds=3.1,
            )
        ]
        if assessment
        else []
    )
    return Situation.model_validate(
        {
            **scene.model_dump(),
            "collision_observations": [] if assessment else [contact],
            "incident_assessments": assessments,
        }
    )


@pytest.mark.parametrize("scenario", ["lane", "rear"])
@pytest.mark.parametrize("assessment", [False, True])
def test_obscured_touch_blocks_contact_dependent_rules(scenario: str, assessment: bool) -> None:
    scene = lane_situation() if scenario == "lane" else rear_situation()
    faults = evaluate_situation(with_contact_visibility(scene, assessment=assessment))
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in faults)
    assert all("the touch is not clearly visible" in p.reason for p in faults)
    assert not any(p.findings for p in faults)


@pytest.mark.parametrize("signals", [False, True])
def test_obscured_touch_keeps_precontact_left_turn_duty(signals: bool) -> None:
    faults = evaluate_situation(with_contact_visibility(oncoming_scene(signals=signals)))
    assert faults[0].status == FaultStatus.AT_FAULT


def test_obscured_touch_in_another_view_does_not_block_this_contact() -> None:
    assert (
        evaluate_situation(with_contact_visibility(rear_situation(), view="another-view"))[0].status
        == FaultStatus.AT_FAULT
    )


def exit_scene(position: str = "on_exit_curve_before_straight_lane", *, signed: bool = False) -> Situation:
    scene = oncoming_scene().model_copy(
        update={"conflict_zone_type": observed("roundabout_unsignalised", "conflict_zone_type")}
    )
    scene = changed_party(
        scene,
        0,
        movement=observed("exiting_roundabout", "movement"),
        roundabout_exit_position=observed(position, "roundabout_exit_position"),
    )
    return changed_party(
        scene,
        1,
        movement=observed("entering_roundabout", "movement"),
        approach_control_faced=observed(
            "give_way_sign_or_line" if signed else "none_confirmed", "approach_control_faced"
        ),
    )


@pytest.mark.parametrize("signed,item", [(False, "84"), (True, "70")])
def test_roundabout_exit_curve_yields_from_oncoming_direction_even_with_give_way(signed: bool, item: str) -> None:
    faults = evaluate_situation(exit_scene(signed=signed))
    assert [p.status for p in faults] == [FaultStatus.NOT_AT_FAULT, FaultStatus.AT_FAULT]
    assert "Roundabout boundary and direction follow the owner's working interpretation" in faults[1].reason
    assert "give way to all vehicles already in it moving towards you from the left" in faults[1].reason
    assert [f.item_number for f in violations_from_faults(faults)] == [item]


def main_road_scene() -> Situation:
    scene = changed_party(
        junction_situation(),
        0,
        movement=observed("entering_from_side_road", "movement"),
        entry_origin=observed("side_road", "entry_origin"),
        road_class_on_approach=observed("side_road", "road_class_on_approach"),
        road_class_basis=observed("short_or_ends_at_junction", "road_class_basis"),
    )
    return changed_party(
        scene,
        1,
        road_class_on_approach=observed("main_road", "road_class_on_approach"),
        road_class_basis=observed("more_than_two_lanes_and_long", "road_class_basis"),
    )


def test_main_road_emits_one_finding_with_both_articles_and_owner_definition() -> None:
    from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD

    faults = evaluate_situation(main_road_scene())
    assert [p.status for p in faults] == [FaultStatus.AT_FAULT, FaultStatus.NOT_AT_FAULT]
    assert len(faults[0].findings) == 1
    assert faults[0].rule_id == RULE_ART_6_MAIN_ROAD.rule_id
    assert "44(2)" in faults[0].reason and "Art. 43" in faults[0].reason
    assert (
        "Main road follows the owner's working definition (spec 003 decision 10); 'main road' is not defined in law."
        in faults[0].reason
    )
    assert any(c.description == "road_class_basis" for c in faults[0].cited_facts)
    assert [(v.item_number, v.aed, v.black_points) for v in violations_from_faults(faults)] == [("53", 400, 4)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("movement", "right_turn"),
        ("movement", "unknown"),
        ("approach_control_faced", "unknown"),
        ("approach_control_faced", "stop_sign_or_line"),
        ("approach_control_faced", "give_way_sign_or_line"),
    ],
)
def test_left_turn_missing_or_contradicting_party_field_abstains(field: str, value: str) -> None:
    scene = changed_party(oncoming_scene(), 0, **{field: observed(value, field)})
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in evaluate_situation(scene))


@pytest.mark.parametrize("state", ["green_arrow", "yellow", "flashing_yellow", "unknown", "red"])
def test_signal_turn_exception_is_only_for_both_full_greens(state: str) -> None:
    scene = changed_party(
        oncoming_scene(signals=True), 0, signal_state_when_crossing_stop_line=observed(state, "signal")
    )
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in evaluate_situation(scene))


@pytest.mark.parametrize("make_scene", [oncoming_scene, exit_scene, main_road_scene])
@pytest.mark.parametrize("officer", ["yes", "unknown"])
def test_new_rules_cannot_override_officer_or_unknown_officer(
    make_scene: Callable[[], Situation], officer: str
) -> None:
    scene = make_scene().model_copy(update={"officer_directing_traffic": observed(officer, "officer")})
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in evaluate_situation(scene))


@pytest.mark.parametrize("position", ["reached_straight_lane_of_exit_road", "not_applicable", "unknown"])
def test_roundabout_exit_requires_the_curve_position(position: str) -> None:
    faults = evaluate_situation(exit_scene(position))
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in faults)
    assert "roundabout_exit_position" in faults[0].reason


@pytest.mark.parametrize(
    "relations,decides",
    [
        (("B_from_A_left", "B_from_A_right"), True),
        (("B_from_A_right", "B_from_A_left"), True),
        (("B_ahead_oncoming", "B_ahead_oncoming"), True),
        (("B_alongside_adjacent_lane", "B_alongside_adjacent_lane"), True),
        (("B_ahead_same_direction", "B_behind"), False),
        (("B_behind", "B_ahead_same_direction"), False),
        (("B_from_A_left", "B_from_A_left"), False),
        (("unknown", "B_ahead_oncoming"), False),
    ],
)
def test_roundabout_exit_all_directions_except_rear_end_shapes(relations: tuple[str, str], decides: bool) -> None:
    scene = exit_scene()
    for i, other in ((0, B), (1, A)):
        scene = changed_party(scene, i, relative_position_of_other={other: observed(relations[i], "relation")})
    assert (evaluate_situation(scene)[1].status == FaultStatus.AT_FAULT) is decides


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("field", ["road_class_on_approach", "road_class_basis"])
def test_main_road_requires_both_cited_road_answers(index: int, field: str) -> None:
    scene = changed_party(main_road_scene(), index, **{field: observed("unknown", field)})
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in evaluate_situation(scene))


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "road", ["main_road", "side_road", "service_road", "roundabout", "car_park_or_access", "unknown"]
)
@pytest.mark.parametrize(
    "basis",
    [
        "more_than_two_lanes_and_long",
        "two_lanes_or_fewer",
        "short_or_ends_at_junction",
        "service_road_beside_main_road",
        "access_or_car_park",
        "roundabout_carriageway",
        "other",
        "unknown",
    ],
)
def test_main_road_class_and_basis_matrix(index: int, road: str, basis: str) -> None:
    scene = changed_party(
        main_road_scene(),
        index,
        road_class_on_approach=observed(road, "road"),
        road_class_basis=observed(basis, "basis"),
    )
    expected = (road, basis) == (
        ("side_road", "short_or_ends_at_junction") if index == 0 else ("main_road", "more_than_two_lanes_and_long")
    )
    assert (evaluate_situation(scene)[0].status == FaultStatus.AT_FAULT) is expected


@pytest.mark.parametrize("kind", ["truck_or_heavy", "bus", "motorcycle", "bicycle_or_personal_transport", "other"])
@pytest.mark.parametrize("rule", ["signal", "sign", "equal", "entry", "roundabout", "lane", "rear", "pedestrian"])
def test_general_driver_duties_party_type_table(kind: str, rule: str) -> None:
    from test_core_fault_rules import pedestrian_situation

    if rule == "signal":
        scene, blamed = signal_situation("red", "green"), 0
    elif rule == "lane":
        scene, blamed = lane_situation(), 0
    elif rule == "rear":
        scene, blamed = rear_situation(), 0
    elif rule == "entry":
        scene, blamed = main_road_scene(), 0
        # Legacy entry duty independently of the new main-road rule.
        for i in range(2):
            scene = changed_party(
                scene,
                i,
                road_class_on_approach=observed("unknown", "road"),
                road_class_basis=observed("unknown", "basis"),
            )
    elif rule == "roundabout":
        scene, blamed = junction_situation(), 0
        scene = scene.model_copy(update={"conflict_zone_type": observed("roundabout_unsignalised", "zone")})
        scene = changed_party(
            scene,
            0,
            movement=observed("entering_roundabout", "move"),
            roundabout_entry_state=observed("entering_from_approach", "state"),
        )
        scene = changed_party(
            scene,
            1,
            movement=observed("circulating_roundabout", "move"),
            roundabout_entry_state=observed("already_inside_circulating", "state"),
        )
    elif rule == "pedestrian":
        scene, blamed = pedestrian_situation(30, "marked_crossing", "no_pedestrian_signal"), 1
    else:
        scene, blamed = junction_situation(), 0
        if rule == "sign":
            scene = changed_party(scene, 0, approach_control_faced=observed("give_way_sign_or_line", "sign"))
    scene = changed_party(scene, blamed, party_type=observed(kind, "type"))
    faults = evaluate_situation(scene)
    if kind in {"bicycle_or_personal_transport", "other"}:
        assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in faults)
    else:
        assert faults[blamed].status == FaultStatus.AT_FAULT
        if rule == "lane":
            assert "86" not in faults[blamed].reason
            assert violations_from_faults(faults) == ()


def test_opposite_blamed_parties_keep_both_reasons_and_abstain() -> None:
    # A moved into B's lane while B crossed its stop line on red.
    scene = lane_situation()
    for i, state in ((0, "green"), (1, "red")):
        scene = changed_party(
            scene,
            i,
            approach_control_faced=observed("signal", "control"),
            signal_state_when_crossing_stop_line=observed(state, "signal"),
        )
    faults = evaluate_situation(scene)
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in faults)
    assert "Both parties breached" in faults[0].reason
    assert "Moved laterally" in faults[0].reason and "Crossed the stop line on red" in faults[1].reason
    assert all(p.findings for p in faults)


def parsed_rule_facts(scenario: str, version: str = "overview-11", *, obscured: bool = False) -> OverviewFacts:
    from overview10_helpers import answer, contact, parse, row
    from overview11_helpers import answer11, assessment11, contact11, parse11

    first, second = row("P1"), row("P2", colour_family="red")
    zone, relations = "unsignalised_junction", ("B_ahead_oncoming", "B_ahead_oncoming")
    if scenario in {"left", "handover"}:
        first = row(
            "P1",
            manoeuvre="roundabout_exit" if scenario == "handover" else "into_side_road",
            turn_direction="left",
            roundabout_exit_position="reached_straight_lane_of_exit_road"
            if scenario == "handover"
            else "not_applicable",
        )
    elif scenario == "exit":
        first = row(
            "P1",
            manoeuvre="roundabout_exit",
            turn_direction="left",
            road_class_on_approach="roundabout",
            road_class_basis="roundabout_carriageway",
            roundabout_exit_position="on_exit_curve_before_straight_lane",
        )
        second = row("P2", colour_family="red", manoeuvre="roundabout_entry")
        zone = "roundabout_unsignalised"
    elif scenario == "main":
        first = row(
            "P1",
            manoeuvre="out_of_side_road",
            road_class_on_approach="side_road",
            road_class_basis="short_or_ends_at_junction",
        )
        relations = ("B_from_A_left", "B_from_A_right")
    else:
        first = row("P1", party_type=scenario)
        zone, relations = "signalised_junction", ("B_from_A_left", "B_from_A_right")
    structured = []

    def field(subject: str, name: str, value: str, other: str = "") -> None:
        structured.append(
            dict(subject=subject, field=name, value=value, other_party=other, timestamp_seconds=2.0, confidence=0.9)
        )

    field("scene", "conflict_zone_type", zone)
    field("scene", "officer_directing_traffic", "no")
    for i, code, other in ((0, "P1", "P2"), (1, "P2", "P1")):
        field(code, "relative_position_of_other", relations[i], other)
        field(code, "approach_control_faced", "signal" if zone == "signalised_junction" else "none_confirmed")
        if zone == "signalised_junction":
            field(code, "signal_state_when_crossing_stop_line", "red" if i == 0 else "green")
    visibility = "possible_obscured" if obscured else "visible"
    if version == "overview-11":
        return parse11(
            answer11(
                rows=[first, second],
                structured_facts=structured,
                contacts=[contact11("detail_window", at=1.5, visibility=visibility)],
                incident_assessment=assessment11(
                    "detail_window",
                    contact_interval_start_seconds=1.0,
                    contact_interval_end_seconds=2.0,
                    contact_state="contact_possible_obscured" if obscured else "contact_observed",
                ),
            ),
            window=(30.0, 34.0),
        )
    return parse(
        answer(rows=[first, second], structured_facts=structured, contacts=[contact(at=3.0, visibility=visibility)])
    )


@pytest.mark.parametrize("version", ["overview-10", "overview-11"])
@pytest.mark.parametrize(
    "scenario,blamed,item",
    [
        ("left", 0, "99b"),
        ("exit", 1, "84"),
        ("main", 0, "53"),
        ("truck", 0, "6"),
        ("motorcycle", 0, "33b"),
        ("bus", 0, None),
    ],
)
@pytest.mark.parametrize("obscured", [False, True])
def test_parsed_facts_produce_valid_report_through_final_consumer(
    version: str, scenario: str, blamed: int, item: str | None, obscured: bool
) -> None:
    import uuid

    from incident_report.domain import Report, ReportView, Role
    from incident_report.guardrails import validate_report
    from incident_report.rules import evaluate_fault
    from incident_report.rules.consistency import check_consistency

    facts = parsed_rule_facts(scenario, version, obscured=obscured)
    view_id = facts.parties[0].view_id
    issues = check_consistency(facts, {view_id: 40.0})
    assert issues == ()
    faults = evaluate_fault(facts, consistency_issues=issues)
    assert faults[blamed].status == FaultStatus.AT_FAULT
    fines = violations_from_faults(
        faults, party_types={p.description: p.party_type.value for p in situation_from_overview(facts).parties}
    )
    assert [f.item_number for f in fines] == ([] if item is None else [item])
    if version == "overview-11":
        assert facts.collision_observations[0].source_part == "detail_window"
        assert facts.collision_observations[0].part_time_seconds == 1.5
        assert facts.candidate_collisions[0].timestamp_seconds == 31.5
    report = Report(
        run_id=uuid.uuid4(),
        input_hash="b" * 64,
        prompt_version=version,
        provenance="synthetic",
        summary="Contact observed.",
        views=(
            ReportView(
                filename="test.mp4",
                role=Role.EXTERIOR,
                label=None,
                acquisition_method=None,
                sha256=view_id,
                duration_seconds=40.0,
            ),
        ),
        facts=facts,
        parties=faults,
        violations=fines,
    )
    validated = validate_report(report)
    assert validated.is_valid, validated.validation_errors
    assert validated.violations == fines
    assert Report.model_validate_json(validated.model_dump_json()) == validated


def test_consistency_issues_override_new_left_turn_rule() -> None:
    from incident_report.rules import evaluate_fault
    from incident_report.rules.consistency import ConsistencyIssue

    facts = parsed_rule_facts("left")
    issue = ConsistencyIssue(
        code="synthetic-conflict",
        subject=facts.parties[0].description,
        fields=("movement",),
        reason="Conflicting movement answers.",
        cited_facts=(facts.candidate_collisions[0],),
    )
    faults = evaluate_fault(facts, consistency_issues=(issue,))
    assert faults[0].status == FaultStatus.CANNOT_DETERMINE
    assert faults[0].findings == ()
    assert violations_from_faults(faults) == ()


def test_known_car_park_rank_keeps_existing_entry_duty() -> None:
    scene = changed_party(
        main_road_scene(),
        0,
        movement=observed("entering_from_car_park_or_driveway", "movement"),
        entry_origin=observed("car_park_or_driveway", "entry_origin"),
        road_class_on_approach=observed("car_park_or_access", "road"),
        road_class_basis=observed("access_or_car_park", "basis"),
    )
    from incident_report.rules.mr130_1997 import RULE_ENTRY

    faults = evaluate_situation(scene)
    assert faults[0].status == FaultStatus.AT_FAULT
    assert faults[0].rule_id == RULE_ENTRY.rule_id


def test_main_road_and_entry_disagreeing_blame_abstains_with_both_findings() -> None:
    scene = changed_party(main_road_scene(), 0, entry_origin=observed("public_road_same_road", "entry_origin"))
    scene = changed_party(
        scene,
        1,
        entry_origin=observed("car_park_or_driveway", "entry_origin"),
        movement=observed("entering_from_car_park_or_driveway", "movement"),
    )
    faults = evaluate_situation(scene)
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in faults)
    assert all(p.findings for p in faults)
    assert "Both parties breached" in faults[0].reason


@pytest.mark.parametrize("version", ["overview-10", "overview-11"])
def test_roundabout_straight_lane_hands_over_to_oncoming_left_turn(version: str) -> None:
    from incident_report.rules import evaluate_fault
    from incident_report.rules.adapter import situation_from_overview
    from incident_report.rules.mr130_1997 import RULE_ONCOMING_LEFT_TURN

    facts = parsed_rule_facts("handover", version)
    scene = situation_from_overview(facts)
    assert scene.parties[0].roundabout_exit_position.value == "reached_straight_lane_of_exit_road"
    assert scene.parties[0].movement.value == "left_turn"
    faults = evaluate_fault(facts)
    assert [p.status for p in faults] == [FaultStatus.AT_FAULT, FaultStatus.NOT_AT_FAULT]
    assert faults[0].rule_id == RULE_ONCOMING_LEFT_TURN.rule_id


@pytest.mark.parametrize("make_scene", [exit_scene, main_road_scene])
def test_signal_control_overrides_new_priority_rules(make_scene: Callable[[], Situation]) -> None:
    scene = make_scene().model_copy(update={"conflict_zone_type": observed("signalised_junction", "zone")})
    for i, state in ((0, "red"), (1, "green")):
        scene = changed_party(
            scene,
            i,
            approach_control_faced=observed("signal", "control"),
            signal_state_when_crossing_stop_line=observed(state, "signal"),
        )
    assert all(
        p.rule_id not in {"mr130-1997-art-44-roundabout-exit", "decree-law-14-2024-art-6-main-road"}
        for p in evaluate_situation(scene)
    )


@pytest.mark.parametrize("make_scene", [oncoming_scene, exit_scene, main_road_scene])
def test_unknown_conflict_zone_cannot_assign_fault(make_scene: Callable[[], Situation]) -> None:
    scene = make_scene().model_copy(update={"conflict_zone_type": observed("unknown", "zone")})
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in evaluate_situation(scene))


def test_oncoming_left_turn_also_applies_in_a_signalised_roundabout_zone() -> None:
    scene = oncoming_scene(signals=True).model_copy(
        update={"conflict_zone_type": observed("roundabout_signalised", "zone")}
    )
    assert evaluate_situation(scene)[0].status == FaultStatus.AT_FAULT


@pytest.mark.parametrize("party_types", [{}, {A: "unknown"}])
def test_missing_class_in_an_explicit_type_map_never_uses_light_item(party_types: dict[str, str]) -> None:
    faults = evaluate_situation(signal_situation("red", "green"))
    assert violations_from_faults(faults, party_types=party_types) == ()


@pytest.mark.parametrize("kind,item", [("truck", "6"), ("motorcycle", "33b"), ("bus", None)])
def test_single_analysis_entry_keeps_class_specific_fine_and_stored_report(
    kind: str, item: str | None, run_log: RunLog
) -> None:
    from incident_report.agreement import AgreementSettings
    from incident_report.domain import Origin, ReportView, Role
    from incident_report.entry import analyse_incident
    from test_report_guardrails import RecordedAnswer

    facts = parsed_rule_facts(kind)
    view = ReportView(
        filename="synthetic.mp4",
        role=Role.EXTERIOR,
        label=None,
        acquisition_method=None,
        sha256=facts.parties[0].view_id,
        duration_seconds=40.0,
    )
    report = analyse_incident(
        [view],
        Origin.REPLAY,
        ai=RecordedAnswer("Contact observed.", facts),
        run_log=run_log,
        prompt_version="overview-11",
        agreement_settings=AgreementSettings(n_runs=1),
    )
    assert report.is_valid, report.validation_errors
    assert report.parties[0].status == FaultStatus.AT_FAULT
    assert [v.item_number for v in report.violations] == ([] if item is None else [item])
    assert run_log.get(report.run_id).report == report
