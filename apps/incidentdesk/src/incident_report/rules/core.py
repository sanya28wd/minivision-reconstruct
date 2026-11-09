"""Pure core-rule assessments over cited typed observations (RPT-18)."""

from dataclasses import dataclass
from typing import Any

from incident_report.facts import CitedFact
from incident_report.rules.decree_law_14_2024 import (
    RULE_ART_5_1_F_CROSSWALK,
    RULE_ART_6_EMERGENCY,
    RULE_ART_6_PRIORITY_LEFT_EQUAL,
    RULE_ART_6_ROUNDABOUT,
    RULE_ART_7_4_PEDESTRIAN_SPEED,
)
from incident_report.rules.models import Finding, Rule
from incident_report.rules.mr130_1997 import (
    RULE_ENTRY,
    RULE_LANE_CHANGE,
    RULE_REAR_END,
    RULE_SIGNAL,
    RULE_STOP_GIVE_WAY,
)
from incident_report.rules.situation import Observed, Situation

LANE_MOVEMENTS = frozenset({"lane_change_left", "lane_change_right", "merging"})
# Catalogue C43/C46-C50/C57/C62: duties bind drivers/vehicles generally, not cycle duties.
VEHICLE_TYPES = frozenset({"car", "taxi", "truck_or_heavy", "bus", "motorcycle", "emergency_or_official_marked"})
CROSSING_RELATIONS = frozenset({("B_from_A_left", "B_from_A_right"), ("B_from_A_right", "B_from_A_left")})
REAR_RELATIONS = frozenset({("B_ahead_same_direction", "B_behind"), ("B_behind", "B_ahead_same_direction")})
RECIPROCAL_RELATIONS = (
    CROSSING_RELATIONS
    | REAR_RELATIONS
    | {
        ("B_alongside_adjacent_lane", "B_alongside_adjacent_lane"),
        ("B_ahead_oncoming", "B_ahead_oncoming"),
    }
)


@dataclass(frozen=True)
class Assessment:
    findings: tuple[Finding, ...]
    missing: tuple[str, ...]
    evidence: tuple[CitedFact, ...]
    explanation: str


def evidence(situation: Situation, *fields: Observed[Any]) -> tuple[CitedFact, ...]:
    result = list(situation.collisions)
    result.extend(situation.officer_directing_traffic.evidence)
    result.extend(situation.conflict_zone_type.evidence)
    result.extend(
        CitedFact(
            description=p.source.description,
            view_id=p.source.view_id,
            timestamp_seconds=p.source.timestamp_seconds,
            confidence=p.source.confidence,
        )
        for p in situation.parties
        if p.source is not None
    )
    for p in situation.parties:
        result.extend(p.party_type.evidence)
        result.extend(p.movement.evidence)
        result.extend(p.approach_control_faced.evidence)
        for relation in p.relative_position_of_other.values():
            result.extend(relation.evidence)
    for field in fields:
        result.extend(field.evidence)
    return tuple(dict.fromkeys(result))


def missing_fields(fields: list[tuple[str, Observed[Any]]]) -> tuple[str, ...]:
    return tuple(
        name
        for name, field in fields
        if field.value is None or field.value in {"unknown", "not_visible", "not_in_view"}
    )


def finding(
    situation: Situation, index: int, rule: Rule, reason: str, cited: tuple[CitedFact, ...], *, causal: bool
) -> Finding:
    return Finding(
        party_description=situation.parties[index].description,
        rule_id=rule.rule_id,
        legal_source=rule.legal_source,
        article=rule.article,
        source_url=rule.source_url,
        reason=f"{reason}; {rule.article}, {rule.legal_source}.",
        classification="Finding" if causal else "Contributing Factor",
        cited_facts=cited,
    )


