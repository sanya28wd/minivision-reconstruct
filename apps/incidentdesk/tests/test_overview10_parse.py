"""overview-10 builder: display names, code rewriting, derived typed facts, candidates, multi-view (spec 003 ticket 3)."""

from __future__ import annotations

from typing import Any

import pytest

from incident_report.ai.dense import consolidate_facts
from incident_report.ai.errors import AIConnectionError
from incident_report.checklist import derive_party_type, derive_road_facts, display_name, rewrite_codes
from incident_report.facts import CitedFact, OverviewFacts, PartyChecklistRow, validate_citations
from incident_report.rules.adapter import party_identities, situation_from_overview
from overview10_helpers import OTHER_VIEW, VIEW, answer, assessment, contact, parse, row


def checklist_row(code: str = "P1", **overrides: Any) -> PartyChecklistRow:
    return PartyChecklistRow.model_validate(row(code, **overrides))


# --- display names ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "P1 silver sedan"),
        ({"party_type": "car_body_unclear"}, "P1 silver car"),
        ({"party_type": "suv", "colour_family": "red"}, "P1 red SUV"),
        ({"colour_family": "unknown"}, "P1 sedan, colour unknown"),
        ({"colour_family": "yellow_or_gold"}, "P1 yellow/gold sedan"),
        ({"party_type": "pedestrian", "colour_family": "unknown"}, "P1 pedestrian"),
        ({"party_type": "unknown", "colour_family": "unknown"}, "P1 road user, colour unknown"),
        ({"party_type": "other", "party_type_other": "tractor"}, "P1 silver tractor"),
        ({"party_type": "other", "party_type_other": "x" * 60}, "P1 silver " + "x" * 40),
        ({"colour_family": "other", "colour_other": "teal"}, "P1 teal sedan"),
        ({"marking": "taxi"}, "P1 silver sedan (taxi)"),
        ({"marking": "emergency_or_official"}, "P1 silver sedan (official markings)"),
    ],
)
def test_display_name(overrides: dict[str, Any], expected: str) -> None:
    assert display_name(checklist_row(**overrides)) == expected


def test_camera_vehicle_display_names() -> None:
    assert display_name(checklist_row("CAM")) == "camera vehicle (silver sedan)"
    assert display_name(checklist_row("CAM", party_type="unknown", colour_family="unknown")) == "camera vehicle"
    assert display_name(checklist_row("CAM", colour_family="unknown")) == "camera vehicle (sedan)"


def test_other_text_is_one_line_and_trimmed_and_still_escaped_at_render() -> None:
    name = display_name(checklist_row(party_type="other", party_type_other="  <b>tractor</b>\n\tdigger  "))
    assert "\n" not in name and "\t" not in name
    from incident_report.web.templating import build_environment

    template = build_environment().from_string("{{ name }}")
    assert "<b>" not in template.render(name=name)


def test_parties_are_built_by_code_with_typed_attributes_and_the_code() -> None:
    facts = parse(answer())
    first = facts.parties[0]
    assert (first.description, first.color, first.body_type, first.party_code) == (
        "P1 silver sedan",
        "silver",
        "sedan",
        "P1",
    )
    assert first.timestamp_seconds == 1.0 and first.confidence == 0.9 and first.view_id == VIEW
    assert [r.display_name for r in facts.party_checklist] == ["P1 silver sedan", "P2 red SUV"]


# --- code rewriting -----------------------------------------------------------------------------------------


def test_rewrite_replaces_whole_word_codes_only_and_leaves_unknown_codes() -> None:
    names = {"P1": "P1 silver sedan", "CAM": "camera vehicle"}
    assert rewrite_codes("P1 hits CAM, P10 and XP1 stay; P2 unknown; P1's door", names) == (
        "P1 silver sedan hits camera vehicle, P10 and XP1 stay; P2 unknown; P1 silver sedan's door"
    )


