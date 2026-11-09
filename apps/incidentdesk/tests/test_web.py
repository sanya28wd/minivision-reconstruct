"""The browser path: upload form -> seam 1 -> Report page, with real PostgreSQL and replay."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from access_client import authorized_client
from conftest import write_recording
from incident_report.ai import ReplayAIConnection
from incident_report.domain import Origin, Role, RunStatus
from incident_report.entry import PROMPT_VERSION
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.runlog import RunLog
from incident_report.samples import SAMPLE_VIEW_BYTES
from incident_report.web import create_app
from video_fixtures import synthetic_video


@pytest.fixture
def client(recordings_dir: Path, run_log: RunLog) -> TestClient:
    return authorized_client(create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log), run_log.database_url)


def _record(recordings_dir: Path, content: bytes, summary: str, role: Role = Role.EXTERIOR) -> None:
    write_recording(recordings_dir, incident_input_hash([(role, sha256_hex(content))]), PROMPT_VERSION, summary)


def _upload(client: TestClient, content: bytes, **form: str):
    return client.post(
        "/reports",
        data={"role": "exterior", **form},
        files={"video": ("front.mp4", content, "video/mp4")},
    )


def test_the_home_page_offers_an_upload_form_with_interior_and_exterior(client: TestClient) -> None:
    page = client.get("/")

    assert page.status_code == 200
    assert 'type="file"' in page.text
    assert 'value="exterior"' in page.text and 'value="interior"' in page.text
    assert 'enctype="multipart/form-data"' in page.text


def test_uploading_a_video_shows_a_report_whose_summary_is_the_recorded_response(
    client: TestClient, recordings_dir: Path, run_log: RunLog
) -> None:
    _record(recordings_dir, synthetic_video(b"upload-1"), "The recorded summary line.")

    page = _upload(client, synthetic_video(b"upload-1"), label="front", acquisition_method="DVR export")

    assert page.status_code == 200  # TestClient follows the redirect to the report page
    assert "The recorded summary line." in page.text
    assert sha256_hex(synthetic_video(b"upload-1")) in page.text
    assert "front.mp4" in page.text and "DVR export" in page.text and "Exterior" in page.text
    (run,) = run_log.list_runs()
    assert run.origin is Origin.APP
    assert run.status is RunStatus.SUCCEEDED
    assert str(run.run_id) in page.text


def test_the_shipped_sample_works_end_to_end_with_the_default_recording(run_log: RunLog) -> None:
    client = authorized_client(create_app(ai=ReplayAIConnection(), run_log=run_log), run_log.database_url)

    page = _upload(client, SAMPLE_VIEW_BYTES)

    assert "Synthetic placeholder summary" in page.text
    assert "synthetic" in page.text.lower()


def test_the_report_page_is_rendered_from_the_stored_report_and_is_reloadable(
    client: TestClient, recordings_dir: Path, run_log: RunLog
) -> None:
    _record(recordings_dir, synthetic_video(b"upload-2"), "Reload me.")
    first = client.post(
        "/reports",
        data={"role": "exterior"},
        files={"video": ("a.mp4", synthetic_video(b"upload-2"), "video/mp4")},
        follow_redirects=False,
    )

    assert first.status_code == 303
    again = client.get(first.headers["location"])
    assert "Reload me." in again.text
    assert len(run_log.list_runs()) == 1  # viewing again does not run again


def test_model_text_and_user_text_are_escaped(client: TestClient, recordings_dir: Path) -> None:
    _record(recordings_dir, synthetic_video(b"upload-3"), "<script>alert('x')</script> & \"quoted\"")

    page = client.post(
        "/reports",
        data={"role": "exterior", "label": "<img src=x onerror=alert(1)>"},
        files={"video": ("<b>evil.mp4", synthetic_video(b"upload-3"), "video/mp4")},
    )

    assert "<script>alert" not in page.text
    assert "&lt;script&gt;alert(" in page.text
    assert "<img src=x" not in page.text and "&lt;img src=x" in page.text
    assert "<b>evil" not in page.text and "&lt;b&gt;evil.mp4" in page.text


def test_a_video_with_no_recording_gets_a_plain_error_and_a_failed_run(client: TestClient, run_log: RunLog) -> None:
    page = _upload(client, synthetic_video(b"never-recorded"))

    assert page.status_code == 422
    assert "No recorded AI response" in page.text
    assert "Nothing was made up" in page.text
    (run,) = run_log.list_runs()
    assert run.status is RunStatus.FAILED and run.report is None


def test_no_file_chosen_is_refused_without_a_run(client: TestClient, run_log: RunLog) -> None:
    page = client.post("/reports", data={"role": "exterior"}, files={"video": ("", b"", "application/octet-stream")})

    assert page.status_code == 400
    assert "Choose a video file" in page.text
    assert run_log.list_runs() == []


def test_an_unknown_role_is_refused_without_a_run(client: TestClient, run_log: RunLog) -> None:
    page = client.post("/reports", data={"role": "rooftop"}, files={"video": ("a.mp4", b"x", "video/mp4")})

    assert page.status_code == 422
    assert run_log.list_runs() == []


def test_overlong_text_fields_are_refused_without_a_run(client: TestClient, run_log: RunLog) -> None:
    page = _upload(client, synthetic_video(b"x"), label="L" * 5000)

    assert page.status_code == 422
    assert run_log.list_runs() == []


def test_an_unknown_or_failed_report_is_not_found(client: TestClient) -> None:
    assert client.get(f"/reports/{uuid.uuid4()}").status_code == 404
    assert client.get("/reports/not-a-uuid").status_code == 422