def causal_conflict(situation: Situation, *required: Observed[Any], control_evidence: bool = True) -> bool:
    """One contact, consistent pair geometry and cited conflicting movements before contact."""
    if len(situation.collisions) != 1:
        return False
    relations = pair_fields(situation)
    values = tuple(f.value for _, f in relations)
    if values not in RECIPROCAL_RELATIONS:
        return False
    controls: list[Observed[Any]] = []
    if control_evidence:
        controls = [p.approach_control_faced for p in situation.parties if p.party_type.value != "pedestrian"]
        controls.extend([situation.officer_directing_traffic, situation.conflict_zone_type])
    movements = [p.movement for p in situation.parties]
    if any(p.value in {"unknown", "other"} for p in movements):
        return False
    contact = situation.collisions[0]
    for observation in (*movements, *(f for _, f in relations), *controls, *required):
        if not observation.is_known or not any(
            e.view_id == contact.view_id and e.timestamp_seconds <= contact.timestamp_seconds
            for e in observation.evidence
        ):
            return False
    return True


def signal_rule(situation: Situation, indices: tuple[int, ...] | None = None) -> Assessment:
    indices = tuple(range(len(situation.parties))) if indices is None else indices
    fields = [
        (
            f"signal_state_when_crossing_stop_line of {situation.parties[i].description}",
            situation.parties[i].signal_state_when_crossing_stop_line,
        )
        for i in indices
    ]
    cited = evidence(situation, *(f for _, f in fields))
    missing = list(missing_fields(fields + pair_fields(situation)))
    findings = []
    for i in indices:
        party = situation.parties[i]
        state = party.signal_state_when_crossing_stop_line.value
        if state == "red":
            findings.append(
                finding(
                    situation,
                    i,
                    RULE_SIGNAL,
                    "Crossed the stop line on red"
                    + (
                        "; no verified item for this vehicle type; officer selects"
                        if party.party_type.value in {"bus", "emergency_or_official_marked"}
                        else ""
                    ),
                    cited,
                    causal=crossing_pair(situation) is None and causal_conflict(situation, *(f for _, f in fields)),
                )
            )
        elif state not in {"green", "unknown", "not_visible"}:
            missing.append(
                f"signal_state_when_crossing_stop_line safe passage of {party.description} unresolved ({state})"
            )
    return Assessment(tuple(findings), tuple(missing), cited, "")


def pair_fields(situation: Situation) -> list[tuple[str, Observed[Any]]]:
    a, b = situation.parties
    return [
        (
            f"relative_position_of_other of {p.description} toward {other.description}",
            p.relative_position_of_other.get(other.description, Observed(value="unknown")),
        )
        for p, other in ((a, b), (b, a))
    ]


def crossing_pair(situation: Situation) -> Assessment | None:
    fields = pair_fields(situation)
    cited = evidence(situation, *(f for _, f in fields))
    missing = missing_fields(fields)
    if missing:
        return Assessment((), missing, cited, "")
    values = tuple(f.value for _, f in fields)
    if values not in CROSSING_RELATIONS:
        return Assessment((), (), cited, "relative_position_of_other does not establish consistent crossing paths.")
    return None


def stop_give_way_rule(situation: Situation) -> Assessment:
    pair = crossing_pair(situation)
    if pair is not None:
        return pair
    controls = [p.approach_control_faced for p in situation.parties]
    cited = evidence(situation, *controls, *(f for _, f in pair_fields(situation)))
    entrants = [i for i, c in enumerate(controls) if c.value in {"stop_sign_or_line", "give_way_sign_or_line"}]
    if len(entrants) != 1 or controls[1 - entrants[0]].value != "none_confirmed":
        return Assessment(
            (), (), cited, "approach_control_faced combination does not establish which signed approach must yield."
        )
    i = entrants[0]
    return Assessment(
        (
            finding(
                situation,
                i,
                RULE_STOP_GIVE_WAY,
                "Entered a crossing path despite the stop/give-way control",
                cited,
                causal=causal_conflict(situation, *(p.approach_control_faced for p in situation.parties)),
            ),
        ),
        (),
        cited,
        "",
    )