def test_codes_in_free_text_become_display_names_in_events_notes_and_assessment() -> None:
    facts = parse(
        answer(
            incident_assessment=assessment(assessment_note="P1 rear-ends P2."),
            events=[{"description": "P1 brakes, P2 stops", "timestamp_seconds": 2.0, "confidence": 0.9}],
            behaviour_observations=[{"description": "P2 signals", "timestamp_seconds": 2.0, "confidence": 0.9}],
        )
    )
    assert facts.events[0].description == "P1 silver sedan brakes, P2 red SUV stops"
    assert facts.behaviour_observations[0].description == "P2 red SUV signals"
    assert facts.observation_notes[0].note == "P1 silver sedan drives straight on."
    assert facts.observation_notes[0].subject == "P1 silver sedan"
    assert facts.incident_assessments[0].assessment_note == "P1 silver sedan rear-ends P2 red SUV."


def test_every_reference_is_rewritten_to_the_display_name() -> None:
    fact = {
        "subject": "P1",
        "field": "relative_position_of_other",
        "value": "B_ahead_same_direction",
        "other_party": "P2",
        "timestamp_seconds": 2.0,
        "confidence": 0.9,
    }
    facts = parse(answer(structured_facts=[fact]))
    pair = next(f for f in facts.structured_facts if f.field == "relative_position_of_other")
    assert (pair.subject, pair.other_party) == ("P1 silver sedan", "P2 red SUV")
    observation = facts.collision_observations[0]
    assert (observation.subject, observation.other_party) == ("P1 silver sedan", "P2 red SUV")


@pytest.mark.parametrize(
    "bad",
    [
        {
            "structured_facts": [
                {
                    "subject": "P9",
                    "field": "motion_state_before_contact",
                    "value": "moving",
                    "other_party": "",
                    "timestamp_seconds": 1.0,
                    "confidence": 0.9,
                }
            ]
        },
        {
            "structured_facts": [
                {
                    "subject": "silver sedan",
                    "field": "motion_state_before_contact",
                    "value": "moving",
                    "other_party": "",
                    "timestamp_seconds": 1.0,
                    "confidence": 0.9,
                }
            ]
        },
        {"collision_observations": [contact(subject="P9")]},
        {"collision_observations": [contact(other="red SUV")]},
        {"collision_observations": [contact(kind="vehicle_overturned", subject="the car", other="")]},
        {"observation_notes": [{"party_code": "P7", "timestamp_seconds": 1.0, "note": "x"}]},
        {
            "driver_observations": [
                {
                    "kind": "looking_at_phone",
                    "subject": "driver",
                    "start_seconds": 1.0,
                    "end_seconds": 2.0,
                    "confidence": 0.9,
                }
            ]
        },
    ],
)
def test_a_reference_that_is_not_a_checklist_code_is_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(AIConnectionError):
        parse(answer(**bad))


def test_contact_other_other_party_stays_plain_words() -> None:
    facts = parse(
        answer(
            contacts=[contact(kind="contact_other", other="a stray dog", at=3.0)],
            incident_assessment=assessment(),
        )
    )
    assert facts.collision_observations[0].other_party == "a stray dog"
    assert "a stray dog" in facts.candidate_collisions[0].description


def test_a_contact_without_visibility_is_rejected() -> None:
    broken = contact()
    del broken["visibility"]
    with pytest.raises(AIConnectionError):
        parse(answer(contacts=[broken]))


# --- checklist -> rule-facing facts: the card's mapping table, row by row ----------------------------------

