"""Tests for preserving engine reason when runs agree on Cannot Determine (RPT-36 T6).

Tests:
1. When a party's engine result is Cannot Determine with a reason and cited facts,
   and agreement agrees on Cannot Determine:
   - Final reason contains "Runs agreed on Cannot Determine" and the engine reason.
   - Cited facts match the engine's cited facts.
   - Status is Cannot Determine, color is grey, agreement_status is "flagged".
2. When a party's engine result is At Fault, but agreement says Cannot Determine:
   - Final reason is today's text: f"Runs agreed on Cannot Determine ({details})."
   - Cited facts is empty tuple.
   - Status is Cannot Determine, color is grey, agreement_status is "flagged".
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from typing import Any

import pytest

import incident_report.entry as entry_mod
from incident_report.agreement import (
    AgreementResult,
    AgreementSettings,
    AgreementStatus,
    FieldAgreement,
    party_canonical_key,
)
from incident_report.ai import AIConnection, AIRequest, AIResponse
from incident_report.domain import Origin, Role, View
from incident_report.entry import analyse_incident
from incident_report.facts import CitedFact, OverviewFacts, PartyFact
from incident_report.hashing import sha256_hex
from incident_report.rules.models import FaultStatus, PartyFault
from incident_report.runlog import RunLog
from video_fixtures import synthetic_video


class FakeMultiRunAI(AIConnection):
    """Returns predetermined AIResponse on successive calls, with synthetic provenance."""

    def __init__(self, responses: Sequence[AIResponse]) -> None:
        self._responses = list(responses)
        self._call_count = 0
        self.calls_received: list[AIRequest] = []

    def analyse(self, request: AIRequest) -> AIResponse:
        self.calls_received.append(request)
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
        else:
            resp = self._responses[-1]
        self._call_count += 1
        return resp


def _make_view(content: bytes = b"test-clip-keep-engine-reason") -> tuple[View, str]:
    video_bytes = synthetic_video(content)
    h = sha256_hex(video_bytes)
    return (
        View(
            stream=io.BytesIO(video_bytes),
            filename="front.mp4",
            role=Role.EXTERIOR,
            duration_seconds=5.0,
        ),
        h,
    )


def test_keep_engine_reason_when_runs_agree_on_cannot_determine(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    view, view_hash = _make_view(b"clip-cd-engine-reason")
    p = PartyFact(
        description="white SUV",
        color="white",
        make_model="Toyota RAV4",
        body_type="suv",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    c = CitedFact(description="contact moment", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[c]
    )
    resp = AIResponse(summary="Summary", provenance="synthetic-fixture", facts=facts)

    engine_cited = (c,)
    engine_reason = "movement of white SUV unknown"

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        return (
            PartyFault(
                party_description="white SUV",
                status=FaultStatus.CANNOT_DETERMINE,
                color="grey",
                rule_id=None,
                reason=engine_reason,
                cited_facts=engine_cited,
            ),
        )

    def mock_compute_agreement(*args: Any, **kwargs: Any) -> AgreementResult:
        p_key = party_canonical_key(p)
        return AgreementResult(
            n_runs=3,
            status=AgreementStatus.FLAGGED,
            fields={
                f"fault_status:{p_key}": FieldAgreement(
                    field_name=f"fault_status:{p_key}",
                    status=AgreementStatus.FLAGGED,
                    agreed_value=(FaultStatus.CANNOT_DETERMINE.value, None),
                    runs_total=3,
                    runs_agreeing=2,
                    details="2 of 3 runs agree (majority).",
                )
            },
        )

    monkeypatch.setattr(entry_mod, "evaluate_fault", mock_evaluate_fault)
    monkeypatch.setattr(entry_mod, "compute_agreement", mock_compute_agreement)

    ai = FakeMultiRunAI([resp, resp, resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )

    assert len(report.parties) == 1
    party = report.parties[0]
    assert party.status == FaultStatus.CANNOT_DETERMINE
    assert party.color == "grey"
    assert party.agreement_status == "flagged"
    assert "Runs agreed on Cannot Determine" in party.reason
    assert "movement of white SUV unknown" in party.reason
    assert (
        party.reason
        == "Runs agreed on Cannot Determine (2 of 3 runs agree (majority).). Reason: movement of white SUV unknown"
    )
    assert party.cited_facts == engine_cited


def test_overruled_at_fault_does_not_keep_engine_reason(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    view, view_hash = _make_view(b"clip-overruled-at-fault")
    p = PartyFact(
        description="white SUV",
        color="white",
        make_model="Toyota RAV4",
        body_type="suv",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    c = CitedFact(description="contact moment", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[c]
    )
    resp = AIResponse(summary="Summary", provenance="synthetic-fixture", facts=facts)

    engine_cited = (c,)
    engine_reason = "Failed to yield at roundabout"

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        return (
            PartyFault(
                party_description="white SUV",
                status=FaultStatus.AT_FAULT,
                color="pink",
                rule_id="RULE-ROUNDABOUT-1",
                legal_source="Federal Traffic Law",
                article="12",
                reason=engine_reason,
                cited_facts=engine_cited,
            ),
        )

    def mock_compute_agreement(*args: Any, **kwargs: Any) -> AgreementResult:
        p_key = party_canonical_key(p)
        return AgreementResult(
            n_runs=3,
            status=AgreementStatus.FLAGGED,
            fields={
                f"fault_status:{p_key}": FieldAgreement(
                    field_name=f"fault_status:{p_key}",
                    status=AgreementStatus.FLAGGED,
                    agreed_value=(FaultStatus.CANNOT_DETERMINE.value, None),
                    runs_total=3,
                    runs_agreeing=2,
                    details="2 of 3 runs agree (majority).",
                )
            },
        )

    monkeypatch.setattr(entry_mod, "evaluate_fault", mock_evaluate_fault)
    monkeypatch.setattr(entry_mod, "compute_agreement", mock_compute_agreement)

    ai = FakeMultiRunAI([resp, resp, resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )

    assert len(report.parties) == 1
    party = report.parties[0]
    assert party.status == FaultStatus.CANNOT_DETERMINE
    assert party.color == "grey"
    assert party.agreement_status == "flagged"
    assert party.reason == "Runs agreed on Cannot Determine (2 of 3 runs agree (majority).)."
    assert party.cited_facts == ()
