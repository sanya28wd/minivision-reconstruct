"""Tests for the agreement signal (RPT-23, spec 001, ADR 0004, ADR 0006).

Tests:
- Unanimous agreement (3/3) gives status trusted.
- Majority agreement (2/3) gives status flagged.
- Split agreement (e.g. 1/1/1 or tie) gives status Cannot Determine.
- Abstention (unknown / omitted) is counted as its own distinct outcome.
- N = 1 skips agreement entirely with status not measured (never trusted).
- Timestamp tolerance: timestamps within 1.0 second count as agreeing.
- Cost, latency, and call count tracking for N runs.
- Fault status override to Cannot Determine on split across runs.
- Template rendering with amber "Check" badge for flagged fields.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from incident_report.agreement import (
    AgreementResult,
    AgreementSettings,
    AgreementStatus,
    FieldAgreement,
    compare_collisions,
    compare_events,
    compare_fault,
    compare_parties,
    compute_agreement,
    evaluate_majority,
    party_canonical_key,
)
from incident_report.ai import AIConnection, AIRequest, AIResponse
from incident_report.domain import Origin, Report, Role, View
from incident_report.entry import PROMPT_VERSION, analyse_incident
from incident_report.facts import CitedFact, OverviewFacts, PartyFact
from incident_report.hashing import sha256_hex
from incident_report.model_calls import ModelCall
from incident_report.rules.models import FaultStatus, PartyFault
from incident_report.runlog import RunLog
from video_fixtures import synthetic_video

# ---------------------------------------------------------------------------
# Unit tests: evaluate_majority and core agreement logic
# ---------------------------------------------------------------------------


def test_evaluate_majority_unanimous_3_of_3_is_trusted() -> None:
    outcomes = ["At Fault", "At Fault", "At Fault"]
    res = evaluate_majority(outcomes, field_name="fault:car1", n_runs=3)
    assert res.status == AgreementStatus.TRUSTED
    assert res.agreed_value == "At Fault"
    assert res.runs_total == 3
    assert res.runs_agreeing == 3


def test_evaluate_majority_2_of_3_is_flagged() -> None:
    outcomes = ["At Fault", "At Fault", "Cannot Determine"]
    res = evaluate_majority(outcomes, field_name="fault:car1", n_runs=3)
    assert res.status == AgreementStatus.FLAGGED
    assert res.agreed_value == "At Fault"
    assert res.runs_total == 3
    assert res.runs_agreeing == 2


def test_evaluate_majority_split_3_runs_is_cannot_determine() -> None:
    outcomes = ["At Fault", "Not at Fault", "Cannot Determine"]
    res = evaluate_majority(outcomes, field_name="fault:car1", n_runs=3)
    assert res.status == AgreementStatus.CANNOT_DETERMINE
    assert res.agreed_value == FaultStatus.CANNOT_DETERMINE.value
    assert res.runs_total == 3
    assert res.runs_agreeing == 1


def test_evaluate_majority_abstention_counted_as_own_outcome() -> None:
    # 2 runs say absent/unknown, 1 run says At Fault -> 2/3 agree on absent!
    outcomes = ["unknown", "unknown", "At Fault"]
    res = evaluate_majority(outcomes, field_name="fault:car1", n_runs=3)
    assert res.status == AgreementStatus.FLAGGED
    assert res.agreed_value == "unknown"
    assert res.runs_total == 3
    assert res.runs_agreeing == 2


def test_evaluate_majority_n_equals_1_is_not_measured_never_trusted() -> None:
    outcomes = ["At Fault"]
    res = evaluate_majority(outcomes, field_name="fault:car1", n_runs=1)
    assert res.status == AgreementStatus.NOT_MEASURED
    assert res.agreed_value == "At Fault"
    assert res.runs_total == 1
    assert res.runs_agreeing == 1


def test_evaluate_majority_tie_in_even_runs_is_cannot_determine() -> None:
    outcomes = ["At Fault", "At Fault", "Not at Fault", "Not at Fault"]
    res = evaluate_majority(outcomes, field_name="fault:car1", n_runs=4)
    assert res.status == AgreementStatus.CANNOT_DETERMINE
    assert res.runs_total == 4
    assert res.runs_agreeing == 2


# ---------------------------------------------------------------------------
# Unit tests: Fact comparators with timestamp tolerance
# ---------------------------------------------------------------------------


def test_compare_collisions_within_tolerance_agree() -> None:
    # 1.0s vs 1.2s vs 0.8s: all within 1.0s of center -> 3/3 agree
    c1 = [CitedFact(description="impact", view_id="v1", timestamp_seconds=1.0, confidence=0.9)]
    c2 = [CitedFact(description="contact", view_id="v1", timestamp_seconds=1.2, confidence=0.9)]
    c3 = [CitedFact(description="collision", view_id="v1", timestamp_seconds=0.8, confidence=0.9)]

    facts_list = [
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=c1
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=c2
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=c3
        ),
    ]

    agreements = compare_collisions(facts_list, tolerance=1.0)
    assert len(agreements) == 1
    coll_agr = list(agreements.values())[0]
    assert coll_agr.status == AgreementStatus.TRUSTED
    assert coll_agr.runs_agreeing == 3
    assert coll_agr.runs_total == 3


def test_compare_collisions_outside_tolerance_split() -> None:
    # 1.0s vs 5.0s vs 10.0s: none within 1.0s -> all separate -> 1/3 each -> Cannot Determine
    c1 = [CitedFact(description="impact", view_id="v1", timestamp_seconds=1.0, confidence=0.9)]
    c2 = [CitedFact(description="impact", view_id="v1", timestamp_seconds=5.0, confidence=0.9)]
    c3 = [CitedFact(description="impact", view_id="v1", timestamp_seconds=10.0, confidence=0.9)]

    facts_list = [
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=c1
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=c2
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=c3
        ),
    ]

    agreements = compare_collisions(facts_list, tolerance=1.0)
    assert len(agreements) >= 1
    # None should be trusted
    assert all(a.status == AgreementStatus.CANNOT_DETERMINE for a in agreements.values())


def test_compare_events_within_tolerance_agree() -> None:
    e1 = [CitedFact(description="vehicle enters junction", view_id="v1", timestamp_seconds=2.0, confidence=0.8)]
    e2 = [CitedFact(description="vehicle enters junction", view_id="v1", timestamp_seconds=2.5, confidence=0.8)]
    e3 = [CitedFact(description="vehicle enters junction", view_id="v1", timestamp_seconds=1.8, confidence=0.8)]

    facts_list = [
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=e1, candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=e2, candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=e3, candidate_collisions=[]
        ),
    ]

    agreements = compare_events(facts_list, tolerance=1.0)
    assert len(agreements) == 1
    ev_agr = list(agreements.values())[0]
    assert ev_agr.status == AgreementStatus.TRUSTED
    assert ev_agr.runs_agreeing == 3


def test_compare_parties_matching_descriptions() -> None:
    p1 = [
        PartyFact(
            description="red Toyota sedan",
            color="red",
            make_model="Toyota",
            body_type="sedan",
            view_id="v1",
            timestamp_seconds=0.5,
            confidence=0.95,
        )
    ]
    p2 = [
        PartyFact(
            description="red Toyota sedan",
            color="red",
            make_model="Toyota",
            body_type="sedan",
            view_id="v1",
            timestamp_seconds=0.7,
            confidence=0.95,
        )
    ]
    p3 = [
        PartyFact(
            description="red Toyota sedan",
            color="red",
            make_model="Toyota",
            body_type="sedan",
            view_id="v1",
            timestamp_seconds=0.5,
            confidence=0.95,
        )
    ]

    facts_list = [
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p1, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p2, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p3, events=[], candidate_collisions=[]
        ),
    ]

    agreements = compare_parties(facts_list, tolerance=1.0)
    assert len(agreements) == 1
    p_agr = list(agreements.values())[0]
    assert p_agr.status == AgreementStatus.TRUSTED
    assert p_agr.runs_agreeing == 3


def test_compare_fault_across_runs() -> None:
    f1 = (
        PartyFault(
            party_description="red Toyota sedan",
            status=FaultStatus.AT_FAULT,
            color="pink",
            reason="Violation of Art 6",
            cited_facts=(CitedFact(description="contact", view_id="v1", timestamp_seconds=1.0, confidence=0.9),),
        ),
    )
    f2 = (
        PartyFault(
            party_description="red Toyota sedan",
            status=FaultStatus.AT_FAULT,
            color="pink",
            reason="Violation of Art 6",
            cited_facts=(CitedFact(description="contact", view_id="v1", timestamp_seconds=1.0, confidence=0.9),),
        ),
    )
    f3 = (
        PartyFault(
            party_description="red Toyota sedan",
            status=FaultStatus.CANNOT_DETERMINE,
            color="grey",
            reason="Insufficient evidence",
            cited_facts=(),
        ),
    )

    p1 = [
        PartyFact(
            description="red Toyota sedan",
            color="red",
            make_model="Toyota",
            body_type="sedan",
            view_id="v1",
            timestamp_seconds=0.5,
            confidence=0.95,
        )
    ]
    facts_list = [
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p1, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p1, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p1, events=[], candidate_collisions=[]
        ),
    ]

    agreements = compare_fault([f1, f2, f3], facts_list)
    key = party_canonical_key(p1[0])
    assert f"fault_status:{key}" in agreements
    agr = agreements[f"fault_status:{key}"]
    assert agr.status == AgreementStatus.FLAGGED
    assert agr.agreed_value == (FaultStatus.AT_FAULT.value, None)
    assert agr.runs_agreeing == 2
    assert agr.runs_total == 3


def test_compute_agreement_overall_status_rules() -> None:
    settings = AgreementSettings(n_runs=3)

    p = [
        PartyFact(
            description="red Toyota sedan",
            color="red",
            make_model="Toyota",
            body_type="sedan",
            view_id="v1",
            timestamp_seconds=0.5,
            confidence=0.95,
        )
    ]
    c = [CitedFact(description="contact", view_id="v1", timestamp_seconds=1.0, confidence=0.9)]
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=p, events=[], candidate_collisions=c
    )

    fault_at_fault = (
        PartyFault(
            party_description="red Toyota sedan",
            status=FaultStatus.AT_FAULT,
            color="pink",
            reason="Art 6",
            cited_facts=tuple(c),
        ),
    )
    fault_cannot_determine = (
        PartyFault(
            party_description="red Toyota sedan",
            status=FaultStatus.CANNOT_DETERMINE,
            color="grey",
            reason="Unclear",
            cited_facts=(),
        ),
    )
    fault_not_at_fault = (
        PartyFault(
            party_description="red Toyota sedan",
            status=FaultStatus.NOT_AT_FAULT,
            color="green",
            reason="Art 6",
            cited_facts=tuple(c),
        ),
    )

    # 1. All runs agree 3/3 -> TRUSTED
    res_trusted = compute_agreement([facts, facts, facts], [fault_at_fault, fault_at_fault, fault_at_fault], settings)
    assert res_trusted.status == AgreementStatus.TRUSTED
    assert res_trusted.n_runs == 3

    # 2. 2 of 3 agree -> FLAGGED
    res_flagged = compute_agreement(
        [facts, facts, facts],
        [fault_at_fault, fault_at_fault, fault_cannot_determine],
        settings,
    )
    assert res_flagged.status == AgreementStatus.FLAGGED

    # 3. Split across runs (1/1/1) -> CANNOT_DETERMINE
    res_split = compute_agreement(
        [facts, facts, facts],
        [fault_at_fault, fault_not_at_fault, fault_cannot_determine],
        settings,
    )
    assert res_split.status == AgreementStatus.CANNOT_DETERMINE

    # 4. n_runs = 1 -> NOT_MEASURED (never trusted)
    res_single = compute_agreement([facts], [fault_at_fault], AgreementSettings(n_runs=1))
    assert res_single.status == AgreementStatus.NOT_MEASURED
    assert res_single.n_runs == 1


def test_agreement_settings_from_env_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INCIDENT_AGREEMENT_RUNS", raising=False)
    monkeypatch.delenv("INCIDENT_AGREEMENT_TOLERANCE_SECONDS", raising=False)
    s = AgreementSettings.from_env()
    assert s.n_runs == 1
    assert s.timestamp_tolerance_seconds == 1.0

    monkeypatch.setenv("INCIDENT_AGREEMENT_RUNS", "5")
    monkeypatch.setenv("INCIDENT_AGREEMENT_TOLERANCE_SECONDS", "1.5")
    s2 = AgreementSettings.from_env()
    assert s2.n_runs == 5
    assert s2.timestamp_tolerance_seconds == 1.5

    with pytest.raises(ValueError, match="at least 1"):
        AgreementSettings(n_runs=0)
    with pytest.raises(ValueError, match="nonnegative"):
        AgreementSettings(timestamp_tolerance_seconds=-0.5)


# ---------------------------------------------------------------------------
# Seam 1 Integration tests with Multi-Run AI
# ---------------------------------------------------------------------------


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


def _make_view(content: bytes = b"test-clip-content") -> tuple[View, str]:
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


def test_seam1_n_equals_3_unanimous_is_trusted(run_log: RunLog) -> None:
    view, view_hash = _make_view(b"clip-unanimous")

    party = PartyFact(description="red Toyota sedan", view_id=view_hash, timestamp_seconds=0.5, confidence=0.95)
    coll = CitedFact(description="contact", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[party], events=[], candidate_collisions=[coll]
    )

    resp = AIResponse(
        summary="Accident observed.",
        provenance="synthetic-fixture",
        facts=facts,
        calls=(
            ModelCall(
                request_hash="aa" * 32,
                input_hash="bb" * 32,
                prompt_version=PROMPT_VERSION,
                model="gemini-3.8-flash",
                view_id=view_hash,
                fps=1.0,
                raw_response='{"parties":[]}',
                latency_ms=10.0,
            ),
        ),
    )

    ai = FakeMultiRunAI([resp, resp, resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )

    assert report.agreement is not None
    assert report.agreement.n_runs == 3
    assert report.agreement.status == AgreementStatus.TRUSTED
    assert len(report.parties) == 1
    assert report.parties[0].agreement_status == "trusted"
    assert "agreement" in report.stage_latencies_ms
    assert report.stage_latencies_ms["agreement"] >= 0

    run = run_log.get(report.run_id)
    assert run.agreement is not None
    assert run.agreement.status == AgreementStatus.TRUSTED
    assert len(run.calls) == 3


def test_seam1_n_equals_3_split_overrides_party_to_cannot_determine(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    view, view_hash = _make_view(b"clip-split")

    p = PartyFact(
        description="red Toyota sedan",
        color="red",
        make_model="Toyota",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    c = CitedFact(description="contact", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[c]
    )

    resp = AIResponse(summary="Summary", provenance="synthetic-fixture", facts=facts)

    # Monkeypatch evaluate_fault to return 3 conflicting outcomes across the 3 calls
    call_idx = 0

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        nonlocal call_idx
        idx = call_idx
        call_idx += 1
        if idx == 0:
            return (
                PartyFault(
                    party_description="red Toyota sedan",
                    status=FaultStatus.AT_FAULT,
                    color="pink",
                    reason="Violation Art 6",
                    cited_facts=(c,),
                ),
            )
        elif idx == 1:
            return (
                PartyFault(
                    party_description="red Toyota sedan",
                    status=FaultStatus.NOT_AT_FAULT,
                    color="green",
                    reason="Priority road",
                    cited_facts=(c,),
                ),
            )
        else:
            return (
                PartyFault(
                    party_description="red Toyota sedan",
                    status=FaultStatus.CANNOT_DETERMINE,
                    color="grey",
                    reason="Unclear",
                    cited_facts=(),
                ),
            )

    import incident_report.entry as entry_mod

    monkeypatch.setattr(entry_mod, "evaluate_fault", mock_evaluate_fault)

    ai = FakeMultiRunAI([resp, resp, resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )

    assert report.agreement is not None
    assert report.agreement.n_runs == 3
    assert report.agreement.status == AgreementStatus.CANNOT_DETERMINE

    # Crucial RPT-23 requirement: split -> Cannot Determine override!
    assert len(report.parties) == 1
    assert report.parties[0].status == FaultStatus.CANNOT_DETERMINE
    assert report.parties[0].color == "grey"
    assert "Cannot Determine (RPT-23)" in report.parties[0].reason
    assert report.parties[0].agreement_status == "cannot_determine"


def test_seam1_n_equals_3_majority_flagged_with_check(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    view, view_hash = _make_view(b"clip-majority")

    p = PartyFact(
        description="red Toyota sedan",
        color="red",
        make_model="Toyota",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    c = CitedFact(description="contact", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[c]
    )

    resp = AIResponse(summary="Summary", provenance="synthetic-fixture", facts=facts)

    call_idx = 0

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        nonlocal call_idx
        idx = call_idx
        call_idx += 1
        if idx in (0, 1):
            return (
                PartyFault(
                    party_description="red Toyota sedan",
                    status=FaultStatus.AT_FAULT,
                    color="pink",
                    reason="Violation Art 6",
                    cited_facts=(c,),
                ),
            )
        else:
            return (
                PartyFault(
                    party_description="red Toyota sedan",
                    status=FaultStatus.CANNOT_DETERMINE,
                    color="grey",
                    reason="Unclear",
                    cited_facts=(),
                ),
            )

    import incident_report.entry as entry_mod

    monkeypatch.setattr(entry_mod, "evaluate_fault", mock_evaluate_fault)

    ai = FakeMultiRunAI([resp, resp, resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )

    assert report.agreement is not None
    assert report.agreement.n_runs == 3
    assert report.agreement.status == AgreementStatus.FLAGGED

    assert len(report.parties) == 1
    assert report.parties[0].status == FaultStatus.AT_FAULT
    assert report.parties[0].color == "pink"
    assert report.parties[0].agreement_status == "flagged"


def test_seam1_n_equals_1_skips_agreement_not_measured(run_log: RunLog) -> None:
    view, view_hash = _make_view(b"clip-n1")
    p = PartyFact(description="blue Nissan Patrol SUV", view_id=view_hash, timestamp_seconds=0.5, confidence=0.95)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[]
    )
    resp = AIResponse(summary="Summary single", provenance="synthetic-fixture", facts=facts)

    ai = FakeMultiRunAI([resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=1),
    )

    assert report.agreement is not None
    assert report.agreement.n_runs == 1
    assert report.agreement.status == AgreementStatus.NOT_MEASURED
    assert len(report.parties) == 1
    assert report.parties[0].agreement_status == "not_measured"


def test_report_template_renders_amber_check_badge_and_cannot_determine() -> None:
    from incident_report.domain import ReportView
    from incident_report.web.templating import build_environment

    env = build_environment()
    template = env.get_template("report.html")

    view = ReportView(
        filename="front.mp4",
        role=Role.EXTERIOR,
        label="Front",
        acquisition_method="Upload",
        sha256="a" * 64,
    )
    fact = CitedFact(description="contact", view_id="a" * 64, timestamp_seconds=1.0, confidence=0.9)

    # Flagged party (2/3 majority)
    flagged_party = PartyFault(
        party_description="red Toyota Camry sedan",
        status=FaultStatus.AT_FAULT,
        color="pink",
        rule_id="art-6",
        legal_source="Federal Decree-Law 14/2024",
        article="Art. 6",
        reason="Failed to yield",
        cited_facts=(fact,),
        agreement_status="flagged",
    )

    # Split party -> Cannot Determine
    split_party = PartyFault(
        party_description="blue Nissan Patrol SUV",
        status=FaultStatus.CANNOT_DETERMINE,
        color="grey",
        rule_id=None,
        legal_source=None,
        article=None,
        reason="Runs disagreed on fault status (split); Cannot Determine (RPT-23).",
        cited_facts=(),
        agreement_status="cannot_determine",
    )

    agreement = AgreementResult(
        n_runs=3,
        status=AgreementStatus.FLAGGED,
        fields={
            "fault_status:red Toyota Camry sedan": FieldAgreement(
                field_name="fault_status:red Toyota Camry sedan",
                status=AgreementStatus.FLAGGED,
                agreed_value="At Fault",
                runs_total=3,
                runs_agreeing=2,
            ),
            "fault_status:blue Nissan Patrol SUV": FieldAgreement(
                field_name="fault_status:blue Nissan Patrol SUV",
                status=AgreementStatus.CANNOT_DETERMINE,
                agreed_value="Cannot Determine",
                runs_total=3,
                runs_agreeing=1,
            ),
        },
    )

    report = Report(
        run_id=uuid.uuid4(),
        input_hash="b" * 64,
        prompt_version="skeleton-1",
        provenance="synthetic-fixture",
        summary="A collision occurred at the intersection.",
        views=(view,),
        facts=OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=[]
        ),
        parties=(flagged_party, split_party),
        is_valid=True,
        validation_errors=(),
        agreement=agreement,
    )

    html = template.render(report=report)

    # 1. Flagged party renders the amber Check badge
    assert 'class="badge badge--check"' in html
    assert "Check" in html

    # 2. Split party renders Cannot Determine with grey chip
    assert "chip--grey" in html
    assert "Cannot Determine" in html
    assert "Runs disagreed on fault status (split); Cannot Determine (RPT-23)." in html

    # 3. Overall agreement is rendered in summary metadata
    assert "Agreement:" in html
    assert "Flagged" in html
    assert "(3 runs)" in html


class SpyMultiViewAI(AIConnection):
    """Spy AI connection capturing all AIRequests in multi-view agreement runs."""

    def __init__(self, per_view_facts: dict[str, OverviewFacts]) -> None:
        self.per_view_facts = per_view_facts
        self.received_requests: list[AIRequest] = []
        self.calls_produced: list[ModelCall] = []

    def analyse(self, request: AIRequest) -> AIResponse:
        self.received_requests.append(request)
        view_key = request.views[0].sha256 if len(request.views) == 1 else "joint"
        facts = self.per_view_facts.get(view_key)
        call = ModelCall(
            request_hash=request.input_hash,
            input_hash=request.input_hash,
            prompt_version=request.prompt_version,
            model="spy-model",
            view_id=view_key,
            fps=1.0,
            raw_response=f"spy-response-for-{view_key[:8]}",
            latency_ms=5.0,
        )
        self.calls_produced.append(call)
        return AIResponse(
            summary=f"Analysis of {view_key[:8]}",
            provenance="synthetic-fixture",
            calls=(call,),
            facts=facts,
        )


def test_multiview_n_runs_agreement_requests_and_combines_facts(run_log: RunLog, tmp_path: Path) -> None:
    """N=3 with two views makes 6 per-view overview requests, and agreement is computed over combined facts."""
    from incident_report.storage import LocalEvidenceStore

    v1_content = synthetic_video(b"multiview-v1")
    v2_content = synthetic_video(b"multiview-v2")
    v1_hash = sha256_hex(v1_content)
    v2_hash = sha256_hex(v2_content)

    v1_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(
                description="white Toyota Camry sedan",
                color="white",
                make_model="Toyota Camry",
                body_type="sedan",
                view_id=v1_hash,
                timestamp_seconds=0.2,
                confidence=0.9,
            )
        ],
        events=[
            CitedFact(description="white Toyota Camry brakes", view_id=v1_hash, timestamp_seconds=0.5, confidence=0.9)
        ],
        candidate_collisions=[],
    )
    v2_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(
                description="blue Nissan Patrol SUV",
                color="blue",
                make_model="Nissan Patrol",
                body_type="suv",
                view_id=v2_hash,
                timestamp_seconds=0.3,
                confidence=0.9,
            )
        ],
        events=[
            CitedFact(description="blue Nissan Patrol turns", view_id=v2_hash, timestamp_seconds=0.6, confidence=0.9)
        ],
        candidate_collisions=[],
    )

    ai = SpyMultiViewAI(per_view_facts={v1_hash: v1_facts, v2_hash: v2_facts})
    views = [
        View(stream=io.BytesIO(v1_content), filename="dashcam.mp4", role=Role.EXTERIOR, label="exterior"),
        View(stream=io.BytesIO(v2_content), filename="cabin.mp4", role=Role.INTERIOR, label="interior"),
    ]

    report = analyse_incident(
        views,
        Origin.APP,
        ai=ai,
        run_log=run_log,
        prompt_version="overview-1",
        evidence=LocalEvidenceStore(tmp_path / "mv_store"),
        agreement_settings=AgreementSettings(n_runs=3),
    )

    # 1. Exactly 6 per-view requests made (3 runs x 2 views)
    assert len(ai.received_requests) == 6
    for req in ai.received_requests:
        assert len(req.views) == 1
    requested_hashes = [req.views[0].sha256 for req in ai.received_requests]
    assert requested_hashes.count(v1_hash) == 3
    assert requested_hashes.count(v2_hash) == 3

    # 2. Agreement is computed over combined facts of each run
    assert report.facts is not None
    assert len(report.facts.parties) == 2
    assert {p.view_id for p in report.facts.parties} == {v1_hash, v2_hash}

    # 3. Agreement was evaluated across all 3 runs
    assert report.agreement is not None
    assert report.agreement.n_runs == 3
    assert report.agreement.status == AgreementStatus.TRUSTED
    assert "party:white:toyota camry:sedan" in report.agreement.fields
    assert "party:blue:nissan patrol:suv" in report.agreement.fields
    assert report.agreement.fields["party:white:toyota camry:sedan"].status == AgreementStatus.TRUSTED
    assert report.agreement.fields["party:blue:nissan patrol:suv"].status == AgreementStatus.TRUSTED


def test_bad_dense_fact_gives_is_valid_false(run_log: RunLog) -> None:
    """Requirement 1: Citation validation runs on consolidated facts after dense pass; bad dense fact sets is_valid=False."""
    from incident_report.ai.dense import DenseSettings

    view, view_hash = _make_view(b"clip-bad-dense")
    party = PartyFact(description="red Toyota Camry sedan", view_id=view_hash, timestamp_seconds=0.5, confidence=0.95)
    coll = CitedFact(description="candidate collision", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[party], events=[], candidate_collisions=[coll]
    )
    resp = AIResponse(summary="Accident observed.", provenance="synthetic-fixture", facts=facts)

    class MockDenseAI:
        def analyse_window(self, request: AIRequest, window: Any) -> tuple[OverviewFacts, tuple[ModelCall, ...]]:
            bad_collision = CitedFact(
                description="confirmed contact out of bounds",
                view_id=view_hash,
                timestamp_seconds=99.0,
                confidence=0.9,
            )
            return OverviewFacts(
                behaviour_observations=[],
                location_conditions=[],
                parties=[],
                events=[],
                candidate_collisions=[bad_collision],
            ), ()

    ai = FakeMultiRunAI([resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        dense_ai=MockDenseAI(),
        dense_settings=DenseSettings(model="gemini-3.8-flash"),
        agreement_settings=AgreementSettings(n_runs=1),
    )

    assert report.is_valid is False
    assert len(report.validation_errors) > 0
    assert any("99.0" in err or "duration" in err.lower() or "range" in err.lower() for err in report.validation_errors)
    assert report.parties == ()
    assert report.violations == ()


def test_seam1_split_party_cannot_determine_has_no_violation(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement 2: Split party downgraded to Cannot Determine gets no violations (RPT-17 x RPT-23)."""
    from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD

    view, view_hash = _make_view(b"clip-seam1-split")
    p = PartyFact(description="red Toyota Camry sedan", view_id=view_hash, timestamp_seconds=0.5, confidence=0.95)
    c = CitedFact(description="contact", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[c]
    )
    resp = AIResponse(summary="Summary", provenance="synthetic-fixture", facts=facts)

    call_idx = 0

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        nonlocal call_idx
        idx = call_idx
        call_idx += 1
        if idx == 0:
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.AT_FAULT,
                    color="pink",
                    rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
                    reason="Violation of main road priority",
                    cited_facts=(c,),
                ),
            )
        elif idx == 1:
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.NOT_AT_FAULT,
                    color="green",
                    rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
                    reason="Right of way",
                    cited_facts=(c,),
                ),
            )
        else:
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.CANNOT_DETERMINE,
                    color="grey",
                    rule_id=None,
                    reason="Cannot determine",
                    cited_facts=(),
                ),
            )

    import incident_report.entry as entry_mod

    monkeypatch.setattr(entry_mod, "evaluate_fault", mock_evaluate_fault)

    ai = FakeMultiRunAI([resp, resp, resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )

    assert len(report.parties) == 1
    assert report.parties[0].status == FaultStatus.CANNOT_DETERMINE
    assert report.parties[0].agreement_status == "cannot_determine"
    assert report.violations == ()


def test_seam1_two_thirds_at_fault_party_keeps_fine_with_flag(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement 2 & 6: 2/3 At Fault party keeps its fine, marked flagged, and summary totals count flagged."""
    from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD

    view, view_hash = _make_view(b"clip-seam1-two-thirds")
    p = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    c = CitedFact(description="contact", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[c]
    )
    resp = AIResponse(summary="Summary", provenance="synthetic-fixture", facts=facts)

    call_idx = 0

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        nonlocal call_idx
        idx = call_idx
        call_idx += 1
        if idx in (0, 1):
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.AT_FAULT,
                    color="pink",
                    rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
                    reason="Failure to yield to main road traffic",
                    cited_facts=(c,),
                ),
            )
        else:
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.NOT_AT_FAULT,
                    color="green",
                    rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
                    reason="Had right of way",
                    cited_facts=(c,),
                ),
            )

    import incident_report.entry as entry_mod

    monkeypatch.setattr(entry_mod, "evaluate_fault", mock_evaluate_fault)

    ai = FakeMultiRunAI([resp, resp, resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )

    assert len(report.parties) == 1
    assert report.parties[0].status == FaultStatus.AT_FAULT
    assert report.parties[0].agreement_status == "flagged"
    assert report.parties[0].agreement_detail == "2 of 3 runs agree (majority)."

    assert len(report.violations) == 1
    v = report.violations[0]
    assert v.agreement_status == "flagged"
    assert v.agreement_detail == "2 of 3 runs agree (majority)."
    assert v.aed == 400
    assert report.violations_summary.totals.flagged_count == 1
    assert report.violations_summary.parties[0].totals.flagged_count == 1


def test_seam1_primary_cannot_determine_overturned_by_majority_at_fault(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 2 & 4: Primary run Cannot Determine overturned by majority At Fault produces violation with cited facts."""
    from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD

    view, view_hash = _make_view(b"clip-overturn")
    p = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    c = CitedFact(description="contact", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[c]
    )
    resp = AIResponse(summary="Summary", provenance="synthetic-fixture", facts=facts)

    call_idx = 0

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        nonlocal call_idx
        idx = call_idx
        call_idx += 1
        if idx == 0:
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.CANNOT_DETERMINE,
                    color="grey",
                    rule_id=None,
                    reason="Unclear from primary overview",
                    cited_facts=(),
                ),
            )
        else:
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.AT_FAULT,
                    color="pink",
                    rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
                    reason="Failed to yield to main road",
                    cited_facts=(c,),
                ),
            )

    import incident_report.entry as entry_mod

    monkeypatch.setattr(entry_mod, "evaluate_fault", mock_evaluate_fault)

    ai = FakeMultiRunAI([resp, resp, resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )

    assert len(report.parties) == 1
    p_fault = report.parties[0]
    assert p_fault.status == FaultStatus.AT_FAULT
    assert p_fault.color == "pink"
    assert p_fault.rule_id == RULE_ART_6_MAIN_ROAD.rule_id
    assert len(p_fault.cited_facts) == 1
    assert p_fault.agreement_status == "flagged"

    assert len(report.violations) == 1
    assert report.violations[0].rule_id == RULE_ART_6_MAIN_ROAD.rule_id
    assert len(report.violations[0].cited_facts) == 1


def test_flagged_decided_status_with_no_agreeing_citations_falls_back_to_cannot_determine(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 4: ADR 0006 invariant: a decided status must never have empty cited_facts."""
    from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD

    view, view_hash = _make_view(b"clip-empty-cites")
    p = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    c = CitedFact(description="contact", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[c]
    )
    resp = AIResponse(summary="Summary", provenance="synthetic-fixture", facts=facts)

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        return (
            PartyFault(
                party_description="red Toyota Camry sedan",
                status=FaultStatus.CANNOT_DETERMINE,
                color="grey",
                rule_id=None,
                reason="Unclear from overview",
                cited_facts=(),
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
                    agreed_value=(FaultStatus.AT_FAULT.value, RULE_ART_6_MAIN_ROAD.rule_id),
                    runs_total=3,
                    runs_agreeing=2,
                    details="2 of 3 runs agree (majority).",
                )
            },
        )

    import incident_report.entry as entry_mod

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

    assert report.parties[0].status == FaultStatus.CANNOT_DETERMINE
    assert "no cited facts (ADR 0006)" in report.parties[0].reason
    assert report.violations == ()


def test_later_run_failing_preserves_earlier_calls_in_run_row(run_log: RunLog) -> None:
    """Requirement 2 & 6: Later run failing still records every earlier call in the failed Run."""
    from incident_report.ai import AIConnectionError

    view, view_hash = _make_view(b"clip-fail-later")
    p = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[]
    )

    call_1 = ModelCall(
        request_hash="11" * 32,
        input_hash="11" * 32,
        prompt_version=PROMPT_VERSION,
        model="gemini-3.8-flash",
        view_id=view_hash,
        fps=1.0,
        raw_response='{"parties":[]}',
        latency_ms=10.0,
    )
    resp_1 = AIResponse(summary="Run 1 success", provenance="synthetic-fixture", facts=facts, calls=(call_1,))

    call_2 = ModelCall(
        request_hash="22" * 32,
        input_hash="22" * 32,
        prompt_version=PROMPT_VERSION,
        model="gemini-3.8-flash",
        view_id=view_hash,
        fps=1.0,
        raw_response="",
        latency_ms=5.0,
    )

    class FailingSecondRunAI(AIConnection):
        def __init__(self) -> None:
            self.calls_count = 0

        def analyse(self, request: AIRequest) -> AIResponse:
            self.calls_count += 1
            if self.calls_count == 1:
                return resp_1
            raise AIConnectionError("Connection lost on run 2", calls=(call_2,))

    ai = FailingSecondRunAI()
    with pytest.raises(AIConnectionError, match="Connection lost on run 2"):
        analyse_incident(
            [view],
            Origin.APP,
            ai=ai,
            run_log=run_log,
            agreement_settings=AgreementSettings(n_runs=3),
        )

    runs = run_log.list_runs()
    assert len(runs) >= 1
    failed_run = runs[-1]
    assert failed_run.status.value == "failed"
    assert len(failed_run.calls) == 2
    assert failed_run.calls[0].request_hash == call_1.request_hash
    assert failed_run.calls[1].request_hash == call_2.request_hash


def test_officer_question_never_reaches_any_request_at_n_greater_than_1(run_log: RunLog) -> None:
    """Requirement 2: Officer question never reaches any AIRequest at N > 1."""
    view, view_hash = _make_view(b"clip-officer-q")
    p = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[]
    )
    resp = AIResponse(summary="Summary", provenance="synthetic-fixture", facts=facts)

    ai = FakeMultiRunAI([resp, resp, resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
        officer_question="Did the red Toyota Camry run the red light?",
    )

    assert len(ai.calls_received) == 3
    for req in ai.calls_received:
        assert not hasattr(req, "officer_question")
        for v in req.views:
            assert "red light" not in v.role.value
    assert report.run_id is not None


def test_typed_party_matching_different_prose_stays_trusted() -> None:
    """Requirement 3: Same vehicle described with different wording across 3 runs matches by typed attributes."""
    p1 = [
        PartyFact(
            description="red Toyota Camry sedan",
            color="red",
            make_model="Toyota Camry",
            body_type="sedan",
            view_id="v1",
            timestamp_seconds=0.5,
            confidence=0.9,
        )
    ]
    p2 = [
        PartyFact(
            description="red sedan (Toyota)",
            color="red",
            make_model="Toyota Camry",
            body_type="sedan",
            view_id="v1",
            timestamp_seconds=0.5,
            confidence=0.9,
        )
    ]
    p3 = [
        PartyFact(
            description="Red Toyota Camry sedan",
            color="red",
            make_model="Toyota Camry",
            body_type="sedan",
            view_id="v1",
            timestamp_seconds=0.5,
            confidence=0.9,
        )
    ]

    facts_list = [
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p1, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p2, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p3, events=[], candidate_collisions=[]
        ),
    ]

    agreements = compare_parties(facts_list)
    assert len(agreements) == 1
    agr = list(agreements.values())[0]
    assert agr.status == AgreementStatus.TRUSTED
    assert agr.runs_agreeing == 3
    assert agr.runs_total == 3


def test_fault_vote_on_status_and_rule_id_splits_if_rules_differ() -> None:
    """Requirement 3: Fault vote is cast on (status, rule_id); 3 runs citing 3 different rules split to Cannot Determine."""
    c = CitedFact(description="contact", view_id="v1", timestamp_seconds=1.0, confidence=0.9)
    f1 = (
        PartyFault(
            party_description="red Toyota sedan",
            status=FaultStatus.AT_FAULT,
            color="pink",
            rule_id="art-6-main-road",
            reason="Main road",
            cited_facts=(c,),
        ),
    )
    f2 = (
        PartyFault(
            party_description="red Toyota sedan",
            status=FaultStatus.AT_FAULT,
            color="pink",
            rule_id="art-6-roundabout",
            reason="Roundabout",
            cited_facts=(c,),
        ),
    )
    f3 = (
        PartyFault(
            party_description="red Toyota sedan",
            status=FaultStatus.AT_FAULT,
            color="pink",
            rule_id="art-6-priority-left",
            reason="Priority left",
            cited_facts=(c,),
        ),
    )

    p1 = [
        PartyFact(
            description="red Toyota sedan",
            color="red",
            make_model="Toyota",
            body_type="sedan",
            view_id="v1",
            timestamp_seconds=0.5,
            confidence=0.95,
        )
    ]
    facts_list = [
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p1, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p1, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p1, events=[], candidate_collisions=[]
        ),
    ]

    agreements = compare_fault([f1, f2, f3], facts_list)
    key = party_canonical_key(p1[0])
    agr = agreements[f"fault_status:{key}"]
    assert agr.status == AgreementStatus.CANNOT_DETERMINE
    assert agr.agreed_value == (FaultStatus.CANNOT_DETERMINE.value, None)


def test_events_matched_by_timestamp_only_without_prose_matching() -> None:
    """Requirement 3: Events match purely by timestamp within tolerance; prose text is not compared with .lower()."""
    e1 = [
        CitedFact(
            description="red sedan enters intersection at speed", view_id="v1", timestamp_seconds=2.0, confidence=0.8
        )
    ]
    e2 = [
        CitedFact(description="vehicle approaches cross junction", view_id="v1", timestamp_seconds=2.4, confidence=0.8)
    ]
    e3 = [CitedFact(description="car crosses white stop bar", view_id="v1", timestamp_seconds=1.8, confidence=0.8)]

    facts_list = [
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=e1, candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=e2, candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=e3, candidate_collisions=[]
        ),
    ]

    agreements = compare_events(facts_list, tolerance=1.0)
    assert len(agreements) == 1
    agr = list(agreements.values())[0]
    assert agr.status == AgreementStatus.TRUSTED
    assert agr.runs_agreeing == 3


def test_quota_and_max_runs_cap() -> None:
    """Requirement 5: AgreementSettings caps n_runs at 10 and raises ConfigError on bad env."""
    from incident_report.config import ConfigError

    with pytest.raises(ValueError, match="cannot exceed 10"):
        AgreementSettings(n_runs=11)

    with pytest.raises(ConfigError, match="between 1 and 10"):
        AgreementSettings.from_env({"INCIDENT_AGREEMENT_RUNS": "12"})

    with pytest.raises(ConfigError, match="positive integer"):
        AgreementSettings.from_env({"INCIDENT_AGREEMENT_RUNS": "invalid"})


def test_template_renders_flagged_fine_badge_and_totals() -> None:
    """Requirement 6: Flagged fine displays amber Check badge and totals say '(includes N flagged)'."""
    from incident_report.domain import ReportView
    from incident_report.rules.models import Violation
    from incident_report.rules.violations import map_violation
    from incident_report.web.templating import build_environment

    env = build_environment()
    template = env.get_template("report.html")

    view = ReportView(
        filename="front.mp4",
        role=Role.EXTERIOR,
        label="Front",
        acquisition_method="Upload",
        sha256="a" * 64,
    )
    fact = CitedFact(description="contact", view_id="a" * 64, timestamp_seconds=1.0, confidence=0.9)

    flagged_party = PartyFault(
        party_description="red Toyota Camry sedan",
        status=FaultStatus.AT_FAULT,
        color="pink",
        rule_id="decree-law-14-2024-art-6-main-road",
        legal_source="Federal Decree-Law 14/2024",
        article="Art. 6",
        reason="Failed to yield",
        cited_facts=(fact,),
        agreement_status="flagged",
        agreement_detail="2 of 3 runs agree (majority).",
    )

    raw_v = Violation(
        rule_id="decree-law-14-2024-art-6-main-road",
        party_description="red Toyota Camry sedan",
        cited_facts=(fact,),
    )
    violation = map_violation(raw_v).model_copy(
        update={
            "agreement_status": "flagged",
            "agreement_detail": "2 of 3 runs agree (majority).",
        }
    )

    agreement = AgreementResult(
        n_runs=3,
        status=AgreementStatus.FLAGGED,
        fields={},
    )

    report = Report(
        run_id=uuid.uuid4(),
        input_hash="b" * 64,
        prompt_version="skeleton-1",
        provenance="synthetic-fixture",
        summary="A collision occurred.",
        views=(view,),
        facts=OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=[]
        ),
        parties=(flagged_party,),
        violations=(violation,),
        is_valid=True,
        validation_errors=(),
        agreement=agreement,
    )

    html = template.render(report=report)

    # 1. Amber Check badge on violation block <h3>
    assert 'class="badge badge--check" data-agreement="flagged"' in html
    assert 'title="2 of 3 runs agree (majority)."' in html

    # 2. Party total includes flagged count
    assert "(includes 1 flagged)" in html

    # 3. Incident total includes flagged count
    assert "(includes 1 flagged)" in html


def test_r1_overview_not_at_fault_final_at_fault_downgrades_to_cannot_determine(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1: Three overview runs Not at Fault, final dense At Fault -> Cannot Determine, no violation."""
    from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD

    view, view_hash = _make_view(b"clip-r1-overview-vs-dense")
    p = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    c = CitedFact(description="contact", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)
    facts = OverviewFacts(
        behaviour_observations=[], location_conditions=[], parties=[p], events=[], candidate_collisions=[c]
    )
    resp = AIResponse(summary="Summary", provenance="synthetic-fixture", facts=facts)

    class DensePassAI(FakeMultiRunAI):
        def analyse_window(self, req: Any, window: Any) -> tuple[OverviewFacts, list[ModelCall]]:
            dense_facts = OverviewFacts(
                behaviour_observations=[
                    CitedFact(
                        description="dense-confirmed-violation",
                        view_id=view_hash,
                        timestamp_seconds=1.0,
                        confidence=0.9,
                    )
                ],
                location_conditions=[],
                parties=[p],
                events=[],
                candidate_collisions=[c],
            )
            return dense_facts, []

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        # Driven by typed facts input: overview has no breach observations; dense pass does
        if any("dense-confirmed-violation" in b.description for b in f.behaviour_observations):
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.AT_FAULT,
                    color="pink",
                    rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
                    reason="Failure to yield to main road traffic",
                    cited_facts=(c,),
                ),
            )
        else:
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.NOT_AT_FAULT,
                    color="green",
                    rule_id=None,
                    reason="Not at fault in overview",
                    cited_facts=(c,),
                ),
            )

    import incident_report.entry as entry_mod

    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-flash")
    monkeypatch.setattr(entry_mod, "evaluate_fault", mock_evaluate_fault)

    ai = DensePassAI([resp, resp, resp])
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
    assert party.rule_id is None
    assert party.agreement_status == "cannot_determine"
    assert "Dense-consolidated fault" in party.reason
    assert "differs from voted overview agreement" in party.reason
    assert report.violations == ()