TABLE = [
    # (manoeuvre, row overrides, movement, roundabout_entry_state, entry_origin)
    ("continue_same_road", {"turn_direction": "straight"}, "straight", "not_in_roundabout", "public_road_same_road"),
    ("continue_same_road", {"turn_direction": "left"}, "straight", "not_in_roundabout", "public_road_same_road"),
    ("continue_same_road", {"turn_direction": "right"}, "straight", "not_in_roundabout", "public_road_same_road"),
    ("continue_same_road", {"turn_direction": "other"}, "unknown", "not_in_roundabout", "public_road_same_road"),
    ("continue_same_road", {"turn_direction": "unknown"}, "unknown", "not_in_roundabout", "public_road_same_road"),
    (
        "continue_same_road",
        {
            "turn_direction": "straight",
            "road_class_on_approach": "roundabout",
            "road_class_basis": "roundabout_carriageway",
        },
        "straight",
        "already_inside_circulating",
        "public_road_same_road",
    ),
    ("into_side_road", {"turn_direction": "left"}, "left_turn", "not_in_roundabout", "public_road_same_road"),
    ("into_side_road", {"turn_direction": "right"}, "right_turn", "not_in_roundabout", "public_road_same_road"),
    ("into_side_road", {"turn_direction": "straight"}, "unknown", "not_in_roundabout", "public_road_same_road"),
    ("into_side_road", {"turn_direction": "unknown"}, "unknown", "not_in_roundabout", "public_road_same_road"),
    (
        "into_side_road",
        {
            "turn_direction": "left",
            "road_class_on_approach": "roundabout",
            "road_class_basis": "roundabout_carriageway",
        },
        "left_turn",
        "unknown",
        "public_road_same_road",
    ),
    (
        "out_of_side_road",
        {"road_class_on_approach": "side_road", "road_class_basis": "short_or_ends_at_junction"},
        "entering_from_side_road",
        "not_in_roundabout",
        "side_road",
    ),
    (
        "out_of_side_road",
        {"road_class_on_approach": "service_road", "road_class_basis": "service_road_beside_main_road"},
        "entering_from_side_road",
        "not_in_roundabout",
        "service_road",
    ),
    (
        "out_of_side_road",
        {"road_class_on_approach": "car_park_or_access", "road_class_basis": "access_or_car_park"},
        "entering_from_car_park_or_driveway",
        "not_in_roundabout",
        "car_park_or_driveway",
    ),
    (
        "out_of_side_road",
        {"road_class_on_approach": "side_road", "road_class_basis": "two_lanes_or_fewer", "turn_direction": "left"},
        "left_turn",
        "not_in_roundabout",
        "unknown",
    ),
    (
        "out_of_side_road",
        {"road_class_on_approach": "main_road", "turn_direction": "straight"},
        "straight",
        "not_in_roundabout",
        "unknown",
    ),
    (
        "out_of_side_road",
        {"road_class_on_approach": "unknown", "road_class_basis": "unknown", "turn_direction": "right"},
        "right_turn",
        "not_in_roundabout",
        "unknown",
    ),
    (
        "out_of_side_road",
        {
            "road_class_on_approach": "roundabout",
            "road_class_basis": "roundabout_carriageway",
            "turn_direction": "left",
        },
        "left_turn",
        "unknown",
        "unknown",
    ),
    ("moving_off_from_kerb", {}, "other", "not_in_roundabout", "roadside_stop"),
    (
        "merging",
        {"road_class_on_approach": "side_road", "road_class_basis": "short_or_ends_at_junction"},
        "merging",
        "not_in_roundabout",
        "side_road",
    ),
    (
        "merging",
        {"road_class_on_approach": "service_road", "road_class_basis": "service_road_beside_main_road"},
        "merging",
        "not_in_roundabout",
        "service_road",
    ),
    ("merging", {"road_class_on_approach": "main_road"}, "merging", "not_in_roundabout", "public_road_same_road"),
    (
        "merging",
        {"road_class_on_approach": "side_road", "road_class_basis": "two_lanes_or_fewer"},
        "merging",
        "not_in_roundabout",
        "public_road_same_road",
    ),
    ("roundabout_entry", {}, "entering_roundabout", "entering_from_approach", "public_road_same_road"),
    ("circulating", {}, "circulating_roundabout", "already_inside_circulating", "unknown"),
    (
        "roundabout_exit",
        {"roundabout_exit_position": "on_exit_curve_before_straight_lane"},
        "exiting_roundabout",
        "exiting",
        "unknown",
    ),
    (
        "roundabout_exit",
        {"roundabout_exit_position": "reached_straight_lane_of_exit_road", "turn_direction": "left"},
        "left_turn",
        "not_in_roundabout",
        "public_road_same_road",
    ),
    (
        "roundabout_exit",
        {"roundabout_exit_position": "reached_straight_lane_of_exit_road", "turn_direction": "straight"},
        "straight",
        "not_in_roundabout",
        "public_road_same_road",
    ),
    (
        "roundabout_exit",
        {"roundabout_exit_position": "reached_straight_lane_of_exit_road", "turn_direction": "unknown"},
        "unknown",
        "not_in_roundabout",
        "public_road_same_road",
    ),
    ("roundabout_exit", {"roundabout_exit_position": "unknown"}, "exiting_roundabout", "unknown", "unknown"),
    ("roundabout_exit", {"roundabout_exit_position": "not_applicable"}, "exiting_roundabout", "unknown", "unknown"),
    ("lane_change", {"turn_direction": "left"}, "lane_change_left", "not_in_roundabout", "public_road_same_road"),
    ("lane_change", {"turn_direction": "right"}, "lane_change_right", "not_in_roundabout", "public_road_same_road"),
    ("lane_change", {"turn_direction": "other"}, "unknown", "not_in_roundabout", "public_road_same_road"),
    ("lane_change", {"turn_direction": "straight"}, "unknown", "not_in_roundabout", "public_road_same_road"),
    (
        "lane_change",
        {
            "turn_direction": "left",
            "road_class_on_approach": "roundabout",
            "road_class_basis": "roundabout_carriageway",
        },
        "lane_change_left",
        "already_inside_circulating",
        "public_road_same_road",
    ),
    ("u_turn", {}, "u_turn", "not_in_roundabout", "unknown"),
    (
        "u_turn",
        {"road_class_on_approach": "roundabout", "road_class_basis": "roundabout_carriageway"},
        "u_turn",
        "unknown",
        "unknown",
    ),
    ("crossing_on_foot", {"party_type": "pedestrian"}, "crossing_on_foot", "not_in_roundabout", "unknown"),
    ("other", {"manoeuvre_other": "reversing"}, "other", "unknown", "unknown"),
    ("unknown", {}, "unknown", "unknown", "unknown"),
]


