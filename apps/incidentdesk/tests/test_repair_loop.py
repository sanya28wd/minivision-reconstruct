"""Tests for Seam 1 repair loop and invalid Report handling (RPT-11)."""

from __future__ import annotations

import io
from collections.abc import Sequence

from incident_report.ai import AIConnection, AIRequest, AIResponse
from incident_report.domain import Origin, Role, RunStatus, View
from incident_report.entry import analyse_incident
from incident_report.facts import CitedFact, OverviewFacts, PartyFact
from incident_report.hashing import sha256_hex
from incident_report.model_calls import ModelCall
from incident_report.runlog import RunLog
from video_fixtures import synthetic_video


class FakeRepairingAIConnection(AIConnection):
    """AI connection simulating initial invalid output and subsequent repair."""

    def __init__(self, initial_facts: OverviewFacts, repaired_facts: OverviewFacts | None = None) -> None:
        self.initial_facts = initial_facts
        self.repaired_facts = repaired_facts
        self.repair_called = 0
        self.repair_errors: list[Sequence[str]] = []

    def analyse(self, request: AIRequest) -> AIResponse:
        return AIResponse(
            summary="Initial analysis summary",
            provenance="mock-gemini",
            facts=self.initial_facts,
            calls=(
                ModelCall(
                    request_hash="11" * 32,
                    input_hash=request.input_hash,
                    prompt_version=request.prompt_version,
                    model="gemini-3.8-flash",
                    view_id=request.views[0].sha256,
                    fps=1.0,
                    raw_response='{"initial": true}',
                    latency_ms=100.0,
                    retry_index=0,
                ),
            ),
        )

    def repair(self, request: AIRequest, response: AIResponse, errors: Sequence[str]) -> AIResponse:
        self.repair_called += 1
        self.repair_errors.append(errors)
        call = ModelCall(
            request_hash="22" * 32,
            input_hash=request.input_hash,
            prompt_version=request.prompt_version + "-repair",
            model="gemini-3.8-flash",
            view_id=request.views[0].sha256,
            fps=1.0,
            raw_response='{"repair": true}',
            latency_ms=80.0,
            retry_index=0,
        )
        return AIResponse(
            summary="Repaired analysis summary",
            provenance="mock-gemini",
            facts=self.repaired_facts,
            calls=response.calls + (call,),
        )


def test_clean_output_requires_no_repair_and_records_clean_run(run_log: RunLog) -> None:
    view_bytes = synthetic_video(b"clean-video-footage")
    view_hash = sha256_hex(view_bytes)
    view = View(
        stream=io.BytesIO(view_bytes),
        filename="front.mp4",
        role=Role.EXTERIOR,
        duration_seconds=10.0,
    )

    valid_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(
                description="silver Toyota sedan",
                view_id=view_hash,
                timestamp_seconds=0.5,
                confidence=0.9,
            )
        ],
        events=[
            CitedFact(
                description="sedan approaches traffic signal",
                view_id=view_hash,
                timestamp_seconds=0.6,
                confidence=0.85,
            )
        ],
        candidate_collisions=[],
    )

    ai = FakeRepairingAIConnection(valid_facts)
    report = analyse_incident([view], Origin.APP, ai=ai, run_log=run_log)

    assert report.is_valid is True
    assert report.validation_errors == ()
    assert ai.repair_called == 0

    run = run_log.get(report.run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert run.repair_attempts == 0
    assert run.repair_outcome is None


def test_invalid_output_is_repaired_once_when_repair_succeeds(run_log: RunLog) -> None:
    view_bytes = synthetic_video(b"repairable-video-footage")
    view_hash = sha256_hex(view_bytes)
    view = View(
        stream=io.BytesIO(view_bytes),
        filename="front.mp4",
        role=Role.EXTERIOR,
        duration_seconds=10.0,
    )

    # Initial facts have out-of-range timestamp (15.0s > the 1.0 s real clip duration)
    bad_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(
                description="white Nissan Patrol SUV",
                view_id=view_hash,
                timestamp_seconds=0.5,
                confidence=0.9,
            )
        ],
        events=[
            CitedFact(
                description="SUV turns left",
                view_id=view_hash,
                timestamp_seconds=15.0,  # Out of range!
                confidence=0.8,
            )
        ],
        candidate_collisions=[],
    )

    # Repaired facts fix the timestamp to 4.5s
    good_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(
                description="white Nissan Patrol SUV",
                view_id=view_hash,
                timestamp_seconds=0.5,
                confidence=0.9,
            )
        ],
        events=[
            CitedFact(
                description="SUV turns left",
                view_id=view_hash,
                timestamp_seconds=0.9,
                confidence=0.9,
            )
        ],
        candidate_collisions=[],
    )

    ai = FakeRepairingAIConnection(bad_facts, good_facts)
    report = analyse_incident([view], Origin.APP, ai=ai, run_log=run_log)

    assert ai.repair_called == 1
    assert len(ai.repair_errors) == 1
    assert any("exceeds view duration" in err for err in ai.repair_errors[0])

    assert report.is_valid is True
    assert report.validation_errors == ()
    assert report.facts == good_facts

    run = run_log.get(report.run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert run.repair_attempts == 1
    assert run.repair_outcome == "repaired"
    assert len(run.calls) == 2


def test_failing_repair_marks_report_invalid_and_records_failure_in_run(run_log: RunLog) -> None:
    view_bytes = synthetic_video(b"stubborn-bad-footage")
    view = View(
        stream=io.BytesIO(view_bytes),
        filename="front.mp4",
        role=Role.EXTERIOR,
        duration_seconds=5.0,
    )

    # Both initial and repaired facts cite unknown view_id
    bad_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(
                description="red delivery motorcycle",
                view_id="completely_wrong_view_id",
                timestamp_seconds=1.0,
                confidence=0.9,
            )
        ],
        events=[],
        candidate_collisions=[],
    )

    ai = FakeRepairingAIConnection(bad_facts, bad_facts)  # repair also returns bad facts
    report = analyse_incident([view], Origin.APP, ai=ai, run_log=run_log)

    assert ai.repair_called == 1
    assert report.is_valid is False
    assert len(report.validation_errors) > 0
    assert any("completely_wrong_view_id" in err for err in report.validation_errors)

    run = run_log.get(report.run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert run.report == report
    assert run.repair_attempts == 1
    assert run.repair_outcome == "failed"
