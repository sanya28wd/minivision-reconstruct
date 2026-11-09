"""Seam 3: deterministic decisions and Findings from cited typed observations."""

from collections.abc import Sequence, Set
from typing import Any

from incident_report.facts import CitedFact, OverviewFacts
from incident_report.rules.adapter import PartyKey, party_identities, situation_from_overview
from incident_report.rules.catalogue import (
    main_road_rule,
    oncoming_left_turn_pattern,
    oncoming_left_turn_rule,
    roundabout_exit_pattern,
    roundabout_exit_rule,
)
from incident_report.rules.consistency import ConsistencyIssue, issue_applies
from incident_report.rules.core import (
    LANE_MOVEMENTS,
    REAR_RELATIONS,
    VEHICLE_TYPES,
    Assessment,
    emergency_rule,
    entry_rule,
    evidence,
    lane_change_rule,
    missing_fields,
    pair_fields,
    pedestrian_rule,
    rear_end_rule,
    roundabout_rule,
    signal_rule,
    stop_give_way_rule,
    uncontrolled_rule,
)
from incident_report.rules.models import FAULT_STATUS_COLORS, FaultStatus, PairSelection, PartyFault
from incident_report.rules.pair_selection import NOT_IN_PAIR_REASON, pair_situation, select_pair
from incident_report.rules.situation import Observed, Situation


def resolve(situation: Situation, assessments: tuple[Assessment, ...]) -> tuple[PartyFault, ...]:
    findings = tuple(f for assessment in assessments for f in assessment.findings)
    causal = tuple(f for f in findings if f.classification == "Finding")
    missing = tuple(dict.fromkeys(m for assessment in assessments for m in assessment.missing))
    cited = tuple(dict.fromkeys(f for assessment in assessments for f in assessment.evidence))
    parties_with_breaches = {f.party_description for f in findings}
    unresolved = any(a.explanation and not a.findings for a in assessments)
    reason = "; ".join(f"{name} unknown or unresolved" for name in missing)
    if len(parties_with_breaches) > 1:
        reason = "Both parties breached a duty; shared fault is Cannot Determine. " + reason
    elif not reason and not causal and findings:
        reason = "Causal condition not established from typed movements, pair relation and timestamps; Contributing Factor only."
    elif not reason:
        reason = "; ".join(a.explanation for a in assessments if a.explanation) or "No covered duty breach established."
    if unresolved:
        reason += " " + "; ".join(a.explanation for a in assessments if a.explanation and not a.findings)
    decided = bool(causal) and len(parties_with_breaches) == 1 and not missing and not unresolved
    result = []
    for party in situation.parties:
        own = tuple(f for f in findings if f.party_description == party.description)
        status = (
            (FaultStatus.AT_FAULT if own else FaultStatus.NOT_AT_FAULT) if decided else FaultStatus.CANNOT_DETERMINE
        )
        basis = causal[0] if decided else None
        messages = " ".join(f"{f.classification}: {f.reason}" for f in own)
        result.append(
            PartyFault(
                party_description=party.description,
                status=status,
                color=FAULT_STATUS_COLORS[status],
                rule_id=basis.rule_id if basis else None,
                legal_source=basis.legal_source if basis else None,
                article=basis.article if basis else None,
                reason=(basis.reason if basis else reason) + (" " + messages if messages else ""),
                cited_facts=cited,
                findings=own,
            )
        )
    return tuple(result)


