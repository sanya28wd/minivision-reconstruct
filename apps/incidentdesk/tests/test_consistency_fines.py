"""Contradictory inputs abstain from dependent standalone fine readings."""

import pytest

from incident_report.agreement import agree_no_collision_violations
from incident_report.facts import DriverObservation
from incident_report.rules.adapter import situation_from_overview
from incident_report.rules.consistency import ConsistencyIssue, check_consistency
from incident_report.rules.driver import PHONE_RULE_ID
from incident_report.rules.engine import evaluate_fault
from incident_report.rules.mr130_1997 import RULE_SIGNAL
from incident_report.rules.violations import (
    evaluate_violations,
    signal_crossing_readings,
    standalone_violation_readings,
)
from test_collision_pair_selection import A, B, sf, signal_facts


def test_direct_red_reading_and_fine_abstain_for_signal_zone_issue() -> None:
    facts = signal_facts((A, B), [], [])
    facts = facts.model_copy(
        update={
            "structured_facts": [
                r.model_copy(update={"value": "unsignalised_junction"}) if r.field == "conflict_zone_type" else r
                for r in facts.structured_facts
            ]
        }
    )
    issues = check_consistency(facts, {"exterior": 5.0})
    situation = situation_from_overview(facts)
    assert any(r.is_red is True for r in signal_crossing_readings(situation))
    readings = signal_crossing_readings(situation, consistency_issues=issues)
    assert all(r.is_red is None for r in readings)
    assert issues[0].cited_facts[0] in readings[0].cited_facts
    assert evaluate_violations(situation, consistency_issues=issues) == ()


@pytest.mark.parametrize("n_runs", [1, 3])
def test_standalone_vote_never_fines_from_contradictory_signal_answers(n_runs: int) -> None:
    facts = signal_facts((A, B), [], [])
    facts = facts.model_copy(
        update={
            "structured_facts": [*facts.structured_facts, sf(A, "roundabout_entry_state", "already_inside_circulating")]
        }
    )
    issues = check_consistency(facts, {"exterior": 5.0})
    faults = evaluate_fault(facts, consistency_issues=issues)
    violations, fields = agree_no_collision_violations(
        [facts] * n_runs, [faults] * n_runs, consistency_issues=[issues] * n_runs
    )
    assert not violations
    assert fields and all(f.agreed_value is not True for f in fields.values())


@pytest.mark.parametrize(
    "subject,fields,blocked",
    [
        (A, ("approach_control_faced",), True),
        (A, ("visible_vehicle_markings",), True),
        ("scene", ("conflict_zone_type",), True),
        (B, ("approach_control_faced",), False),
        (A, ("motion_state_before_contact",), False),
    ],
)
def test_signal_guard_only_blocks_fields_and_parties_the_reading_depends_on(
    subject: str, fields: tuple[str, ...], blocked: bool
) -> None:
    facts = signal_facts((A, B), [], [])
    issue = ConsistencyIssue(
        code="synthetic-check",
        subject=subject,
        fields=fields,
        reason="Synthetic conflicting answer",
        cited_facts=(facts.structured_facts[0].citation(),),
    )
    readings = signal_crossing_readings(situation_from_overview(facts), consistency_issues=(issue,))
    first = next(r for r in readings if r.party_description == A)
    assert first.is_red is (None if blocked else True)


@pytest.mark.parametrize(
    "field,expected", [("approach_control_faced", PHONE_RULE_ID), ("driver_observations", RULE_SIGNAL.rule_id)]
)
def test_independent_standalone_breach_survives_unrelated_issue(field: str, expected: str) -> None:
    facts = signal_facts((A, B), [], []).model_copy(
        update={
            "driver_observations": [
                DriverObservation(
                    subject=A,
                    kind="phone_in_hand",
                    view_id="interior",
                    start_seconds=0.0,
                    end_seconds=2.0,
                    confidence=0.9,
                )
            ]
        }
    )
    issue = ConsistencyIssue(
        code="synthetic-check",
        subject=A,
        fields=(field,),
        reason="Synthetic conflicting answer",
        cited_facts=(facts.structured_facts[0].citation(),),
    )
    readings = standalone_violation_readings(facts, situation_from_overview(facts), consistency_issues=(issue,))
    assert {r.rule_id for r in readings if r.is_breach is True} == {expected}


