"""Pure code-side rules for the overview-10 party checklist (spec 003 ticket 3).

The model answers closed questions (what the road user is, what it was doing, what the road was). Code turns
those answers into the typed facts the rules already read, builds the display names, and builds the candidate
collisions from the typed contacts. Nothing here reads prose or calls a model (ADR 0007).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from incident_report.facts import (
    CAMERA_CODE,
    CONTACT_KINDS,
    PARTY_CODE_PATTERN,
    ChecklistPartyType,
    CitedFact,
    CollisionObservation,
    IncidentAssessmentRow,
    PartyChecklistRow,
)
from incident_report.rules.consistency import CONTACT_MATCH_SECONDS

# Car bodies (card section 4): the party_types a taxi marking or a plain "car" can apply to.
CAR_BODIES: frozenset[str] = frozenset({"sedan", "suv", "hatchback", "car_body_unclear", "van", "pickup"})
# A road user that cannot carry emergency or official vehicle markings.
_NOT_A_VEHICLE: frozenset[str] = frozenset({"pedestrian", "bicycle", "unknown"})
_OTHER_TEXT_LIMIT = 40  # display-name limit from the card (section 3); not a legal figure.
_BODY_WORDS: Mapping[str, str] = {
    "sedan": "sedan",
    "suv": "SUV",
    "hatchback": "hatchback",
    "car_body_unclear": "car",
    "van": "van",
    "pickup": "pickup",
    "truck": "truck",
    "bus": "bus",
    "motorcycle": "motorcycle",
    "bicycle": "bicycle",
    "pedestrian": "pedestrian",
}
_COLOUR_WORDS: Mapping[str, str] = {
    "yellow_or_gold": "yellow/gold",
    "brown_or_beige": "brown/beige",
}
_CODE_IN_TEXT = re.compile(r"\b(?:CAM|P[1-9][0-9]*)\b")


def _clean(text: str) -> str:
    """One line, trimmed to the display limit; model text is escaped again when rendered."""
    return " ".join(text.split())[:_OTHER_TEXT_LIMIT].strip()


def body_word(row: PartyChecklistRow) -> str | None:
    """The body word for a display name; None when the kind of road user is not known."""
    if row.party_type == "other":
        return _clean(row.party_type_other) or None
    return _BODY_WORDS.get(row.party_type)


def colour_word(row: PartyChecklistRow) -> str | None:
    """The colour for a display name; None when unknown."""
    if row.colour_family == "unknown":
        return None
    if row.colour_family == "other":
        return _clean(row.colour_other) or None
    return _COLOUR_WORDS.get(row.colour_family, row.colour_family)


def display_name(row: PartyChecklistRow) -> str:
    """Build the name every other fact uses for this road user (colour + body, with its code)."""
    body, colour = body_word(row), colour_word(row)
    if row.party_code == CAMERA_CODE:
        detail = " ".join(w for w in (colour, body) if w)
        return f"camera vehicle ({detail})" if detail else "camera vehicle"
    if row.party_type == "pedestrian":
        name = f"{row.party_code} pedestrian"
    elif colour is None:
        name = f"{row.party_code} {body or 'road user'}, colour unknown"
    else:
        name = f"{row.party_code} {colour} {body or 'road user'}"
    if row.marking == "taxi":
        name += " (taxi)"
    elif row.marking == "emergency_or_official":
        name += " (official markings)"
    return name


def party_color(row: PartyChecklistRow) -> str | None:
    return colour_word(row)


def party_body_type(row: PartyChecklistRow) -> str | None:
    return body_word(row)


def rewrite_codes(text: str, names: Mapping[str, str]) -> str:
    """Replace whole-word P<n>/CAM tokens that match a checklist code; leave unknown codes as written."""
    return _CODE_IN_TEXT.sub(lambda m: names.get(m.group(0), m.group(0)), text)


def validate_codes(rows: Sequence[PartyChecklistRow]) -> None:
    """Codes are unique, P1..Pn without gaps, and at most one CAM."""
    codes = [r.party_code for r in rows]
    if len(set(codes)) != len(codes):
        raise ValueError("party codes must be unique")
    numbered = sorted(int(c[1:]) for c in codes if c != CAMERA_CODE)
    if numbered != list(range(1, len(numbered) + 1)):
        raise ValueError("party codes must run P1, P2, ... without gaps")
    if not all(PARTY_CODE_PATTERN.fullmatch(c) for c in codes):
        raise ValueError("party codes must be P<n> or CAM")


# ---------------------------------------------------------------------------------------------------------
# Checklist -> rule-facing facts (card section 4). The model never emits these four fields in overview-10.
# ---------------------------------------------------------------------------------------------------------


def derive_party_type(row: PartyChecklistRow) -> str:
    """The rules' party_type from the checklist body and marking."""
    body: ChecklistPartyType = row.party_type
    if row.marking == "emergency_or_official" and body not in _NOT_A_VEHICLE:
        return "emergency_or_official_marked"
    if row.marking == "taxi" and body in CAR_BODIES:
        return "taxi"
    if body in CAR_BODIES:
        return "car"
    return {
        "truck": "truck_or_heavy",
        "bus": "bus",
        "motorcycle": "motorcycle",
        "bicycle": "bicycle_or_personal_transport",
        "pedestrian": "pedestrian",
        "other": "other",
    }.get(body, "unknown")


@dataclass(frozen=True)
class RoadFacts:
    movement: str
    roundabout_entry_state: str
    entry_origin: str