def _vehicle_assessments(situation: Situation) -> tuple[Assessment, ...]:
    """Independent breaches are collected; unsupported applicability blocks a fault pair."""
    controls = tuple(p.approach_control_faced.value for p in situation.parties)
    lanes = any(p.movement.value in LANE_MOVEMENTS for p in situation.parties)
    relations = tuple(f.value for _, f in pair_fields(situation))
    rear = relations in REAR_RELATIONS
    assessments = []
    if lanes:
        assessments.append(lane_change_rule(situation))
    if rear and not lanes:
        assessments.append(rear_end_rule(situation))
    if rear and not lanes and any(p.movement.value != "straight" for p in situation.parties):
        assessments.append(
            Assessment((), (), (), "movement: turning or entry duties in a rear-end pair are unresolved.")
        )
    signal_indices = tuple(i for i, control in enumerate(controls) if control == "signal")
    if signal_indices:
        assessments.append(signal_rule(situation, signal_indices))
    if controls == ("signal", "signal"):
        if oncoming_left_turn_pattern(situation) and all(
            p.signal_state_when_crossing_stop_line.value == "green" for p in situation.parties
        ):
            assessments.append(oncoming_left_turn_rule(situation))
        elif any(p.movement.value not in {"straight", *LANE_MOVEMENTS, "unknown"} for p in situation.parties):
            assessments.append(Assessment((), (), (), "movement: turn-specific duties at signals are not established."))
    elif (
        roundabout_exit_pattern(situation)
        and situation.conflict_zone_type.value == "roundabout_unsignalised"
        and all(c in {"none_confirmed", "give_way_sign_or_line"} for c in controls)
    ):
        assessments.append(roundabout_exit_rule(situation))
    elif controls != ("none_confirmed", "none_confirmed"):
        if (
            not rear
            and any(c in {"stop_sign_or_line", "give_way_sign_or_line"} for c in controls)
            and all(c in {"stop_sign_or_line", "give_way_sign_or_line", "none_confirmed"} for c in controls)
        ):
            assessments.append(stop_give_way_rule(situation))
        else:
            assessments.append(Assessment((), (), (), "approach_control_faced: uncovered control combination."))
    elif not rear:
        if not lanes:
            if oncoming_left_turn_pattern(situation):
                assessments.append(oncoming_left_turn_rule(situation))
            elif situation.conflict_zone_type.value == "roundabout_unsignalised":
                assessments.append(roundabout_rule(situation))
            elif any(
                p.movement.value in {"entering_from_side_road", "entering_from_car_park_or_driveway"}
                for p in situation.parties
            ):
                if any(p.movement.value == "entering_from_side_road" for p in situation.parties) and any(
                    p.road_class_on_approach.is_known or p.road_class_basis.is_known for p in situation.parties
                ):
                    main = main_road_rule(situation)
                    assessments.append(main)
                    # One combined finding if the entry duty agrees. Preserve an
                    # independently established opposing breach for conflict resolution.
                    entry = entry_rule(situation)
                    if (
                        main.findings
                        and entry.findings
                        and {f.party_description for f in entry.findings}
                        != {f.party_description for f in main.findings}
                    ):
                        assessments.append(entry)
                else:
                    assessments.append(entry_rule(situation))
            elif situation.conflict_zone_type.value == "unsignalised_junction" and all(
                p.movement.value == "straight" for p in situation.parties
            ):
                assessments.append(uncontrolled_rule(situation))
            elif situation.conflict_zone_type.value == "mid_block" and all(
                p.movement.value == "straight" for p in situation.parties
            ):
                assessments.append(rear_end_rule(situation))
            else:
                assessments.append(Assessment((), (), (), "Uncovered typed-field combination."))
    if lanes and any(p.movement.value not in {"straight", *LANE_MOVEMENTS, "unknown"} for p in situation.parties):
        assessments.append(
            Assessment((), (), (), "movement: counterparty duties outside the lane-change rule are unresolved.")
        )
    return tuple(assessments)


