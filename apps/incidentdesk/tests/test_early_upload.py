"""Early upload HTTP seam: no evidence before submit, owned opaque handles."""

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_client import authorized_client
from conftest import write_recording
from incident_report.ai import ReplayAIConnection
from incident_report.domain import Role
from incident_report.entry import PROMPT_VERSION
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.runlog import RunLog
from incident_report.storage import LocalEvidenceStore
from incident_report.web import create_app
from video_fixtures import synthetic_video


def test_stage_is_owned_and_not_evidence_until_submit(tmp_path: Path, recordings_dir: Path, run_log: RunLog) -> None:
    evidence = LocalEvidenceStore(tmp_path / "evidence")
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=evidence)
    client = authorized_client(app, run_log.database_url)
    other = authorized_client(app, run_log.database_url)
    content = synthetic_video(b"early-upload")
    digest = sha256_hex(content)
    write_recording(recordings_dir, incident_input_hash([(Role.EXTERIOR, digest)]), PROMPT_VERSION, "Staged summary")
    token = re.search(r'name="staging_csrf" value="([^"]+)"', client.get("/").text)
    assert token is not None
    response = client.post(
        "/uploads/stage", headers={"X-CSRF-Token": token[1]}, files={"video": ("front.mp4", content)}
    )
    assert response.status_code == 201
    stage_id = response.json()["staging_id"]
    assert len(stage_id) >= 43
    assert not evidence.exists(digest)
    assert run_log.list_runs() == []
    refused = other.post("/reports", data={"exterior_staging_ids": stage_id}, files={"video": ("", b"")})
    assert refused.status_code == 422
    assert not evidence.exists(digest)
    page = client.post(
        "/reports", data={"exterior_staging_ids": stage_id, "exterior_label": "front"}, files={"video": ("", b"")}
    )
    assert page.status_code == 200
    assert "Staged summary" in page.text
    assert evidence.exists(digest)
    (run,) = run_log.list_runs()
    (view,) = run_log.intake_views(run.run_id)
    assert view.sha256 == digest and view.working_copy is not None
    assert view.ffprobe_stdout and view.session_id
    assert view.label == "front"
    events = [e for e in run_log.audit.list_entries() if e.event_type == "upload" and e.run_id == run.run_id]
    assert len(events) == 1
    assert events[0].details["sha256"] == digest
    assert run_log.audit.verify().valid


def test_staging_enforces_auth_csrf_and_content_limits(tmp_path: Path, recordings_dir: Path, run_log: RunLog) -> None:
    from fastapi.testclient import TestClient

    app = create_app(
        ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=LocalEvidenceStore(tmp_path / "evidence")
    )
    anonymous = TestClient(app, base_url="https://testserver")
    assert anonymous.post("/uploads/stage", files={"video": ("x.mp4", b"x")}).status_code == 403
    client = authorized_client(app, run_log.database_url)
    assert client.post("/uploads/stage", files={"video": ("x.mp4", b"x")}).status_code == 403
    token = re.search(r'name="staging_csrf" value="([^"]+)"', client.get("/").text)
    assert token is not None
    headers = {"X-CSRF-Token": token[1]}
    assert (
        client.post(
            "/uploads/stage",
            headers={**headers, "Origin": "https://attacker.example"},
            files={"video": ("x.mp4", b"x")},
        ).status_code
        == 403
    )
    assert client.post("/uploads/stage", headers=headers, files={"video": ("x.mp4", b"invalid")}).status_code == 422
    assert (
        client.post(
            "/uploads/stage",
            headers=headers,
            files=[("video", ("x.mp4", synthetic_video(b"a"))), ("video", ("y.mp4", synthetic_video(b"b")))],
        ).status_code
        == 422
    )
    assert run_log.list_runs() == []