def entry_rule(situation: Situation) -> Assessment:
    fields = [(f"entry_origin of {p.description}", p.entry_origin) for p in situation.parties]
    cited = evidence(situation, *(f for _, f in fields), *(f for _, f in pair_fields(situation)))
    missing = missing_fields(fields + pair_fields(situation))
    if missing:
        return Assessment((), missing, cited, "")
    pair = crossing_pair(situation)
    if pair is not None:
        return pair
    entrants = [
        i
        for i, p in enumerate(situation.parties)
        if p.entry_origin.value in {"side_road", "service_road", "car_park_or_driveway", "roadside_stop", "unpaved"}
    ]
    if len(entrants) != 1:
        return Assessment((), (), cited, "entry_origin combination has no single entering party.")
    i = entrants[0]
    if situation.parties[i].movement.value not in {
        "entering_from_side_road",
        "entering_from_car_park_or_driveway",
        "merging",
    }:
        return Assessment((), (), cited, "movement does not establish entry from the observed origin.")
    return Assessment(
        (
            finding(
                situation,
                i,
                RULE_ENTRY,
                "Entered from the side into the other party's crossing path",
                cited,
                causal=causal_conflict(situation, *(f for _, f in fields)),
            ),
        ),
        (),
        cited,
        "",
    )


def roundabout_rule(situation: Situation) -> Assessment:
    fields = [(f"roundabout_entry_state of {p.description}", p.roundabout_entry_state) for p in situation.parties]
    cited = evidence(situation, *(f for _, f in fields), *(f for _, f in pair_fields(situation)))
    missing = missing_fields(fields + pair_fields(situation))
    if missing:
        return Assessment((), missing, cited, "")
    states = [p.roundabout_entry_state.value for p in situation.parties]
    if states.count("already_inside_circulating") != 1 or states.count("entering_from_approach") != 1:
        return Assessment((), (), cited, "roundabout_entry_state: both or neither circulating; no covered priority.")
    i = 0 if states[0] == "entering_from_approach" else 1
    entrant, circulating = situation.parties[i], situation.parties[1 - i]
    if (
        entrant.movement.value != "entering_roundabout"
        or circulating.movement.value != "circulating_roundabout"
        or pair_fields(situation)[i][1].value != "B_from_A_left"
        or pair_fields(situation)[1 - i][1].value != "B_from_A_right"
    ):
        return Assessment(
            (),
            (),
            cited,
            "roundabout_entry_state, movement and relative_position_of_other do not establish entry across circulating traffic from the left.",
        )
    return Assessment(
        (
            finding(
                situation,
                i,
                RULE_ART_6_ROUNDABOUT,
                "Failed to yield to circulating traffic from the left",
                cited,
                causal=causal_conflict(situation, *(f for _, f in fields)),
            ),
        ),
        (),
        cited,
        "",
    )


def uncontrolled_rule(situation: Situation) -> Assessment:
    pair = crossing_pair(situation)
    if pair is not None:
        return pair
    cited = evidence(situation, *(f for _, f in pair_fields(situation)))
    i = 0 if pair_fields(situation)[0][1].value == "B_from_A_left" else 1
    return Assessment(
        (
            finding(
                situation,
                i,
                RULE_ART_6_PRIORITY_LEFT_EQUAL,
                "Failed to yield to the crossing vehicle from the left at confirmed uncontrolled approaches",
                cited,
                causal=causal_conflict(situation, *(p.approach_control_faced for p in situation.parties)),
            ),
        ),
        (),
        cited,
        "",
    )