@pytest.mark.parametrize(("manoeuvre", "overrides", "movement", "state", "origin"), TABLE)
def test_mapping_table_row_by_row(
    manoeuvre: str, overrides: dict[str, Any], movement: str, state: str, origin: str
) -> None:
    derived = derive_road_facts(checklist_row(manoeuvre=manoeuvre, **overrides))
    assert (derived.movement, derived.roundabout_entry_state, derived.entry_origin) == (movement, state, origin)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"party_type": "sedan"}, "car"),
        ({"party_type": "suv"}, "car"),
        ({"party_type": "hatchback"}, "car"),
        ({"party_type": "car_body_unclear"}, "car"),
        ({"party_type": "van"}, "car"),
        ({"party_type": "pickup"}, "car"),
        ({"party_type": "truck"}, "truck_or_heavy"),
        ({"party_type": "bus"}, "bus"),
        ({"party_type": "motorcycle"}, "motorcycle"),
        ({"party_type": "bicycle"}, "bicycle_or_personal_transport"),
        ({"party_type": "pedestrian"}, "pedestrian"),
        ({"party_type": "other", "party_type_other": "tractor"}, "other"),
        ({"party_type": "unknown"}, "unknown"),
        ({"party_type": "sedan", "marking": "taxi"}, "taxi"),
        ({"party_type": "van", "marking": "taxi"}, "taxi"),
        ({"party_type": "truck", "marking": "taxi"}, "truck_or_heavy"),
        ({"party_type": "sedan", "marking": "emergency_or_official"}, "emergency_or_official_marked"),
        ({"party_type": "truck", "marking": "emergency_or_official"}, "emergency_or_official_marked"),
        ({"party_type": "motorcycle", "marking": "emergency_or_official"}, "emergency_or_official_marked"),
        ({"party_type": "sedan", "marking": "none_seen"}, "car"),
        ({"party_type": "sedan", "marking": "unknown"}, "car"),
    ],
)
def test_party_type_mapping(overrides: dict[str, Any], expected: str) -> None:
    assert derive_party_type(checklist_row(**overrides)) == expected