def evaluate_situation(situation: Situation) -> tuple[PartyFault, ...]:
    if not situation.parties:
        return ()
    cited = evidence(situation)
    structural = None
    if not situation.collisions:
        structural = "No collision candidate found in facts."
    elif len(situation.collisions) > 1:
        structural = "Multi-collision incident: not covered."
    elif len(situation.parties) != 2:
        structural = (
            "Multi-party collision: not covered."
            if len(situation.parties) > 2
            else f"Expected two parties, found {len(situation.parties)}."
        )
    if structural:
        return resolve(situation, (Assessment((), (), cited, structural),))
    fields: list[tuple[str, Observed[Any]]] = [
        (f"party_type of {p.description}", p.party_type) for p in situation.parties
    ]
    unknown_types = missing_fields(fields)
    if unknown_types:
        return resolve(situation, (Assessment((), unknown_types, evidence(situation, *(f for _, f in fields)), ""),))
    types = tuple(p.party_type.value for p in situation.parties)
    emergency = emergency_rule(situation)
    if emergency is not None and (emergency.findings or emergency.explanation):
        outcomes = [emergency]
        if any(p.movement.value in LANE_MOVEMENTS for p in situation.parties):
            outcomes.append(lane_change_rule(situation))
        return resolve(situation, tuple(outcomes))
    fields += [(f"movement of {p.description}", p.movement) for p in situation.parties]
    fields += [
        (f"approach_control_faced of {p.description}", p.approach_control_faced)
        for p in situation.parties
        if p.party_type.value != "pedestrian"
    ]
    fields += [
        ("officer_directing_traffic", situation.officer_directing_traffic),
        ("conflict_zone_type", situation.conflict_zone_type),
    ]
    missing = missing_fields(fields)
    context = evidence(situation, *(f for _, f in fields))
    outcomes = []
    if emergency is not None:
        outcomes.append(emergency)
    if missing:
        outcomes.append(Assessment((), missing, context, ""))
    if situation.officer_directing_traffic.value == "yes":
        outcomes.append(Assessment((), (), context, "officer_directing_traffic: officer instructions are not covered."))
    elif types.count("pedestrian") == 1 and all(t in {"pedestrian", *VEHICLE_TYPES} for t in types):
        outcomes.append(pedestrian_rule(situation))
        drivers_with_signals = tuple(
            i
            for i, p in enumerate(situation.parties)
            if p.party_type.value != "pedestrian" and p.approach_control_faced.value == "signal"
        )
        if drivers_with_signals:
            outcomes.append(signal_rule(situation, drivers_with_signals))
    elif not all(t in VEHICLE_TYPES for t in types):
        outcomes.append(Assessment((), (), context, "party_type pairing is not covered by these core rules."))
    else:
        outcomes.extend(_vehicle_assessments(situation))
    with_context = []
    for assessment in outcomes:
        merged = tuple(dict.fromkeys((*context, *assessment.evidence)))
        with_context.append(
            Assessment(
                tuple(
                    f.model_copy(update={"cited_facts": tuple(dict.fromkeys((*merged, *f.cited_facts)))})
                    for f in assessment.findings
                ),
                assessment.missing,
                merged,
                assessment.explanation,
            )
        )
    return resolve(situation, tuple(with_context))


def _evaluate_selected_pair(situation: Situation, selection: PairSelection) -> tuple[PartyFault, ...]:
    """Assess only the selected pair; every other party is Cannot Determine, never blamed."""
    pair = {selection.subject, selection.other_party}
    selected_situation = pair_situation(situation, selection)
    evaluated = (
        evaluate_situation(selected_situation)
        if selection.corroborated
        else tuple(
            PartyFault(
                party_description=p.description,
                status=FaultStatus.CANNOT_DETERMINE,
                color=FAULT_STATUS_COLORS[FaultStatus.CANNOT_DETERMINE],
                reason="Collision pair not confirmed by the close-up check",
                cited_facts=selection.observations,
            )
            for p in selected_situation.parties
        )
    )
    pair_faults = {f.party_description: f.model_copy(update={"pair_selection": selection}) for f in evaluated}
    result = []
    for party in situation.parties:
        if party.description in pair:
            result.append(pair_faults[party.description])
            continue
        status = FaultStatus.CANNOT_DETERMINE
        result.append(
            PartyFault(
                party_description=party.description,
                status=status,
                color=FAULT_STATUS_COLORS[status],
                reason=f"{NOT_IN_PAIR_REASON} ({selection.subject}; {selection.other_party}).",
                cited_facts=selection.observations,
                pair_selection=selection,
            )
        )
    return tuple(result)