def lane_change_rule(situation: Situation) -> Assessment:
    obscured = obscured_touch(situation)
    if obscured is not None:
        return obscured
    fields: list[tuple[str, Observed[Any]]] = []
    missing: list[str] = []
    findings = []
    for i, p in enumerate(situation.parties):
        if p.movement.value not in LANE_MOVEMENTS:
            continue
        own_fields = [
            (f"{name} of {p.description}", getattr(p, name))
            for name in (
                "lateral_move_into_other_party_path",
                "lateral_move_start_t",
                "contact_t",
                "lane_line_crossed_before_contact",
                "signal_given",
            )
        ]
        fields.extend(own_fields)
        missing.extend(missing_fields(own_fields + pair_fields(situation)))
        cited = evidence(situation, *(f for _, f in own_fields), *(f for _, f in pair_fields(situation)))
        start, contact = p.lateral_move_start_t.value, p.contact_t.value
        ordered = start is not None and contact is not None and start <= contact
        clock = {e.view_id for e in p.lateral_move_start_t.evidence} & {e.view_id for e in p.contact_t.evidence}
        time_matches_contact = (
            bool(situation.collisions)
            and contact == situation.collisions[0].timestamp_seconds
            and situation.collisions[0].view_id in clock
        )
        if start is not None and contact is not None and not (ordered and clock and time_matches_contact):
            missing.append(
                f"lateral_move_start_t/contact_t of {p.description} need ordered timestamps matching this contact in the same view"
            )
        intruded = p.lateral_move_into_other_party_path.value == "yes"
        if intruded:
            causal = bool(ordered and clock and time_matches_contact) and causal_conflict(
                situation, *(f for _, f in own_fields)
            )
            if p.lane_line_crossed_before_contact.value == "none":
                missing.append(f"lane_line_crossed_before_contact of {p.description} contradicts the lane change")
                causal = False
            findings.append(
                finding(
                    situation,
                    i,
                    RULE_LANE_CHANGE,
                    "Moved laterally into the other party's path; applicable schedule item for the officer to select: "
                    "Item 29 (sudden deviation), 83 (no indicator)"
                    + (" or 86 (compulsory lane)" if p.party_type.value in {"car", "taxi"} else "")
                    + "; not determined from video",
                    cited,
                    causal=causal,
                )
            )
        # Research section 5.4: none_visible does not prove indicator non-use (no Item 83 finding).
    cited = evidence(situation, *(f for _, f in fields), *(f for _, f in pair_fields(situation)))
    return Assessment(
        tuple(findings),
        tuple(missing),
        cited,
        "" if findings else "No causal lateral intrusion proved; indicator behavior alone cannot assign fault.",
    )


def rear_end_rule(situation: Situation) -> Assessment:
    obscured = obscured_touch(situation)
    if obscured is not None:
        return obscured
    fields = pair_fields(situation)
    for p in situation.parties:
        fields += [
            (f"{name} of {p.description}", getattr(p, name))
            for name in ("motion_state_before_contact", "impact_zone_on_party", "lateral_move_into_other_party_path")
        ]
    relation = tuple(f.value for _, f in pair_fields(situation))
    follower = 0 if relation == ("B_ahead_same_direction", "B_behind") else 1
    leader, following = situation.parties[1 - follower], situation.parties[follower]
    action = leader.leading_vehicle_action.get(following.description, Observed(value="unknown"))
    fields.append((f"leading_vehicle_action of {leader.description} toward {following.description}", action))
    cited = evidence(situation, *(f for _, f in fields))
    missing = missing_fields(fields)
    if missing:
        return Assessment((), missing, cited, "")
    if relation not in {("B_ahead_same_direction", "B_behind"), ("B_behind", "B_ahead_same_direction")}:
        return Assessment((), (), cited, "relative_position_of_other does not establish a rear-end pair.")
    if action.value not in {"none_visible", "stopped_in_traffic", "stopped_at_signal"}:
        return Assessment(
            (), (), cited, "leading_vehicle_action exception or unexplained braking; follower fault is unresolved."
        )
    if any(p.lateral_move_into_other_party_path.value != "no" for p in situation.parties):
        return Assessment((), (), cited, "lateral_move_into_other_party_path exception to the rear-end rule.")
    if (
        following.motion_state_before_contact.value != "moving"
        or following.impact_zone_on_party.value not in {"front", "front_left", "front_right"}
        or leader.impact_zone_on_party.value not in {"rear", "rear_left", "rear_right"}
    ):
        return Assessment(
            (), (), cited, "motion_state_before_contact/impact_zone_on_party do not establish follower contact."
        )
    if leader.motion_state_before_contact.value != "moving" and not (
        leader.motion_state_before_contact.value == "stopped_in_lane"
        and action.value in {"stopped_in_traffic", "stopped_at_signal"}
    ):
        return Assessment(
            (), (), cited, "leading_vehicle_action does not establish a justified stop; leader exception unresolved."
        )
    return Assessment(
        (
            finding(
                situation,
                follower,
                RULE_REAR_END,
                "Following vehicle contacted the rear of the leader with observed leader exceptions excluded",
                cited,
                causal=causal_conflict(situation, *(f for _, f in fields)),
            ),
        ),
        (),
        cited,
        "",
    )


