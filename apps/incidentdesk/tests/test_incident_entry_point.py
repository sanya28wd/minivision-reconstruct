"""Seam 1 (incident entry point), exercised with the replay adapter at seam 2 and real PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import make_view, write_recording
from incident_report.ai import AIRequest, AIResponse, RecordingNotFound, ReplayAIConnection
from incident_report.domain import InvalidIncident, Origin, Role, RunStatus
from incident_report.entry import PROMPT_VERSION, analyse_incident
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.runlog import RunLog
from video_fixtures import synthetic_video


def _recorded(recordings_dir: Path, content: bytes, role: Role, summary: str) -> ReplayAIConnection:
    input_hash = incident_input_hash([(role, sha256_hex(content))])
    write_recording(recordings_dir, input_hash, PROMPT_VERSION, summary)
    return ReplayAIConnection(recordings_dir)


def test_one_view_gives_a_report_whose_summary_is_the_recorded_response(recordings_dir: Path, run_log: RunLog) -> None:
    ai = _recorded(recordings_dir, synthetic_video(b"clip-a"), Role.EXTERIOR, "Recorded summary for clip a.")

    report = analyse_incident(
        [make_view(synthetic_video(b"clip-a"), Role.EXTERIOR, label="front", acquisition_method="DVR export")],
        Origin.APP,
        ai=ai,
        run_log=run_log,
    )

    assert report.summary == "Recorded summary for clip a."
    assert [(v.role, v.label, v.acquisition_method, v.sha256) for v in report.views] == [
        (Role.EXTERIOR, "front", "DVR export", sha256_hex(synthetic_video(b"clip-a")))
    ]


def test_each_request_writes_exactly_one_run_row_holding_the_final_report(
    recordings_dir: Path, run_log: RunLog
) -> None:
    ai = _recorded(recordings_dir, synthetic_video(b"clip-b"), Role.EXTERIOR, "Summary b.")
    before = datetime.now(UTC)

    report = analyse_incident([make_view(synthetic_video(b"clip-b"))], Origin.APP, ai=ai, run_log=run_log)

    after = datetime.now(UTC)
    runs = run_log.list_runs()
    assert len(runs) == 1
    run = run_log.get(report.run_id)
    assert run == runs[0]
    assert run.origin is Origin.APP
    assert run.status is RunStatus.SUCCEEDED
    assert run.input_hash == report.input_hash
    assert run.report == report
    assert before <= run.created_at <= after
    assert run.total_latency_ms >= 0


def test_origin_is_recorded_for_app_benchmark_and_replay(recordings_dir: Path, run_log: RunLog) -> None:
    ai = _recorded(recordings_dir, synthetic_video(b"clip-c"), Role.EXTERIOR, "Summary c.")

    for origin in (Origin.APP, Origin.BENCHMARK, Origin.REPLAY):
        report = analyse_incident([make_view(synthetic_video(b"clip-c"))], origin, ai=ai, run_log=run_log)
        assert run_log.get(report.run_id).origin is origin

    assert len(run_log.list_runs()) == 3


def test_missing_recording_fails_plainly_and_logs_a_failed_run_with_no_report(
    recordings_dir: Path, run_log: RunLog
) -> None:
    ai = ReplayAIConnection(recordings_dir)  # empty directory: nothing recorded

    with pytest.raises(RecordingNotFound, match="Nothing was made up"):
        analyse_incident([make_view(synthetic_video(b"never-recorded"))], Origin.APP, ai=ai, run_log=run_log)

    (run,) = run_log.list_runs()
    assert run.status is RunStatus.FAILED
    assert run.report is None
    assert run.error is not None and "No recorded AI response" in run.error
    assert run.input_hash == incident_input_hash([(Role.EXTERIOR, sha256_hex(synthetic_video(b"never-recorded")))])


def test_recording_for_another_prompt_version_is_not_used(recordings_dir: Path, run_log: RunLog) -> None:
    input_hash = incident_input_hash([(Role.EXTERIOR, sha256_hex(synthetic_video(b"clip-d")))])
    write_recording(recordings_dir, input_hash, "some-other-version", "Wrong version summary.")

    with pytest.raises(RecordingNotFound):
        analyse_incident(
            [make_view(synthetic_video(b"clip-d"))], Origin.APP, ai=ReplayAIConnection(recordings_dir), run_log=run_log
        )


def test_the_same_footage_in_the_other_role_has_no_recording(recordings_dir: Path, run_log: RunLog) -> None:
    ai = _recorded(recordings_dir, synthetic_video(b"clip-e"), Role.EXTERIOR, "Exterior summary.")

    with pytest.raises(RecordingNotFound):
        analyse_incident([make_view(synthetic_video(b"clip-e"), Role.INTERIOR)], Origin.APP, ai=ai, run_log=run_log)


def test_label_and_acquisition_method_do_not_change_what_is_replayed(recordings_dir: Path, run_log: RunLog) -> None:
    ai = _recorded(recordings_dir, synthetic_video(b"clip-f"), Role.EXTERIOR, "Summary f.")

    report = analyse_incident(
        [
            make_view(
                synthetic_video(b"clip-f"), label="CCTV junction", acquisition_method="phone recording of a screen"
            )
        ],
        Origin.APP,
        ai=ai,
        run_log=run_log,
    )

    assert report.summary == "Summary f."


def test_several_views_replay_regardless_of_upload_order(recordings_dir: Path, run_log: RunLog) -> None:
    input_hash = incident_input_hash(
        [(Role.EXTERIOR, sha256_hex(synthetic_video(b"ext"))), (Role.INTERIOR, sha256_hex(synthetic_video(b"int")))]
    )
    write_recording(recordings_dir, input_hash, PROMPT_VERSION, "Two views.")
    ai = ReplayAIConnection(recordings_dir)

    forward = analyse_incident(
        [make_view(synthetic_video(b"ext"), Role.EXTERIOR), make_view(synthetic_video(b"int"), Role.INTERIOR)],
        Origin.REPLAY,
        ai=ai,
        run_log=run_log,
    )
    backward = analyse_incident(
        [make_view(synthetic_video(b"int"), Role.INTERIOR), make_view(synthetic_video(b"ext"), Role.EXTERIOR)],
        Origin.REPLAY,
        ai=ai,
        run_log=run_log,
    )

    assert forward.summary == backward.summary == "Two views."
    assert [v.role for v in forward.views] == [Role.EXTERIOR, Role.INTERIOR]


def test_an_incident_without_views_is_refused_and_writes_no_run(recordings_dir: Path, run_log: RunLog) -> None:
    with pytest.raises(InvalidIncident):
        analyse_incident([], Origin.APP, ai=ReplayAIConnection(recordings_dir), run_log=run_log)

    assert run_log.list_runs() == []


def test_an_unexpected_ai_failure_is_logged_as_failed_and_not_swallowed(run_log: RunLog) -> None:
    class Broken:
        def analyse(self, request: AIRequest) -> AIResponse:
            raise RuntimeError("secret internals should not be logged")

    with pytest.raises(RuntimeError):
        analyse_incident([make_view(synthetic_video(b"clip-g"))], Origin.APP, ai=Broken(), run_log=run_log)

    (run,) = run_log.list_runs()
    assert run.status is RunStatus.FAILED
    assert run.report is None
    assert run.error == "Unexpected error (RuntimeError)."
