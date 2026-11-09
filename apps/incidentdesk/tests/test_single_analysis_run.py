"""Spec 003: a Run uses one analysis; missing research votes cannot decide."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from access_client import authorized_client
from incident_report.agreement import (
    AgreementResult,
    AgreementSettings,
    AgreementStatus,
    agree_contributing_factors,
    agree_no_collision_violations,
    compare_collisions,
    compare_events,
    compare_fault,
    compare_parties,
    evaluate_majority,
)
from incident_report.ai import AIResponse
from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo
from incident_report.config import ProcessingSettings
from incident_report.domain import Origin, RunStatus
from incident_report.entry import analyse_incident
from incident_report.rules import evaluate_fault
from incident_report.rules.models import FaultStatus, PartyFault
from incident_report.runlog import RunLog
from incident_report.web import create_app
from incident_report.web.templating import build_environment
from test_agreement_signal import FakeMultiRunAI, _make_view
from test_collision_recall_all_runs import contact, facts
from test_live_gemini import PUBLIC_VIDEO, FakeTransport
from test_no_incident_factors import facts as signal_facts
from test_processing_screen import _upload
from test_report_parity import full_report


def test_single_analysis_defaults_and_research_opt_in() -> None:
    assert AgreementSettings().n_runs == 1
    assert AgreementSettings.from_env({}).n_runs == 1
    assert AgreementSettings.from_env({"INCIDENT_AGREEMENT_RUNS": "3"}).n_runs == 3
    assert ProcessingSettings().agreement_runs == 1
    assert ProcessingSettings.from_env({"INCIDENT_AGREEMENT_RUNS": "3"}).agreement_runs == 1


@pytest.mark.parametrize("missing", [[], ["__absent__"]])
@pytest.mark.parametrize("support", [0, 1, 2])
def test_missing_values_never_agree(missing: list[str], support: int) -> None:
    vote = evaluate_majority(
        ["At Fault"] * support + missing * (3 - support), field_name="fault_status:party", n_runs=3
    )
    assert vote.runs_agreeing == support
    assert vote.status == (AgreementStatus.FLAGGED if support == 2 else AgreementStatus.CANNOT_DETERMINE)
    assert vote.agreed_value != "At Fault" if support < 2 else vote.agreed_value == "At Fault"
    if support:
        assert f"{support} of 3" in (vote.details or "")


@pytest.mark.parametrize("support", [0, 1, 2])
def test_all_vote_sites_count_present_support(support: int) -> None:
    observed = facts(contact(3), parties=("silver sedan",))
    observed = observed.model_copy(update={"events": [contact(2, "approached")]})
    empty = facts()
    readings = [observed] * support + [empty] * (3 - support)
    for compare in (compare_parties, compare_events, compare_collisions):
        fields = compare(readings)
        assert bool(fields) == bool(support)
        for field in fields.values():
            assert field.runs_agreeing == support
            assert field.status == (AgreementStatus.FLAGGED if support == 2 else AgreementStatus.CANNOT_DETERMINE)
    # Both duty-vote sites distinguish absent runs from explicit unknown readings.
    red = signal_facts()
    from incident_report.rules.models import FaultStatus, PartyFault

    cited = red.parties[0]
    fault = PartyFault(
        party_description=cited.description,
        status=FaultStatus.AT_FAULT,
        color="pink",
        reason="Synthetic cited fault",
        cited_facts=(cited,),
    )
    fault_runs = [(fault,)] * support + [()] * (3 - support)
    fault_fields = compare_fault(fault_runs, [red] * support + [empty] * (3 - support))
    assert bool(fault_fields) == bool(support)
    for field in fault_fields.values():
        assert field.runs_agreeing == support
        assert field.status == (AgreementStatus.FLAGGED if support == 2 else AgreementStatus.CANNOT_DETERMINE)
    violations, fields = agree_no_collision_violations([red] * 3, fault_runs)
    assert bool(violations) == (support == 2)
    for field in fields.values():
        assert field.runs_agreeing == support
        assert field.status == (AgreementStatus.FLAGGED if support == 2 else AgreementStatus.CANNOT_DETERMINE)
    contribution = evaluate_fault(signal_facts(collision=True, phase_t=4.0))
    findings, fields = agree_contributing_factors([contribution] * support + [()] * (3 - support))
    assert bool(findings) == bool(support)
    for field in fields.values():
        assert field.runs_agreeing == support
        assert field.status == (AgreementStatus.FLAGGED if support == 2 else AgreementStatus.CANNOT_DETERMINE)


@pytest.mark.parametrize("override", ["3", "not-a-number"])
def test_officer_upload_and_retry_force_one_analysis(
    run_log: RunLog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    override: str,
) -> None:
    monkeypatch.setenv("INCIDENT_AGREEMENT_RUNS", override)
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    raw = json.dumps({"parties": [], "events": [], "candidate_collisions": []})
    transport = FakeTransport([raw] * 6)
    ai = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(video, True), transport=transport
    )
    observed: list[int | None] = []

    def capture(*args: Any, **kwargs: Any) -> Any:
        settings = kwargs.get("agreement_settings")
        observed.append(settings.n_runs if settings else None)
        return analyse_incident(*args, **kwargs)

    monkeypatch.setattr("incident_report.web.app.analyse_incident", capture)
    with caplog.at_level(logging.WARNING):
        app = create_app(ai=ai, run_log=run_log, prompt_version="overview-1")
        with authorized_client(app, run_log.database_url) as client:
            _upload(client, PUBLIC_VIDEO)
    assert observed == [1]
    assert len(transport.calls) == 1
    run = run_log.list_runs()[0]
    assert run.status == RunStatus.SUCCEEDED
    assert len(run.calls) == 1
    assert run.stage_latencies_ms["overview"] >= 0
    assert len([r for r in caplog.records if "INCIDENT_AGREEMENT_RUNS" in r.message]) == 1

    # Retry is a separate officer entry path; make a genuinely failed upload first.
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic failure before generation")

    monkeypatch.setattr("incident_report.web.app.analyse_incident", fail)
    with authorized_client(app, run_log.database_url) as client:
        _upload(client, PUBLIC_VIDEO)
        failed = next(r for r in run_log.list_runs() if r.status == RunStatus.FAILED)
        monkeypatch.setattr("incident_report.web.app.analyse_incident", capture)
        response = client.post(f"/reports/{failed.run_id}/retry", follow_redirects=False)
        assert response.status_code == 303
    assert observed == [1, 1]
    assert len(transport.calls) == 2
    assert len([r for r in caplog.records if "INCIDENT_AGREEMENT_RUNS" in r.message]) == 1


@pytest.mark.parametrize("n", [1, 3])
def test_report_copy_distinguishes_single_analysis_from_older_method(n: int) -> None:
    report = full_report()
    assert report.agreement is not None
    report = report.model_copy(update={"agreement": report.agreement.model_copy(update={"n_runs": n})})
    rendered = build_environment().get_template("report.html").render(report=report)
    if n == 1:
        assert "Agreement:" not in rendered
        assert "Runs disagreed (majority)" not in rendered
        assert "badge--check" not in rendered
        assert "Synthetic agreement detail" not in rendered
        assert "1 of 1" not in rendered
    else:
        assert "recorded under the older multi-analysis method" in rendered
        assert "Synthetic agreement detail" in rendered
        assert "badge--check" in rendered


def test_single_analysis_close_up_copy_and_missing_encodings() -> None:
    from incident_report.agreement import FieldAgreement
    from incident_report.ai.dense import CandidateWindow
    from incident_report.entry import _flag_agreement_confirmed_collisions

    for absent in (None, "__absent__"):
        old = FieldAgreement(
            field_name="collision:@3.0s",
            status=AgreementStatus.CANNOT_DETERMINE,
            runs_total=1,
            runs_agreeing=0,
            values=[absent],
        )
        agreement = AgreementResult(n_runs=1, status=AgreementStatus.NOT_MEASURED, fields={"collision:@3.0s": old})
        result = _flag_agreement_confirmed_collisions(
            agreement,
            [(0, contact(3))],
            [CandidateWindow("a" * 64, 3, 2, 4, "contact", "agreement_run", proposed_by_runs=(0,))],
            1,
        )
        assert list(result.fields) == ["collision:@3.0s"]
        assert result.fields["collision:@3.0s"].details == "Confirmed by the close-up check."


@pytest.mark.parametrize("overview_decision", [False, True])
def test_absent_research_votes_cannot_keep_primary_fault(
    run_log: RunLog,
    monkeypatch: pytest.MonkeyPatch,
    overview_decision: bool,
) -> None:
    view, digest = _make_view()
    observed = facts(parties=("silver sedan",))
    p = observed.parties[0].model_copy(update={"view_id": digest})
    observed = observed.model_copy(update={"parties": [p]})
    fault = PartyFault(
        party_description=p.description,
        status=FaultStatus.AT_FAULT,
        color="pink",
        reason="Synthetic primary decision",
        cited_facts=(p,),
    )

    def engine(value: Any, **kwargs: Any) -> tuple[PartyFault, ...]:
        return (fault,) if value.parties and (overview_decision or "dense_confirmed_pairs" in kwargs) else ()

    monkeypatch.setattr("incident_report.entry.evaluate_fault", engine)
    report = analyse_incident(
        [view],
        Origin.REPLAY,
        ai=FakeMultiRunAI(
            [AIResponse(summary="fixture", provenance="synthetic", facts=f) for f in (observed, facts(), facts())]
        ),
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
        processing_settings=ProcessingSettings(concurrent_analysis=False),
    )
    assert report.parties[0].status == FaultStatus.CANNOT_DETERMINE
    assert f"seen in only {int(overview_decision)} of 3 analyses" in report.parties[0].reason
    assert report.violations == ()


@pytest.mark.parametrize("support", [0, 2])
def test_missing_primary_keeps_all_research_positions(run_log: RunLog, support: int) -> None:
    view, digest = _make_view()
    observed = facts(parties=("silver sedan",))
    observed = observed.model_copy(update={"parties": [observed.parties[0].model_copy(update={"view_id": digest})]})
    responses = [AIResponse(summary="missing primary", provenance="synthetic", facts=None)]
    responses += [AIResponse(summary="additional", provenance="synthetic", facts=observed if support else None)] * 2
    ai = FakeMultiRunAI(responses)
    report = analyse_incident(
        [view],
        Origin.REPLAY,
        ai=ai,
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
        processing_settings=ProcessingSettings(concurrent_analysis=False),
    )
    assert len(ai.calls_received) == 3
    assert report.agreement is not None
    assert report.agreement.n_runs == 3
    if support:
        field = report.agreement.fields["party:desc:silver sedan"]
        assert field.runs_total == 3
        assert field.runs_agreeing == 2
        assert field.status == AgreementStatus.FLAGGED
        assert field.values[0] in (None, "__absent__")
    else:
        assert report.agreement.fields == {}
        assert report.agreement.status == AgreementStatus.NOT_MEASURED


@pytest.mark.parametrize("unknown", [True, False])
def test_violation_unknown_is_an_outcome_and_missing_is_not(unknown: bool) -> None:
    observed = signal_facts(signal="unknown" if unknown else "red")
    fault_runs = [evaluate_fault(observed)] * 3 if unknown else [evaluate_fault(observed), (), ()]
    violations, fields = agree_no_collision_violations([observed] * 3, fault_runs)
    from incident_report.rules.mr130_1997 import RULE_SIGNAL

    vote = fields[f"violation:party:desc:white toyota sedan:{RULE_SIGNAL.rule_id}"]
    assert violations == ()
    if unknown:
        assert vote.values == [None, None, None]
        assert vote.status == AgreementStatus.TRUSTED
        assert vote.agreed_value is None
        assert vote.runs_agreeing == 3
    else:
        assert vote.status == AgreementStatus.CANNOT_DETERMINE
        assert vote.agreed_value is None
        assert vote.runs_agreeing == 1
        assert "1 of 3" in (vote.details or "")


def test_empty_overview_agreement_cannot_trust_final_fault(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    view, digest = _make_view()
    party = facts(parties=("silver sedan",)).parties[0].model_copy(update={"view_id": digest})
    fault = PartyFault(
        party_description=party.description,
        status=FaultStatus.AT_FAULT,
        color="pink",
        reason="Synthetic final decision without overview support",
        cited_facts=(party,),
    )

    def engine(value: Any, **kwargs: Any) -> tuple[PartyFault, ...]:
        return (fault,) if "dense_confirmed_pairs" in kwargs else ()

    monkeypatch.setattr("incident_report.entry.evaluate_fault", engine)
    report = analyse_incident(
        [view],
        Origin.REPLAY,
        ai=FakeMultiRunAI([AIResponse(summary="empty overview", provenance="synthetic", facts=facts())] * 3),
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
        processing_settings=ProcessingSettings(concurrent_analysis=False),
    )
    assert report.agreement is not None
    assert report.agreement.fields == {}
    assert report.agreement.status == AgreementStatus.NOT_MEASURED
    assert report.parties[0].status == FaultStatus.CANNOT_DETERMINE
    assert report.parties[0].agreement_status == AgreementStatus.CANNOT_DETERMINE.value
    assert "seen in only 0 of 3 analyses" in report.parties[0].reason
    assert report.violations == ()