def obscured_touch(situation: Situation) -> Assessment | None:
    """Revision 3B: only deciding views and the selected pair can obscure this touch.

    The source-part clock is already rebased upstream; no time adjustment here.
    Earlier records without visibility retain their previous behavior.
    """
    views = {c.view_id for c in situation.collisions}
    pair = {p.description for p in situation.parties}
    contacts = tuple(
        o.citation()
        for o in situation.collision_observations
        if o.visibility == "possible_obscured" and o.view_id in views and {o.subject, o.other_party} == pair
    )
    # Assessment rows have no confidence field: retain the deciding candidate's
    # citation rather than manufacture confidence for a new observation.
    states = tuple(
        c
        for c in situation.collisions
        if any(
            a.contact_state == "contact_possible_obscured" and a.view_id == c.view_id
            for a in situation.incident_assessments
        )
    )
    if contacts or states:
        return Assessment(
            (), (), tuple(dict.fromkeys((*evidence(situation), *contacts, *states))), "the touch is not clearly visible"
        )
    return None


def pedestrian_rule(situation: Situation) -> Assessment:
    i = 0 if situation.parties[0].party_type.value == "pedestrian" else 1
    pedestrian, driver = situation.parties[i], situation.parties[1 - i]
    fields = [
        (f"{name} of {pedestrian.description}", getattr(pedestrian, name))
        for name in (
            "pedestrian_crossing_place",
            "pedestrian_signal_state",
            "pedestrian_started_before_vehicle_arrived",
        )
    ]
    fields.append(("posted_speed_limit_kmh", situation.posted_speed_limit_kmh))
    if driver.approach_control_faced.value == "signal":
        fields.append(
            (
                f"signal_state_when_crossing_stop_line of {driver.description}",
                driver.signal_state_when_crossing_stop_line,
            )
        )
    cited = evidence(situation, *(f for _, f in fields))
    missing = missing_fields(fields)
    if missing:
        return Assessment((), missing, cited, "")
    limit = situation.posted_speed_limit_kmh.value
    if not isinstance(limit, int):
        return Assessment((), ("posted_speed_limit_kmh",), cited, "")
    if limit == 80:  # Owner ruling: Art. 7(4) and 39(2) boundary conflict, 2026-10-15.
        return Assessment((), (), cited, "posted_speed_limit_kmh is exactly 80: Cannot Determine by owner ruling.")
    place = pedestrian.pedestrian_crossing_place.value
    at_crossing = place in {"marked_crossing", "signalised_crossing", "on_slip_lane_crossing"}
    if at_crossing and limit > 80:
        return Assessment(
            (), (), cited, "Crosswalk above 80: Art. 5(1)(f) and 7(4) would give opposite outcomes; Cannot Determine."
        )
    if (
        pedestrian.movement.value != "crossing_on_foot"
        or pedestrian.pedestrian_started_before_vehicle_arrived.value != "yes"
    ):
        return Assessment(
            (), (), cited, "pedestrian_started_before_vehicle_arrived/movement do not establish a covered crossing."
        )
    if place == "outside_any_crossing" and limit > 80:
        return Assessment(
            (
                finding(
                    situation,
                    i,
                    RULE_ART_7_4_PEDESTRIAN_SPEED,
                    "Crossed outside a designated place on a road with a posted limit above 80 km/h",
                    cited,
                    causal=causal_conflict(situation, *(f for _, f in fields)),
                ),
            ),
            (),
            cited,
            "",
        )
    state = pedestrian.pedestrian_signal_state.value
    if at_crossing and state == "green":
        return Assessment(
            (
                finding(
                    situation,
                    1 - i,
                    RULE_ART_5_1_F_CROSSWALK,
                    "Did not yield to a pedestrian already crossing lawfully",
                    cited,
                    causal=causal_conflict(situation, *(f for _, f in fields)),
                ),
            ),
            (),
            cited,
            "",
        )
    if (
        at_crossing
        and state == "no_pedestrian_signal"
        and place != "signalised_crossing"
        and driver.approach_control_faced.value == "none_confirmed"
    ):
        return Assessment(
            (
                finding(
                    situation,
                    1 - i,
                    RULE_ART_5_1_F_CROSSWALK,
                    "Did not yield to a pedestrian already crossing at an uncontrolled crossing",
                    cited,
                    causal=causal_conflict(situation, *(f for _, f in fields)),
                ),
            ),
            (),
            cited,
            "",
        )
    return Assessment(
        (),
        (),
        cited,
        "pedestrian_signal_state or crossing combination unresolved; a red/flashing signal never automatically blames the driver.",
    )


