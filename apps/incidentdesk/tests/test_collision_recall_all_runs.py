"""RPT-36 T5: a contact seen by any overview run is checked by the dense pass, then flagged.

Synthetic responses only; no model calls.
"""

from __future__ import annotations

from typing import Any

from incident_report.agreement import AgreementSettings, AgreementStatus
from incident_report.ai import AIResponse
from incident_report.ai.dense import (
    CandidateWindow,
    DenseSettings,
    consolidate_facts,
    dense_request_hash,
    union_windows,
)
from incident_report.ai.interface import AIRequest
from incident_report.config import ProcessingSettings
from incident_report.domain import Origin, ReportView, Role
from incident_report.entry import analyse_incident
from incident_report.facts import CitedFact, OverviewFacts, PartyFact
from incident_report.runlog import RunLog

SETTINGS = DenseSettings(model="gemini-3.8-flash")
VIEW = "a" * 64


def view() -> ReportView:
    return ReportView(
        role=Role.EXTERIOR, sha256=VIEW, filename="public.mp4", label=None, acquisition_method=None, duration_seconds=30
    )


def contact(t: float, text: str = "contact", view_id: str = VIEW) -> CitedFact:
    return CitedFact(description=f"{text} {t}", view_id=view_id, timestamp_seconds=t, confidence=0.9)


def facts(*collisions: CitedFact, parties: tuple[str, ...] = ()) -> OverviewFacts:
    return OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[PartyFact(description=d, view_id=VIEW, timestamp_seconds=1.0, confidence=0.9) for d in parties],
        events=[],
        candidate_collisions=list(collisions),
    )


def primary_windows(*stamps: float) -> tuple[CandidateWindow, ...]:
    return tuple(CandidateWindow(VIEW, t, max(0.0, t - 2), t + 2, f"contact {t}", "overview") for t in stamps)


# ---- window union -----------------------------------------------------------------------------


def test_contact_seen_only_by_run_two_gets_a_window_with_provenance() -> None:
    selected, dropped = union_windows(primary_windows(), [(1, [contact(9.0)])], SETTINGS, {VIEW: 30.0})
    assert dropped == ()
    assert len(selected) == 1
    assert selected[0].source == "agreement_run"
    assert selected[0].proposed_by_runs == (1,)
    assert (selected[0].start_seconds, selected[0].end_seconds) == (7.0, 11.0)


def test_primary_window_is_unchanged_and_gains_provenance_only() -> None:
    before = primary_windows(5.0)
    selected, _ = union_windows(before, [(1, [contact(6.0)]), (2, [contact(4.5)])], SETTINGS, {VIEW: 30.0})
    assert len(selected) == 1
    window = selected[0]
    assert (window.view_id, window.candidate_timestamp, window.start_seconds, window.end_seconds) == (
        VIEW,
        5.0,
        3.0,
        7.0,
    )
    assert window.source == "overview"
    assert window.candidate_description == before[0].candidate_description
    assert window.proposed_by_runs == (0, 1, 2)
    assert dense_request_hash(_req(), window, SETTINGS) == dense_request_hash(_req(), before[0], SETTINGS)


def test_overlapping_windows_from_several_runs_merge_once() -> None:
    selected, _ = union_windows(
        primary_windows(),
        [(1, [contact(9.0)]), (2, [contact(10.5)]), (2, [contact(20.0)])],
        SETTINGS,
        {VIEW: 30.0},
    )
    assert [(w.start_seconds, w.end_seconds, w.proposed_by_runs) for w in selected] == [
        (7.0, 12.5, (1, 2)),
        (18.0, 22.0, (2,)),
    ]


def test_different_views_never_merge() -> None:
    selected, _ = union_windows(
        primary_windows(), [(1, [contact(9.0), contact(9.0, view_id="b" * 64)])], SETTINGS, {VIEW: 30.0, "b" * 64: 30.0}
    )
    assert sorted(w.view_id for w in selected) == [VIEW, "b" * 64]


def test_window_cap_keeps_primary_and_drops_least_supported_with_a_record() -> None:
    cands = [(1, [contact(t) for t in (3.0, 9.0, 15.0, 21.0)]), (2, [contact(15.0)])]
    selected, dropped = union_windows(primary_windows(26.0), cands, SETTINGS, {VIEW: 30.0}, cap=3)
    assert [w.candidate_timestamp for w in selected if w.source == "overview"] == [26.0]
    kept = [w for w in selected if w.source == "agreement_run"]
    assert len(kept) == 2
    assert any(w.proposed_by_runs == (1, 2) for w in kept)  # more support is kept first
    assert len(dropped) == 2
    assert all(w.source == "agreement_run" for w in dropped)


def test_no_candidates_in_any_run_gives_no_windows() -> None:
    assert union_windows((), [(1, []), (2, [])], SETTINGS, {VIEW: 30.0}) == ((), ())


# ---- consolidation ----------------------------------------------------------------------------