def _turn(direction: str) -> str:
    return {"left": "left_turn", "right": "right_turn", "straight": "straight"}.get(direction, "unknown")


def derive_road_facts(row: PartyChecklistRow) -> RoadFacts:
    """The card's mapping table, row by row. Exterior views only; interior views derive nothing."""
    in_ring = row.road_class_on_approach == "roundabout"
    same_road_state = "already_inside_circulating" if in_ring else "not_in_roundabout"
    not_ring_state = "unknown" if in_ring else "not_in_roundabout"
    turn = _turn(row.turn_direction)
    road, basis = row.road_class_on_approach, row.road_class_basis
    short_side_road = road == "side_road" and basis == "short_or_ends_at_junction"
    match row.manoeuvre:
        case "continue_same_road":
            movement = "straight" if row.turn_direction in {"straight", "left", "right"} else "unknown"
            return RoadFacts(movement, same_road_state, "public_road_same_road")
        case "into_side_road":
            return RoadFacts("unknown" if turn == "straight" else turn, not_ring_state, "public_road_same_road")
        case "out_of_side_road":
            if short_side_road:
                return RoadFacts("entering_from_side_road", "not_in_roundabout", "side_road")
            if road == "service_road":
                return RoadFacts("entering_from_side_road", "not_in_roundabout", "service_road")
            if road == "car_park_or_access":
                return RoadFacts("entering_from_car_park_or_driveway", "not_in_roundabout", "car_park_or_driveway")
            return RoadFacts(turn, not_ring_state, "unknown")
        case "moving_off_from_kerb":
            return RoadFacts("other", "not_in_roundabout", "roadside_stop")
        case "merging":
            origin = (
                "side_road"
                if short_side_road
                else "service_road"
                if road == "service_road"
                else "public_road_same_road"
            )
            return RoadFacts("merging", "not_in_roundabout", origin)
        case "roundabout_entry":
            return RoadFacts("entering_roundabout", "entering_from_approach", "public_road_same_road")
        case "circulating":
            return RoadFacts("circulating_roundabout", "already_inside_circulating", "unknown")
        case "roundabout_exit":
            if row.roundabout_exit_position == "on_exit_curve_before_straight_lane":
                return RoadFacts("exiting_roundabout", "exiting", "unknown")
            if row.roundabout_exit_position == "reached_straight_lane_of_exit_road":
                return RoadFacts(turn, "not_in_roundabout", "public_road_same_road")
            return RoadFacts("exiting_roundabout", "unknown", "unknown")
        case "lane_change":
            movement = {"left": "lane_change_left", "right": "lane_change_right"}.get(row.turn_direction, "unknown")
            return RoadFacts(movement, same_road_state, "public_road_same_road")
        case "u_turn":
            return RoadFacts("u_turn", not_ring_state, "unknown")
        case "crossing_on_foot":
            return RoadFacts("crossing_on_foot", "not_in_roundabout", "unknown")
        case "other":
            return RoadFacts("other", "unknown", "unknown")
        case _:
            return RoadFacts("unknown", "unknown", "unknown")


# ---------------------------------------------------------------------------------------------------------
# Candidate collisions built from typed contacts (card section 1b A).
# ---------------------------------------------------------------------------------------------------------


def _contact_key(contact: CollisionObservation) -> tuple[str, ...]:
    if contact.kind in {"vehicle_contact_vehicle", "vehicle_struck_pedestrian"}:
        return ("pair", *sorted((contact.subject, contact.other_party)))
    if contact.kind == "contact_other":
        return ("thing", contact.subject, " ".join(contact.other_party.casefold().split()))
    return ("overturned", contact.subject)


def build_candidates(
    contacts: Sequence[CollisionObservation], *, camera_name: str | None = None, camera_role: str | None = None
) -> list[CitedFact]:
    """One candidate per contact on a view; the same pair (or thing) reported again within the match limit merges.

    Time is the earliest merged contact, confidence the highest. The description is the earliest contact's
    plain description, plus a note when the touch is not clearly visible (obscured, or the camera vehicle with
    an unknown camera role).
    """
    groups: list[list[CollisionObservation]] = []
    for contact in sorted((c for c in contacts if c.kind in CONTACT_KINDS), key=lambda c: c.timestamp_seconds):
        key = _contact_key(contact)
        for group in groups:
            if (
                _contact_key(group[0]) == key
                and contact.timestamp_seconds - group[0].timestamp_seconds <= CONTACT_MATCH_SECONDS
            ):
                group.append(contact)
                break
        else:
            groups.append([contact])
    candidates = []
    for group in groups:
        first = group[0]
        camera_unclear = (
            camera_name is not None and camera_role == "unknown" and camera_name in {first.subject, first.other_party}
        )
        note = " (touch not clearly visible)" if first.visibility == "possible_obscured" or camera_unclear else ""
        candidates.append(
            CitedFact(
                description=first.description + note,
                view_id=first.view_id,
                timestamp_seconds=first.timestamp_seconds,
                confidence=max(c.confidence for c in group),
            )
        )
    return candidates


def obscured_contact_line(assessment: IncidentAssessmentRow) -> str | None:
    """The plain line shown for a contact that may have happened but whose touch was not seen."""
    if assessment.contact_state != "contact_possible_obscured":
        return None
    start, end = assessment.contact_interval_start_seconds, assessment.contact_interval_end_seconds
    return f"contact possible but not clearly visible, {start:g}–{end:g} s"