def test_r2_missing_typed_attributes_description_fallback_matching() -> None:
    """R2 (round 2): Parties lacking typed attributes fall back to exact normalised description.

    - Exact same description (differing only in case / whitespace) matches as one party (TRUSTED).
    - Different descriptions without typed attributes do not match (no prose word-list parsing)
      and split to Cannot Determine.
    """
    # 1. Exact same description across 3 runs matches
    p_same_1 = [PartyFact(description="unknown red vehicle", view_id="v1", timestamp_seconds=0.5, confidence=0.9)]
    p_same_2 = [PartyFact(description="Unknown   Red  Vehicle", view_id="v1", timestamp_seconds=0.6, confidence=0.9)]
    p_same_3 = [PartyFact(description="UNKNOWN RED VEHICLE", view_id="v1", timestamp_seconds=0.7, confidence=0.9)]

    facts_same = [
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p_same_1, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p_same_2, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p_same_3, events=[], candidate_collisions=[]
        ),
    ]
    same_agrs = compare_parties(facts_same)
    assert len(same_agrs) == 1
    same_agr = list(same_agrs.values())[0]
    assert same_agr.field_name == "party:desc:unknown red vehicle"
    assert same_agr.status == AgreementStatus.TRUSTED
    assert same_agr.runs_agreeing == 3

    # 2. Different descriptions without typed attributes do not match across runs
    p_diff_1 = [PartyFact(description="unknown red vehicle", view_id="v1", timestamp_seconds=0.5, confidence=0.9)]
    p_diff_2 = [PartyFact(description="crimson automobile", view_id="v1", timestamp_seconds=0.6, confidence=0.9)]
    p_diff_3 = [PartyFact(description="cherry sedan", view_id="v1", timestamp_seconds=0.7, confidence=0.9)]

    facts_diff = [
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p_diff_1, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p_diff_2, events=[], candidate_collisions=[]
        ),
        OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=p_diff_3, events=[], candidate_collisions=[]
        ),
    ]
    diff_agrs = compare_parties(facts_diff)
    assert len(diff_agrs) == 3
    for agr in diff_agrs.values():
        assert agr.status == AgreementStatus.CANNOT_DETERMINE
        assert agr.runs_agreeing == 1
        assert "party:desc:" in agr.field_name


