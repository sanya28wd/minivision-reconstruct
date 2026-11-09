"""Violation mapping and fines calculation (RPT-17 round 2, spec 001 Seam 3, ADR 0006, ADR 0007).

Pure deterministic mapping from typed Violation inputs to official fines schedule rows (RPT-08).
All figures come directly from the FinesTable snapshot; values are never typed by hand.
Collision violations follow final fault decisions; explicit no-fixed-item mappings give none.
Without a collision, independently cited red crossings use the same official mapping,
without assigning collision Fault Status (RPT-26).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from incident_report.facts import CitedFact, DriverObservation, OverviewFacts
from incident_report.fines import Fine, FinesTable, Referral
from incident_report.rules.adapter import party_identities, situation_party_keys
from incident_report.rules.consistency import ConsistencyIssue, issues_for_fields, party_names_by_view
from incident_report.rules.core import VEHICLE_TYPES
from incident_report.rules.decree_law_14_2024 import (
    RULE_ART_5_1_F_CROSSWALK,
    RULE_ART_6_EMERGENCY,
    RULE_ART_6_MAIN_ROAD,
    RULE_ART_6_PRIORITY_LEFT_EQUAL,
    RULE_ART_6_ROUNDABOUT,
    RULE_ART_7_4_PEDESTRIAN_SPEED,
)
from incident_report.rules.driver import PHONE_RULE_ID, SEATBELT_RULE_ID
from incident_report.rules.engine import apply_consistency_issues, evaluate_situation
from incident_report.rules.models import (
    FaultStatus,
    IncidentViolationsSummary,
    PartyFault,
    PartyViolation,
    PartyViolationsSummary,
    Violation,
    ViolationTotals,
)
from incident_report.rules.mr130_1997 import (
    RULE_ENTRY,
    RULE_LANE_CHANGE,
    RULE_ONCOMING_LEFT_TURN,
    RULE_REAR_END,
    RULE_ROUNDABOUT_EXIT,
    RULE_ROUNDABOUT_EXIT_SIGNED,
    RULE_SIGNAL,
    RULE_STOP_GIVE_WAY,
)
from incident_report.rules.situation import Situation


class NoFixedItem(Enum):
    """The duty is proved, but video cannot select a fixed schedule item."""

    OFFICER_SELECTION = "officer_selection"


FineMapping = Mapping[str, str | NoFixedItem]


# Mapping from Decree-Law No. 14 of 2024 rule IDs to official schedule fine keys in FinesTable.
RULE_ID_TO_FINE_KEY: dict[str, str | NoFixedItem] = {
    PHONE_RULE_ID: "mobile_phone_distraction",
    SEATBELT_RULE_ID: "seatbelt",
    RULE_SIGNAL.rule_id: "red_light_light",
    RULE_ONCOMING_LEFT_TURN.rule_id: "wrong_turn",
    RULE_ROUNDABOUT_EXIT.rule_id: "priority_behind_left",
    RULE_ROUNDABOUT_EXIT_SIGNED.rule_id: "traffic_signs",
    RULE_STOP_GIVE_WAY.rule_id: "traffic_signs",
    RULE_ENTRY.rule_id: "failure_to_yield",
    RULE_LANE_CHANGE.rule_id: NoFixedItem.OFFICER_SELECTION,
    RULE_REAR_END.rule_id: "tailgating",
    RULE_ART_6_EMERGENCY.rule_id: "emergency_priority",
    RULE_ART_6_MAIN_ROAD.rule_id: "failure_to_yield",
    RULE_ART_6_PRIORITY_LEFT_EQUAL.rule_id: "priority_behind_left",
    RULE_ART_6_ROUNDABOUT.rule_id: "priority_behind_left",
    RULE_ART_5_1_F_CROSSWALK.rule_id: "pedestrian_priority",
    RULE_ART_7_4_PEDESTRIAN_SPEED.rule_id: "referral_art_39",
}

# C22/C43: the base table names heavy, light and motorcycle separately; no
# verified definition maps a generic bus or marked official vehicle to a class.
RULE_PARTY_TO_FINE_KEY: dict[tuple[str, str], str | NoFixedItem] = {
    (RULE_SIGNAL.rule_id, "car"): "red_light_light",
    (RULE_SIGNAL.rule_id, "taxi"): "red_light_light",
    (RULE_SIGNAL.rule_id, "truck_or_heavy"): "red_light_heavy",
    (RULE_SIGNAL.rule_id, "motorcycle"): "red_light_motorcycle",
    (RULE_SIGNAL.rule_id, "bus"): NoFixedItem.OFFICER_SELECTION,
    (RULE_SIGNAL.rule_id, "emergency_or_official_marked"): NoFixedItem.OFFICER_SELECTION,
}


def fine_key_for_party(rule_id: str, party_type: str | None, mapping: FineMapping) -> str | NoFixedItem | None:
    # None is compatibility for historical untyped callers only. Production
    # callers supply the cited Situation's types; explicit unknown never licenses a light fine.
    if party_type is not None and rule_id == RULE_SIGNAL.rule_id:
        if party_type not in VEHICLE_TYPES and party_type != "pedestrian":
            return NoFixedItem.OFFICER_SELECTION
        if (rule_id, party_type) in RULE_PARTY_TO_FINE_KEY:
            return RULE_PARTY_TO_FINE_KEY[rule_id, party_type]
    return mapping.get(rule_id)


@dataclass(frozen=True)
class FineResearchReference:
    """The source row and, where applicable, its criminal-referral article."""

    row_id: str
    referral_article: str | None


# Source rows in docs/research/UAE_ROAD_STRUCTURE_AND_PRIORITY.md section 2.2,
# or C-prefixed rows in RTA_RULES_CATALOGUE.md (ticket 9).
# Keep legacy mappings visible too: the research check must not silently exempt them.
# Referral context is explicit because R13b also lists administrative Item 89.
RULE_ID_TO_RESEARCH_ROW: dict[str, FineResearchReference] = {
    PHONE_RULE_ID: FineResearchReference("R18", None),
    SEATBELT_RULE_ID: FineResearchReference("R19", None),
    RULE_SIGNAL.rule_id: FineResearchReference("R2", None),
    RULE_ONCOMING_LEFT_TURN.rule_id: FineResearchReference("C1", None),
    RULE_ROUNDABOUT_EXIT.rule_id: FineResearchReference("R5", None),
    RULE_ROUNDABOUT_EXIT_SIGNED.rule_id: FineResearchReference("R3", None),
    RULE_STOP_GIVE_WAY.rule_id: FineResearchReference("R3", None),
    RULE_ENTRY.rule_id: FineResearchReference("R7", None),
    RULE_LANE_CHANGE.rule_id: FineResearchReference("R8", None),
    RULE_REAR_END.rule_id: FineResearchReference("R10", None),
    RULE_ART_6_EMERGENCY.rule_id: FineResearchReference("R1", None),
    RULE_ART_6_MAIN_ROAD.rule_id: FineResearchReference("R7", None),
    RULE_ART_6_PRIORITY_LEFT_EQUAL.rule_id: FineResearchReference("R4", None),
    RULE_ART_6_ROUNDABOUT.rule_id: FineResearchReference("R5", None),
    RULE_ART_5_1_F_CROSSWALK.rule_id: FineResearchReference("R13", None),
    RULE_ART_7_4_PEDESTRIAN_SPEED.rule_id: FineResearchReference("R13b", "39"),
}


def _lookup_fines_row(table: FinesTable, key: str) -> Fine | Referral:
    """Look up a fine row or referral by key, raising KeyError if not found."""
    try:
        return table.get(key)
    except KeyError:
        pass
    for ref in table.referrals:
        if ref.key == key:
            return ref
    raise KeyError(f"Unknown fine or referral key: {key!r}")


def map_violation(
    violation: Violation,
    fines_table: FinesTable | None = None,
    rule_mapping: FineMapping | None = None,
    *,
    party_type: str | None = None,
) -> PartyViolation:
    """Pure mapping from a typed violation to an official fines-table row (RPT-08).

    Requires an explicit mapping: an unmapped rule id raises KeyError (ADR 0006).
    A no-fixed-item entry raises ValueError; it cannot produce a schedule row.
    """
    table = fines_table if fines_table is not None else FinesTable.default()
    mapping = rule_mapping if rule_mapping is not None else RULE_ID_TO_FINE_KEY
    fine_key = fine_key_for_party(violation.rule_id, party_type, mapping)
    if fine_key is None:
        raise KeyError(f"Unmapped rule id: {violation.rule_id!r}")
    if isinstance(fine_key, NoFixedItem):
        raise ValueError("No fixed schedule item: officer selection required")
    row = _lookup_fines_row(table, fine_key)

    if isinstance(row, Fine):
        return PartyViolation(
            party_description=violation.party_description,
            rule_id=violation.rule_id,
            item_number=row.item_number,
            name_en=row.en,
            name_ar=row.ar,
            aed=row.aed,
            black_points=row.black_points,
            impound_days=row.impound_days,
            source_url=row.source_url,
            status=row.status,
            cited_facts=violation.cited_facts,
            notes=row.notes
            + (
                " item mapped by the owner (spec 003 decision 12); the item wording does not name this breach"
                if violation.rule_id == RULE_ONCOMING_LEFT_TURN.rule_id
                else ""
            ),
            research_reference=row.research_reference,
        )

    # Referral row
    return PartyViolation(
        party_description=violation.party_description,
        rule_id=violation.rule_id,
        item_number=row.item_number,
        name_en=row.en,
        name_ar=row.ar,
        aed=None,
        black_points=None,
        impound_days=None,
        source_url=row.source_url,
        status=row.status,
        cited_facts=violation.cited_facts,
        notes=row.notes,
        research_reference=row.research_reference,
    )


def violations_from_faults(
    faults: Sequence[PartyFault],
    fines_table: FinesTable | None = None,
    rule_mapping: FineMapping | None = None,
    party_types: Mapping[str, str] | None = None,
) -> tuple[PartyViolation, ...]:
    """Derive violations from the engine's fault decisions (RPT-17 round 2, ADR 0007).

    A party At Fault under rule R produces one Violation(rule_id=R, party, cited_facts of that decision).
    Not at Fault, Cannot Determine and explicit no-fixed-item entries produce no violations.
    Production callers supply party_types from the same facts' Situation. A missing
    name in that supplied map is unknown; None retains the legacy untyped interface.
    """
    table = fines_table if fines_table is not None else FinesTable.default()
    mapping = rule_mapping if rule_mapping is not None else RULE_ID_TO_FINE_KEY
    violations: list[PartyViolation] = []
    for fault in faults:
        if fault.status == FaultStatus.AT_FAULT and fault.rule_id is not None:
            party_type = party_types.get(fault.party_description, "unknown") if party_types is not None else None
            if isinstance(fine_key_for_party(fault.rule_id, party_type, mapping), NoFixedItem):
                continue
            v_input = Violation(
                rule_id=fault.rule_id,
                party_description=fault.party_description,
                cited_facts=fault.cited_facts,
            )
            pv = map_violation(v_input, fines_table=table, rule_mapping=mapping, party_type=party_type)
            if fault.agreement_status is not None or fault.agreement_detail is not None:
                pv = pv.model_copy(
                    update={
                        "agreement_status": fault.agreement_status,
                        "agreement_detail": fault.agreement_detail,
                    }
                )
            violations.append(pv)
    return tuple(violations)


# Typed dependencies of each standalone reading, not a blanket party veto.
SIGNAL_FIELDS = frozenset(
    {
        "party_type",
        "movement",
        "approach_control_faced",
        "signal_state_when_crossing_stop_line",
        "officer_directing_traffic",
        "conflict_zone_type",
        "visible_vehicle_markings",
        "emergency_lights",
        "emergency_siren",
        "parties",
    }
)
STANDALONE_FIELDS = {
    RULE_SIGNAL.rule_id: SIGNAL_FIELDS,
    PHONE_RULE_ID: frozenset({"driver_observations", "parties"}),
    SEATBELT_RULE_ID: frozenset({"driver_observations", "parties"}),
}


def filter_consistent_standalone_violations(
    violations: tuple[PartyViolation, ...],
    consistency_issues: tuple[ConsistencyIssue, ...],
    *,
    facts: OverviewFacts,
    source_facts: Sequence[OverviewFacts],
) -> tuple[PartyViolation, ...]:
    """Final evidence guards voted aliases using the existing typed identity key."""
    final_identities = party_identities(facts)
    source_identities = [(source, party_identities(source)) for source in source_facts]
    result = []
    for violation in violations:
        source_views = {c.view_id for c in violation.cited_facts}
        keys = {
            identities.key(p.description, p.view_id)
            for source, identities in source_identities
            for p in source.parties
            if p.description == violation.party_description and p.view_id in source_views
        }
        final_keys = {
            key
            for key in final_identities.sources
            for source_key in keys
            if key == source_key or (not any(source_key[1:]) and key[0] == source_key[0])
        }
        key = next(iter(final_keys)) if len(final_keys) == 1 else None
        # A dependent fine cannot use unresolved identity as permission to
        # bypass the final evidence guard. Independent field families remain
        # eligible; this adds no synthetic contradiction to the observations.
        if key is None and any(STANDALONE_FIELDS[violation.rule_id].intersection(i.fields) for i in consistency_issues):
            continue
        relevant = tuple(
            i.model_copy(update={"subject": violation.party_description})
            if i.party_key is None and key is not None and i.subject == final_identities.sources[key].description
            else i
            for i in consistency_issues
        )
        if not issues_for_fields(
            violation.party_description, STANDALONE_FIELDS[violation.rule_id], relevant, party_key=key
        ):
            result.append(violation)
    return tuple(result)


@dataclass(frozen=True)
class SignalCrossingReading:
    """A crossing observation: red, green, or explicit abstention (None)."""

    party_description: str
    is_red: bool | None
    cited_facts: tuple[CitedFact, ...]


def signal_crossing_readings(
    situation: Situation, *, consistency_issues: tuple[ConsistencyIssue, ...] = ()
) -> tuple[SignalCrossingReading, ...]:
    """Read only covered, cited light-vehicle straight crossings on a common view.

    Officer control, uncertain readings, emergency qualification and disjoint view
    clocks abstain. Green is an observed non-breach, not an omitted red reading. A cited
    none_confirmed approach has no signal question and produces no reading.
    """
    readings = []
    for party, key in zip(situation.parties, situation_party_keys(situation), strict=True):
        blocked = issues_for_fields(party.description, SIGNAL_FIELDS, consistency_issues, party_key=key)
        if blocked:
            readings.append(
                SignalCrossingReading(
                    party.description, None, tuple(dict.fromkeys(c for issue in blocked for c in issue.cited_facts))
                )
            )
            continue
        if party.approach_control_faced.value == "none_confirmed":
            continue
        fields = (
            situation.officer_directing_traffic,
            party.party_type,
            party.movement,
            party.approach_control_faced,
            party.signal_state_when_crossing_stop_line,
        )
        cited = tuple(dict.fromkeys(c for field in fields for c in field.evidence))
        if (
            situation.officer_directing_traffic.value != "no"
            or party.party_type.value not in VEHICLE_TYPES
            or party.movement.value != "straight"
            or party.approach_control_faced.value != "signal"
            or party.signal_state_when_crossing_stop_line.value not in {"red", "green"}
            or party.visible_vehicle_markings.value not in {"unknown", "none_visible"}
            or party.emergency_lights.value == "yes"
            or party.emergency_siren.value == "yes"
        ):
            readings.append(SignalCrossingReading(party.description, None, cited))
            continue
        common_views = set.intersection(*(set(c.view_id for c in field.supporting_evidence) for field in fields))
        if not common_views:
            readings.append(SignalCrossingReading(party.description, None, cited))
            continue
        readings.append(
            SignalCrossingReading(party.description, party.signal_state_when_crossing_stop_line.value == "red", cited)
        )
    return tuple(readings)


@dataclass(frozen=True)
class StandaloneViolationReading:
    """A cited breach/non-breach/abstention for the existing RPT-26 vote."""

    party_description: str
    rule_id: str
    is_breach: bool | None
    cited_facts: tuple[CitedFact, ...]


def _driver_intervals_overlap(a: DriverObservation, b: DriverObservation) -> bool:
    if a.start_seconds == a.end_seconds:
        return b.start_seconds <= a.start_seconds < b.end_seconds or b.start_seconds == b.end_seconds == a.start_seconds
    if b.start_seconds == b.end_seconds:
        return a.start_seconds <= b.start_seconds < a.end_seconds
    return max(a.start_seconds, b.start_seconds) < min(a.end_seconds, b.end_seconds)


def standalone_violation_readings(
    facts: OverviewFacts, situation: Situation, *, consistency_issues: tuple[ConsistencyIssue, ...] = ()
) -> tuple[StandaloneViolationReading, ...]:
    readings = [
        StandaloneViolationReading(r.party_description, RULE_SIGNAL.rule_id, r.is_red, r.cited_facts)
        for r in (
            ()
            if facts.candidate_collisions
            else signal_crossing_readings(situation, consistency_issues=consistency_issues)
        )
    ]
    party_names = party_names_by_view(facts)
    identities = party_identities(facts)
    for party in facts.parties:
        observations = [o for o in facts.driver_observations if o.subject == party.description]
        if not observations:
            continue
        handheld = [o for o in observations if o.kind == "phone_in_hand"]
        mounted = [o for o in observations if o.kind == "phone_on_mount"]
        absent = [o for o in observations if o.kind == "seatbelt_not_worn"]
        worn = [o for o in observations if o.kind == "seatbelt_worn"]
        # Wearing a belt later does not erase a visible earlier breach. Only
        # overlapping contradictory observations on the same view clock abstain.
        uncontradicted_absent = [
            a for a in absent if not any(a.view_id == w.view_id and _driver_intervals_overlap(a, w) for w in worn)
        ]
        for rule_id, breach, evidence in (
            (PHONE_RULE_ID, True if handheld else False if mounted else None, handheld or mounted),
            (
                SEATBELT_RULE_ID,
                True if uncontradicted_absent else False if worn and not absent else None,
                uncontradicted_absent or absent + worn,
            ),
        ):
            blocked = issues_for_fields(
                party_names.get((party.view_id, party.description), party.description),
                STANDALONE_FIELDS[rule_id],
                consistency_issues,
                party_key=identities.key(party.description, party.view_id),
            )
            readings.append(
                StandaloneViolationReading(
                    party.description,
                    rule_id,
                    None if blocked else breach,
                    tuple(
                        dict.fromkeys(
                            (*[o.citation() for o in evidence], *(c for issue in blocked for c in issue.cited_facts))
                        )
                    ),
                )
            )
    return tuple(readings)


def _violations_without_collision(
    situation: Situation, table: FinesTable, mapping: FineMapping, consistency_issues: tuple[ConsistencyIssue, ...]
) -> tuple[PartyViolation, ...]:
    """MR Art. 25(1): a red crossing needs no collision or causal inference."""
    violations = []
    for reading in signal_crossing_readings(situation, consistency_issues=consistency_issues):
        if reading.is_red is not True:
            continue
        violation = Violation(
            rule_id=RULE_SIGNAL.rule_id, party_description=reading.party_description, cited_facts=reading.cited_facts
        )
        party = next(p for p in situation.parties if p.description == reading.party_description)
        if not isinstance(fine_key_for_party(violation.rule_id, party.party_type.value, mapping), NoFixedItem):
            violations.append(
                map_violation(violation, fines_table=table, rule_mapping=mapping, party_type=party.party_type.value)
            )
    return tuple(violations)


def evaluate_violations(
    situation: Situation,
    fines_table: FinesTable | None = None,
    rule_mapping: FineMapping | None = None,
    *,
    consistency_issues: tuple[ConsistencyIssue, ...] = (),
) -> tuple[PartyViolation, ...]:
    """Pure deterministic function extracting and mapping all violations from a Situation."""
    if not situation.collisions:
        table = fines_table if fines_table is not None else FinesTable.default()
        mapping = rule_mapping if rule_mapping is not None else RULE_ID_TO_FINE_KEY
        return _violations_without_collision(situation, table, mapping, consistency_issues)
    faults = apply_consistency_issues(
        evaluate_situation(situation), consistency_issues, party_keys=situation_party_keys(situation)
    )
    return violations_from_faults(
        faults,
        fines_table=fines_table,
        rule_mapping=rule_mapping,
        party_types={p.description: p.party_type.value for p in situation.parties},
    )


def calculate_violation_totals(violations: Sequence[PartyViolation]) -> ViolationTotals:
    """Calculate running totals of fines and black points across a set of violations."""
    total_aed = 0
    court_decided_count = 0
    for v in violations:
        if v.aed is not None:
            total_aed += v.aed
        else:
            court_decided_count += 1

    total_points = sum(v.black_points for v in violations if v.black_points is not None)
    provisional_count = sum(1 for v in violations if v.is_provisional)
    flagged_count = sum(1 for v in violations if getattr(v, "agreement_status", None) == "flagged")

    return ViolationTotals(
        aed=total_aed,
        black_points=total_points,
        court_decided_count=court_decided_count,
        provisional_count=provisional_count,
        flagged_count=flagged_count,
    )


def summarize_violations(violations: Sequence[PartyViolation]) -> IncidentViolationsSummary:
    """Group violations by party and compute running totals per party and for the incident."""
    grouped: dict[str, list[PartyViolation]] = {}
    for v in violations:
        party_list = grouped.setdefault(v.party_description, [])
        party_list.append(v)

    party_summaries = tuple(
        PartyViolationsSummary(
            party_description=desc,
            violations=tuple(party_viols),
            totals=calculate_violation_totals(party_viols),
        )
        for desc, party_viols in grouped.items()
    )

    incident_totals = calculate_violation_totals(violations)
    return IncidentViolationsSummary(
        parties=party_summaries,
        totals=incident_totals,
    )
