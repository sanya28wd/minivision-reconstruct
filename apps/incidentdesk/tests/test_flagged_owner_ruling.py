"""Owner ruling 2026-10-16, FIX-RPT23 review: FLAGGED majority beats final evaluation."""

import pytest

from incident_report.agreement import AgreementSettings, AgreementStatus, party_canonical_key
from incident_report.ai import AIResponse
from incident_report.domain import Origin
from incident_report.entry import analyse_incident
from incident_report.facts import CitedFact, OverviewFacts, PartyFact
from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD
from incident_report.rules.models import FaultStatus, PartyFault
from incident_report.runlog import RunLog
from test_agreement_signal import FakeMultiRunAI, _make_view


@pytest.mark.parametrize("majority", [FaultStatus.AT_FAULT, FaultStatus.NOT_AT_FAULT])
def test_flagged_majority_wins_when_final_evaluation_decides_opposite(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch, majority: FaultStatus
) -> None:
    view, digest = _make_view(b"RPT-42-owner-ruling")
    party = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id=digest,
        timestamp_seconds=0.0,
        confidence=1.0,
    )
    collision = CitedFact(description="Synthetic contact", view_id=digest, timestamp_seconds=0.0, confidence=1.0)
    minority = FaultStatus.NOT_AT_FAULT if majority is FaultStatus.AT_FAULT else FaultStatus.AT_FAULT

    def response(status: FaultStatus) -> AIResponse:
        return AIResponse(
            summary="Synthetic agreement scenario",
            provenance="synthetic",
            facts=OverviewFacts(
                parties=[party],
                events=[collision],
                candidate_collisions=[collision],
                location_conditions=[],
                behaviour_observations=[collision.model_copy(update={"description": status.value})],
            ),
        )

    evaluated: list[FaultStatus] = []

    def fault(
        facts: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        # Rule-engine stub keyed by input, matching existing seam-1 agreement tests.
        # The primary facts are also the final consolidated input in this scenario.
        status = FaultStatus(facts.behaviour_observations[0].description)
        evaluated.append(status)
        return (
            PartyFault(
                party_description=party.description,
                status=status,
                color="pink" if status is FaultStatus.AT_FAULT else "green",
                rule_id=RULE_ART_6_MAIN_ROAD.rule_id if status is FaultStatus.AT_FAULT else None,
                reason=f"Synthetic agreeing evidence: {status.value}",
                cited_facts=(collision,),
                legal_source=RULE_ART_6_MAIN_ROAD.legal_source if status is FaultStatus.AT_FAULT else None,
                article=RULE_ART_6_MAIN_ROAD.article if status is FaultStatus.AT_FAULT else None,
            ),
        )

    monkeypatch.setattr("incident_report.entry.evaluate_fault", fault)
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=FakeMultiRunAI([response(minority), response(majority), response(majority)]),
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )
    assert evaluated == [minority, majority, majority, minority]
    assert report.agreement is not None
    vote = report.agreement.fields[f"fault_status:{party_canonical_key(party)}"]
    assert (vote.status, vote.runs_agreeing, vote.runs_total) == (AgreementStatus.FLAGGED, 2, 3)
    assert report.parties[0].status is majority
    assert report.parties[0].agreement_status == "flagged"
    assert report.parties[0].reason == f"Synthetic agreeing evidence: {majority.value}"
    assert report.parties[0].cited_facts == (collision,)