def emergency_rule(situation: Situation) -> Assessment | None:
    """Map observations to Art. 6 in code. Duty status of ambulance/fire is unproved."""
    fields: list[tuple[str, Observed[Any]]] = []
    qualified = []
    missing: list[str] = []
    for i, p in enumerate(situation.parties):
        markings = p.visible_vehicle_markings.value
        warnings_visible = p.emergency_lights.value == "yes" or p.emergency_siren.value == "yes"
        if (
            p.party_type.value != "emergency_or_official_marked"
            and markings in {"unknown", "none_visible"}
            and not warnings_visible
        ):
            continue
        fields.append((f"visible_vehicle_markings of {p.description}", p.visible_vehicle_markings))
        if markings in {"unknown", "none_visible"}:
            missing.append(f"visible_vehicle_markings of {p.description}")
        elif markings == "police":
            police_fields = [
                (f"emergency_lights of {p.description}", p.emergency_lights),
                (f"emergency_siren of {p.description}", p.emergency_siren),
            ]
            fields.extend(police_fields)
            missing.extend(missing_fields(police_fields))
            if p.emergency_lights.value == p.emergency_siren.value == "yes":
                qualified.append(i)
        elif markings in {"ambulance", "civil_defence"}:
            fields.append((f"emergency_lights of {p.description}", p.emergency_lights))
            missing.extend(missing_fields(fields[-1:]))
            missing.append(f"on duty qualification of {p.description} unknown")
        elif markings == "other_official_marked":
            missing.append(f"Art. 6 qualification of other_official_marked {p.description} unknown")
    if not fields:
        return None
    cited = evidence(situation, *(f for _, f in fields), *(f for _, f in pair_fields(situation)))
    if missing:
        return Assessment((), tuple(missing), cited, "")
    if len(qualified) > 1:
        return Assessment(
            (), (), cited, "Both parties are emergency police vehicles; priority between them is unresolved."
        )
    if not qualified:
        return None
    return Assessment(
        (
            finding(
                situation,
                1 - qualified[0],
                RULE_ART_6_EMERGENCY,
                "Did not give way to visibly marked police using lights and siren; Art. 6 applies in all cases",
                cited,
                causal=crossing_pair(situation) is None
                and causal_conflict(situation, *(f for _, f in fields), control_evidence=False),
            ),
        ),
        (),
        cited,
        "",
    )