def test_derived_facts_are_cited_to_the_checklist_moments_with_row_confidence() -> None:
    facts = parse(
        answer(
            rows=[
                row("P1", attributes_seen_at_seconds=1.2, movement_seen_at_seconds=2.4, confidence=0.7),
                row("P2", party_type="suv", colour_family="red"),
            ]
        )
    )
    mine = {f.field: f for f in facts.structured_facts if f.subject == "P1 silver sedan"}
    assert set(mine) == {"party_type", "movement", "roundabout_entry_state", "entry_origin"}
    assert mine["party_type"].timestamp_seconds == 1.2
    assert all(mine[f].timestamp_seconds == 2.4 for f in ("movement", "roundabout_entry_state", "entry_origin"))
    assert all(f.confidence == 0.7 and f.view_id == VIEW for f in mine.values())


def test_interior_view_derives_no_road_facts_but_keeps_parties_and_names() -> None:
    facts = parse(
        answer(
            rows=[row("CAM", party_type="pedestrian", colour_family="unknown", manoeuvre="unknown")],
            contacts=[],
            observation_notes=[],
            incident_assessment=assessment(
                camera_role="interior_view",
                contact_state="no_contact_observed",
                contact_interval_start_seconds=None,
                contact_interval_end_seconds=None,
            ),
        ),
        role="interior",
    )
    assert facts.structured_facts == []
    assert [p.description for p in facts.parties] == ["camera vehicle (pedestrian)"]
    assert facts.party_checklist[0].display_name == "camera vehicle (pedestrian)"


def test_interior_view_still_rejects_model_road_facts() -> None:
    fact = {
        "subject": "P1",
        "field": "motion_state_before_contact",
        "value": "moving",
        "other_party": "",
        "timestamp_seconds": 1.0,
        "confidence": 0.9,
    }
    with pytest.raises(AIConnectionError):
        parse(answer(structured_facts=[fact]), role="interior")


# --- candidate collisions built from typed contacts ---------------------------------------------------------


def test_the_contact_list_becomes_one_candidate_with_names_time_and_confidence() -> None:
    facts = parse(answer())
    [candidate] = facts.candidate_collisions
    assert candidate.description == "P1 silver sedan: vehicle contact vehicle (P2 red SUV)"
    assert candidate.timestamp_seconds == 3.0 and candidate.view_id == VIEW and candidate.confidence == 0.9


def test_a_pair_reported_both_ways_within_the_match_limit_is_one_candidate() -> None:
    facts = parse(
        answer(
            contacts=[
                contact("P1", "P2", at=3.0, confidence=0.7),
                contact("P2", "P1", at=3.9, confidence=0.95),
            ]
        )
    )
    [candidate] = facts.candidate_collisions
    assert candidate.timestamp_seconds == 3.0 and candidate.confidence == 0.95


def test_a_pair_reported_both_ways_exactly_one_second_apart_is_one_candidate() -> None:
    facts = parse(answer(contacts=[contact("P1", "P2", at=3.0), contact("P2", "P1", at=4.0)]))
    assert [c.timestamp_seconds for c in facts.candidate_collisions] == [3.0]


def test_the_same_pair_more_than_one_second_apart_is_two_candidates() -> None:
    facts = parse(answer(contacts=[contact("P1", "P2", at=3.0), contact("P2", "P1", at=4.01)]))
    assert [c.timestamp_seconds for c in facts.candidate_collisions] == [3.0, 4.01]


def test_a_contact_and_an_overturn_one_and_a_half_seconds_later_are_two_candidates() -> None:
    facts = parse(
        answer(
            contacts=[
                contact("P1", "P2", at=3.0),
                contact("P1", "", at=4.5, kind="vehicle_overturned"),
            ]
        )
    )
    assert [c.timestamp_seconds for c in facts.candidate_collisions] == [3.0, 4.5]


