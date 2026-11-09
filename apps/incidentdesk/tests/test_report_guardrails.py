"""RPT-33: crafted bad outputs through Seam 1; no live model calls."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import pytest

from conftest import make_view
from incident_report.agreement import AgreementSettings
from incident_report.ai import AIRequest, AIResponse
from incident_report.domain import Attribution, Origin
from incident_report.entry import analyse_incident
from incident_report.facts import CitedFact, CollisionObservation, OverviewFacts, PartyFact
from incident_report.feedback import FeedbackStore, FeedbackSubmission
from incident_report.hashing import sha256_hex
from incident_report.intake import configured_store
from incident_report.questions import QARequest, QAResponse, QASentence, QuestionStore
from incident_report.rules.decree_law_14_2024 import RULE_ART_6_EMERGENCY, RULE_ART_7_4_PEDESTRIAN_SPEED
from incident_report.rules.models import FaultStatus, PartyFault, Violation
from incident_report.rules.mr130_1997 import RULE_REAR_END
from incident_report.rules.violations import map_violation
from incident_report.runlog import RunLog
from incident_report.web import create_app
from incident_report.web.templating import build_environment
from test_invalid_report_rendering import make_report as report_for_rendering
from test_new_incident_screen import SpyAIConnection
from test_report_decisions import _open_session, _override
from video_fixtures import synthetic_video


@dataclass
class RecordedAnswer:
    summary: str
    facts: OverviewFacts | None = None

    def analyse(self, request: AIRequest) -> AIResponse:
        return AIResponse(summary=self.summary, provenance="synthetic", facts=self.facts)


@pytest.mark.parametrize("raw_id", ["Vehicle_C", "Vehicle 1", "f863bf15-d9ec-4f4e-a262-8253b54e535b"])
def test_raw_id_in_summary_marks_stored_report_invalid_without_rewriting_it(run_log: RunLog, raw_id: str) -> None:
    summary = f"The {raw_id} hit the silver Toyota sedan."
    report = analyse_incident([make_view()], Origin.REPLAY, ai=RecordedAnswer(summary), run_log=run_log)

    assert not report.is_valid
    assert report.summary == summary
    assert any("raw_ids" in reason for reason in report.validation_errors)
    stored = run_log.get(report.run_id).report
    assert stored == report
    assert stored is not None
    assert stored.guardrail_results["raw_ids"]


def test_uncited_injury_in_summary_is_invalid_even_when_no_typed_injury_was_returned(run_log: RunLog) -> None:
    summary = "The driver suffered a serious injury."
    report = analyse_incident([make_view()], Origin.REPLAY, ai=RecordedAnswer(summary), run_log=run_log)
    assert not report.is_valid
    assert report.summary == summary
    assert report.guardrail_results["injury_citations"]
    assert run_log.get(report.run_id).report == report


def test_fine_in_summary_without_a_violation_is_not_silently_removed(run_log: RunLog) -> None:
    summary = "Fine: AED 400, 4 black points and 30 impound days under item 52."
    report = analyse_incident([make_view()], Origin.REPLAY, ai=RecordedAnswer(summary), run_log=run_log)
    assert not report.is_valid
    assert report.summary == summary
    assert report.guardrail_results["fine_requires_violation"]
    assert report.guardrail_results["legal_sources"]


@pytest.mark.parametrize("bad_field", ["aed", "black_points", "impound_days", "item_number", "source_url", "rule_id"])
def test_tampered_legal_figure_is_invalid_on_the_live_path(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch, bad_field: str
) -> None:
    view = make_view()
    fact = CitedFact(
        description="Contact",
        view_id=sha256_hex(synthetic_video(b"tiny-dummy-view-bytes")),
        timestamp_seconds=0.0,
        confidence=1.0,
    )
    facts = OverviewFacts(
        parties=[], events=[fact], candidate_collisions=[fact], behaviour_observations=[], location_conditions=[]
    )
    row = map_violation(
        Violation(rule_id=RULE_REAR_END.rule_id, party_description="silver Toyota sedan", cited_facts=(fact,))
    )
    bad_value = 99999 if bad_field in {"aed", "black_points", "impound_days"} else "invented"
    bad = row.model_copy(update={bad_field: bad_value})
    monkeypatch.setattr("incident_report.entry.violations_from_faults", lambda *args: (bad,))
    report = analyse_incident([view], Origin.REPLAY, ai=RecordedAnswer("Contact observed.", facts), run_log=run_log)
    assert not report.is_valid
    assert report.violations == (bad,)
    assert report.guardrail_results["legal_sources"]
    assert run_log.get(report.run_id).report == report


def test_referral_cannot_carry_a_fine(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    view = make_view()
    fact = CitedFact(
        description="Contact",
        view_id=sha256_hex(synthetic_video(b"tiny-dummy-view-bytes")),
        timestamp_seconds=0.0,
        confidence=1.0,
    )
    facts = OverviewFacts(
        parties=[], events=[fact], candidate_collisions=[fact], behaviour_observations=[], location_conditions=[]
    )
    referral = map_violation(
        Violation(
            rule_id=RULE_ART_7_4_PEDESTRIAN_SPEED.rule_id, party_description="pedestrian in blue", cited_facts=(fact,)
        )
    )
    bad = referral.model_copy(update={"aed": 1})
    monkeypatch.setattr("incident_report.entry.violations_from_faults", lambda *args: (bad,))
    report = analyse_incident([view], Origin.REPLAY, ai=RecordedAnswer("Contact observed.", facts), run_log=run_log)
    assert not report.is_valid
    assert report.violations[0].aed == 1
    assert report.guardrail_results["referral_without_fine"]


@pytest.mark.parametrize("view_id,timestamp", [("missing-view", 0.0), ("known", 999.0)])
def test_injury_grade_with_bad_citation_is_invalid(run_log: RunLog, view_id: str, timestamp: float) -> None:
    view = make_view()
    fingerprint = sha256_hex(synthetic_video(b"tiny-dummy-view-bytes"))
    collision = CitedFact(description="Contact", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)
    injury = CollisionObservation(
        kind="person_injured_visible",
        subject="driver in blue",
        other_party="",
        injury_grade="serious",
        view_id=fingerprint if view_id == "known" else view_id,
        timestamp_seconds=timestamp,
        confidence=1.0,
    )
    facts = OverviewFacts(
        parties=[PartyFact(description="driver in blue", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)],
        events=[],
        candidate_collisions=[collision],
        behaviour_observations=[],
        location_conditions=[],
        collision_observations=[injury],
    )
    report = analyse_incident([view], Origin.REPLAY, ai=RecordedAnswer("Contact observed.", facts), run_log=run_log)
    assert not report.is_valid
    assert report.facts == facts
    assert report.guardrail_results["injury_citations"]
    assert report.validation_errors


def test_injury_summary_cannot_invent_a_grade_over_a_no_injury_observation(run_log: RunLog) -> None:
    view = make_view()
    fingerprint = sha256_hex(synthetic_video(b"tiny-dummy-view-bytes"))
    collision = CitedFact(description="Contact", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)
    observation = CollisionObservation(
        kind="no_injury_visible",
        subject="driver in blue",
        other_party="",
        injury_grade="unknown",
        view_id=fingerprint,
        timestamp_seconds=0.0,
        confidence=1.0,
    )
    facts = OverviewFacts(
        parties=[PartyFact(description="driver in blue", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)],
        events=[],
        candidate_collisions=[collision],
        behaviour_observations=[],
        location_conditions=[],
        collision_observations=[observation],
    )
    report = analyse_incident(
        [view], Origin.REPLAY, ai=RecordedAnswer("driver in blue suffered a serious injury.", facts), run_log=run_log
    )
    assert not report.is_valid
    assert report.guardrail_results["injury_citations"]


def test_fabricated_article_in_fault_is_invalid(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    fingerprint = sha256_hex(synthetic_video(b"tiny-dummy-view-bytes"))
    fact = CitedFact(description="Contact", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)
    facts = OverviewFacts(
        parties=[], events=[], candidate_collisions=[fact], behaviour_observations=[], location_conditions=[]
    )
    fault = PartyFault(
        party_description="silver Toyota sedan",
        status=FaultStatus.AT_FAULT,
        color="pink",
        rule_id=RULE_REAR_END.rule_id,
        legal_source=RULE_REAR_END.legal_source,
        article="Article 99999",
        reason="Following distance breach",
        cited_facts=(fact,),
    )
    monkeypatch.setattr("incident_report.entry.evaluate_fault", lambda *args, **kwargs: (fault,))
    report = analyse_incident(
        [make_view()], Origin.REPLAY, ai=RecordedAnswer("Contact observed.", facts), run_log=run_log
    )
    assert not report.is_valid
    assert report.parties[0].article == "Article 99999"
    assert report.guardrail_results["legal_sources"]


def test_officer_override_and_feedback_never_reach_any_analysis_request(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    canaries = (
        "OFFICER_PRIVATE_CANARY_339",
        "OVERRIDE_PRIVATE_CANARY_339",
        "FEEDBACK_PRIVATE_CANARY_339",
        "QA_PRIVATE_CANARY_339",
    )
    clip = synthetic_video(b"guardrail-isolation")
    fingerprint = sha256_hex(clip)
    party = PartyFact(description="silver Toyota sedan", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)
    contact = CitedFact(description="Contact", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)
    facts = OverviewFacts(
        parties=[party], events=[], candidate_collisions=[contact], behaviour_observations=[], location_conditions=[]
    )
    spy = SpyAIConnection(default_facts=facts)
    session = _open_session(create_app(ai=spy, run_log=run_log, evidence=None), run_log.database_url)
    attribution = Attribution(session_id=session.session_id, link_id=session.link_id)
    first = analyse_incident(
        [make_view(clip)], Origin.REPLAY, ai=spy, run_log=run_log, officer_question=canaries[0], attribution=attribution
    )
    assert first.is_valid

    class RecordedQA:
        def answer(self, request: QARequest) -> QAResponse:
            assert canaries[3] in request.data_json
            return QAResponse(
                sentences=[QASentence(text="Cannot Determine from the footage", view_id=None, timestamp_seconds=None)]
            )

    QuestionStore(run_log.database_url).ask(first, canaries[3], qa=RecordedQA(), evidence=configured_store())
    assert _override(session, first, value="At Fault", reason="other", text=canaries[1]).status_code == 303
    FeedbackStore(run_log.database_url).record_feedback(
        FeedbackSubmission(
            feedback_id=uuid.uuid4(),
            created_at=datetime.now(UTC),
            text=canaries[2],
            run_id=first.run_id,
            session_id=session.session_id,
            link_id=session.link_id,
        )
    )
    # Force the repair path, then dense, with existing officer-only records present.
    bad_fact = contact.model_copy(update={"timestamp_seconds": 999.0})
    spy.default_facts = facts.model_copy(update={"events": [bad_fact]})
    spy.repair_facts[fingerprint] = facts
    second = analyse_incident(
        [make_view(clip, label=canaries[1], acquisition_method=canaries[2])],
        Origin.REPLAY,
        ai=spy,
        run_log=run_log,
        officer_question=canaries[0],
        attribution=attribution,
        agreement_settings=AgreementSettings(n_runs=3),
    )
    assert second.is_valid
    assert len(spy.received_requests) == 4
    assert len(spy.received_repairs) == 3
    assert len(spy.received_windows) >= 2
    requests = spy.received_requests + [r for r, _, _ in spy.received_repairs] + [r for r, _ in spy.received_windows]
    for request in requests:
        payload = json.dumps(asdict(request), default=str)
        assert all(canary not in payload for canary in canaries)
        assert set(asdict(request)) == {"input_hash", "prompt_version", "views"}
    for _, response, errors in spy.received_repairs:
        assert all(canary not in repr((response, errors)) for canary in canaries)
    assert run_log.get_officer_question(first.run_id) == canaries[0]
    assert QuestionStore(run_log.database_url).list_for_report(first.run_id)[0]["question"] == canaries[3]
    assert run_log.decisions.state(first.run_id).overrides["party:0"].reason_text == canaries[1]
    assert any(f.text == canaries[2] for f in FeedbackStore(run_log.database_url).list_feedback())


def test_plain_indefinite_article_is_not_a_raw_vehicle_id(run_log: RunLog) -> None:
    report = analyse_incident(
        [make_view()], Origin.REPLAY, ai=RecordedAnswer("The red car a few metres ahead stopped."), run_log=run_log
    )
    assert report.is_valid
    assert not report.guardrail_results["raw_ids"]


def test_injury_in_event_text_still_requires_visible_collision_observation(run_log: RunLog) -> None:
    fingerprint = sha256_hex(synthetic_video(b"tiny-dummy-view-bytes"))
    event = CitedFact(
        description="driver in blue suffered serious injuries",
        view_id=fingerprint,
        timestamp_seconds=0.0,
        confidence=1.0,
    )
    facts = OverviewFacts(
        parties=[], events=[event], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
    )
    report = analyse_incident(
        [make_view()], Origin.REPLAY, ai=RecordedAnswer("Vehicle stopped.", facts), run_log=run_log
    )
    assert not report.is_valid
    assert report.facts == facts
    assert report.guardrail_results["injury_citations"]


@pytest.mark.parametrize("bad_reason", ["Fine AED 99999 under Article 999", "driver in blue suffered serious injuries"])
def test_final_fault_prose_cannot_hide_unsourced_claims(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch, bad_reason: str
) -> None:
    fingerprint = sha256_hex(synthetic_video(b"tiny-dummy-view-bytes"))
    event = CitedFact(description="Contact", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)
    facts = OverviewFacts(
        parties=[], events=[], candidate_collisions=[event], behaviour_observations=[], location_conditions=[]
    )
    fault = PartyFault(
        party_description="silver Toyota sedan",
        status=FaultStatus.AT_FAULT,
        color="pink",
        rule_id=RULE_REAR_END.rule_id,
        legal_source=RULE_REAR_END.legal_source,
        article=RULE_REAR_END.article,
        reason=bad_reason,
        cited_facts=(event,),
    )
    monkeypatch.setattr("incident_report.entry.evaluate_fault", lambda *args, **kwargs: (fault,))
    report = analyse_incident(
        [make_view()], Origin.REPLAY, ai=RecordedAnswer("Contact observed.", facts), run_log=run_log
    )
    assert not report.is_valid
    assert report.parties[0].reason == bad_reason


def test_final_fault_citation_must_resolve_to_a_view(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    cited = CitedFact(description="Contact", view_id="not-a-known-view", timestamp_seconds=0.0, confidence=1.0)
    facts = OverviewFacts(
        parties=[], events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
    )
    fault = PartyFault(
        party_description="silver Toyota sedan",
        status=FaultStatus.NOT_AT_FAULT,
        color="green",
        reason="Stationary",
        cited_facts=(cited,),
    )
    monkeypatch.setattr("incident_report.entry.evaluate_fault", lambda *args, **kwargs: (fault,))
    report = analyse_incident(
        [make_view()], Origin.REPLAY, ai=RecordedAnswer("Vehicle stopped.", facts), run_log=run_log
    )
    assert not report.is_valid
    assert report.parties[0].cited_facts == (cited,)
    assert report.guardrail_results["citations"]


def test_final_citation_description_cannot_hide_a_legal_figure(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    fingerprint = sha256_hex(synthetic_video(b"tiny-dummy-view-bytes"))
    cited = CitedFact(description="Fine AED 99999", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)
    facts = OverviewFacts(
        parties=[], events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
    )
    fault = PartyFault(
        party_description="silver Toyota sedan",
        status=FaultStatus.NOT_AT_FAULT,
        color="green",
        reason="Stationary",
        cited_facts=(cited,),
    )
    monkeypatch.setattr("incident_report.entry.evaluate_fault", lambda *args, **kwargs: (fault,))
    report = analyse_incident(
        [make_view()], Origin.REPLAY, ai=RecordedAnswer("Vehicle stopped.", facts), run_log=run_log
    )
    assert not report.is_valid
    assert report.guardrail_results["legal_sources"]


@pytest.mark.parametrize("summary", ["Injury Grade: Cannot Determine", "Injuries cannot be determined from this view"])
def test_explicit_injury_abstention_does_not_claim_an_injury(run_log: RunLog, summary: str) -> None:
    report = analyse_incident([make_view()], Origin.REPLAY, ai=RecordedAnswer(summary), run_log=run_log)
    assert report.is_valid
    assert report.summary == summary


def test_known_article_prefix_does_not_allow_invented_article(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    fingerprint = sha256_hex(synthetic_video(b"tiny-dummy-view-bytes"))
    fact = CitedFact(description="Contact", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)
    facts = OverviewFacts(
        parties=[], events=[fact], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
    )
    rule = RULE_ART_6_EMERGENCY
    fault = PartyFault(
        party_description="silver Toyota sedan",
        status=FaultStatus.AT_FAULT,
        color="pink",
        rule_id=rule.rule_id,
        legal_source=rule.legal_source,
        article=rule.article,
        reason="Breach under Art. 69999",
        cited_facts=(fact,),
    )
    monkeypatch.setattr("incident_report.entry.evaluate_fault", lambda *args, **kwargs: (fault,))
    report = analyse_incident(
        [make_view()], Origin.REPLAY, ai=RecordedAnswer("Vehicle stopped.", facts), run_log=run_log
    )
    assert not report.is_valid
    assert report.guardrail_results["legal_sources"]


@pytest.mark.parametrize("rule_id", [RULE_REAR_END.rule_id, RULE_ART_7_4_PEDESTRIAN_SPEED.rule_id])
def test_unchanged_sourced_fine_or_referral_remains_valid(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch, rule_id: str
) -> None:
    fingerprint = sha256_hex(synthetic_video(b"tiny-dummy-view-bytes"))
    fact = CitedFact(description="Contact", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)
    facts = OverviewFacts(
        parties=[], events=[fact], candidate_collisions=[fact], behaviour_observations=[], location_conditions=[]
    )
    row = map_violation(Violation(rule_id=rule_id, party_description="silver Toyota sedan", cited_facts=(fact,)))
    monkeypatch.setattr("incident_report.entry.violations_from_faults", lambda *args: (row,))
    report = analyse_incident(
        [make_view()], Origin.REPLAY, ai=RecordedAnswer("Contact observed.", facts), run_log=run_log
    )
    assert report.is_valid
    assert report.violations == (row,)
    assert all(not errors for errors in report.guardrail_results.values())
    assert run_log.get(report.run_id).report == report


def test_named_cited_visible_injury_grade_remains_valid(run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate collision classification from fault-engine PartyFact subclasses in citations.
    # Retain the complete Report/RunLog roundtrip assertion, as in the penalty controls.
    monkeypatch.setattr("incident_report.entry.evaluate_fault", lambda *args, **kwargs: ())
    fingerprint = sha256_hex(synthetic_video(b"tiny-dummy-view-bytes"))
    fact = CitedFact(description="Contact", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)
    observation = CollisionObservation(
        kind="person_injured_visible",
        subject="driver in blue",
        other_party="",
        injury_grade="serious",
        view_id=fingerprint,
        timestamp_seconds=0.0,
        confidence=1.0,
    )
    facts = OverviewFacts(
        parties=[PartyFact(description="driver in blue", view_id=fingerprint, timestamp_seconds=0.0, confidence=1.0)],
        events=[],
        candidate_collisions=[fact],
        behaviour_observations=[],
        location_conditions=[],
        collision_observations=[observation],
    )
    report = analyse_incident(
        [make_view()],
        Origin.REPLAY,
        ai=RecordedAnswer("driver in blue suffered a serious injury.", facts),
        run_log=run_log,
    )
    assert report.is_valid
    assert report.collision_summary.records[0].injuries[0].grade == "serious"
    assert run_log.get(report.run_id).report == report


@pytest.mark.parametrize(
    "template,review", [("report.html", False), ("report.html", True), ("report_print.html", False)]
)
def test_report_checks_are_visible_and_escaped_on_screen_review_and_print(template: str, review: bool) -> None:
    report = report_for_rendering(is_valid=False, summary="Synthetic report").model_copy(
        update={"guardrail_results": {"citations": ("<script>bad citation</script>",), "legal_sources": ()}}
    )
    html = build_environment().get_template(template).render(report=report, review_workspace=review)
    assert "Report checks" in html
    assert "Evidence citations" in html
    assert "Legal sources" in html
    assert "Failed" in html and "Passed" in html
    assert "&lt;script&gt;bad citation&lt;/script&gt;" in html
    assert "<script>bad citation</script>" not in html


def test_legacy_report_does_not_claim_unrecorded_checks_passed() -> None:
    report = report_for_rendering(is_valid=True, summary="Historical report")
    html = build_environment().get_template("report.html").render(report=report)
    assert "Checks were not recorded for this report." in html