def _evaluate_fault(
    facts: OverviewFacts,
    *,
    dense_confirmed_pairs: Set[frozenset[str]] = frozenset(),
    consistency_issues: tuple[ConsistencyIssue, ...] = (),
) -> tuple[PartyFault, ...]:
    situation = situation_from_overview(facts)
    from incident_report.rules.drowsiness import assess_drowsiness, drowsiness_findings

    extra = drowsiness_findings(facts)
    assessments = assess_drowsiness(facts)
    selection = select_pair(facts, situation)
    if selection is not None:
        selection = selection.model_copy(
            update={"corroborated": frozenset((selection.subject, selection.other_party)) in dense_confirmed_pairs}
        )
    faults = _evaluate_selected_pair(situation, selection) if selection else evaluate_situation(situation)
    faults = (
        tuple(
            p.model_copy(
                update={
                    "findings": (*p.findings, *(f for f in extra if f.party_description == p.party_description)),
                    "reason": " ".join(
                        (
                            p.reason,
                            *(
                                a.reason
                                for a in assessments
                                if a.party_description == p.party_description and a.causal_role is not None
                            ),
                        )
                    ),
                    "cited_facts": tuple(
                        dict.fromkeys(
                            (
                                # Persist conclusion citations as their declared CitedFact
                                # shape; PartyFact subclasses serialize as CitedFact too.
                                *(
                                    CitedFact(
                                        description=c.description,
                                        view_id=c.view_id,
                                        timestamp_seconds=c.timestamp_seconds,
                                        confidence=c.confidence,
                                    )
                                    for c in p.cited_facts
                                ),
                                *(
                                    c
                                    for a in assessments
                                    if a.party_description == p.party_description
                                    for c in a.cited_facts
                                ),
                            )
                        )
                    ),
                }
            )
            for p in faults
        )
        if assessments
        else faults
    )
    if not facts.driver_observations or not facts.candidate_collisions:
        return faults
    from incident_report.rules.models import Finding, Violation
    from incident_report.rules.violations import map_violation, standalone_violation_readings

    findings = []
    for reading in standalone_violation_readings(facts, situation, consistency_issues=consistency_issues):
        if reading.is_breach is not True:
            continue
        row = map_violation(
            Violation(
                rule_id=reading.rule_id, party_description=reading.party_description, cited_facts=reading.cited_facts
            )
        )
        findings.append(
            Finding(
                party_description=reading.party_description,
                rule_id=reading.rule_id,
                legal_source="MR 178/2017 as amended",
                article=f"Schedule item {row.item_number}",
                source_url=row.source_url,
                classification="Contributing Factor",
                cited_facts=reading.cited_facts,
                reason=f"{row.name_en}; schedule status: {row.status}. Observed contributing factor; causal role unestablished.",
            )
        )
    return tuple(
        p.model_copy(
            update={"findings": (*p.findings, *(f for f in findings if f.party_description == p.party_description))}
        )
        for p in faults
    )


def apply_consistency_issues(
    faults: tuple[PartyFault, ...],
    issues: tuple[ConsistencyIssue, ...],
    *,
    party_keys: Sequence[PartyKey | None] = (),
) -> tuple[PartyFault, ...]:
    """Keep abstention authoritative, including after repeated-run output is applied."""
    result = []
    for index, party in enumerate(faults):
        key = party_keys[index] if index < len(party_keys) else None
        relevant = tuple(i for i in issues if issue_applies(i, party.party_description, key))
        if not relevant:
            result.append(party)
            continue
        status = FaultStatus.CANNOT_DETERMINE
        result.append(
            party.model_copy(
                update={
                    "status": status,
                    "color": FAULT_STATUS_COLORS[status],
                    "rule_id": None,
                    "legal_source": None,
                    "article": None,
                    "findings": (),
                    "reason": "Cannot Determine: inconsistent typed answers. "
                    + " ".join(dict.fromkeys(i.reason for i in relevant)),
                    "cited_facts": tuple(dict.fromkeys(c for i in relevant for c in i.cited_facts)),
                }
            )
        )
    return tuple(result)


def evaluate_fault(
    facts: OverviewFacts,
    *,
    dense_confirmed_pairs: Set[frozenset[str]] = frozenset(),
    consistency_issues: tuple[ConsistencyIssue, ...] = (),
) -> tuple[PartyFault, ...]:
    """Evaluate typed facts with externally supplied guards; no entry or dense reads."""
    return apply_consistency_issues(
        _evaluate_fault(facts, dense_confirmed_pairs=dense_confirmed_pairs, consistency_issues=consistency_issues),
        consistency_issues,
        party_keys=tuple(party_identities(facts).sources),
    )
