"""Consistency abstention survives dense consolidation, agreement and storage."""

import pytest

from conftest import make_view
from incident_report.agreement import AgreementSettings
from incident_report.ai import AIRequest, AIResponse
from incident_report.ai.dense import CandidateWindow, DenseSettings
from incident_report.config import ProcessingSettings
from incident_report.domain import Origin, Report
from incident_report.entry import analyse_incident
from incident_report.facts import OverviewFacts
from incident_report.hashing import sha256_hex
from incident_report.model_calls import ModelCall
from incident_report.rules.models import FaultStatus
from incident_report.runlog import RunLog
from test_collision_pair_selection import A, B, collision, sf, signal_facts
from video_fixtures import synthetic_video


@pytest.mark.parametrize("n_runs", [1, 3])
@pytest.mark.parametrize("stage", ["overview", "dense"])
def test_report_retains_consistency_abstention(run_log: RunLog, n_runs: int, stage: str) -> None:
    video = synthetic_video(b"consistency")
    view_id = sha256_hex(video)
    initial = signal_facts((A, B), [collision(0.5)], [])
    data = initial.model_dump()
    for values in data.values():
        if isinstance(values, list):
            for record in values:
                record.update(view_id=view_id, timestamp_seconds=0.5)
    initial = OverviewFacts.model_validate(data)
    incompatible = sf(A, "roundabout_entry_state", "already_inside_circulating").model_copy(
        update={"view_id": view_id, "timestamp_seconds": 0.5}
    )
    contradictory = initial.model_copy(update={"structured_facts": [*initial.structured_facts, incompatible]})

    class TwoPass:
        def analyse(self, request: AIRequest) -> AIResponse:
            return AIResponse(
                summary="Synthetic collision",
                provenance="synthetic",
                facts=contradictory if stage == "overview" else initial,
                calls=(),
            )

        def analyse_window(
            self, request: AIRequest, window: CandidateWindow
        ) -> tuple[OverviewFacts, tuple[ModelCall, ...]]:
            return contradictory, ()

    report = analyse_incident(
        [make_view(video)],
        Origin.BENCHMARK,
        ai=TwoPass(),
        run_log=run_log,
        dense_settings=DenseSettings(model="gemini-3.8-flash"),
        agreement_settings=AgreementSettings(n_runs=n_runs),
        processing_settings=ProcessingSettings(concurrent_analysis=False),
    )
    assert report.is_valid
    assert report.consistency_issues
    assert {i.subject for i in report.consistency_issues} == {A}
    fault = next(p for p in report.parties if p.party_description == A)
    assert fault.status == FaultStatus.CANNOT_DETERMINE
    assert "inconsistent typed answers" in fault.reason
    assert incompatible.citation() in fault.cited_facts
    assert not fault.findings
    assert all(v.party_description != A for v in report.violations)
    assert run_log.get(report.run_id).report == Report.model_validate_json(report.model_dump_json())


@pytest.mark.parametrize(
    "contradictory_runs,expected_fines,n_runs",
    [((0,), 0, 1), ((0, 1, 2), 0, 3), ((1,), 1, 3), ((1, 2), 0, 3), ((0,), 0, 3)],
)
@pytest.mark.parametrize("alias_in_clean_runs", [False, True])
def test_report_guards_standalone_fines_and_retains_every_overview_issue(
    run_log: RunLog, contradictory_runs: tuple[int, ...], expected_fines: int, n_runs: int, alias_in_clean_runs: bool
) -> None:
    video = synthetic_video(b"consistency-fines-r2")
    view_id = sha256_hex(video)
    data = signal_facts((A, B), [], []).model_dump()
    for values in data.values():
        if isinstance(values, list):
            for record in values:
                record.update(view_id=view_id, timestamp_seconds=0.5)
    data["parties"][0].update(color="white", body_type="SUV")
    clean = OverviewFacts.model_validate(data)
    aliased_data = clean.model_dump()
    aliased_data["parties"][0]["description"] = "white sport utility vehicle"
    for record in aliased_data["structured_facts"]:
        if record["subject"] == A:
            record["subject"] = "white sport utility vehicle"
        if record["other_party"] == A:
            record["other_party"] = "white sport utility vehicle"
    aliased = OverviewFacts.model_validate(aliased_data)
    contradictory = clean.model_copy(
        update={
            "structured_facts": [
                r.model_copy(update={"value": "unsignalised_junction"}) if r.field == "conflict_zone_type" else r
                for r in clean.structured_facts
            ]
        }
    )

    class SequenceAI:
        count = 0

        def analyse(self, request: AIRequest) -> AIResponse:
            facts = (
                contradictory
                if self.count in contradictory_runs
                else aliased
                if alias_in_clean_runs and self.count
                else clean
            )
            self.count += 1
            return AIResponse(summary="Synthetic signal crossing", provenance="synthetic", facts=facts, calls=())

    report = analyse_incident(
        [make_view(video)],
        Origin.BENCHMARK,
        ai=SequenceAI(),
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=n_runs),
        processing_settings=ProcessingSettings(concurrent_analysis=False),
    )
    assert report.is_valid
    assert len(report.violations) == expected_fines
    assert len(report.overview_consistency_issues) == n_runs
    assert {i for i, issues in enumerate(report.overview_consistency_issues) if issues} == set(contradictory_runs)
    assert run_log.get(report.run_id).report == Report.model_validate_json(report.model_dump_json())
    from incident_report.web.templating import build_environment

    html = build_environment().get_template("report.html").render(report=report)
    assert "Dependent findings and fines are withheld" in html
    assert "Overview analysis" in html


@pytest.mark.parametrize("contradictory_run", [1, 2])
def test_missing_primary_keeps_three_run_issues_and_votes_aligned(run_log: RunLog, contradictory_run: int) -> None:
    video = synthetic_video(b"missing-primary-consistency-r3")
    view_id = sha256_hex(video)
    data = signal_facts((A, B), [], []).model_dump()
    for values in data.values():
        if isinstance(values, list):
            for record in values:
                record.update(view_id=view_id, timestamp_seconds=0.5)
    clean = OverviewFacts.model_validate(data)
    contradictory = clean.model_copy(
        update={
            "structured_facts": [
                r.model_copy(update={"value": "unsignalised_junction"}) if r.field == "conflict_zone_type" else r
                for r in clean.structured_facts
            ]
        }
    )

    class SequenceAI:
        count = 0

        def analyse(self, request: AIRequest) -> AIResponse:
            index = self.count
            self.count += 1
            return AIResponse(
                summary="Synthetic missing primary",
                provenance="synthetic",
                facts=None if index == 0 else contradictory if index == contradictory_run else clean,
                calls=(),
            )

    report = analyse_incident(
        [make_view(video)],
        Origin.BENCHMARK,
        ai=SequenceAI(),
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=3),
        processing_settings=ProcessingSettings(concurrent_analysis=False),
    )
    assert len(report.overview_consistency_issues) == 3
    assert {i for i, issues in enumerate(report.overview_consistency_issues) if issues} == {contradictory_run}
    assert report.agreement is not None
    assert report.agreement.n_runs == 3
    votes = [
        value for key, value in report.agreement.fields.items() if key.startswith("violation:party:desc:white suv:")
    ]
    assert votes
    # A missing primary is "__absent__" (not a vote); an issue-blocked run is an explicit unknown (None).
    expected = ["__absent__", None, True] if contradictory_run == 1 else ["__absent__", True, None]
    assert all(vote.values == expected and vote.runs_total == 3 for vote in votes)
    assert not report.violations
    assert run_log.get(report.run_id).report == Report.model_validate_json(report.model_dump_json())