def test_the_same_subject_and_thing_merge_but_a_different_thing_does_not() -> None:
    facts = parse(
        answer(
            contacts=[
                contact("P1", "a lamp post", at=3.0, kind="contact_other"),
                contact("P1", "A  lamp post", at=3.5, kind="contact_other"),
                contact("P1", "a barrier", at=3.5, kind="contact_other"),
            ]
        )
    )
    assert len(facts.candidate_collisions) == 2


def test_an_obscured_contact_gets_the_touch_not_clearly_visible_suffix() -> None:
    facts = parse(
        answer(
            contacts=[contact(visibility="possible_obscured")],
            incident_assessment=assessment(contact_state="contact_possible_obscured"),
        )
    )
    assert facts.candidate_collisions[0].description.endswith(" (touch not clearly visible)")
    plain = parse(answer())
    assert "touch not clearly visible" not in plain.candidate_collisions[0].description


def test_a_camera_contact_with_unknown_camera_role_is_shown_as_possible() -> None:
    facts = parse(
        answer(
            rows=[row("CAM"), row("P1", colour_family="red")],
            contacts=[contact("P1", "CAM")],
            incident_assessment=assessment(camera_role="unknown"),
        )
    )
    assert "touch not clearly visible" in facts.candidate_collisions[0].description
    involved = parse(
        answer(
            rows=[row("CAM"), row("P1", colour_family="red")],
            contacts=[contact("P1", "CAM")],
            incident_assessment=assessment(camera_role="camera_vehicle_involved"),
        )
    )
    assert "touch not clearly visible" not in involved.candidate_collisions[0].description


def test_aftermath_items_never_become_candidates() -> None:
    facts = parse(
        answer(
            contacts=[
                contact(),
                contact("P1", "", at=6.0, kind="vehicle_stopped_at_scene", visibility="visible"),
                contact("P2", "", at=7.0, kind="no_injury_visible", visibility="visible"),
            ]
        )
    )
    assert len(facts.candidate_collisions) == 1
    assert len(facts.collision_observations) == 3


# --- validation, storage and consumers ----------------------------------------------------------------------


def test_a_view_with_an_assessment_needs_no_separately_listed_candidate() -> None:
    facts = parse(answer(contacts=[contact("P1", "", at=6.0, kind="vehicle_stopped_at_scene")]))
    assert facts.candidate_collisions == []
    assert validate_citations(facts, [VIEW], view_durations={VIEW: 30.0}).is_valid


def test_older_versions_keep_the_every_typed_contact_has_a_candidate_check() -> None:
    old = OverviewFacts.model_validate(
        {
            "parties": [{"description": "silver sedan", "view_id": VIEW, "timestamp_seconds": 1.0, "confidence": 0.9}],
            "events": [],
            "candidate_collisions": [],
            "behaviour_observations": [],
            "location_conditions": [],
            "collision_observations": [
                {
                    "kind": "vehicle_overturned",
                    "subject": "silver sedan",
                    "other_party": "",
                    "injury_grade": "unknown",
                    "view_id": VIEW,
                    "timestamp_seconds": 2.0,
                    "confidence": 0.9,
                }
            ],
        }
    )
    assert not validate_citations(old, [VIEW], view_durations={VIEW: 30.0}).is_valid


def test_report_facts_round_trip_through_json() -> None:
    facts = parse(answer())
    again = OverviewFacts.model_validate_json(facts.model_dump_json())
    assert again == facts


def test_situation_carries_the_road_class_fields_with_citations() -> None:
    facts = parse(
        answer(
            rows=[
                row(
                    "P1",
                    road_class_on_approach="side_road",
                    road_class_basis="short_or_ends_at_junction",
                    roundabout_exit_position="unknown",
                    movement_seen_at_seconds=2.0,
                ),
                row("P2", colour_family="red"),
            ]
        )
    )
    situation = situation_from_overview(facts)
    first = next(p for p in situation.parties if p.description == "P1 silver sedan")
    assert first.road_class_on_approach.value == "side_road"
    assert first.road_class_basis.value == "short_or_ends_at_junction"
    assert first.roundabout_exit_position.value == "unknown"
    [evidence] = first.road_class_on_approach.evidence
    assert (evidence.view_id, evidence.timestamp_seconds) == (VIEW, 2.0)
    assert first.movement.value == "straight"