def test_stage_expiry_removes_bytes_and_refuses_submit(
    tmp_path: Path, recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile
    import time

    created = []
    original = tempfile.TemporaryFile

    def temporary(*args: Any, **kwargs: Any) -> Any:
        stream = original(*args, **kwargs)
        created.append(stream)
        return stream

    monkeypatch.setattr(tempfile, "TemporaryFile", temporary)
    monkeypatch.setenv("INCIDENT_STAGING_TTL_SECONDS", "0.05")
    app = create_app(
        ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=LocalEvidenceStore(tmp_path / "evidence")
    )
    client = authorized_client(app, run_log.database_url)
    token = re.search(r'name="staging_csrf" value="([^"]+)"', client.get("/").text)
    assert token is not None
    response = client.post(
        "/uploads/stage", headers={"X-CSRF-Token": token[1]}, files={"video": ("x.mp4", synthetic_video(b"expiry"))}
    )
    assert response.status_code == 201
    time.sleep(0.1)
    assert created and all(stream.closed for stream in created)
    page = client.post(
        "/reports", data={"exterior_staging_ids": response.json()["staging_id"]}, files={"video": ("", b"")}
    )
    assert page.status_code == 422
    assert run_log.list_runs() == []


def test_public_stage_prepares_provider_and_expiry_deletes_it(
    tmp_path: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    from threading import Event

    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo
    from incident_report.ai.google_video import GoogleVideoTransport
    from test_google_video_transport import FakeClient

    class NotifyingClient(FakeClient):
        vertexai = False

        def __init__(self) -> None:
            super().__init__()
            self.upload_event = Event()
            self.delete_event = Event()

        def upload(self, **kwargs: Any) -> SimpleNamespace:
            result = super().upload(**kwargs)
            self.upload_event.set()
            return result

        def delete(self, **kwargs: Any) -> None:
            super().delete(**kwargs)
            self.delete_event.set()

    sdk = NotifyingClient()
    content = synthetic_video(b"public-preupload")
    path = tmp_path / "declared.mp4"
    path.write_bytes(content)
    monkeypatch.setenv("INCIDENT_STAGING_TTL_SECONDS", "0.3")
    ai = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(path, True), transport=GoogleVideoTransport(sdk)
    )
    app = create_app(ai=ai, run_log=run_log, evidence=LocalEvidenceStore(tmp_path / "evidence"))
    client = authorized_client(app, run_log.database_url)
    token = re.search(r'name="staging_csrf" value="([^"]+)"', client.get("/").text)
    assert token is not None
    response = client.post("/uploads/stage", headers={"X-CSRF-Token": token[1]}, files={"video": ("x.mp4", content)})
    assert response.status_code == 201
    assert sdk.upload_event.wait(1)
    assert sdk.delete_event.wait(2)
    assert sdk.deleted == ["files/synthetic"]
    assert run_log.list_runs() == []


def test_upload_page_has_progressive_staging_script(tmp_path: Path, recordings_dir: Path, run_log: RunLog) -> None:
    app = create_app(
        ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=LocalEvidenceStore(tmp_path / "evidence")
    )
    client = authorized_client(app, run_log.database_url)
    page = client.get("/")
    assert "/static/early-upload.js" in page.text
    assert 'role="status"' in page.text
    script = client.get("/static/early-upload.js")
    assert script.status_code == 200
    assert "textContent" in script.text


def test_submit_transfers_ready_provider_lease_through_report_analysis(tmp_path: Path, run_log: RunLog) -> None:
    from threading import Event

    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo
    from incident_report.ai.google_video import GoogleVideoTransport
    from incident_report.ai.interface import AIRequest, AIResponse
    from test_google_video_transport import FakeClient

    class Client(FakeClient):
        vertexai = False

        def __init__(self) -> None:
            super().__init__()
            self.uploads = 0
            self.upload_event = Event()

        def upload(self, **kwargs: Any) -> SimpleNamespace:
            self.uploads += 1
            response = super().upload(**kwargs)
            self.upload_event.set()
            return response

    class ObservingAI(GeminiAIConnection):
        def analyse(self, request: AIRequest) -> AIResponse:
            self.transport.generate(self.video, "observe", self.settings)
            return AIResponse(summary="Prepared report", provenance="synthetic")

    content = synthetic_video(b"submit-prepared")
    path = tmp_path / "public.mp4"
    path.write_bytes(content)
    sdk = Client()
    ai = ObservingAI(
        GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(path, True), transport=GoogleVideoTransport(sdk)
    )
    client = authorized_client(
        create_app(ai=ai, run_log=run_log, evidence=LocalEvidenceStore(tmp_path / "evidence")), run_log.database_url
    )
    token = re.search(r'name="staging_csrf" value="([^"]+)"', client.get("/").text)
    assert token is not None
    stage = client.post("/uploads/stage", headers={"X-CSRF-Token": token[1]}, files={"video": ("public.mp4", content)})
    assert stage.status_code == 201
    assert sdk.upload_event.wait(2)
    response = client.post(
        "/reports", data={"exterior_staging_ids": stage.json()["staging_id"]}, files={"video": ("", b"")}
    )
    assert response.status_code == 200
    assert "Prepared report" in response.text
    assert sdk.uploads == 1
    assert "file_data" in sdk.generated["contents"][0]
    assert sdk.deleted == ["files/synthetic"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("INCIDENT_MAX_UPLOAD_BYTES", "10"),
        ("INCIDENT_MAX_TOTAL_UPLOAD_BYTES", "10"),
        ("INCIDENT_MAX_DURATION_SECONDS", "0.0001"),
    ],
)
def test_staging_obeys_configured_intake_caps(
    tmp_path: Path, recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    client = authorized_client(
        create_app(
            ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=LocalEvidenceStore(tmp_path / "evidence")
        ),
        run_log.database_url,
    )
    token = re.search(r'name="staging_csrf" value="([^"]+)"', client.get("/").text)
    assert token is not None
    response = client.post(
        "/uploads/stage", headers={"X-CSRF-Token": token[1]}, files={"video": ("x.mp4", synthetic_video(b"caps"))}
    )
    assert response.status_code == 422
    assert run_log.list_runs() == []