def test_r2_multiview_description_fallback_party_pairing_and_contradiction() -> None:
    """R2 (round 2): In cross-view reconciliation:
    - Same description in two views without typed attributes -> one party and a Contradiction.
    - Different descriptions without typed attributes -> two parties (no contradiction).
    """
    from incident_report.facts import StructuredFact
    from incident_report.rules.adapter import situation_from_overview

    # Case A: Same description ("white Toyota sedan") in two views ("front", "rear")
    # with conflicting traffic signal readings -> 1 party, 1 Contradiction
    p_front_same = PartyFact(description="white Toyota sedan", view_id="front", timestamp_seconds=1.0, confidence=0.9)
    p_rear_same = PartyFact(description="White   Toyota   Sedan", view_id="rear", timestamp_seconds=1.5, confidence=0.9)

    fact_front = StructuredFact(
        subject="white Toyota sedan",
        field="signal_state_when_crossing_stop_line",
        value="red",
        other_party="",
        confidence=0.95,
        timestamp_seconds=1.0,
        view_id="front",
    )
    fact_rear = StructuredFact(
        subject="White   Toyota   Sedan",
        field="signal_state_when_crossing_stop_line",
        value="green",
        other_party="",
        confidence=0.95,
        timestamp_seconds=1.5,
        view_id="rear",
    )

    overview_same = OverviewFacts(
        parties=[p_front_same, p_rear_same],
        structured_facts=[fact_front, fact_rear],
        events=[],
        candidate_collisions=[],
        behaviour_observations=[],
        location_conditions=[],
    )

    scene_same = situation_from_overview(overview_same)
    assert len(scene_same.parties) == 1
    assert len(scene_same.contradictions) == 1
    assert scene_same.contradictions[0].field == "signal_state_when_crossing_stop_line"

    # Case B: Different descriptions without typed attributes ("white Toyota sedan" vs "blue Nissan SUV")
    # -> 2 distinct parties, no contradiction
    p_front_diff = PartyFact(description="white Toyota sedan", view_id="front", timestamp_seconds=1.0, confidence=0.9)
    p_rear_diff = PartyFact(description="blue Nissan SUV", view_id="rear", timestamp_seconds=1.5, confidence=0.9)

    fact_front_diff = StructuredFact(
        subject="white Toyota sedan",
        field="signal_state_when_crossing_stop_line",
        value="red",
        other_party="",
        confidence=0.95,
        timestamp_seconds=1.0,
        view_id="front",
    )
    fact_rear_diff = StructuredFact(
        subject="blue Nissan SUV",
        field="signal_state_when_crossing_stop_line",
        value="green",
        other_party="",
        confidence=0.95,
        timestamp_seconds=1.5,
        view_id="rear",
    )

    overview_diff = OverviewFacts(
        parties=[p_front_diff, p_rear_diff],
        structured_facts=[fact_front_diff, fact_rear_diff],
        events=[],
        candidate_collisions=[],
        behaviour_observations=[],
        location_conditions=[],
    )

    scene_diff = situation_from_overview(overview_diff)
    assert len(scene_diff.parties) == 2
    assert len(scene_diff.contradictions) == 0