def test_older_party_situations_default_the_new_fields_to_unknown() -> None:
    old = OverviewFacts.model_validate(
        {
            "parties": [{"description": "silver sedan", "view_id": VIEW, "timestamp_seconds": 1.0, "confidence": 0.9}],
            "events": [],
            "candidate_collisions": [],
            "behaviour_observations": [],
            "location_conditions": [],
        }
    )
    party = situation_from_overview(old).parties[0]
    assert party.road_class_on_approach.value == "unknown" and party.roundabout_exit_position.value == "unknown"


def test_dense_consolidation_keeps_the_overview_10_records() -> None:
    facts = parse(answer())
    dense_like = OverviewFacts(
        parties=[],
        events=[CitedFact(description="dense", view_id=VIEW, timestamp_seconds=3.0, confidence=0.9)],
        candidate_collisions=[
            CitedFact(description="dense contact", view_id=VIEW, timestamp_seconds=3.0, confidence=0.9)
        ],
        behaviour_observations=[],
        location_conditions=[],
    )
    merged = consolidate_facts(facts, [dense_like])
    assert merged.party_checklist == facts.party_checklist
    assert merged.incident_assessments == facts.incident_assessments
    assert merged.observation_notes == facts.observation_notes
    assert [p.party_code for p in merged.parties] == ["P1", "P2"]


# --- multi-view: a code is per view, never an identity ------------------------------------------------------


def test_the_same_code_in_two_views_is_never_the_same_party_because_of_the_code() -> None:
    first = parse(answer(), view=VIEW)
    second = parse(
        answer(
            rows=[
                row("P1", colour_family="blue", party_type="van"),
                row("P2", colour_family="black", party_type="bus"),
            ],
            contacts=[contact()],
        ),
        view=OTHER_VIEW,
    )
    both = first.model_copy(
        update={
            "parties": [*first.parties, *second.parties],
            "party_checklist": [*first.party_checklist, *second.party_checklist],
            "structured_facts": [*first.structured_facts, *second.structured_facts],
        }
    )
    identities = party_identities(both)
    assert len(identities.sources) == 4
    keys = {identities.key(p.description, p.view_id) for p in both.parties}
    assert len(keys) == 4
    assert all("P1" not in key[0] for key in keys)


def test_parties_without_both_colour_and_body_keep_view_local_keys() -> None:
    unknown = {"colour_family": "unknown", "party_type": "unknown"}
    first = parse(
        answer(rows=[row("P1", **unknown), row("P2", **unknown)], contacts=[], observation_notes=[]), view=VIEW
    )
    second = parse(
        answer(rows=[row("P1", **unknown), row("P2", **unknown)], contacts=[], observation_notes=[]), view=OTHER_VIEW
    )
    both = first.model_copy(
        update={
            "parties": [*first.parties, *second.parties],
            "structured_facts": [*first.structured_facts, *second.structured_facts],
        }
    )
    identities = party_identities(both)
    assert len(identities.sources) == 4
    assert len({identities.key(p.description, p.view_id) for p in both.parties}) == 4


def test_attribute_matching_across_views_still_ignores_the_code() -> None:
    first = parse(answer(), view=VIEW)
    second = parse(
        answer(
            rows=[
                row("P3", colour_family="silver"),
                row("P1", colour_family="red", party_type="suv", manoeuvre="circulating"),
                row("P2", colour_family="blue", party_type="bus"),
            ],
            contacts=[],
            observation_notes=[],
        ),
        view=OTHER_VIEW,
    )
    silver_a = next(p for p in first.parties if p.color == "silver")
    silver_b = next(p for p in second.parties if p.color == "silver")
    assert silver_a.party_code != silver_b.party_code
    both = first.model_copy(update={"parties": [*first.parties, *second.parties]})
    identities = party_identities(both)
    assert identities.key(silver_a.description, silver_a.view_id) == identities.key(
        silver_b.description, silver_b.view_id
    )
