"""PERF-01 entry seam, synthetic/replay responses only."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Barrier, Lock
from typing import Any

import pytest

from conftest import write_recording
from incident_report.agreement import AgreementSettings
from incident_report.ai import AIResponse, ReplayAIConnection
from incident_report.config import ProcessingSettings
from incident_report.domain import Origin, ReportView, Role
from incident_report.entry import analyse_incident
from incident_report.facts import OverviewFacts
from incident_report.hashing import incident_input_hash
from incident_report.runlog import RunLog


def view() -> ReportView:
    return ReportView(
        role=Role.EXTERIOR,
        sha256="a" * 64,
        filename="public.mp4",
        label=None,
        acquisition_method=None,
        duration_seconds=10,
    )


def test_replay_sequential_and_concurrent_reports_match(recordings_dir: Path, run_log: RunLog) -> None:
    v = view()
    write_recording(recordings_dir, incident_input_hash([(v.role, v.sha256)]), "skeleton-1", "Recorded")
    reports = [
        analyse_incident(
            [v],
            Origin.REPLAY,
            ai=ReplayAIConnection(recordings_dir),
            run_log=run_log,
            agreement_settings=AgreementSettings(n_runs=3),
            processing_settings=ProcessingSettings(concurrent_analysis=enabled),
        )
        for enabled in (False, True)
    ]

    def content(report: Any) -> Any:
        data = report.model_dump(mode="json", exclude={"run_id", "created_at", "stage_latencies_ms"})
        data["agreement"].pop("total_latency_ms")
        return data

    assert content(reports[0]) == content(reports[1])
    for report in reports:
        assert all(f"overview.run.{i}.total" in report.stage_latencies_ms for i in range(3))
        assert report.stage_latencies_ms["checks"] >= 0


def test_overviews_overlap_and_keep_primary_under_shuffled_finishes(run_log: RunLog) -> None:
    barrier = Barrier(3)
    lock = Lock()
    counter = 0
    finished = []

    class AI:
        def analyse(self, request: Any) -> AIResponse:
            nonlocal counter
            with lock:
                index = counter
                counter += 1
            barrier.wait(timeout=3)
            time.sleep((2 - index) * 0.03)
            finished.append(index)
            return AIResponse(
                f"run {index}",
                "synthetic",
                OverviewFacts(
                    behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=[]
                ),
            )

    report = analyse_incident(
        [view()], Origin.REPLAY, ai=AI(), run_log=run_log, agreement_settings=AgreementSettings(n_runs=3)
    )
    assert finished == [2, 1, 0]
    assert report.summary == "run 0"


def test_one_overview_timeout_returns_without_waiting_for_slow_workers(run_log: RunLog) -> None:
    from threading import Event

    release = Event()

    class AI:
        def analyse(self, request: Any) -> AIResponse:
            from incident_report.timings import capture_timings, stage_timer, timed_call

            # The live benchmark transport nests an attempt collector in the run collector.
            with capture_timings(propagate=True):
                timed_call("upload", lambda: None)
                with stage_timer("generate"):
                    release.wait(3)
            return AIResponse("late", "synthetic")

    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="overview"):
            analyse_incident(
                [view()],
                Origin.REPLAY,
                ai=AI(),
                run_log=run_log,
                agreement_settings=AgreementSettings(n_runs=3),
                processing_settings=ProcessingSettings(overview_timeout_seconds=0.05),
            )
        assert time.monotonic() - started < 2
        stages = run_log.list_runs()[0].stage_latencies_ms
        assert stages["overview.run.0.total"] >= 40
        assert stages["overview.run.0.incomplete"] == 1
        assert stages["overview.run.0.upload"] > 0
        assert stages["overview.run.0.generate"] >= 40
    finally:
        release.set()


def test_dense_windows_overlap_but_consolidate_in_window_order(run_log: RunLog) -> None:
    from incident_report.ai.dense import DenseSettings
    from incident_report.facts import CitedFact

    v = view()
    barrier = Barrier(2)
    finished = []
    facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[],
        events=[],
        candidate_collisions=[
            CitedFact(description=f"contact {t}", view_id=v.sha256, timestamp_seconds=t, confidence=1)
            for t in (2.0, 7.0)
        ],
    )

    class AI:
        def analyse(self, request: Any) -> AIResponse:
            return AIResponse("recorded", "synthetic", facts)

        def analyse_window(self, request: Any, window: Any) -> Any:
            barrier.wait(timeout=3)
            if window.candidate_timestamp == 2:
                time.sleep(0.05)
            finished.append(window.candidate_timestamp)
            return OverviewFacts(
                behaviour_observations=[],
                location_conditions=[],
                parties=[],
                events=[],
                candidate_collisions=[
                    CitedFact(
                        description=f"settled {window.candidate_timestamp}",
                        view_id=v.sha256,
                        timestamp_seconds=window.candidate_timestamp,
                        confidence=1,
                    )
                ],
            ), ()

    report = analyse_incident(
        [v],
        Origin.REPLAY,
        ai=AI(),
        run_log=run_log,
        agreement_settings=AgreementSettings(n_runs=1),
        dense_settings=DenseSettings(model="gemini-3.8-flash"),
    )
    assert finished == [7.0, 2.0]
    assert [f.candidate_collisions[0].timestamp_seconds for f in report.dense_facts] == [2.0, 7.0]
    assert all(f"dense.window.{i}.total" in report.stage_latencies_ms for i in range(2))


def test_concurrent_dense_windows_each_get_overview_parties_and_match_sequential(run_log: RunLog) -> None:
    from incident_report.ai.dense import DenseSettings
    from incident_report.facts import CitedFact, PartyFact

    v = view()
    seen: dict[bool, list[tuple[float, tuple[str, ...]]]] = {False: [], True: []}
    facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(description=d, view_id=v.sha256, timestamp_seconds=1.0, confidence=1)
            for d in ("white sedan", "grey hatchback")
        ],
        events=[],
        candidate_collisions=[
            CitedFact(description=f"contact {t}", view_id=v.sha256, timestamp_seconds=t, confidence=1)
            for t in (2.0, 7.0)
        ],
    )

    def make_ai(enabled: bool) -> Any:
        class AI:
            def analyse(self, request: Any) -> AIResponse:
                return AIResponse("recorded", "synthetic", facts)

            def analyse_window(self, request: Any, window: Any) -> Any:
                seen[enabled].append((window.candidate_timestamp, tuple(window.overview_party_descriptions)))
                return OverviewFacts(
                    behaviour_observations=[],
                    location_conditions=[],
                    parties=[],
                    events=[],
                    candidate_collisions=[
                        CitedFact(
                            description=f"settled {window.candidate_timestamp}",
                            view_id=v.sha256,
                            timestamp_seconds=window.candidate_timestamp,
                            confidence=1,
                        )
                    ],
                ), ()

        return AI()

    reports = [
        analyse_incident(
            [v],
            Origin.REPLAY,
            ai=make_ai(enabled),
            run_log=run_log,
            agreement_settings=AgreementSettings(n_runs=1),
            dense_settings=DenseSettings(model="gemini-3.8-flash"),
            processing_settings=ProcessingSettings(concurrent_analysis=enabled),
        )
        for enabled in (False, True)
    ]
    expected = ("white sedan", "grey hatchback")
    for enabled in (False, True):
        assert sorted(seen[enabled]) == [(2.0, expected), (7.0, expected)]

    def content(report: Any) -> Any:
        data = report.model_dump(mode="json", exclude={"run_id", "created_at", "stage_latencies_ms"})
        data["agreement"].pop("total_latency_ms")
        return data

    assert content(reports[0]) == content(reports[1])


def test_failed_run_keeps_completed_stage_timings(run_log: RunLog) -> None:
    class Broken:
        def analyse(self, request: Any) -> AIResponse:
            raise RuntimeError("synthetic")

    with pytest.raises(RuntimeError, match="synthetic"):
        analyse_incident(
            [view()], Origin.REPLAY, ai=Broken(), run_log=run_log, agreement_settings=AgreementSettings(n_runs=1)
        )
    run = run_log.list_runs()[0]
    assert run.report is None
    assert run.stage_latencies_ms["overview.run.0.total"] >= 0
    assert run.stage_latencies_ms["overviews_wall"] >= 0


def test_entry_shares_upload_and_records_generation_parse_and_backoff_timings(tmp_path: Path, run_log: RunLog) -> None:
    import json
    from types import SimpleNamespace

    from incident_report.ai.dense import DensePassAIConnection, DenseSettings
    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo
    from incident_report.ai.google_video import GoogleVideoTransport
    from incident_report.hashing import sha256_hex
    from test_google_video_transport import FakeClient

    path = tmp_path / "public.mp4"
    path.write_bytes(b"public research; synthetic SDK")
    v = view().model_copy(update={"sha256": sha256_hex(path.read_bytes())})
    uploads = []
    generations = []
    client = FakeClient()
    original = client.upload

    def upload(**kwargs: Any) -> Any:
        uploads.append(True)
        return original(**kwargs)

    def generate(**kwargs: Any) -> Any:
        generations.append(kwargs)
        raw = json.dumps(
            {
                "parties": [],
                "events": [],
                "candidate_collisions": [{"description": "contact", "timestamp_seconds": 2, "confidence": 1}],
            }
        )
        return SimpleNamespace(model_dump_json=lambda **kwargs: raw)

    client.upload = upload  # type: ignore[method-assign]
    client.generate_content = generate  # type: ignore[method-assign]
    transport = GoogleVideoTransport(client, inline_enabled=False)
    video = PublicResearchVideo(path, True)
    settings = GeminiSettings(model="gemini-3.8-flash")
    dense_settings = DenseSettings(model=settings.model)
    report = analyse_incident(
        [v],
        Origin.REPLAY,
        ai=GeminiAIConnection(settings, video, transport=transport),
        dense_ai=DensePassAIConnection(dense_settings, video, transport=transport),
        dense_settings=dense_settings,
        run_log=run_log,
        prompt_version="overview-1",
        agreement_settings=AgreementSettings(n_runs=3),
    )
    assert uploads == [True]
    assert client.deleted == ["files/synthetic"]
    assert len(generations) == 4
    stages = report.stage_latencies_ms
    assert sum(stages[f"overview.run.{i}.upload"] > 0 for i in range(3)) == 1
    assert all(stages[f"overview.run.{i}.generate"] > 0 for i in range(3))
    assert all(stages[f"overview.run.{i}.parse"] > 0 for i in range(3))
    assert all(stages[f"overview.run.{i}.retry_backoff"] == 0 for i in range(3))
    assert stages["dense.window.0.generate"] > 0
    assert stages["dense.window.0.parse"] > 0
    assert stages["rules"] >= 0
    assert run_log.get(report.run_id).stage_latencies_ms == stages
    calls = run_log.get(report.run_id).calls
    assert len(calls) == 4
    assert len({c.request_hash for c in calls[:3]}) == 1
