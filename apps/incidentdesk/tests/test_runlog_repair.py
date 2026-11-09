"""Tests for repair tracking on Run and RunLog persistence (RPT-11)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from incident_report.domain import Origin, Report, ReportView, Role, Run, RunStatus
from incident_report.facts import OverviewFacts, PartyFact
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.runlog import RunLog


def test_run_model_holds_repair_attempts_and_outcome() -> None:
    run_id = uuid.uuid4()
    view = ReportView(
        filename="front.mp4",
        role=Role.EXTERIOR,
        label=None,
        acquisition_method=None,
        sha256="aa" * 32,
    )
    report = Report(
        run_id=run_id,
        input_hash="11" * 32,
        prompt_version="overview-1",
        provenance="gemini-api",
        summary="A test summary",
        views=(view,),
        facts=OverviewFacts(
            behaviour_observations=[],
            location_conditions=[],
            parties=[
                PartyFact(
                    description="silver Toyota sedan",
                    view_id="aa" * 32,
                    timestamp_seconds=1.0,
                    confidence=0.9,
                )
            ],
            events=[],
            candidate_collisions=[],
        ),
        is_valid=True,
    )

    run = Run(
        run_id=run_id,
        origin=Origin.APP,
        created_at=datetime.now(UTC),
        input_hash="11" * 32,
        report=report,
        status=RunStatus.SUCCEEDED,
        total_latency_ms=120.0,
        repair_attempts=1,
        repair_outcome="repaired",
    )

    assert run.repair_attempts == 1
    assert run.repair_outcome == "repaired"


def test_runlog_records_and_retrieves_repair_tracking_data(run_log: RunLog) -> None:
    run_id = uuid.uuid4()
    view = ReportView(
        filename="view.mp4",
        role=Role.EXTERIOR,
        label="front",
        acquisition_method="police",
        sha256=sha256_hex(b"footage"),
    )
    input_hash = incident_input_hash([(Role.EXTERIOR, view.sha256)])
    report = Report(
        run_id=run_id,
        input_hash=input_hash,
        prompt_version="overview-1",
        provenance="gemini-api",
        summary="Validated report after repair.",
        views=(view,),
        is_valid=True,
    )
    run = Run(
        run_id=run_id,
        origin=Origin.APP,
        created_at=datetime.now(UTC),
        input_hash=input_hash,
        report=report,
        status=RunStatus.SUCCEEDED,
        total_latency_ms=250.0,
        repair_attempts=1,
        repair_outcome="repaired",
    )

    run_log.record(run)

    fetched = run_log.get(run_id)
    assert fetched.repair_attempts == 1
    assert fetched.repair_outcome == "repaired"
    assert fetched.report == report

    all_runs = run_log.list_runs()
    matching = [r for r in all_runs if r.run_id == run_id]
    assert len(matching) == 1
    assert matching[0].repair_attempts == 1
    assert matching[0].repair_outcome == "repaired"