def test_driver_issue_also_guards_a_view_local_party_alias() -> None:
    facts = signal_facts((A, B), [], [])
    source = facts.parties[0].model_copy(update={"color": "white", "body_type": "SUV"})
    alias = source.model_copy(update={"description": "white sport utility vehicle", "view_id": "interior"})
    facts = facts.model_copy(
        update={
            "parties": [source, facts.parties[1], alias],
            "driver_observations": [
                DriverObservation(
                    subject=alias.description,
                    kind="phone_in_hand",
                    view_id="interior",
                    start_seconds=0.0,
                    end_seconds=2.0,
                    confidence=0.9,
                )
            ],
        }
    )
    issue = ConsistencyIssue(
        code="synthetic-check",
        subject=A,
        fields=("driver_observations",),
        reason="Synthetic conflicting driver evidence",
        cited_facts=(facts.structured_facts[0].citation(),),
    )
    readings = standalone_violation_readings(facts, situation_from_overview(facts), consistency_issues=(issue,))
    assert not any(r.rule_id == PHONE_RULE_ID and r.is_breach is True for r in readings)


def test_issues_do_not_suppress_other_typed_party_with_the_same_display_name() -> None:
    from incident_report.facts import OverviewFacts, PartyFact

    name = "shared vehicle"
    parties = [
        PartyFact(description=name, color=color, body_type=body, view_id=view, timestamp_seconds=0.0, confidence=0.9)
        for color, body, view in [("white", "SUV", "one"), ("red", "sedan", "two")]
    ]
    records = [
        sf(name, "party_type", "pedestrian").model_copy(update={"view_id": "one"}),
        sf(name, "movement", "left_turn").model_copy(update={"view_id": "one"}),
    ]
    records += [
        sf(subject, field, value).model_copy(update={"view_id": "two"})
        for subject, field, value in [
            (name, "party_type", "car"),
            (name, "movement", "straight"),
            (name, "approach_control_faced", "signal"),
            (name, "signal_state_when_crossing_stop_line", "red"),
            ("scene", "officer_directing_traffic", "no"),
            ("scene", "conflict_zone_type", "signalised_junction"),
        ]
    ]
    facts = OverviewFacts(
        parties=parties,
        events=[],
        candidate_collisions=[],
        behaviour_observations=[],
        location_conditions=[],
        structured_facts=records,
    )
    issues = check_consistency(facts, {"one": 5.0, "two": 5.0})
    readings = signal_crossing_readings(situation_from_overview(facts), consistency_issues=issues)
    assert [r.is_red for r in readings] == [None, True]
    faults = evaluate_fault(facts, consistency_issues=issues)
    assert "inconsistent typed answers" in faults[0].reason
    assert "inconsistent typed answers" not in faults[1].reason
    violations = evaluate_violations(situation_from_overview(facts), consistency_issues=issues)
    assert len(violations) == 1 and {c.view_id for c in violations[0].cited_facts} == {"two"}


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unresolved_voted_identity_cannot_bypass_a_relevant_keyed_issue(ambiguous: bool) -> None:
    from incident_report.facts import OverviewFacts
    from incident_report.rules.adapter import party_identities
    from incident_report.rules.models import Violation
    from incident_report.rules.violations import filter_consistent_standalone_violations, map_violation

    source = signal_facts((A, B), [], [])
    first = source.parties[0].model_copy(update={"color": "white", "body_type": "SUV"})
    source = source.model_copy(update={"parties": [first, source.parties[1]]})
    final_parties = (
        [first, first.model_copy(update={"description": "other white SUV"})] if ambiguous else [source.parties[1]]
    )
    final = OverviewFacts(
        parties=final_parties, events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
    )
    issue_key = next(iter(party_identities(final).sources))
    issue = ConsistencyIssue(
        code="signal_zone",
        subject=final_parties[0].description,
        party_key=issue_key,
        fields=("approach_control_faced", "conflict_zone_type"),
        reason="Synthetic contradictory signal answers",
        cited_facts=(source.structured_facts[0].citation(),),
    )
    violation = map_violation(
        Violation(
            party_description=A, rule_id=RULE_SIGNAL.rule_id, cited_facts=(source.structured_facts[0].citation(),)
        )
    )
    assert filter_consistent_standalone_violations((violation,), (issue,), facts=final, source_facts=[source]) == ()
