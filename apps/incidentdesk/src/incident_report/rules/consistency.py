"""Pure checks on typed answers (spec 003 ticket 1); never repair observations."""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from incident_report.facts import (
    CAMERA_CODE,
    CONTACT_KINDS,
    ChecklistParty,
    CitedFact,
    CollisionObservation,
    IncidentAssessment,
    OverviewFacts,
    StructuredFact,
    outside_detail_window,
)
from incident_report.rules.adapter import PartyIdentityMap, PartyKey


class ConsistencyIssue(BaseModel):
    """A party (or scene) whose contradictory evidence must not decide fault."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    subject: str
    fields: tuple[str, ...]
    reason: str
    cited_facts: tuple[CitedFact, ...]
    party_key: PartyKey | None = None  # default-ok: legacy issues carry only a display subject


def issue_applies(issue: ConsistencyIssue, party: str, party_key: PartyKey | None) -> bool:
    """Scoped generated issues follow identity; legacy subject-only issues keep compatibility."""
    if issue.subject == "scene":
        return True
    return issue.party_key == party_key if issue.party_key is not None else issue.subject == party


def issues_for_fields(
    party: str, fields: frozenset[str], issues: tuple[ConsistencyIssue, ...], *, party_key: PartyKey | None = None
) -> tuple[ConsistencyIssue, ...]:
    """A reading only abstains for contradictory inputs it actually depends on."""
    return tuple(i for i in issues if issue_applies(i, party, party_key) and fields.intersection(i.fields))


# Current typed vocabulary, spec 003 ticket 1. Unknown/missing answers do not
# assert a contradiction. Cross-field checks follow the adapter across Views;
# timestamps and reciprocal positions retain each View's own clock.
_UNKNOWN = {"unknown", "not_visible", "not_in_view", "not_applicable"}
_ROUNDABOUTS = {"roundabout_unsignalised", "roundabout_signalised"}
_TURNS = {"left_turn", "right_turn", "u_turn"}
_VEHICLE_MOVEMENTS = _TURNS | {
    "lane_change_left",
    "lane_change_right",
    "merging",
    "entering_from_side_road",
    "entering_from_car_park_or_driveway",
    "entering_roundabout",
    "circulating_roundabout",
    "exiting_roundabout",
}


# Project assumption, to be validated (ADR 0006; round-2 task): positions
# no more than 1.0 s apart may be compared; view clocks are never aligned.
PAIR_MAX_TIME_DELTA_SECONDS = 1.0
# Project assumption, to be validated (ADR 0006; spec 003 section 1b): contacts of the same pair this close
# in time are one contact, and an aftermath or interval time this close to a contact still matches it.
CONTACT_MATCH_SECONDS = 1.0
IMPOSSIBLE_PAIR_REASONS = {
    # With the same heading, each vehicle cannot be ahead of the other.
    ("B_ahead_same_direction", "B_ahead_same_direction"): "Both parties place the other ahead in the same direction.",
    # The same two headings cannot be both parallel and opposing.
    (
        "B_ahead_same_direction",
        "B_ahead_oncoming",
    ): "The parties describe their headings as both the same and oncoming.",
    # Mirror of the parallel-versus-opposing contradiction above.
    (
        "B_ahead_oncoming",
        "B_ahead_same_direction",
    ): "The parties describe their headings as both oncoming and the same.",
}
# Behind/behind can describe back-to-back reversing. Alongside/left/right
# readings do not establish a heading or longitudinal alignment: do not reject
# them merely because the core cannot decide fault from that combination.


@dataclass(frozen=True)
class NamedCheck:
    """One independently extensible known-answer consistency predicate."""

    name: str
    fields: tuple[str, str]
    predicate: Callable[[str, str], bool]
    reason: str


CHECKS = (
    NamedCheck(
        "roundabout_movement_state",
        ("movement", "roundabout_entry_state"),
        lambda a, b: a in {"circulating_roundabout", "entering_roundabout"} and b == "not_in_roundabout",
        "Roundabout movement contradicts the answer that the party is not in the roundabout.",
    ),
    NamedCheck(
        "side_road_origin",
        ("movement", "entry_origin"),
        lambda a, b: a == "entering_from_side_road" and b == "public_road_same_road",
        "Entry from a side road contradicts an origin on the same public road.",
    ),
    NamedCheck(
        "roundabout_zone",
        ("movement", "conflict_zone_type"),
        lambda a, b: (
            a in {"entering_roundabout", "circulating_roundabout", "exiting_roundabout"} and b not in _ROUNDABOUTS
        ),
        "Roundabout movement contradicts a conflict zone outside a roundabout.",
    ),
    NamedCheck(
        "roundabout_zone",
        ("roundabout_entry_state", "conflict_zone_type"),
        lambda a, b: a != "not_in_roundabout" and b not in _ROUNDABOUTS,
        "Roundabout state contradicts a conflict zone outside a roundabout.",
    ),
    NamedCheck(
        "signal_control",
        ("signal_state_when_crossing_stop_line", "approach_control_faced"),
        lambda a, b: b not in {"signal", "officer"},
        "A stop-line signal reading contradicts an approach controlled without a signal or officer.",
    ),
    NamedCheck(
        "signal_zone",
        ("approach_control_faced", "conflict_zone_type"),
        lambda a, b: a == "signal" and b in {"unsignalised_junction", "roundabout_unsignalised"},
        "A signal-controlled approach contradicts an unsignalised conflict zone.",
    ),
    NamedCheck(
        "pedestrian_movement",
        ("party_type", "movement"),
        lambda a, b: a == "pedestrian" and b in _VEHICLE_MOVEMENTS,
        "A pedestrian is assigned a vehicle manoeuvre.",
    ),
    NamedCheck(
        "stationary_turn",
        ("motion_state_before_contact", "movement"),
        lambda a, b: a == "parked_at_kerb" and b in _TURNS,
        "A kerb-parked party is described as turning.",
    ),
)


def _conflict(first: StructuredFact, second: StructuredFact) -> tuple[str, str] | None:
    for check in CHECKS:
        if check.fields == (first.field, second.field) and check.predicate(first.value, second.value):
            return check.name, check.reason
    return None


def _citations(facts: OverviewFacts) -> Iterator[tuple[tuple[str, ...], str, CitedFact]]:
    for group in (
        "parties",
        "unmatched_dense_parties",
        "events",
        "candidate_collisions",
        "behaviour_observations",
        "location_conditions",
    ):
        for record in getattr(facts, group):
            subject = record.description if group in {"parties", "unmatched_dense_parties"} else "scene"
            yield (
                (subject,),
                group,
                CitedFact.model_validate(
                    record.model_dump(include={"description", "view_id", "timestamp_seconds", "confidence"})
                ),
            )
    for record in facts.structured_facts:
        subjects = (record.subject, record.other_party) if record.other_party else (record.subject,)
        yield subjects, record.field, record.citation()
    for reading in facts.text_readings:
        yield ("scene",), "text_readings", reading.citation()
    for collision in facts.collision_observations:
        subjects = (collision.subject,)
        if collision.kind in {"vehicle_contact_vehicle", "vehicle_struck_pedestrian"}:
            subjects += (collision.other_party,)
        yield subjects, "collision_observations", collision.citation()
    for driver in facts.driver_observations:
        for cited in (driver.citation(), driver.citation(end=True)):
            yield (driver.subject,), "driver_observations", cited
    for cue in facts.drowsiness_observations:
        for cited in cue.citations():
            yield (cue.subject,), "drowsiness_observations", cited


def party_names_by_view(facts: OverviewFacts) -> dict[tuple[str, str], str]:
    """Resolve view-local names to existing adapter display labels."""
    from incident_report.rules.adapter import party_identities

    identities = party_identities(facts)
    return {local: identities.sources[key].description for local, key in identities.view_keys.items()}


def check_consistency(facts: OverviewFacts, view_durations: Mapping[str, float]) -> tuple[ConsistencyIssue, ...]:
    """Flag incompatible known typed answers, retaining their original citations."""
    issues: list[ConsistencyIssue] = []
    records = facts.structured_facts
    from incident_report.rules.adapter import party_identities

    identities = party_identities(facts)

    def subject(name: str, view_id: str) -> str:
        key = identities.key(name, view_id)
        return identities.sources[key].description if key in identities.sources else name

    for first in records:
        if first.value in _UNKNOWN or first.subject == "scene":
            continue
        for second in records:
            if second.value in _UNKNOWN or identities.key(second.subject, second.view_id) not in {
                identities.key(first.subject, first.view_id),
                ("scene", "", ""),
            }:
                continue
            conflict = _conflict(first, second)
            if conflict:
                code, reason = conflict
                issue = ConsistencyIssue(
                    code=code,
                    subject=subject(first.subject, first.view_id),
                    party_key=identities.key(first.subject, first.view_id),
                    fields=(first.field, second.field),
                    reason=reason,
                    cited_facts=(first.citation(), second.citation()),
                )
                if issue not in issues:
                    issues.append(issue)
    # Flag only explicitly impossible nearby position pairs. Unlisted pairs
    # remain possible; the core still owns its separate decision coverage.
    for first in records:
        if first.field != "relative_position_of_other" or first.value in _UNKNOWN:
            continue
        for second in records:
            if (
                second.field == first.field
                and identities.key(second.subject, second.view_id) == identities.key(first.other_party, first.view_id)
                and identities.key(second.other_party, second.view_id) == identities.key(first.subject, first.view_id)
                and second.view_id == first.view_id
                and second.value not in _UNKNOWN
                and abs(first.timestamp_seconds - second.timestamp_seconds) <= PAIR_MAX_TIME_DELTA_SECONDS
                and (first.value, second.value) in IMPOSSIBLE_PAIR_REASONS
            ):
                issues.append(
                    ConsistencyIssue(
                        code="pair_relations",
                        subject=subject(first.subject, first.view_id),
                        party_key=identities.key(first.subject, first.view_id),
                        fields=(first.field,),
                        reason=IMPOSSIBLE_PAIR_REASONS[(first.value, second.value)],
                        cited_facts=(first.citation(), second.citation()),
                    )
                )
    for subjects, field, cited in _citations(facts):
        duration = view_durations.get(cited.view_id)
        if duration is not None and not 0 <= cited.timestamp_seconds <= duration:
            for name in subjects:
                issues.append(
                    ConsistencyIssue(
                        code="timestamp_outside_view",
                        subject=subject(name, cited.view_id),
                        party_key=identities.key(name, cited.view_id) if name != "scene" else None,
                        fields=(field,),
                        reason=f"Cited time {cited.timestamp_seconds:g}s is outside the view duration {duration:g}s.",
                        cited_facts=(cited,),
                    )
                )
    issues.extend(_assessment_and_checklist_issues(facts, view_durations, identities, subject))
    return tuple(dict.fromkeys(issues))


# ---------------------------------------------------------------------------------------------------------
# overview-10: checklist answers (spec 003 ticket 3, card section 5) and the contact evidence contract
# (card section 1b C). Both run only on views that carry the overview-10 records; overview-1..9 facts,
# stored Reports and dense-added candidates have none, so they get no issues from here.
# ---------------------------------------------------------------------------------------------------------
_VEHICLE_MANOEUVRES = {
    "into_side_road",
    "out_of_side_road",
    "moving_off_from_kerb",
    "merging",
    "lane_change",
    "roundabout_entry",
    "circulating",
    "roundabout_exit",
    "u_turn",
}
_AFTERMATH_KINDS = {
    "vehicle_stopped_at_scene",
    "vehicle_driven_away",
    "vehicle_left_scene",
    "no_injury_visible",
    "person_injured_visible",
}
_CONTACT_STATES_WITH_TOUCH = {"contact_observed", "contact_possible_obscured"}
_UNKNOWN_ZONES = {"unknown", "other"}


def _row_citation(row: ChecklistParty, field: str, value: str, seen_at: float) -> CitedFact:
    return CitedFact(
        description=f"{row.display_name}: {field} = {value}",
        view_id=row.view_id,
        timestamp_seconds=seen_at,
        confidence=row.confidence,
    )


def _checklist_row_issues(
    row: ChecklistParty, zones: tuple[StructuredFact, ...], first_contact: float | None
) -> Iterator[tuple[str, tuple[str, ...], str, tuple[CitedFact, ...]]]:
    """(code, fields, reason, cited facts) for each contradiction in one checklist row."""
    here = _row_citation(row, "manoeuvre", row.manoeuvre, row.movement_seen_at_seconds)
    turn = _row_citation(row, "turn_direction", row.turn_direction, row.movement_seen_at_seconds)
    if row.manoeuvre in {"into_side_road", "lane_change", "merging"} and row.turn_direction == "straight":
        yield (
            "checklist_turn_direction",
            ("movement", "manoeuvre", "turn_direction"),
            f"The manoeuvre {row.manoeuvre.replace('_', ' ')} contradicts a straight turn direction.",
            (here, turn),
        )
    if row.manoeuvre in {"circulating", "roundabout_entry", "roundabout_exit"}:
        for zone in zones:
            if zone.value not in _ROUNDABOUTS and zone.value not in _UNKNOWN_ZONES:
                yield (
                    "checklist_roundabout_zone",
                    ("movement", "roundabout_entry_state", "conflict_zone_type"),
                    "A roundabout manoeuvre contradicts a conflict zone outside a roundabout.",
                    (here, zone.citation()),
                )
    if row.manoeuvre == "circulating" and row.road_class_on_approach not in {"roundabout", "unknown"}:
        yield (
            "checklist_circulating_road_class",
            ("movement", "road_class_on_approach"),
            "Circulating in a roundabout contradicts an approach road that is not a roundabout.",
            (
                here,
                _row_citation(row, "road_class_on_approach", row.road_class_on_approach, row.movement_seen_at_seconds),
            ),
        )
    position = _row_citation(
        row, "roundabout_exit_position", row.roundabout_exit_position, row.movement_seen_at_seconds
    )
    if (
        row.roundabout_exit_position in {"on_exit_curve_before_straight_lane", "reached_straight_lane_of_exit_road"}
        and row.manoeuvre != "roundabout_exit"
    ):
        yield (
            "checklist_exit_position",
            ("movement", "roundabout_exit_position"),
            "A roundabout exit position is given for a manoeuvre that is not a roundabout exit.",
            (here, position),
        )
    if row.manoeuvre == "roundabout_exit" and row.roundabout_exit_position == "not_applicable":
        yield (
            "checklist_exit_position",
            ("movement", "roundabout_exit_position"),
            "A roundabout exit has no exit position (not applicable).",
            (here, position),
        )
    road = _row_citation(row, "road_class_basis", row.road_class_basis, row.movement_seen_at_seconds)
    bad_basis = (
        (row.road_class_on_approach == "main_road" and row.road_class_basis != "more_than_two_lanes_and_long")
        or (row.road_class_on_approach == "side_road" and row.road_class_basis == "more_than_two_lanes_and_long")
        or (
            row.road_class_on_approach == "roundabout"
            and row.road_class_basis not in {"roundabout_carriageway", "unknown"}
        )
    )
    if bad_basis:
        yield (
            "checklist_road_class_basis",
            ("road_class_on_approach", "road_class_basis"),
            f"The road class {row.road_class_on_approach.replace('_', ' ')} contradicts its basis.",
            (
                _row_citation(row, "road_class_on_approach", row.road_class_on_approach, row.movement_seen_at_seconds),
                road,
            ),
        )
    body = _row_citation(row, "party_type", row.party_type, row.attributes_seen_at_seconds)
    if row.manoeuvre == "crossing_on_foot" and row.party_type != "pedestrian":
        yield (
            "checklist_pedestrian",
            ("movement", "party_type"),
            "Crossing on foot contradicts a road user that is not a pedestrian.",
            (here, body),
        )
    if row.party_type == "pedestrian" and row.manoeuvre in _VEHICLE_MANOEUVRES:
        yield (
            "checklist_pedestrian",
            ("movement", "party_type"),
            "A pedestrian is assigned a vehicle manoeuvre.",
            (here, body),
        )
    if row.marking == "taxi" and row.party_type in {"motorcycle", "bicycle", "pedestrian"}:
        yield (
            "checklist_taxi_marking",
            ("party_type", "marking"),
            f"A taxi marking contradicts a {row.party_type}.",
            (body, _row_citation(row, "marking", row.marking, row.attributes_seen_at_seconds)),
        )
    if row.movement_seen_at_seconds < row.first_seen_seconds or row.attributes_seen_at_seconds < row.first_seen_seconds:
        yield (
            "checklist_time_order",
            ("movement", "party_type"),
            "A checklist moment is earlier than the first moment the road user was seen.",
            (here, _row_citation(row, "first_seen_seconds", f"{row.first_seen_seconds:g}", row.first_seen_seconds)),
        )
    if first_contact is not None and row.movement_seen_at_seconds > first_contact:
        yield (
            "checklist_movement_after_contact",
            ("movement",),
            f"The movement moment {row.movement_seen_at_seconds:g}s is after the road user's first contact at "
            f"{first_contact:g}s.",
            (here,),
        )


_NEGATIVE_TRIGGERS = frozenset({"negative_state", "no_typed_contact"})
_LATE_TRIGGER = "contact_outside_detail_window"


def _recovery_failed_issues(
    facts: OverviewFacts,
    assessment: IncidentAssessment,
    contacts: list[CollisionObservation],
    add: Callable[..., None],
) -> None:
    """overview-11 (card section 4): the second, higher-detail request failed, so the first answer cannot stand as it
    is. A negative first answer is a scene issue (every party Cannot Determine, never No collision); a contact seen only
    outside the detail window makes the parties in that contact Cannot Determine."""
    view = assessment.view_id
    if _NEGATIVE_TRIGGERS & set(assessment.recovery_triggers):
        add(
            "recovery_failed",
            "scene",
            view,
            ("incident_assessment", "collision_observations"),
            "The second check of this view (whole clip at higher detail) failed, so the first answer, which saw no "
            "usable contact, cannot stand.",
            # No citation: the only candidate is the assessment, which carries no confidence, and a conclusion may not
            # cite a fact below the confidence threshold. The reason names the check that failed.
            (),
        )
    window = (assessment.detail_window_start_seconds, assessment.detail_window_end_seconds)
    if _LATE_TRIGGER in assessment.recovery_triggers and None not in window:
        listed = {r.display_name for r in facts.party_checklist if r.view_id == view}
        for contact in contacts:
            if not outside_detail_window(contact.timestamp_seconds, (window[0] or 0.0, window[1] or 0.0)):
                continue
            for name in dict.fromkeys((contact.subject, contact.other_party)):
                if name in listed:
                    add(
                        "recovery_failed",
                        name,
                        view,
                        ("collision_observations",),
                        "contact seen only at low frame rate; the detail check failed",
                        (contact.citation(),),
                    )


def _assessment_and_checklist_issues(
    facts: OverviewFacts,
    view_durations: Mapping[str, float],
    identities: PartyIdentityMap,
    subject: Callable[[str, str], str],
) -> list[ConsistencyIssue]:
    # Scope note (review N1): the 1b contact list is overview-only today because dense-2 returns no
    # collision_observations (it uses OVERVIEW_SCHEMA). Ticket 8, which puts dense output under the same contract,
    # must filter dense-added items out of the contact list used below.
    issues: list[ConsistencyIssue] = []

    def add(
        code: str,
        name: str,
        view_id: str,
        fields: tuple[str, ...],
        reason: str,
        cited: tuple[CitedFact, ...],
    ) -> None:
        issues.append(
            ConsistencyIssue(
                code=code,
                subject=name if name == "scene" else subject(name, view_id),
                party_key=None if name == "scene" else identities.key(name, view_id),
                fields=fields,
                reason=reason,
                cited_facts=cited,
            )
        )

    # --- checklist rows ---------------------------------------------------------------------------
    for row in facts.party_checklist:
        contact_times = [
            c.timestamp_seconds
            for c in facts.collision_observations
            if c.view_id == row.view_id and c.kind in CONTACT_KINDS and row.display_name in {c.subject, c.other_party}
        ]
        zones = tuple(
            s
            for s in facts.structured_facts
            if s.view_id == row.view_id and s.field == "conflict_zone_type" and s.value not in _UNKNOWN
        )
        for code, fields, reason, cited in _checklist_row_issues(
            row, zones, min(contact_times) if contact_times else None
        ):
            add(code, row.display_name, row.view_id, fields, reason, cited)
        duration = view_durations.get(row.view_id)
        if duration is not None:
            for field, moment in (
                ("first_seen_seconds", row.first_seen_seconds),
                ("attributes_seen_at_seconds", row.attributes_seen_at_seconds),
                ("movement_seen_at_seconds", row.movement_seen_at_seconds),
            ):
                if not 0 <= moment <= duration:
                    add(
                        "checklist_time_outside_view",
                        row.display_name,
                        row.view_id,
                        ("party_checklist",),
                        f"Checklist time {moment:g}s is outside the view duration {duration:g}s.",
                        (_row_citation(row, field, f"{moment:g}", moment),),
                    )
    for note in facts.observation_notes:
        duration = view_durations.get(note.view_id)
        if duration is not None and not 0 <= note.timestamp_seconds <= duration:
            add(
                "note_time_outside_view",
                note.subject,
                note.view_id,
                ("observation_notes",),
                f"Observation note time {note.timestamp_seconds:g}s is outside the view duration {duration:g}s.",
                (
                    CitedFact(
                        description=f"{note.subject}: {note.note}",
                        view_id=note.view_id,
                        timestamp_seconds=note.timestamp_seconds,
                        confidence=0.0,  # notes carry no confidence: 0.0 means none given, and no decision reads it
                    ),
                ),
            )

    # --- contact evidence contract, per assessed view --------------------------------------------
    for assessment in facts.incident_assessments:
        view = assessment.view_id
        located = CitedFact(
            description=f"incident assessment: {assessment.contact_state}",
            view_id=view,
            timestamp_seconds=assessment.contact_interval_start_seconds or 0.0,
            confidence=0.0,  # the assessment carries no confidence: 0.0 means none given, and no decision reads it
        )
        contacts = [c for c in facts.collision_observations if c.view_id == view and c.kind in CONTACT_KINDS]
        start, end = assessment.contact_interval_start_seconds, assessment.contact_interval_end_seconds
        if assessment.recovery == "failed":
            _recovery_failed_issues(facts, assessment, contacts, add)
        if assessment.contact_state in _CONTACT_STATES_WITH_TOUCH and start is not None and end is not None:
            if not any(
                start - CONTACT_MATCH_SECONDS <= c.timestamp_seconds <= end + CONTACT_MATCH_SECONDS for c in contacts
            ):
                add(
                    "assessment_contact_missing",
                    "scene",
                    view,
                    ("incident_assessment", "collision_observations"),
                    f"The assessment says {assessment.contact_state.replace('_', ' ')} between {start:g}s and {end:g}s "
                    "but no listed contact is inside that interval.",
                    (located,),
                )
        elif assessment.contact_state == "no_contact_observed" and contacts:
            add(
                "assessment_contact_unexpected",
                "scene",
                view,
                ("incident_assessment", "collision_observations"),
                "The assessment says no contact was observed but a contact is listed.",
                (located, *(c.citation() for c in contacts)),
            )
        duration = view_durations.get(view)
        if duration is not None and any(t is not None and not 0 <= t <= duration for t in (start, end)):
            add(
                "assessment_time_outside_view",
                "scene",
                view,
                ("incident_assessment",),
                f"The assessment contact interval is outside the view duration {duration:g}s.",
                (located,),
            )
        for obs in facts.collision_observations:
            if obs.view_id != view or obs.kind not in _AFTERMATH_KINDS:
                continue
            named = [c.timestamp_seconds for c in contacts if obs.subject in {c.subject, c.other_party}]
            if not named:
                add(
                    "aftermath_without_contact",
                    obs.subject,
                    view,
                    ("collision_observations",),
                    f"{obs.kind.replace('_', ' ')} is not tied to a contact this road user was in on the same view.",
                    (obs.citation(),),
                )
            elif obs.timestamp_seconds < min(named) - CONTACT_MATCH_SECONDS:
                add(
                    "aftermath_before_contact",
                    obs.subject,
                    view,
                    ("collision_observations",),
                    f"{obs.kind.replace('_', ' ')} at {obs.timestamp_seconds:g}s is earlier than this road user's "
                    f"first contact at {min(named):g}s.",
                    (obs.citation(),),
                )
        camera = next((r for r in facts.party_checklist if r.view_id == view and r.party_code == CAMERA_CODE), None)
        if camera is None:
            continue
        if assessment.camera_role == "fixed_camera":
            add(
                "camera_listed_for_fixed_camera",
                camera.display_name,
                view,
                ("incident_assessment", "party_checklist"),
                "A camera vehicle is listed although the camera is fixed.",
                (located, _row_citation(camera, "party_code", camera.party_code, camera.first_seen_seconds)),
            )
        if assessment.camera_role in {"fixed_camera", "witness_vehicle_not_involved"}:
            for c in contacts:
                if camera.display_name not in {c.subject, c.other_party}:
                    continue
                reason = (
                    f"The camera vehicle is in a contact although the camera is "
                    f"{assessment.camera_role.replace('_', ' ')}."
                )
                others = {c.subject, c.other_party} - {camera.display_name}
                named_others = [
                    o for o in others if c.kind in {"vehicle_contact_vehicle", "vehicle_struck_pedestrian"} and o
                ]
                for name in (camera.display_name, *named_others):
                    add(
                        "camera_in_contact",
                        name,
                        view,
                        ("incident_assessment", "collision_observations"),
                        reason,
                        (located, c.citation()),
                    )
    return issues
