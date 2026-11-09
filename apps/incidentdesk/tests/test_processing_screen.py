"""Tests for the processing screen (RPT-21): live steps, leave & return, retry.

Covers:
1. Steps update live and elapsed time shown via SSE (/reports/{run_id}/events).
2. Leaving and returning shows correct status ("Processing") and current step.
3. Forced failures (including timeouts) show a plain message and Retry action; never a partial Report.
4. Retry re-runs the same inputs (same input hash) as a new Run, leaving failed run in log.
5. Per-stage latencies recorded in the Run (stage_latencies_ms).
6. Configurable per-stage timeout settings (project assumptions).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from access_client import authorized_client
from conftest import write_recording
from incident_report.ai import ReplayAIConnection
from incident_report.config import ProcessingSettings
from incident_report.domain import Origin, Report, ReportView, Role, Run, RunStatus, View
from incident_report.entry import PROMPT_VERSION, analyse_incident
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.runlog import RunLog
from incident_report.storage import LocalEvidenceStore
from incident_report.web import create_app
from test_ui_shell import Page
from video_fixtures import synthetic_video


@pytest.fixture
def client(recordings_dir: Path, run_log: RunLog, tmp_path: Path) -> TestClient:
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    return authorized_client(
        create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=evidence),
        run_log.database_url,
    )


def _record(recordings_dir: Path, content: bytes, summary: str, role: Role = Role.EXTERIOR) -> None:
    write_recording(recordings_dir, incident_input_hash([(role, sha256_hex(content))]), PROMPT_VERSION, summary)


def _upload(client: TestClient, content: bytes, **form: str) -> Response:
    res = client.post(
        "/reports",
        data={"role": "exterior", **form},
        files={"video": ("front.mp4", content, "video/mp4")},
    )
    return res  # type: ignore[no-any-return]


def test_processing_screen_renders_active_step_and_elapsed_time(client: TestClient, run_log: RunLog) -> None:
    run_id = uuid.uuid4()
    created = datetime.now(UTC) - timedelta(seconds=12)
    run_log.record(
        Run(
            run_id=run_id,
            origin=Origin.APP,
            created_at=created,
            input_hash="a" * 64,
            report=None,
            status=RunStatus.PROCESSING,
            total_latency_ms=0.0,
            current_step="overview",
        )
    )

    page = client.get(f"/reports/{run_id}")

    assert page.status_code == 200
    assert "Processing incident" in page.text
    assert "Status: <strong>Processing</strong>" in page.text
    assert "You may safely leave this page and return later." in page.text
    # Overview is active
    assert 'aria-current="step"' in page.text
    assert "step-item--active" in page.text
    assert "<strong>Overview</strong>" in page.text
    # Earlier steps are done
    assert "Uploading" in page.text
    assert "Fingerprinting" in page.text
    assert "step-item--done" in page.text
    # Later steps are pending
    assert "Collision check" in page.text
    assert "Rules" in page.text
    assert "Checks" in page.text
    assert "Done" in page.text
    assert "step-item--pending" in page.text
    # Meta refresh is inside noscript during processing, and external processing.js is included
    assert '<noscript><meta http-equiv="refresh" content="2"></noscript>' in page.text
    assert '<script src="/static/js/processing.js" defer></script>' in page.text
    # Elapsed time is displayed
    assert "Elapsed time:" in page.text
    assert "12" in page.text
    # Shell conformance
    parsed = Page(page.text)
    assert parsed.count("h1") == 1
    assert (parsed.count("header"), parsed.count("main"), parsed.count("footer")) == (1, 1, 1)


def test_leaving_and_returning_shows_updated_processing_step(client: TestClient, run_log: RunLog) -> None:
    run_id = uuid.uuid4()
    run_log.record(
        Run(
            run_id=run_id,
            origin=Origin.APP,
            created_at=datetime.now(UTC),
            input_hash="b" * 64,
            report=None,
            status=RunStatus.PROCESSING,
            total_latency_ms=0.0,
            current_step="uploading",
        )
    )

    # First view: uploading
    page1 = client.get(f"/reports/{run_id}")
    assert page1.status_code == 200
    assert "<strong>Uploading</strong>" in page1.text

    # Worker advances step to collision check
    run_log.update_step(run_id, "collision check")

    # Returning officer sees the new step
    page2 = client.get(f"/reports/{run_id}")
    assert page2.status_code == 200
    assert "<strong>Collision check</strong>" in page2.text
    assert "Status: <strong>Processing</strong>" in page2.text


def test_forced_failure_shows_plain_message_and_retry_with_no_partial_report(
    client: TestClient, run_log: RunLog
) -> None:
    run_id = uuid.uuid4()
    run_log.record(
        Run(
            run_id=run_id,
            origin=Origin.APP,
            created_at=datetime.now(UTC),
            input_hash="c" * 64,
            report=None,
            status=RunStatus.FAILED,
            total_latency_ms=150.0,
            error="Stage 'rules' timed out after 30.0s.",
        )
    )

    page = client.get(f"/reports/{run_id}")

    assert page.status_code == 422
    assert "Analysis failed" in page.text
    assert "Stage &#39;rules&#39; timed out after 30.0s." in page.text or "Stage 'rules' timed out" in page.text
    assert "The analysis could not be completed. No partial report has been produced." in page.text
    # Retry button is present
    assert f'action="/reports/{run_id}/retry"' in page.text
    assert "Retry analysis" in page.text
    # No meta refresh on failure
    assert '<meta http-equiv="refresh"' not in page.text
    # No partial report content
    assert "Fault determination" not in page.text
    assert "Vehicle 1" not in page.text
    # Shell conformance
    parsed = Page(page.text)
    assert parsed.count("h1") == 1


def test_retry_reruns_same_inputs_with_new_run_preserving_failed_run(
    client: TestClient, run_log: RunLog, recordings_dir: Path
) -> None:
    content = synthetic_video(b"retry-test-footage")

    # 1. Upload without recording -> analysis fails
    page1 = _upload(client, content, label="retry camera")
    assert page1.status_code == 422
    assert "No recorded AI response exists" in page1.text

    runs_before = run_log.list_runs()
    assert len(runs_before) == 1
    failed_run = runs_before[0]
    assert failed_run.status is RunStatus.FAILED
    assert failed_run.report is None

    # 2. Add recording for this input hash
    _record(recordings_dir, content, "Retry succeeded summary.")

    # 3. Post to retry endpoint
    retry_page = client.post(f"/reports/{failed_run.run_id}/retry", follow_redirects=True)
    assert retry_page.status_code == 200
    assert "Retry succeeded summary." in retry_page.text

    # 4. Check runs in log
    runs_after = run_log.list_runs()
    assert len(runs_after) == 2
    # Verify failed run is still present and unchanged
    old_run = run_log.get(failed_run.run_id)
    assert old_run.status is RunStatus.FAILED
    assert old_run.error is not None
    # Verify new run succeeded with identical input_hash
    new_runs = [r for r in runs_after if r.run_id != failed_run.run_id]
    assert len(new_runs) == 1
    successful_run = new_runs[0]
    assert successful_run.status is RunStatus.SUCCEEDED
    assert successful_run.input_hash == failed_run.input_hash
    assert successful_run.report is not None
    assert successful_run.report.summary == "Retry succeeded summary."


def test_sse_events_stream_step_progress_and_completion(
    client: TestClient, run_log: RunLog, recordings_dir: Path
) -> None:
    content = synthetic_video(b"sse-success")
    _record(recordings_dir, content, "SSE success summary.")

    upload_page = _upload(client, content)
    assert upload_page.status_code == 200

    (run,) = run_log.list_runs()
    assert run.status is RunStatus.SUCCEEDED

    response = client.get(f"/reports/{run.run_id}/events")
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert "event: done" in response.text
    data = json.loads(response.text.split("data: ")[1].strip())
    assert data["step"] == "done"
    assert data["status"] == "succeeded"
    assert data["redirect"] == f"/reports/{run.run_id}"


def test_sse_events_stream_for_failed_run(client: TestClient, run_log: RunLog) -> None:
    run_id = uuid.uuid4()
    run_log.record(
        Run(
            run_id=run_id,
            origin=Origin.APP,
            created_at=datetime.now(UTC),
            input_hash="e" * 64,
            report=None,
            status=RunStatus.FAILED,
            total_latency_ms=100.0,
            error="Simulated failure for SSE.",
        )
    )

    response = client.get(f"/reports/{run_id}/events")
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert "event: error" in response.text
    data = json.loads(response.text.split("data: ")[1].strip())
    assert data["step"] == "failed"
    assert data["status"] == "failed"
    assert data["error"] == "Simulated failure for SSE."


def test_stage_latencies_recorded_in_run(client: TestClient, run_log: RunLog, recordings_dir: Path) -> None:
    content = synthetic_video(b"latency-check")
    _record(recordings_dir, content, "Latency test summary.")

    page = _upload(client, content)
    assert page.status_code == 200

    (run,) = run_log.list_runs()
    assert run.status is RunStatus.SUCCEEDED
    assert run.report is not None

    latencies = run.report.stage_latencies_ms
    assert isinstance(latencies, dict)
    for expected_key in ("intake", "overview", "dense", "rules", "validation"):
        assert expected_key in latencies, f"Missing latency key: {expected_key}"
        assert latencies[expected_key] >= 0.0


def test_per_stage_timeout_configuration_and_enforcement(run_log: RunLog, recordings_dir: Path, tmp_path: Path) -> None:
    import io

    content = synthetic_video(b"timeout-test")
    _record(recordings_dir, content, "Timeout test summary.")

    evidence = LocalEvidenceStore(tmp_path / "evidence")
    view = View(stream=io.BytesIO(content), filename="timeout.mp4", role=Role.EXTERIOR)

    # Set a short timeout for the overview stage (project assumption setting)
    proc_settings = ProcessingSettings(overview_timeout_seconds=0.01)
    ai = ReplayAIConnection(recordings_dir)
    orig_analyse = ai.analyse

    def slow_analyse(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.05)
        return orig_analyse(*args, **kwargs)

    ai.analyse = slow_analyse  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="The overview stage timed out."):
        analyse_incident(
            (view,),
            Origin.APP,
            ai=ai,
            run_log=run_log,
            evidence=evidence,
            processing_settings=proc_settings,
        )

    (run,) = run_log.list_runs()
    assert run.status is RunStatus.FAILED
    assert run.report is None
    assert "The overview stage timed out." in (run.error or "")


def test_stage_timeout_fires_promptly_without_waiting_for_worker_thread(
    run_log: RunLog, recordings_dir: Path, tmp_path: Path
) -> None:
    import io

    content = synthetic_video(b"timeout-prompt-test")
    _record(recordings_dir, content, "Timeout prompt test summary.")

    evidence = LocalEvidenceStore(tmp_path / "evidence")
    view = View(stream=io.BytesIO(content), filename="timeout_prompt.mp4", role=Role.EXTERIOR)

    proc_settings = ProcessingSettings(overview_timeout_seconds=0.1)
    ai = ReplayAIConnection(recordings_dir)
    original_analyse = ai.analyse
    worker_threads: list[threading.Thread] = []

    def slow_analyse(*args: Any, **kwargs: Any) -> Any:
        worker_threads.append(threading.current_thread())
        time.sleep(3.0)
        return original_analyse(*args, **kwargs)

    ai.analyse = slow_analyse  # type: ignore[method-assign]

    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="The overview stage timed out."):
        analyse_incident(
            (view,),
            Origin.APP,
            ai=ai,
            run_log=run_log,
            evidence=evidence,
            processing_settings=proc_settings,
        )
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"Timeout took too long to fire: {elapsed:.2f}s (worker was not detached)"
    (run,) = run_log.list_runs()
    assert run.status is RunStatus.FAILED
    assert "The overview stage timed out." in (run.error or "")

    # Let the real entry-stage worker finish with a valid replay response.
    # The terminal Run must remain failed after the detached thread exits.
    assert len(worker_threads) == 1
    worker_threads[0].join(timeout=5.0)
    assert not worker_threads[0].is_alive()
    late_run = run_log.get(run.run_id)
    assert late_run.status is RunStatus.FAILED
    assert late_run.error == run.error
    assert late_run.report is None
    assert late_run.current_step is None


def test_late_worker_or_upsert_cannot_overwrite_terminal_run(run_log: RunLog) -> None:
    run_id = uuid.uuid4()
    created = datetime.now(UTC)
    run_log.record(
        Run(
            run_id=run_id,
            origin=Origin.APP,
            created_at=created,
            input_hash="d" * 64,
            report=None,
            status=RunStatus.FAILED,
            total_latency_ms=100.0,
            error="The overview stage timed out.",
        )
    )

    # Attempt to update step on a failed run
    run_log.update_step(run_id, "collision check")
    run = run_log.get(run_id)
    assert run.current_step is None
    assert run.status is RunStatus.FAILED

    # Attempt to upsert/record on a failed run
    run_log.record(
        run.model_copy(
            update={
                "status": RunStatus.PROCESSING,
                "current_step": "rules",
                "error": None,
            }
        )
    )
    run_after = run_log.get(run_id)
    assert run_after.status is RunStatus.FAILED
    assert run_after.error == "The overview stage timed out."
    assert run_after.current_step is None


@pytest.mark.parametrize(("agreement_runs", "age_seconds"), [(3, 210), (5, 230)])
def test_processing_run_inside_agreement_timeout_budget_is_not_stale(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch, agreement_runs: int, age_seconds: int
) -> None:
    monkeypatch.setenv("INCIDENT_AGREEMENT_RUNS", str(agreement_runs))
    log = RunLog(run_log.database_url)
    run = Run(
        run_id=uuid.uuid4(),
        origin=Origin.APP,
        created_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        input_hash="a" * 64,
        report=None,
        status=RunStatus.PROCESSING,
        total_latency_ms=0.0,
        current_step="agreement",
    )
    log.record(run)

    assert log.get(run.run_id).status is RunStatus.PROCESSING
    assert log.list_runs()[0].status is RunStatus.PROCESSING


def test_stale_processing_run_reads_as_failed_interrupted_with_retry(client: TestClient, run_log: RunLog) -> None:
    run_id = uuid.uuid4()
    old_created = datetime.now(UTC) - timedelta(seconds=run_log._processing_settings.max_processing_seconds + 30)
    run_log.record(
        Run(
            run_id=run_id,
            origin=Origin.APP,
            created_at=old_created,
            input_hash="f" * 64,
            report=None,
            status=RunStatus.PROCESSING,
            total_latency_ms=0.0,
            current_step="overview",
        )
    )

    # Accessing show_report marks it stale and renders failed with Retry
    page = client.get(f"/reports/{run_id}")
    assert page.status_code == 422
    assert "Analysis failed" in page.text
    assert "Analysis was interrupted." in page.text
    assert f'action="/reports/{run_id}/retry"' in page.text
    assert "Retry analysis" in page.text

    # run_log.list_runs() also marks stale runs as failed
    run_in_log = run_log.get(run_id)
    assert run_in_log.status is RunStatus.FAILED
    assert run_in_log.error == "Analysis was interrupted."
    assert run_in_log.current_step is None


def test_retry_guard_rejects_non_failed_runs_with_409(client: TestClient, run_log: RunLog) -> None:
    # 1. Processing run
    proc_id = uuid.uuid4()
    run_log.record(
        Run(
            run_id=proc_id,
            origin=Origin.APP,
            created_at=datetime.now(UTC),
            input_hash="1" * 64,
            report=None,
            status=RunStatus.PROCESSING,
            total_latency_ms=0.0,
            current_step="overview",
        )
    )
    res_proc = client.post(f"/reports/{proc_id}/retry")
    assert res_proc.status_code == 409
    assert "Only a failed report can be retried." in res_proc.text

    # 2. Succeeded run
    succ_id = uuid.uuid4()
    view = ReportView(
        filename="dummy.mp4",
        role=Role.EXTERIOR,
        label=None,
        acquisition_method=None,
        sha256="1" * 64,
    )
    succ_report = Report(
        run_id=succ_id,
        input_hash="1" * 64,
        prompt_version="overview-1",
        provenance="replay",
        summary="Succeeded report.",
        views=(view,),
    )
    run_log.record_intake_view(succ_id, 0, view)
    run_log.record(
        Run(
            run_id=succ_id,
            origin=Origin.APP,
            created_at=datetime.now(UTC),
            input_hash="1" * 64,
            report=succ_report,
            status=RunStatus.SUCCEEDED,
            total_latency_ms=10.0,
            current_step="done",
        )
    )
    res_succ = client.post(f"/reports/{succ_id}/retry")
    assert res_succ.status_code == 409
    assert "Only a failed report can be retried." in res_succ.text


def test_retry_double_click_redirects_to_existing_in_flight_run(client: TestClient, run_log: RunLog) -> None:
    failed_id = uuid.uuid4()
    input_hash = "2" * 64
    view = ReportView(
        filename="retry.mp4",
        role=Role.EXTERIOR,
        label=None,
        acquisition_method=None,
        sha256=input_hash,
    )
    run_log.record_intake_view(failed_id, 0, view)
    run_log.record(
        Run(
            run_id=failed_id,
            origin=Origin.APP,
            created_at=datetime.now(UTC),
            input_hash=input_hash,
            report=None,
            status=RunStatus.FAILED,
            total_latency_ms=50.0,
            error="Initial failure.",
        )
    )

    in_flight_id = uuid.uuid4()
    run_log.record(
        Run(
            run_id=in_flight_id,
            origin=Origin.APP,
            created_at=datetime.now(UTC),
            input_hash=input_hash,
            report=None,
            status=RunStatus.PROCESSING,
            total_latency_ms=0.0,
            current_step="overview",
        )
    )

    res = client.post(f"/reports/{failed_id}/retry", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == f"/reports/{in_flight_id}"

    runs = [r for r in run_log.list_runs() if r.input_hash == input_hash]
    assert len(runs) == 2


def test_sse_live_events_receive_emitted_step_with_elapsed_time(client: TestClient, run_log: RunLog) -> None:
    run_id = uuid.uuid4()
    created = datetime.now(UTC)
    run_log.record(
        Run(
            run_id=run_id,
            origin=Origin.APP,
            created_at=created,
            input_hash="3" * 64,
            report=None,
            status=RunStatus.PROCESSING,
            total_latency_ms=0.0,
            current_step="fingerprinting",
        )
    )

    broadcaster = cast(Any, client.app).state.broadcaster

    def emit_later() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if run_id in broadcaster._subscribers and broadcaster._subscribers[run_id]:
                break
            time.sleep(0.01)
        time.sleep(0.02)
        broadcaster.emit(
            run_id,
            "step",
            {
                "step": "overview",
                "status": "processing",
                "started_at": created.isoformat(),
                "elapsed_seconds": 1.5,
            },
        )
        time.sleep(0.02)
        broadcaster.emit(
            run_id,
            "done",
            {
                "step": "done",
                "status": "succeeded",
                "redirect": f"/reports/{run_id}",
                "started_at": created.isoformat(),
                "elapsed_seconds": 3.0,
            },
        )

    t = threading.Thread(target=emit_later)
    t.start()

    events: list[tuple[str, dict[str, Any]]] = []
    with client.stream("GET", f"/reports/{run_id}/events") as stream:
        event_name = "message"
        for line in stream.iter_lines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                raw_data = line.removeprefix("data: ")
                events.append((event_name, json.loads(raw_data)))
                if event_name == "done":
                    break

    t.join()

    assert any(ev == "step" and data.get("step") == "fingerprinting" for ev, data in events)
    overview_ev = next((data for ev, data in events if ev == "step" and data.get("step") == "overview"), None)
    assert overview_ev is not None
    assert overview_ev["started_at"] == created.isoformat()
    assert overview_ev["elapsed_seconds"] == 1.5
    done_ev = next((data for ev, data in events if ev == "done"), None)
    assert done_ev is not None
    assert done_ev["redirect"] == f"/reports/{run_id}"


def _submit_crashing_analysis(client: TestClient, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, retry: bool) -> Run:
    if retry:
        # A genuine failed upload supplies the stored intake needed by Retry.
        _upload(client, synthetic_video(b"initial-crash-retry"))
        (original,) = run_log.list_runs()
        assert original.status is RunStatus.FAILED

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("SECRET key=abc123")

    monkeypatch.setattr("incident_report.web.app.analyse_incident", crash)
    if retry:
        response = client.post(f"/reports/{original.run_id}/retry", follow_redirects=False)
        assert response.status_code == 303
        run_id = uuid.UUID(response.headers["location"].rsplit("/", 1)[1])
    else:
        response = client.post(
            "/reports",
            data={"role": "exterior"},
            files={"video": ("front.mp4", synthetic_video(b"crashing-job"), "video/mp4")},
            follow_redirects=False,
        )
        assert response.status_code == 303
        run_id = uuid.UUID(response.headers["location"].rsplit("/", 1)[1])
    return run_log.get(run_id)


@pytest.mark.parametrize("retry", [False, True], ids=["upload", "retry"])
def test_crashed_analysis_is_failed_and_sse_error_is_plain(
    client: TestClient, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, retry: bool
) -> None:
    run = _submit_crashing_analysis(client, run_log, monkeypatch, retry)
    assert run.status is RunStatus.FAILED
    assert run.error == "Analysis failed."
    assert run.report is None
    assert run.current_step is None

    response = client.get(f"/reports/{run.run_id}/events")
    assert response.status_code == 200
    assert "event: error" in response.text
    data = json.loads(response.text.split("data: ")[1].strip())
    assert data["error"] == "Analysis failed."
    assert data["status"] == "failed"
    assert "SECRET" not in response.text
    assert "abc123" not in response.text
    assert "RuntimeError" not in response.text
    page = client.get(f"/reports/{run.run_id}")
    assert page.status_code == 422
    assert "Analysis failed." in page.text
    assert "Retry analysis" in page.text


@pytest.mark.parametrize("retry", [False, True], ids=["upload", "retry"])
def test_crashed_analysis_logs_only_run_id_and_exception_type(
    client: TestClient, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, retry: bool
) -> None:
    run = _submit_crashing_analysis(client, run_log, monkeypatch, retry)
    records = [record for record in caplog.records if record.name == "incident_report.web.app"]
    assert records
    assert str(run.run_id) in records[-1].getMessage()
    assert "RuntimeError" in records[-1].getMessage()
    assert all(record.exc_info is None for record in records)
    assert "SECRET" not in caplog.text
    assert "abc123" not in caplog.text
    assert "Traceback" not in caplog.text


def test_mid_run_leave_and_return_with_blocking_spy(
    client: TestClient, run_log: RunLog, recordings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = synthetic_video(b"blocking-spy-footage")
    _record(recordings_dir, content, "Spy summary.")

    step_reached = threading.Event()
    resume_event = threading.Event()
    orig_analyse = analyse_incident

    def spy_analyse(*args: Any, **kwargs: Any) -> Any:
        on_step = kwargs.get("on_step")
        if on_step:
            on_step("collision check")
        run_id = kwargs.get("run_id")
        if run_id:
            run_log.update_step(run_id, "collision check")
        step_reached.set()
        assert resume_event.wait(timeout=5.0), "Timed out waiting for resume"
        return orig_analyse(*args, **kwargs)

    monkeypatch.setattr("incident_report.web.app.analyse_incident", spy_analyse)

    upload_res: list[Response] = []

    def upload_worker() -> None:
        upload_res.append(_upload(client, content))

    worker_thread = threading.Thread(target=upload_worker)
    worker_thread.start()

    assert step_reached.wait(timeout=5.0), "Worker did not reach spy in time"

    (in_flight_run,) = [r for r in run_log.list_runs() if r.status is RunStatus.PROCESSING]
    assert in_flight_run.current_step == "collision check"

    mid_page = client.get(f"/reports/{in_flight_run.run_id}")
    assert mid_page.status_code == 200
    assert "Status: <strong>Processing</strong>" in mid_page.text
    assert "<strong>Collision check</strong>" in mid_page.text

    resume_event.set()
    worker_thread.join(timeout=5.0)

    assert len(upload_res) == 1
    assert upload_res[0].status_code == 200
    assert "Spy summary." in upload_res[0].text