def test_confirmed_agreement_window_adds_a_collision_and_unconfirmed_adds_nothing() -> None:
    overview = facts()
    confirmed = facts(contact(9.0, "settled"))
    empty = facts()
    merged = consolidate_facts(overview, [confirmed, empty], agreement_only=frozenset({0, 1}))
    assert [c.description for c in merged.candidate_collisions] == ["settled 9.0"]
    assert consolidate_facts(overview, [empty], agreement_only=frozenset({0})).candidate_collisions == []


def test_unconfirmed_agreement_window_adds_no_other_evidence() -> None:
    noisy = facts(parties=("blue sedan",))
    merged = consolidate_facts(facts(), [noisy], agreement_only=frozenset({0}))
    assert merged.parties == [] and merged.unmatched_dense_parties == []


def test_agreement_window_does_not_replace_primary_settled_collision() -> None:
    overview = facts(contact(5.0, "primary"))
    primary_dense = facts()  # primary window settles nothing: primary collision stays
    extra = facts(contact(20.0, "settled"))
    merged = consolidate_facts(overview, [primary_dense, extra], agreement_only=frozenset({1}))
    assert [c.description for c in merged.candidate_collisions] == ["primary 5.0", "settled 20.0"]


def test_primary_only_consolidation_is_identical_to_today() -> None:
    overview = facts(contact(5.0, "primary"))
    dense = facts(contact(5.2, "settled"))
    assert consolidate_facts(overview, [dense]) == consolidate_facts(overview, [dense], agreement_only=frozenset())


# ---- entry ------------------------------------------------------------------------------------


def _req() -> AIRequest:
    return AIRequest(input_hash="h", prompt_version="dense-2", views=())


def run_entry(
    run_log: RunLog, per_run: list[OverviewFacts], dense_by_time: dict[float, OverviewFacts], calls: list[Any]
) -> Any:
    state = {"i": 0}

    class AI:
        def analyse(self, request: Any) -> AIResponse:
            index = state["i"]
            state["i"] += 1
            return AIResponse(f"run {index}", "synthetic", per_run[index])

        def analyse_window(self, request: Any, window: Any) -> Any:
            calls.append(window)
            return dense_by_time.get(window.candidate_timestamp, facts()), ()

    return analyse_incident(
        [view()],
        Origin.REPLAY,
        ai=AI(),
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=len(per_run)),
        dense_settings=SETTINGS,
        processing_settings=ProcessingSettings(concurrent_analysis=False),
    )


def test_entry_confirmed_contact_from_run_two_is_collision_flagged_with_note(run_log: RunLog) -> None:
    calls: list[Any] = []
    report = run_entry(
        run_log,
        [facts(), facts(contact(9.0)), facts()],
        {9.0: facts(contact(9.5, "settled"))},
        calls,
    )
    assert [(w.source, w.proposed_by_runs) for w in calls] == [("agreement_run", (1,))]
    assert report.incident_status == "Collision"
    assert [c.description for c in report.facts.candidate_collisions] == ["settled 9.5"]
    field = report.agreement.fields["collision:@9.5s"]
    assert field.status == AgreementStatus.FLAGGED
    assert "Seen by run 2 of 3, confirmed by the close-up check" in (field.details or "")
    assert report.agreement.status == AgreementStatus.FLAGGED
    assert len(report.dense_facts) == 1


def test_entry_unconfirmed_contact_is_not_added_but_evidence_is_kept(run_log: RunLog) -> None:
    calls: list[Any] = []
    report = run_entry(run_log, [facts(), facts(contact(9.0)), facts()], {}, calls)
    assert len(calls) == 1
    assert report.facts.candidate_collisions == []
    assert report.incident_status != "Collision"
    assert len(report.dense_facts) == 1
    assert "collision:@9.5s" not in report.agreement.fields


def test_entry_primary_only_case_calls_the_same_windows_as_before(run_log: RunLog) -> None:
    calls: list[Any] = []
    run_entry(run_log, [facts(contact(5.0)), facts(contact(5.5)), facts()], {5.0: facts(contact(5.0, "s"))}, calls)
    assert [(w.start_seconds, w.end_seconds, w.source) for w in calls] == [(3.0, 7.0, "overview")]


def test_entry_no_incident_in_every_run_makes_no_dense_call(run_log: RunLog) -> None:
    calls: list[Any] = []
    report = run_entry(run_log, [facts(), facts(), facts()], {}, calls)
    assert calls == []
    assert report.dense_facts == ()


def test_entry_single_run_has_no_agreement_windows(run_log: RunLog) -> None:
    calls: list[Any] = []
    run_entry(run_log, [facts()], {}, calls)
    assert calls == []


def test_flagged_collision_note_only_for_confirmed_agreement_contacts(run_log: RunLog) -> None:
    report = run_entry(run_log, [facts(), facts(contact(9.0)), facts()], {9.0: facts(contact(9.5, "settled"))}, [])
    note = report.flagged_collision_note(report.facts.candidate_collisions[0])
    assert note == "Seen by run 2 of 3, confirmed by the close-up check."
    assert report.flagged_collision_note(contact(40.0)) is None
