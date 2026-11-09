"""Tests for Seam 3, the fault rule engine over a typed Situation (RPT-15 round 2).

Rules under test (Federal Decree-Law No. 14 of 2024, https://uaelegislation.gov.ae/en/legislations/2598):
- Art. 6: emergency or official vehicle, main road over secondary road, equal roads (traffic from
  the left), roundabout (circulating traffic)
- Art. 5(1)(f): the driver must stop for a pedestrian who has started crossing at a crosswalk
- Art. 7(4): a pedestrian crossing a road with a posted limit above 80 km/h bears liability

The engine decides only from explicit Situation fields. Any needed field that is unknown gives
Cannot Determine with a reason naming it. Prose is never read: the adapter from today's prose-only
OverviewFacts leaves every field unknown, so live reports are Cannot Determine.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from incident_report.facts import CitedFact, OverviewFacts, PartyFact
from incident_report.rules import (
    ALL_RULES,
    FAULT_STATUS_COLORS,
    RULE_ART_5_1_F_CROSSWALK,
    RULE_ART_6_EMERGENCY,
    RULE_ART_6_PRIORITY_LEFT_EQUAL,
    RULE_ART_6_ROUNDABOUT,
    RULE_ART_7_4_PEDESTRIAN_SPEED,
    FaultStatus,
    JunctionControl,
    Observed,
    PartyFault,
    PartySituation,
    Rule,
    Situation,
    Tri,
    evaluate_fault,
    evaluate_situation,
    situation_from_overview,
)
from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD
from incident_report.rules.mr130_1997 import CATALOGUE_RULES, MR_RULES, RULE_ENTRY

AT = FaultStatus.AT_FAULT
NOT = FaultStatus.NOT_AT_FAULT
CD = FaultStatus.CANNOT_DETERMINE

CAMRY = "silver Toyota Camry sedan"
PATROL = "blue Nissan Patrol SUV"
WALKER = "pedestrian in grey clothes"

COLLISION = CitedFact(description="collision observed", view_id="view-1", timestamp_seconds=2.5, confidence=0.95)


def known(value: Any, tag: str = "observed") -> Any:
    """A known value with a cited fact whose description is `tag` (so a test can find it again)."""
    fact = CitedFact(description=tag, view_id="view-1", timestamp_seconds=1.0, confidence=0.9)
    return Observed(value=value, evidence=(fact,))


def field(value: Any, tag: str) -> Any:
    """Known when a value is given, otherwise None (the model then keeps its unknown default)."""
    return known(value, tag) if value is not None else None


def party(
    name: str,
    *,
    party_type: str | None = "car",
    emergency: Tri | None = Tri.NO,
    entry_origin: str | None = None,
    circulating: Tri | None = None,
    from_left: Tri | None = None,
    crossing: Tri | None = None,
) -> PartySituation:
    values: dict[str, Any] = {
        "party_type": field(party_type, f"party_type of {name}"),
        "entry_origin": field(entry_origin, f"entry_origin of {name}"),
        "approach_control_faced": known("none_confirmed", f"approach_control_faced of {name}"),
    }
    movement = (
        "straight" if entry_origin == "public_road_same_road" or from_left is not None else "entering_from_side_road"
    )
    if emergency != Tri.NO and party_type == "car":
        values["visible_vehicle_markings"] = known("police", f"visible_vehicle_markings of {name}")
        values["party_type"] = known("emergency_or_official_marked", f"party_type of {name}")
        values["emergency_lights"] = field("yes" if emergency == Tri.YES else None, f"emergency_lights of {name}")
        values["emergency_siren"] = field("yes" if emergency == Tri.YES else None, f"emergency_siren of {name}")
    if circulating is not None:
        values["roundabout_entry_state"] = known(
            "already_inside_circulating" if circulating == Tri.YES else "entering_from_approach",
            f"roundabout_entry_state of {name}",
        )
        movement = "circulating_roundabout" if circulating == Tri.YES else "entering_roundabout"
    if party_type == "pedestrian":
        movement = "crossing_on_foot"
        values["pedestrian_crossing_place"] = field(
            "marked_crossing" if crossing == Tri.YES else "outside_any_crossing" if crossing == Tri.NO else None,
            f"pedestrian_crossing_place of {name}",
        )
        values["pedestrian_signal_state"] = known("no_pedestrian_signal", f"pedestrian_signal_state of {name}")
        values["pedestrian_started_before_vehicle_arrived"] = known(
            "yes", f"pedestrian_started_before_vehicle_arrived of {name}"
        )
    values["movement"] = known(movement, f"movement of {name}")
    other = PATROL if name == CAMRY else CAMRY
    if from_left is not None:
        values["relative_position_of_other"] = {
            other: known(
                "B_from_A_right" if from_left == Tri.YES else "B_from_A_left", f"relative_position_of_other of {name}"
            )
        }
    elif circulating is not None:
        values["relative_position_of_other"] = {
            other: known(
                "B_from_A_right" if circulating == Tri.YES else "B_from_A_left", f"relative_position_of_other of {name}"
            )
        }
    return PartySituation(description=name, **{k: v for k, v in values.items() if v is not None})


def situation(
    *parties: PartySituation,
    control: JunctionControl | None = JunctionControl.UNCONTROLLED,
    conflict: Tri | None = Tri.YES,
    speed: int | None = None,
    collisions: tuple[CitedFact, ...] = (COLLISION,),
) -> Situation:
    rewritten = list(parties)
    origins = [p.entry_origin.value for p in parties]
    if len(parties) == 2 and (
        set(origins) == {"public_road_same_road", "side_road"}
        or any(p.party_type.value in {"pedestrian", "emergency_or_official_marked"} for p in parties)
    ):
        for i, p in enumerate(rewritten):
            rewritten[i] = p.model_copy(
                update={
                    "relative_position_of_other": {
                        parties[1 - i].description: known(
                            "B_from_A_left" if i == 0 else "B_from_A_right",
                            f"relative_position_of_other of {p.description}",
                        )
                    }
                }
            )
    for i, p in enumerate(rewritten):
        changes: dict[str, Any] = {}
        if control == JunctionControl.SIGNAL:
            changes["approach_control_faced"] = known("signal", f"approach_control_faced of {p.description}")
        if control == JunctionControl.ROUNDABOUT and p.roundabout_entry_state.value == "unknown":
            changes["movement"] = known("entering_roundabout", f"movement of {p.description}")
        if conflict != Tri.YES:
            changes["movement"] = known("other" if conflict == Tri.NO else "unknown", f"movement of {p.description}")
        rewritten[i] = p.model_copy(update=changes)
    zone = (
        None
        if control is None
        else "signalised_junction"
        if control == JunctionControl.SIGNAL
        else "roundabout_unsignalised"
        if control == JunctionControl.ROUNDABOUT
        else "unsignalised_junction"
    )
    return Situation(
        parties=tuple(rewritten),
        collisions=collisions,
        conflict_zone_type=known(zone or "unknown", "conflict_zone_type"),
        officer_directing_traffic=known("no", "officer_directing_traffic"),
        posted_speed_limit_kmh=known(speed if speed is not None else "unknown", "posted_speed_limit_kmh"),
    )


def statuses(faults: tuple[PartyFault, ...]) -> tuple[FaultStatus, ...]:
    return tuple(f.status for f in faults)


def assert_decided(faults: tuple[PartyFault, ...], expected: tuple[FaultStatus, ...], rule: Rule) -> None:
    assert statuses(faults) == expected
    for f in faults:
        assert f.rule_id == rule.rule_id
        assert f.article == rule.article
        assert f.legal_source == rule.legal_source
        assert f.color == ("pink" if f.status == AT else "green")
        assert f.cited_facts, "every decision cites its facts"
        assert COLLISION in f.cited_facts


def assert_cannot_determine(faults: tuple[PartyFault, ...], reason_contains: str) -> None:
    assert len(faults) > 0
    for f in faults:
        assert f.status == CD
        assert f.color == "grey"
        assert f.rule_id is None
        assert reason_contains in f.reason, f"{reason_contains!r} not in {f.reason!r}"


# --- Art. 6, emergency or official vehicle -----------------------------------------------------------


@pytest.mark.parametrize(
    ("emergency_a", "emergency_b", "expected"),
    [
        pytest.param(Tri.YES, Tri.NO, (NOT, AT), id="first-is-emergency"),
        pytest.param(Tri.NO, Tri.YES, (AT, NOT), id="second-is-emergency"),
    ],
)
def test_art_6_emergency_vehicle_has_priority(
    emergency_a: Tri, emergency_b: Tri, expected: tuple[FaultStatus, ...]
) -> None:
    sit = situation(party(CAMRY, emergency=emergency_a), party(PATROL, emergency=emergency_b))
    assert_decided(evaluate_situation(sit), expected, RULE_ART_6_EMERGENCY)


def test_art_6_emergency_outranks_main_road() -> None:
    """The emergency vehicle comes first even when it was on the secondary road."""
    sit = situation(
        party(CAMRY, emergency=Tri.YES, entry_origin="side_road"),
        party(PATROL, emergency=Tri.NO, entry_origin="public_road_same_road"),
    )
    assert_decided(evaluate_situation(sit), (NOT, AT), RULE_ART_6_EMERGENCY)


def test_art_6_emergency_applies_at_a_roundabout() -> None:
    sit = situation(
        party(CAMRY, emergency=Tri.NO, circulating=Tri.YES),
        party(PATROL, emergency=Tri.YES, circulating=Tri.NO),
        control=JunctionControl.ROUNDABOUT,
    )
    assert_decided(evaluate_situation(sit), (AT, NOT), RULE_ART_6_EMERGENCY)


def test_art_6_both_emergency_is_cannot_determine() -> None:
    sit = situation(party(CAMRY, emergency=Tri.YES), party(PATROL, emergency=Tri.YES))
    assert_cannot_determine(evaluate_situation(sit), "Both parties are emergency")


@pytest.mark.parametrize("unknown_party", [0, 1])
def test_art_6_emergency_unknown_is_cannot_determine(unknown_party: int) -> None:
    parties = [party(CAMRY, emergency=Tri.NO), party(PATROL, emergency=Tri.NO)]
    name = (CAMRY, PATROL)[unknown_party]
    parties[unknown_party] = party(name, emergency=None, entry_origin="public_road_same_road")
    assert_cannot_determine(evaluate_situation(situation(*parties)), f"emergency_siren of {name}")


# --- Art. 6, main road over secondary road ------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin_a", "origin_b", "expected"),
    [
        pytest.param("public_road_same_road", "side_road", (NOT, AT), id="first-on-main"),
        pytest.param("side_road", "public_road_same_road", (AT, NOT), id="second-on-main"),
    ],
)
def test_art_6_main_road_has_priority(origin_a: str, origin_b: str, expected: tuple[FaultStatus, ...]) -> None:
    sit = situation(party(CAMRY, entry_origin=origin_a), party(PATROL, entry_origin=origin_b))
    assert_decided(evaluate_situation(sit), expected, RULE_ENTRY)


@pytest.mark.parametrize(
    ("origin_a", "origin_b"),
    [
        pytest.param("public_road_same_road", "public_road_same_road", id="main-main"),
        pytest.param("side_road", "side_road", id="secondary-secondary"),
        pytest.param("public_road_same_road", "public_road_same_road", id="main-equal"),
        pytest.param("public_road_same_road", "public_road_same_road", id="equal-main"),
        pytest.param("side_road", "public_road_same_road", id="secondary-equal"),
        pytest.param("public_road_same_road", "side_road", id="equal-secondary"),
    ],
)
def test_art_6_unpaired_road_classes_are_cannot_determine(origin_a: str, origin_b: str) -> None:
    sit = situation(party(CAMRY, entry_origin=origin_a), party(PATROL, entry_origin=origin_b))
    # The original legal-rank-only scenario supplied no observed relative paths.
    sit = sit.model_copy(
        update={"parties": tuple(p.model_copy(update={"relative_position_of_other": {}}) for p in sit.parties)}
    )
    assert_cannot_determine(evaluate_situation(sit), "relative_position_of_other")


@pytest.mark.parametrize(
    ("origin_a", "origin_b", "unknown_name"),
    [
        pytest.param(None, "public_road_same_road", CAMRY, id="first-unknown"),
        pytest.param("side_road", None, PATROL, id="second-unknown"),
    ],
)
def test_art_6_main_road_unknown_road_class_is_cannot_determine(
    origin_a: str | None, origin_b: str | None, unknown_name: str
) -> None:
    sit = situation(party(CAMRY, entry_origin=origin_a), party(PATROL, entry_origin=origin_b))
    assert_cannot_determine(evaluate_situation(sit), f"entry_origin of {unknown_name}")


def test_art_6_both_road_classes_unknown_names_both() -> None:
    faults = evaluate_situation(situation(party(CAMRY), party(PATROL)))
    assert_cannot_determine(faults, f"entry_origin of {CAMRY}")
    assert_cannot_determine(faults, f"entry_origin of {PATROL}")


# --- Art. 6, equal roads: traffic from the left --------------------------------------------------------


@pytest.mark.parametrize(
    ("left_a", "left_b", "expected"),
    [
        pytest.param(Tri.YES, Tri.NO, (NOT, AT), id="first-from-the-left"),
        pytest.param(Tri.NO, Tri.YES, (AT, NOT), id="second-from-the-left"),
    ],
)
def test_art_6_equal_roads_priority_to_traffic_from_the_left(
    left_a: Tri, left_b: Tri, expected: tuple[FaultStatus, ...]
) -> None:
    sit = situation(
        party(CAMRY, entry_origin="public_road_same_road", from_left=left_a),
        party(PATROL, entry_origin="public_road_same_road", from_left=left_b),
    )
    assert_decided(evaluate_situation(sit), expected, RULE_ART_6_PRIORITY_LEFT_EQUAL)


@pytest.mark.parametrize(
    ("left_a", "left_b", "reason"),
    [
        pytest.param(Tri.YES, Tri.YES, "relative_position_of_other", id="both-from-the-left"),
        pytest.param(Tri.NO, Tri.NO, "relative_position_of_other", id="neither-from-the-left"),
    ],
)
def test_art_6_equal_roads_no_priority_is_cannot_determine(left_a: Tri, left_b: Tri, reason: str) -> None:
    sit = situation(
        party(CAMRY, entry_origin="public_road_same_road", from_left=left_a),
        party(PATROL, entry_origin="public_road_same_road", from_left=left_b),
    )
    assert_cannot_determine(evaluate_situation(sit), reason)


@pytest.mark.parametrize(
    ("left_a", "left_b", "unknown_name"),
    [
        pytest.param(None, Tri.NO, CAMRY, id="first-unknown"),
        pytest.param(Tri.YES, None, PATROL, id="second-unknown"),
    ],
)
def test_art_6_equal_roads_unknown_direction_is_cannot_determine(
    left_a: Tri | None, left_b: Tri | None, unknown_name: str
) -> None:
    sit = situation(
        party(CAMRY, entry_origin="public_road_same_road", from_left=left_a),
        party(PATROL, entry_origin="public_road_same_road", from_left=left_b),
    )
    assert_cannot_determine(evaluate_situation(sit), f"relative_position_of_other of {unknown_name}")


# --- Art. 6, roundabout: circulating traffic -----------------------------------------------------------


@pytest.mark.parametrize(
    ("circ_a", "circ_b", "expected"),
    [
        pytest.param(Tri.YES, Tri.NO, (NOT, AT), id="first-circulating"),
        pytest.param(Tri.NO, Tri.YES, (AT, NOT), id="second-circulating"),
    ],
)
def test_art_6_roundabout_priority_to_circulating_traffic(
    circ_a: Tri, circ_b: Tri, expected: tuple[FaultStatus, ...]
) -> None:
    sit = situation(
        party(CAMRY, circulating=circ_a),
        party(PATROL, circulating=circ_b),
        control=JunctionControl.ROUNDABOUT,
    )
    assert_decided(evaluate_situation(sit), expected, RULE_ART_6_ROUNDABOUT)


@pytest.mark.parametrize(
    ("circ_a", "circ_b", "reason"),
    [
        pytest.param(Tri.YES, Tri.YES, "both or neither circulating", id="both-circulating"),
        pytest.param(Tri.NO, Tri.NO, "both or neither circulating", id="neither-circulating"),
    ],
)
def test_art_6_roundabout_no_priority_is_cannot_determine(circ_a: Tri, circ_b: Tri, reason: str) -> None:
    sit = situation(
        party(CAMRY, circulating=circ_a),
        party(PATROL, circulating=circ_b),
        control=JunctionControl.ROUNDABOUT,
    )
    assert_cannot_determine(evaluate_situation(sit), reason)


@pytest.mark.parametrize(
    ("circ_a", "circ_b", "unknown_name"),
    [
        pytest.param(None, Tri.NO, CAMRY, id="first-unknown"),
        pytest.param(Tri.YES, None, PATROL, id="second-unknown"),
    ],
)
def test_art_6_roundabout_unknown_circulation_is_cannot_determine(
    circ_a: Tri | None, circ_b: Tri | None, unknown_name: str
) -> None:
    sit = situation(
        party(CAMRY, circulating=circ_a),
        party(PATROL, circulating=circ_b),
        control=JunctionControl.ROUNDABOUT,
    )
    assert_cannot_determine(evaluate_situation(sit), f"roundabout_entry_state of {unknown_name}")


def test_art_6_roundabout_does_not_need_road_class() -> None:
    """Fields a branch does not use may stay unknown (road class is irrelevant at a roundabout)."""
    sit = situation(
        party(CAMRY, entry_origin=None, circulating=Tri.YES),
        party(PATROL, entry_origin=None, circulating=Tri.NO),
        control=JunctionControl.ROUNDABOUT,
    )
    assert_decided(evaluate_situation(sit), (NOT, AT), RULE_ART_6_ROUNDABOUT)


# --- Art. 6 scope: junction control and conflict type --------------------------------------------------


def test_art_6_signal_controlled_junction_is_cannot_determine() -> None:
    sit = situation(
        party(CAMRY, entry_origin="public_road_same_road"),
        party(PATROL, entry_origin="side_road"),
        control=JunctionControl.SIGNAL,
    )
    assert_cannot_determine(evaluate_situation(sit), "signal_state_when_crossing_stop_line")


def test_art_6_unknown_junction_control_is_cannot_determine() -> None:
    sit = situation(
        party(CAMRY, entry_origin="public_road_same_road"), party(PATROL, entry_origin="side_road"), control=None
    )
    assert_cannot_determine(evaluate_situation(sit), "conflict_zone_type")


def test_art_6_not_a_right_of_way_conflict_is_cannot_determine() -> None:
    """A rear-end or lane-change collision is not a priority conflict, even on a main road versus a side road."""
    sit = situation(
        party(CAMRY, entry_origin="public_road_same_road"), party(PATROL, entry_origin="side_road"), conflict=Tri.NO
    )
    assert_cannot_determine(evaluate_situation(sit), "Uncovered typed-field combination")


def test_art_6_unknown_conflict_type_is_cannot_determine() -> None:
    sit = situation(
        party(CAMRY, entry_origin="public_road_same_road"), party(PATROL, entry_origin="side_road"), conflict=None
    )
    assert_cannot_determine(evaluate_situation(sit), "movement")


# --- Art. 5(1)(f) and Art. 7(4): pedestrians -----------------------------------------------------------


@pytest.mark.parametrize("walker_first", [True, False])
@pytest.mark.parametrize("speed", [30, 80])
def test_art_5_1_f_driver_must_stop_for_pedestrian_at_crosswalk(walker_first: bool, speed: int) -> None:
    walker = party(WALKER, party_type="pedestrian", emergency=None, crossing=Tri.YES)
    driver = party(CAMRY)
    parties = (walker, driver) if walker_first else (driver, walker)
    expected = (NOT, AT) if walker_first else (AT, NOT)
    if speed == 80:
        assert_cannot_determine(evaluate_situation(situation(*parties, speed=speed)), "exactly 80")
    else:
        assert_decided(evaluate_situation(situation(*parties, speed=speed)), expected, RULE_ART_5_1_F_CROSSWALK)


@pytest.mark.parametrize("walker_first", [True, False])
@pytest.mark.parametrize("speed", [81, 100, 120])
def test_art_7_4_pedestrian_bears_liability_above_80_kmh(walker_first: bool, speed: int) -> None:
    walker = party(WALKER, party_type="pedestrian", emergency=None, crossing=Tri.NO)
    driver = party(CAMRY)
    parties = (walker, driver) if walker_first else (driver, walker)
    expected = (AT, NOT) if walker_first else (NOT, AT)
    assert_decided(evaluate_situation(situation(*parties, speed=speed)), expected, RULE_ART_7_4_PEDESTRIAN_SPEED)


def test_art_7_4_limit_of_exactly_80_is_not_above_80() -> None:
    walker = party(WALKER, party_type="pedestrian", emergency=None, crossing=Tri.NO)
    faults = evaluate_situation(situation(walker, party(CAMRY), speed=80))
    assert_cannot_determine(faults, "exactly 80")


def test_pedestrian_at_crosswalk_on_road_above_80_is_cannot_determine() -> None:
    """Both rules apply with opposite outcomes and no source orders them (ADR 0006)."""
    walker = party(WALKER, party_type="pedestrian", emergency=None, crossing=Tri.YES)
    faults = evaluate_situation(situation(walker, party(CAMRY), speed=100))
    assert_cannot_determine(faults, "would give opposite outcomes")


@pytest.mark.parametrize(
    ("crossing", "speed", "reason"),
    [
        pytest.param(None, 50, f"pedestrian_crossing_place of {WALKER}", id="crosswalk-unknown"),
        pytest.param(Tri.YES, None, "posted_speed_limit_kmh", id="limit-unknown-at-crosswalk"),
        pytest.param(Tri.NO, None, "posted_speed_limit_kmh", id="limit-unknown-off-crosswalk"),
        pytest.param(None, 100, f"pedestrian_crossing_place of {WALKER}", id="crosswalk-unknown-fast"),
    ],
)
def test_pedestrian_rules_unknown_field_is_cannot_determine(
    crossing: Tri | None, speed: int | None, reason: str
) -> None:
    walker = party(WALKER, party_type="pedestrian", emergency=None, crossing=crossing)
    assert_cannot_determine(evaluate_situation(situation(walker, party(CAMRY), speed=speed)), reason)


# --- who the parties are, and how many -----------------------------------------------------------------


@pytest.mark.parametrize("unknown_party", [0, 1])
def test_unknown_party_kind_is_cannot_determine(unknown_party: int) -> None:
    parties = [party(CAMRY, entry_origin="public_road_same_road"), party(PATROL, entry_origin="side_road")]
    name = (CAMRY, PATROL)[unknown_party]
    parties[unknown_party] = party(name, party_type=None, entry_origin="public_road_same_road")
    assert_cannot_determine(evaluate_situation(situation(*parties)), f"party_type of {name}")


@pytest.mark.parametrize(
    ("party_type_a", "party_type_b"),
    [
        pytest.param("pedestrian", "pedestrian", id="two-pedestrians"),
        pytest.param("bicycle_or_personal_transport", "car", id="cyclist-vehicle"),
        pytest.param("car", "other", id="vehicle-animal"),
        pytest.param("pedestrian", "bicycle_or_personal_transport", id="pedestrian-cyclist"),
    ],
)
def test_uncovered_kind_pairs_are_cannot_determine(party_type_a: str, party_type_b: str) -> None:
    sit = situation(
        party(CAMRY, party_type=party_type_a, entry_origin="public_road_same_road"),
        party(PATROL, party_type=party_type_b, entry_origin="side_road"),
    )
    assert_cannot_determine(evaluate_situation(sit), "party_type pairing is not covered")


def test_cannot_determine_no_collision() -> None:
    sit = situation(
        party(CAMRY, entry_origin="public_road_same_road"), party(PATROL, entry_origin="side_road"), collisions=()
    )
    faults = evaluate_situation(sit)
    assert len(faults) == 2
    for f in faults:
        assert f.status == CD
        assert f.color == "grey"
        assert "no collision" in f.reason.lower()


def test_multi_collision_is_cannot_determine() -> None:
    second = CitedFact(description="second impact", view_id="view-1", timestamp_seconds=3.5, confidence=0.9)
    sit = situation(
        party(CAMRY, entry_origin="public_road_same_road"),
        party(PATROL, entry_origin="side_road"),
        collisions=(COLLISION, second),
    )
    assert_cannot_determine(evaluate_situation(sit), "Multi-collision incident")


def test_cannot_determine_multi_party_chain() -> None:
    sit = situation(
        party(CAMRY, entry_origin="public_road_same_road"),
        party(PATROL, entry_origin="side_road"),
        party("white delivery van", entry_origin="side_road"),
    )
    faults = evaluate_situation(sit)
    assert len(faults) == 3
    for f in faults:
        assert f.status == CD
        assert f.color == "grey"
        assert "multi-party" in f.reason.lower()


def test_one_party_is_cannot_determine() -> None:
    assert_cannot_determine(evaluate_situation(situation(party(CAMRY))), "found 1")


def test_no_parties_gives_no_faults() -> None:
    assert evaluate_situation(situation()) == ()


# --- traceability and determinism ---------------------------------------------------------------------


def test_a_decision_cites_the_facts_of_every_field_it_used() -> None:
    sit = situation(party(CAMRY, entry_origin="public_road_same_road"), party(PATROL, entry_origin="side_road"))
    for fault in evaluate_situation(sit):
        cited = {f.description for f in fault.cited_facts}
        assert {
            "collision observed",
            "conflict_zone_type",
            "officer_directing_traffic",
            f"entry_origin of {CAMRY}",
            f"entry_origin of {PATROL}",
            f"movement of {CAMRY}",
            f"movement of {PATROL}",
        } <= cited


def test_a_known_value_must_cite_a_fact() -> None:
    with pytest.raises(ValidationError, match="must cite at least one fact"):
        Observed(value="public_road_same_road")
    with pytest.raises(ValidationError, match="must cite at least one fact"):
        Observed(value=100)
    assert not Observed(value="unknown").is_known
    assert not Observed(value=None).is_known


def test_determinism_same_input_same_output() -> None:
    sit = situation(party(CAMRY, entry_origin="public_road_same_road"), party(PATROL, entry_origin="side_road"))
    first = evaluate_situation(sit)
    for _ in range(50):
        assert evaluate_situation(sit) == first


def test_rules_are_data_with_official_source_and_article() -> None:
    expected = {
        RULE_ART_6_EMERGENCY,
        RULE_ART_6_MAIN_ROAD,
        RULE_ART_6_PRIORITY_LEFT_EQUAL,
        RULE_ART_6_ROUNDABOUT,
        RULE_ART_5_1_F_CROSSWALK,
        RULE_ART_7_4_PEDESTRIAN_SPEED,
        *MR_RULES,
        *CATALOGUE_RULES,
    }
    assert len(ALL_RULES) == len(expected)
    assert len({r.rule_id for r in ALL_RULES}) == len(expected)
    assert set(ALL_RULES) == expected
    assert {r.article for r in ALL_RULES} == {
        "Art. 6",
        "Art. 5(1)(f)",
        "Art. 7(4)",
        "MR 130/1997 Art. 25(1)",
        "MR 130/1997 Art. 1(3), 27",
        "MR 130/1997 Art. 43/60",
        "MR 130/1997 Art. 58-59",
        "MR 130/1997 Art. 50",
        "MR 130/1997 Art. 59(5), 59(2)",
        "MR 130/1997 Art. 27, 44(1); Decree-Law 14/2024 Art. 6",
        "MR 130/1997 Art. 44(1); Decree-Law 14/2024 Art. 6",
        "Art. 6; MR 130/1997 Art. 44(2) and Art. 43",
    }
    for rule in ALL_RULES:
        if rule in (*MR_RULES, *CATALOGUE_RULES):
            assert rule.legal_source == "MR 130/1997 (unofficial translation)"
            assert rule.source_url == "https://uaelegislation.gov.ae/ar/legislations/1020"
        else:
            assert rule.legal_source == "Federal Decree-Law No. 14 of 2024"
            assert rule.source_url == "https://uaelegislation.gov.ae/en/legislations/2598"


# --- no text parsing: today's prose-only facts ---------------------------------------------------------


def _facts(party_descriptions: list[str], event: str) -> OverviewFacts:
    def cited(text: str) -> CitedFact:
        return CitedFact(description=text, view_id="view-1", timestamp_seconds=2.0, confidence=0.9)

    return OverviewFacts(
        behaviour_observations=[],
        parties=[
            PartyFact(description=d, view_id="view-1", timestamp_seconds=1.0, confidence=0.9)
            for d in party_descriptions
        ],
        events=[cited(event)],
        candidate_collisions=[cited(f"Collision at an uncontrolled junction: {event}")],
        location_conditions=[cited("Uncontrolled junction, main road and secondary road")],
    )


def test_reviewer_probe_prose_is_never_guessed_into_a_fault() -> None:
    """Round 1 marked the white sedan At Fault here (the party named first was taken as the main-road party)."""
    facts = _facts(
        ["white sedan", "red SUV"],
        "The red SUV pulled out from the secondary road into the path of the white sedan on the main road",
    )
    faults = evaluate_fault(facts)
    assert [f.party_description for f in faults] == ["white sedan", "red SUV"]
    assert statuses(faults) == (CD, CD)
    for f in faults:
        assert f.color == "grey"
        assert f.rule_id is None
        assert "unknown" in f.reason


@pytest.mark.parametrize(
    "event",
    [
        pytest.param("The ambulance with sirens on entered the junction", id="emergency-words"),
        pytest.param("The pedestrian started crossing at the crosswalk", id="crosswalk-words"),
        pytest.param(
            "The sedan was circulating in the roundabout and the SUV entered from the left", id="roundabout-words"
        ),
        pytest.param("Equal roads: the SUV approached from the left of the sedan", id="equal-roads-words"),
        pytest.param("Traffic lights were red for the SUV; speed limit 100 km/h", id="signal-and-speed-words"),
    ],
)
def test_adapter_reads_no_prose_so_every_party_is_cannot_determine(event: str) -> None:
    faults = evaluate_fault(_facts(["silver sedan", "blue SUV"], event))
    assert statuses(faults) == (CD, CD)


def test_adapter_leaves_every_decision_field_unknown_and_keeps_structure() -> None:
    facts = _facts(["silver sedan", "blue SUV"], "The SUV hit the sedan")
    sit = situation_from_overview(facts)
    assert [p.description for p in sit.parties] == ["silver sedan", "blue SUV"]
    assert sit.collisions == tuple(facts.candidate_collisions)
    unknowns: list[Observed[Any]] = [
        sit.junction_control,
        sit.right_of_way_conflict,
        sit.speed_limit_kmh,
        sit.conflict_zone_type,
        sit.officer_directing_traffic,
        sit.posted_speed_limit_kmh,
    ]
    for p in sit.parties:
        unknowns += [
            p.party_type,
            p.movement,
            p.approach_control_faced,
            p.visible_vehicle_markings,
            p.lateral_move_into_other_party_path,
            p.lateral_move_start_t,
            p.contact_t,
            p.kind,
            p.road_class,
            p.circulating_in_roundabout,
            p.approaching_from_other_party_left,
            p.emergency_signals_active,
            p.started_crossing_at_crosswalk,
        ]
    assert not any(o.is_known for o in unknowns)


def test_adapter_without_collision_says_so_and_without_parties_gives_nothing() -> None:
    no_collision = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[PartyFact(description="silver sedan", view_id="view-1", timestamp_seconds=1.0, confidence=0.9)],
        events=[],
        candidate_collisions=[],
    )
    assert_cannot_determine(evaluate_fault(no_collision), "No collision candidate found")
    assert (
        evaluate_fault(
            OverviewFacts(
                behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=[]
            )
        )
        == ()
    )


def test_rules_package_does_no_text_parsing() -> None:
    """A guard for the round 1 defect: the rules package must not use regexes or string-searching on prose."""
    rules_dir = Path(__file__).parent.parent / "src" / "incident_report" / "rules"
    text_methods = {
        "lower",
        "upper",
        "casefold",
        "find",
        "index",
        "startswith",
        "endswith",
        "split",
        "partition",
        "search",
        "match",
    }
    for py_file in rules_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "re" for alias in node.names), f"{py_file.name} imports re"
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "re", f"{py_file.name} imports from re"
            elif isinstance(node, ast.Attribute) and node.attr in text_methods:
                raise AssertionError(f"{py_file.name} calls .{node.attr}(): decisions must not parse text")
            elif isinstance(node, ast.Compare) and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
                # `in` on a string literal would be a substring test; sets and tuples of enums are fine.
                operands = [node.left, *node.comparators]
                assert not any(isinstance(o, ast.Constant) and isinstance(o.value, str) for o in operands), (
                    f"{py_file.name} line {node.lineno}: substring test on a string literal"
                )


@pytest.mark.parametrize("status", [FaultStatus.AT_FAULT, FaultStatus.NOT_AT_FAULT])
def test_a_decided_fault_status_must_cite_a_fact(status: FaultStatus) -> None:
    """ADR 0006: At Fault and Not at Fault always link to the facts that produced them."""
    with pytest.raises(ValueError, match="must cite at least one fact"):
        PartyFault(
            party_description="silver Toyota Camry sedan",
            status=status,
            color=FAULT_STATUS_COLORS[status],
            reason="decided without evidence",
            cited_facts=(),
        )


def test_cannot_determine_may_have_no_citation() -> None:
    fault = PartyFault(
        party_description="silver Toyota Camry sedan",
        status=FaultStatus.CANNOT_DETERMINE,
        color=FAULT_STATUS_COLORS[FaultStatus.CANNOT_DETERMINE],
        reason="No collision candidate found in facts.",
        cited_facts=(),
    )
    assert fault.cited_facts == ()