def test_r2_pythonhashseed_determinism_subprocess() -> None:
    """R2: Agreement calculation produces identical deterministic output across different PYTHONHASHSEED values."""
    import os
    import subprocess
    import sys

    sub_script = """
import json
from incident_report.agreement import compare_parties
from incident_report.facts import OverviewFacts, PartyFact

p1 = [
    PartyFact(
        description="dark blue Toyota Camry sedan",
        color="blue",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id="v1",
        timestamp_seconds=0.5,
        confidence=0.9,
    ),
    PartyFact(
        description="  silver   nissan  altima ",
        view_id="v1",
        timestamp_seconds=0.6,
        confidence=0.9,
    ),
]
p2 = [
    PartyFact(
        description="blue sedan (Toyota)",
        color="blue",
        make_model="Toyota",
        body_type="sedan",
        view_id="v1",
        timestamp_seconds=0.5,
        confidence=0.9,
    ),
    PartyFact(
        description="Silver Nissan Altima",
        view_id="v1",
        timestamp_seconds=0.6,
        confidence=0.9,
    ),
]

facts_list = [
    OverviewFacts(behaviour_observations=[], location_conditions=[], parties=p1, events=[], candidate_collisions=[]),
    OverviewFacts(behaviour_observations=[], location_conditions=[], parties=p2, events=[], candidate_collisions=[]),
]

agrs = compare_parties(facts_list)
out = {k: {"status": v.status.value, "agreeing": v.runs_agreeing, "total": v.runs_total} for k, v in sorted(agrs.items())}
print(json.dumps(out, sort_keys=True))
"""

    results = []
    for seed in ("0", "42", "12345"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        proc = subprocess.run(
            [sys.executable, "-c", sub_script],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        results.append(proc.stdout.strip())

    assert len(results) == 3
    assert results[0] == results[1] == results[2]


def test_r3_citations_deduplicated_across_all_fact_groups() -> None:
    """R3: Citations helper de-duplicates against events, candidate collisions, and parties."""
    from incident_report.entry import _append_cited_facts_to_events

    cand = CitedFact(description="contact", view_id="v1", timestamp_seconds=1.0, confidence=0.9)
    evt = CitedFact(description="brake lights", view_id="v1", timestamp_seconds=0.5, confidence=0.8)
    party = PartyFact(
        description="red sedan",
        color="red",
        make_model="Toyota",
        body_type="sedan",
        view_id="v1",
        timestamp_seconds=0.2,
        confidence=0.9,
    )
    new_evt = CitedFact(description="debris scattered", view_id="v1", timestamp_seconds=2.0, confidence=0.85)

    facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[party],
        events=[evt],
        candidate_collisions=[cand],
    )

    # Citations contain:
    # 1. 'cand' (already in candidate_collisions) -> must NOT be added to events
    # 2. 'evt' (already in events) -> must NOT be added
    # 3. cited fact with same view_id and timestamp as 'party' -> must NOT be added
    # 4. 'new_evt' -> MUST be added
    party_cf = CitedFact(description="red sedan", view_id="v1", timestamp_seconds=0.2, confidence=0.9)
    citations = [cand, evt, party_cf, new_evt]

    updated = _append_cited_facts_to_events(facts, citations)
    assert len(updated.events) == 2
    assert updated.events[0] == evt
    assert updated.events[1] == new_evt


def test_r4_like_for_like_and_agreement_stage_latency(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    """R4: Agreement votes on overview-stage faults of every run, and agreement stage latency includes extra runs."""
    import time

    from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD

    view, view_hash = _make_view(b"clip-r4-latency")
    p = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    c = CitedFact(description="contact", view_id=view_hash, timestamp_seconds=1.0, confidence=0.9)

    # 3 differing responses:
    # Run 0 (primary): normal driving -> Not at Fault
    # Run 1: breach -> At Fault
    # Run 2: breach -> At Fault
    facts_normal = OverviewFacts(
        behaviour_observations=[
            CitedFact(description="normal driving", view_id=view_hash, timestamp_seconds=0.5, confidence=0.9)
        ],
        location_conditions=[],
        parties=[p],
        events=[],
        candidate_collisions=[c],
    )
    facts_breach = OverviewFacts(
        behaviour_observations=[
            CitedFact(description="breach of priority", view_id=view_hash, timestamp_seconds=0.5, confidence=0.9)
        ],
        location_conditions=[],
        parties=[p],
        events=[],
        candidate_collisions=[c],
    )

    resp0 = AIResponse(summary="Summary 0", provenance="synthetic", facts=facts_normal)
    resp1 = AIResponse(summary="Summary 1", provenance="synthetic", facts=facts_breach)
    resp2 = AIResponse(summary="Summary 2", provenance="synthetic", facts=facts_breach)

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        # Driven by typed facts input: breach observation gives At Fault, else Not at Fault
        if any("breach" in b.description for b in f.behaviour_observations):
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.AT_FAULT,
                    color="pink",
                    rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
                    reason="Breach of priority",
                    cited_facts=(c,),
                ),
            )
        else:
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.NOT_AT_FAULT,
                    color="green",
                    rule_id=None,
                    reason="Normal driving observed",
                    cited_facts=(c,),
                ),
            )

    import incident_report.entry as entry_mod

    monkeypatch.setattr(entry_mod, "evaluate_fault", mock_evaluate_fault)

    class SleepingMultiRunAI(FakeMultiRunAI):
        def __init__(self, responses: list[AIResponse], delay: float = 0.05) -> None:
            super().__init__(responses)
            self._delay = delay

        def analyse(self, request: AIRequest) -> AIResponse:
            time.sleep(self._delay)
            return super().analyse(request)

    ai = SleepingMultiRunAI([resp0, resp1, resp2], delay=0.05)
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )

    # Like-for-like vote on overview-stage faults of every run:
    # Run 0 is Not at Fault; runs 1 and 2 are At Fault.
    # Primary run alone would give 1/1 Not at Fault.
    # The 3-run vote is 2/3 At Fault (FLAGGED).
    assert report.agreement is not None
    p_key = party_canonical_key(p)
    fault_agr = report.agreement.fields[f"fault_status:{p_key}"]
    assert fault_agr.runs_total == 3
    assert fault_agr.runs_agreeing == 2
    assert fault_agr.status == AgreementStatus.FLAGGED
    assert fault_agr.agreed_value == ("At Fault", RULE_ART_6_MAIN_ROAD.rule_id)

    # Agreement stage latency includes the extra runs (N-1 = 2 extra calls at 0.05s each -> >= 80ms)
    assert "agreement" in report.stage_latencies_ms
    assert report.stage_latencies_ms["agreement"] >= (3 - 1) * 50 * 0.8
    assert report.agreement.total_latency_ms == report.stage_latencies_ms["agreement"]


