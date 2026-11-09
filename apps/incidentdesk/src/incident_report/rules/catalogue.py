"""Spec 003 ticket 9: catalogue C1-C2, C10-C17 and C48; typed facts only."""

from incident_report.rules.core import (
    RECIPROCAL_RELATIONS,
    Assessment,
    causal_conflict,
    evidence,
    finding,
    missing_fields,
    pair_fields,
)
from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD
from incident_report.rules.mr130_1997 import RULE_ONCOMING_LEFT_TURN, RULE_ROUNDABOUT_EXIT, RULE_ROUNDABOUT_EXIT_SIGNED
from incident_report.rules.situation import Situation


def oncoming_left_turn_pattern(situation: Situation) -> bool:
    return sorted(p.movement.value for p in situation.parties) == ["left_turn", "straight"] and all(
        f.value == "B_ahead_oncoming" for _, f in pair_fields(situation)
    )


def oncoming_left_turn_rule(situation: Situation) -> Assessment:
    controls = tuple(p.approach_control_faced.value for p in situation.parties)
    signal_fields = tuple(p.signal_state_when_crossing_stop_line for p in situation.parties)
    at_signal = (
        controls == ("signal", "signal")
        and situation.conflict_zone_type.value in {"signalised_junction", "roundabout_signalised"}
        and all(f.value == "green" for f in signal_fields)
    )
    uncontrolled = (
        controls == ("none_confirmed", "none_confirmed")
        and situation.conflict_zone_type.value == "unsignalised_junction"
    )
    required = signal_fields if at_signal else ()
    cited = evidence(situation, *required)
    if not oncoming_left_turn_pattern(situation) or not (at_signal or uncontrolled):
        return Assessment((), (), cited, "Oncoming left-turn pattern or junction controls not covered.")
    i = next(i for i, p in enumerate(situation.parties) if p.movement.value == "left_turn")
    return Assessment(
        (
            finding(
                situation,
                i,
                RULE_ONCOMING_LEFT_TURN,
                "Turned left across oncoming straight traffic; فيتركها تمر أولاً (let them pass first). "
                "Translation is ours, not certified; catalogue oncoming-turn rows. Fine: Making wrong turn; "
                "item mapped by the owner (spec 003 decision 12); the item wording does not name this breach",
                cited,
                causal=causal_conflict(situation, *required),
            ),
        ),
        (),
        cited,
        "",
    )


def roundabout_exit_pattern(situation: Situation) -> bool:
    return sorted(p.movement.value for p in situation.parties) == ["entering_roundabout", "exiting_roundabout"]


def roundabout_exit_rule(situation: Situation) -> Assessment:
    i = next(i for i, p in enumerate(situation.parties) if p.movement.value == "entering_roundabout")
    entrant, exiter = situation.parties[i], situation.parties[1 - i]
    position = exiter.roundabout_exit_position
    fields = [("roundabout_exit_position", position), *pair_fields(situation)]
    cited = evidence(situation, *(f for _, f in fields))
    missing = missing_fields(fields)
    if missing:
        return Assessment((), missing, cited, "")
    if position.value != "on_exit_curve_before_straight_lane":
        return Assessment(
            (),
            (),
            cited,
            "roundabout_exit_position: vehicle is not on the exit curve before a straight lane; "
            "The oncoming-turn rule needs a separate typed left turn across oncoming straight traffic, otherwise not covered.",
        )
    relations = tuple(f.value for _, f in pair_fields(situation))
    controls = tuple(p.approach_control_faced.value for p in situation.parties)
    if (
        situation.conflict_zone_type.value != "roundabout_unsignalised"
        or situation.officer_directing_traffic.value != "no"
        or entrant.approach_control_faced.value not in {"none_confirmed", "give_way_sign_or_line"}
        or exiter.approach_control_faced.value != "none_confirmed"
        or relations not in RECIPROCAL_RELATIONS
        or any(v in {"B_behind", "B_ahead_same_direction"} for v in relations)
    ):
        return Assessment((), (), cited, f"Roundabout exit controls/relative positions are not covered: {controls}.")
    signed = entrant.approach_control_faced.value == "give_way_sign_or_line"
    rule = RULE_ROUNDABOUT_EXIT_SIGNED if signed else RULE_ROUNDABOUT_EXIT
    return Assessment(
        (
            finding(
                situation,
                i,
                rule,
                "Did not yield to the vehicle still on the exit curve; "
                "RTA LMV Handbook p.105 (guidance): "
                '"give way to all vehicles already in it moving towards you from the left". '
                "Roundabout boundary and direction follow the owner's working interpretation "
                "(spec 003 decision 9; owner ruling 2026-10-18): the law names vehicles already in "
                "the roundabout coming from the left and does not define where a roundabout ends. "
                "Catalogue roundabout entry and exit rows; MR 130 translation is ours, not certified. "
                "Decree-law source: https://uaelegislation.gov.ae/en/legislations/2598. "
                "Handbook: https://www.drivedubai.ae/public/uploads/downloads/20210128140116RTA-Handbook-Light-Motor-Vechicle-English.pdf",
                cited,
                causal=causal_conflict(situation, position),
            ),
        ),
        (),
        cited,
        "",
    )


def main_road_rule(situation: Situation) -> Assessment:
    fields = [
        (f"{name} of {p.description}", getattr(p, name))
        for p in situation.parties
        for name in ("road_class_on_approach", "road_class_basis")
    ]
    fields += pair_fields(situation)
    cited = evidence(situation, *(f for _, f in fields))
    missing = missing_fields(fields)
    if missing:
        return Assessment((), missing, cited, "")
    entrants = [i for i, p in enumerate(situation.parties) if p.movement.value == "entering_from_side_road"]
    if len(entrants) != 1:
        return Assessment((), (), cited, "Main-road rule requires one typed side-road entrant.")
    i = entrants[0]
    side, main = situation.parties[i], situation.parties[1 - i]
    if (
        side.road_class_on_approach.value != "side_road"
        or side.road_class_basis.value != "short_or_ends_at_junction"
        or main.road_class_on_approach.value != "main_road"
        or main.road_class_basis.value != "more_than_two_lanes_and_long"
        or situation.conflict_zone_type.value != "unsignalised_junction"
        or situation.officer_directing_traffic.value != "no"
        or any(p.approach_control_faced.value != "none_confirmed" for p in situation.parties)
    ):
        return Assessment((), (), cited, "Main-road road_class_on_approach/road_class_basis or controls not covered.")
    return Assessment(
        (
            finding(
                situation,
                i,
                RULE_ART_6_MAIN_ROAD,
                "Entered from a side road into the main-road vehicle's path; "
                "(https://uaelegislation.gov.ae/ar/legislations/1020; translation is ours, not certified), "
                "catalogue main-road and entry rows. "
                "Main road follows the owner's working definition (spec 003 decision 10); 'main road' is not defined in law. "
                "Lanes counted per direction. "
                + "; ".join(
                    f"{p.description}: {p.road_class_on_approach.value}, {p.road_class_basis.value}"
                    for p in situation.parties
                ),
                cited,
                causal=causal_conflict(situation, *(f for _, f in fields)),
            ),
        ),
        (),
        cited,
        "",
    )