def test_f1_flagged_majority_overturn_takes_agreeing_run_reason_and_citations(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: Majority At Fault + final Cannot Determine -> At Fault with agreeing run's reason and citations.

    The final run's text ('footage unclear') must not appear.
    """
    from incident_report.rules.decree_law_14_2024 import RULE_ART_6_MAIN_ROAD

    view, view_hash = _make_view(b"clip-f1-flagged-overturn")
    p = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.95,
    )
    c_agreeing = CitedFact(
        description="contact with main road traffic",
        view_id=view_hash,
        timestamp_seconds=1.0,
        confidence=0.9,
    )
    facts_at_fault = OverviewFacts(
        behaviour_observations=[
            CitedFact(
                description="yield failure observed",
                view_id=view_hash,
                timestamp_seconds=0.8,
                confidence=0.9,
            )
        ],
        location_conditions=[],
        parties=[p],
        events=[],
        candidate_collisions=[c_agreeing],
    )
    facts_unclear = OverviewFacts(
        behaviour_observations=[
            CitedFact(
                description="camera obstructed",
                view_id=view_hash,
                timestamp_seconds=0.8,
                confidence=0.9,
            )
        ],
        location_conditions=[],
        parties=[p],
        events=[],
        candidate_collisions=[c_agreeing],
    )

    resp_unclear = AIResponse(summary="Summary primary unclear", provenance="synthetic", facts=facts_unclear)
    resp_af1 = AIResponse(summary="Summary 1", provenance="synthetic", facts=facts_at_fault)
    resp_af2 = AIResponse(summary="Summary 2", provenance="synthetic", facts=facts_at_fault)

    def mock_evaluate_fault(
        f: OverviewFacts, *, dense_confirmed_pairs: object = frozenset(), consistency_issues: object = ()
    ) -> tuple[PartyFault, ...]:
        if any("camera obstructed" in b.description for b in f.behaviour_observations):
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.CANNOT_DETERMINE,
                    color="grey",
                    rule_id=None,
                    reason="Final CD reason: footage unclear",
                    cited_facts=(),
                ),
            )
        else:
            return (
                PartyFault(
                    party_description="red Toyota Camry sedan",
                    status=FaultStatus.AT_FAULT,
                    color="pink",
                    rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
                    reason="Failure to yield to main road traffic",
                    article="Article 6",
                    legal_source="Federal Decree-Law No. 14 of 2024",
                    cited_facts=(c_agreeing,),
                ),
            )

    import incident_report.entry as entry_mod

    monkeypatch.setattr(entry_mod, "evaluate_fault", mock_evaluate_fault)

    # Primary run is unclear (Cannot Determine); runs 2 and 3 are At Fault.
    # Majority vote = 2/3 At Fault (FLAGGED).
    # Final evaluation pf is from primary run (Cannot Determine).
    ai = FakeMultiRunAI([resp_unclear, resp_af1, resp_af2])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
    )

    assert len(report.parties) == 1
    party = report.parties[0]
    assert party.status == FaultStatus.AT_FAULT
    assert party.agreement_status == "flagged"
    assert party.reason == "Failure to yield to main road traffic"
    assert "footage unclear" not in party.reason
    assert party.cited_facts == (c_agreeing,)
    assert party.rule_id == RULE_ART_6_MAIN_ROAD.rule_id
    assert party.article == "Article 6"
    assert party.legal_source == "Federal Decree-Law No. 14 of 2024"


def test_f4_full_make_model_distinguishes_models_of_same_make() -> None:
    """F4: Keep the full make_model in the typed key.

    Toyota Camry and Toyota Corolla must have distinct keys, not merged as 'toyota'.
    """
    p_camry = PartyFact(
        description="white Toyota Camry sedan",
        color="white",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id="v1",
        timestamp_seconds=1.0,
        confidence=0.9,
    )
    p_corolla = PartyFact(
        description="white Toyota Corolla sedan",
        color="white",
        make_model="Toyota Corolla",
        body_type="sedan",
        view_id="v1",
        timestamp_seconds=1.0,
        confidence=0.9,
    )

    key_camry = party_canonical_key(p_camry)
    key_corolla = party_canonical_key(p_corolla)

    assert key_camry != key_corolla
    assert key_camry == "party:white:toyota camry:sedan"
    assert key_corolla == "party:white:toyota corolla:sedan"


def test_f3_citations_deduplicated_on_view_time_and_description() -> None:
    """F3: Helper de-duplicates on (view, time, description); distinct facts at same time are kept."""
    from incident_report.entry import _append_cited_facts_to_events

    party = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id="v1",
        timestamp_seconds=1.0,
        confidence=0.9,
    )
    facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[party],
        events=[],
        candidate_collisions=[],
    )

    # Cited fact with same view and time as party, but DIFFERENT description
    violation_cf = CitedFact(
        description="crossed stop line while signal red",
        view_id="v1",
        timestamp_seconds=1.0,
        confidence=0.95,
    )
    # Cited fact with same view, time, and description as party (duplicate)
    duplicate_party_cf = CitedFact(
        description="red Toyota Camry sedan",
        view_id="v1",
        timestamp_seconds=1.0,
        confidence=0.9,
    )

    updated = _append_cited_facts_to_events(facts, [duplicate_party_cf, violation_cf])
    # Duplicate party fact should be omitted, but distinct violation fact MUST be kept
    assert len(updated.events) == 1
    assert updated.events[0] == violation_cf


def test_f3_no_collision_call_site_preserves_distinct_violation_citation(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: RPT-26 no-collision call site appends violation citation sharing timestamp with party."""
    from incident_report.rules.models import Violation
    from incident_report.rules.mr130_1997 import RULE_SIGNAL
    from incident_report.rules.violations import map_violation

    view, view_hash = _make_view(b"clip-f3-no-collision")
    p = PartyFact(
        description="red Toyota Camry sedan",
        color="red",
        make_model="Toyota Camry",
        body_type="sedan",
        view_id=view_hash,
        timestamp_seconds=1.0,
        confidence=0.95,
    )
    facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[p],
        events=[],
        candidate_collisions=[],  # No collision -> RPT-26 branch
    )
    resp = AIResponse(summary="No collision summary", provenance="synthetic", facts=facts)

    # Violation citation at timestamp 1.0 (same moment as party sighting)
    viol_citation = CitedFact(
        description="disobeyed traffic light signal",
        view_id=view_hash,
        timestamp_seconds=1.0,
        confidence=0.9,
    )
    violation = map_violation(
        Violation(
            rule_id=RULE_SIGNAL.rule_id,
            party_description="red Toyota Camry sedan",
            cited_facts=(viol_citation,),
        )
    )

    import incident_report.entry as entry_mod

    # Mock agree_no_collision_violations to return this violation
    monkeypatch.setattr(
        entry_mod,
        "agree_no_collision_violations",
        lambda facts_list, faults_list, *, consistency_issues=(): ((violation,), {}),
    )

    ai = FakeMultiRunAI([resp])
    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=1),
    )

    assert len(report.violations) == 1
    # Distinct violation citation must be in report.facts.events
    assert report.facts is not None
    assert any(
        e.description == "disobeyed traffic light signal" and e.timestamp_seconds == 1.0 for e in report.facts.events
    )
