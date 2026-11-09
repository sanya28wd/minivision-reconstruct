"""Web integration tests for Feedback button, form, validation, and storage (RPT-43, spec 001)."""

from __future__ import annotations

import dataclasses
import io
import json
import re
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient

from access_client import authorized_client
from conftest import write_recording
from incident_report.ai import AIConnection, AIRequest, AIResponse, ReplayAIConnection
from incident_report.domain import Role
from incident_report.entry import PROMPT_VERSION
from incident_report.feedback import (
    MAX_FEEDBACK_BODY_SIZE_BYTES,
    MAX_SCREENSHOT_SIZE_BYTES,
    FeedbackCategory,
    FeedbackStore,
)
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.runlog import RunLog
from incident_report.storage import create_evidence_store
from incident_report.web import create_app
from video_fixtures import synthetic_video


def _extract_csrf(page_text: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page_text)
    assert match is not None
    return match.group(1)


@pytest.fixture(autouse=True)
def clean_feedback_table(run_log: RunLog) -> None:
    with psycopg.connect(run_log.database_url) as conn:
        conn.execute("DELETE FROM feedback")


@pytest.fixture
def client(recordings_dir: Path, run_log: RunLog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    monkeypatch.setenv("INCIDENT_EVIDENCE_DIR", str(evidence_dir))
    from incident_report.config import Settings

    settings = Settings.from_env({"DATABASE_URL": run_log.database_url})
    store = create_evidence_store(settings)
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=store)
    return authorized_client(app, run_log.database_url)


def _record(recordings_dir: Path, content: bytes, summary: str, role: Role = Role.EXTERIOR) -> None:
    write_recording(recordings_dir, incident_input_hash([(role, sha256_hex(content))]), PROMPT_VERSION, summary)


def _record_with_collision(recordings_dir: Path, content: bytes, summary: str, role: Role = Role.EXTERIOR) -> None:
    input_hash = incident_input_hash([(role, sha256_hex(content))])
    path = recordings_dir / f"{input_hash}.{PROMPT_VERSION}.json"
    path.write_text(
        json.dumps(
            {
                "provenance": "synthetic",
                "note": "Fixture with candidate collision.",
                "input_hash": input_hash,
                "prompt_version": PROMPT_VERSION,
                "response": {
                    "summary": summary,
                    "facts": {
                        "parties": [],
                        "events": [],
                        "candidate_collisions": [
                            {
                                "description": "silver Toyota strikes white Nissan",
                                "timestamp_seconds": 0.5,
                                "confidence": 0.95,
                            }
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def test_feedback_button_present_on_every_screen(client: TestClient, recordings_dir: Path) -> None:
    # 1. On upload / home page
    home_page = client.get("/")
    assert home_page.status_code == 200
    assert 'href="/feedback"' in home_page.text

    # 2. On report page
    _record(recordings_dir, synthetic_video(b"feedback-rep-1"), "Report for feedback.")
    rep_resp = client.post(
        "/reports",
        data={"role": "exterior"},
        files={"video": ("front.mp4", synthetic_video(b"feedback-rep-1"), "video/mp4")},
    )
    assert rep_resp.status_code == 200
    assert 'href="/feedback"' in rep_resp.text
    # "Feedback on this report" button in workspace auto-attaching run and version
    assert "Feedback on this report" in rep_resp.text
    assert "/feedback?run_id=" in rep_resp.text


def test_get_feedback_form_renders_csrf_and_fields(client: TestClient) -> None:
    page = client.get("/feedback?screen=/&run_id=&incident_id=&report_version=")
    assert page.status_code == 200
    assert 'action="/feedback"' in page.text
    assert 'name="csrf_token"' in page.text
    assert 'name="text"' in page.text
    assert 'name="category"' in page.text
    assert 'name="screenshot"' in page.text
    assert 'name="name"' in page.text
    assert "wrong result" in page.text
    assert "confusing" in page.text


def test_submitting_feedback_text_only_succeeds(client: TestClient, run_log: RunLog) -> None:
    # Get form to obtain CSRF token
    form_page = client.get("/feedback?screen=/")
    assert form_page.status_code == 200

    csrf_token = _extract_csrf(form_page.text)

    response = client.post(
        "/feedback",
        data={
            "csrf_token": csrf_token,
            "text": "The timeline of events is clear and easy to follow.",
            "category": "other",
            "name": "Traffic Investigator",
            "screen": "/",
        },
        headers={"User-Agent": "CustomBrowser/1.0"},
    )
    assert response.status_code == 200
    assert "Thank you" in response.text or "received" in response.text.lower()
    assert "The timeline of events is clear" in response.text

    # Verify stored in database
    store = FeedbackStore(run_log.database_url)
    all_fb = store.list_feedback()
    assert len(all_fb) == 1
    fb = all_fb[0]
    assert fb.text == "The timeline of events is clear and easy to follow."
    assert fb.category == FeedbackCategory.OTHER
    assert fb.name == "Traffic Investigator"
    assert fb.screen == "/"
    assert fb.browser is not None and "CustomBrowser" in fb.browser
    assert fb.session_id is not None
    assert fb.link_id is not None
    assert fb.screenshot_sha256 is None


def test_submitting_feedback_on_report_auto_attaches_incident_and_run(
    client: TestClient, run_log: RunLog, recordings_dir: Path
) -> None:
    _record(recordings_dir, synthetic_video(b"auto-attach-1"), "Report for auto attach.")
    rep_resp = client.post(
        "/reports",
        data={"role": "exterior"},
        files={"video": ("front.mp4", synthetic_video(b"auto-attach-1"), "video/mp4")},
    )
    assert rep_resp.status_code == 200
    (run,) = run_log.list_runs()

    # Get feedback page for this report
    form_page = client.get(
        f"/feedback?run_id={run.run_id}&incident_id={run.input_hash}&report_version=overview-1&screen=/reports/{run.run_id}"
    )
    assert form_page.status_code == 200
    assert str(run.run_id) in form_page.text

    csrf_token = _extract_csrf(form_page.text)

    post_resp = client.post(
        "/feedback",
        data={
            "csrf_token": csrf_token,
            "text": "Incorrect party categorization in report.",
            "category": "wrong result",
            "run_id": str(run.run_id),
            "incident_id": run.input_hash,
            "report_version": "overview-1",
            "screen": f"/reports/{run.run_id}",
        },
    )
    assert post_resp.status_code == 200

    store = FeedbackStore(run_log.database_url)
    all_fb = store.list_feedback()
    assert len(all_fb) == 1
    fb = all_fb[0]
    assert fb.run_id == run.run_id
    assert fb.incident_id == run.input_hash
    assert fb.report_version == (run.report.prompt_version if run.report else None)
    assert fb.category == FeedbackCategory.WRONG_RESULT


def test_empty_feedback_text_is_rejected(client: TestClient) -> None:
    form_page = client.get("/feedback")
    csrf_token = _extract_csrf(form_page.text)

    response = client.post(
        "/feedback",
        data={"csrf_token": csrf_token, "text": "   "},
    )
    assert response.status_code == 422
    assert "required" in response.text.lower()


def test_missing_or_invalid_csrf_token_is_rejected(client: TestClient) -> None:
    # 1. Missing CSRF token
    resp1 = client.post(
        "/feedback",
        data={"text": "Valid text."},
    )
    assert resp1.status_code == 403

    # 2. Invalid CSRF token
    resp2 = client.post(
        "/feedback",
        data={"csrf_token": "wrong_csrf_token_value", "text": "Valid text."},
    )
    assert resp2.status_code == 403


def test_cross_origin_submission_is_rejected(client: TestClient) -> None:
    form_page = client.get("/feedback")
    csrf_token = _extract_csrf(form_page.text)

    response = client.post(
        "/feedback",
        data={"csrf_token": csrf_token, "text": "Valid text."},
        headers={"Origin": "https://malicious-site.example.com"},
    )
    assert response.status_code == 403


def test_feedback_text_is_escaped_on_display(client: TestClient) -> None:
    form_page = client.get("/feedback")
    csrf_token = _extract_csrf(form_page.text)

    malicious_text = "<script>alert('xss')</script>"
    malicious_name = "<img src=x onerror=alert(1)>"

    response = client.post(
        "/feedback",
        data={
            "csrf_token": csrf_token,
            "text": malicious_text,
            "name": malicious_name,
        },
    )
    assert response.status_code == 200
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;alert" in response.text
    assert "<img src=x" not in response.text
    assert "&lt;img src=x" in response.text


def test_screenshot_validation_and_storage(client: TestClient, run_log: RunLog) -> None:
    form_page = client.get("/feedback")
    csrf_token = _extract_csrf(form_page.text)

    # 1. Non-image file is rejected
    bad_file = (
        "screenshot.png",
        b"this is plain text not an image",
        "image/png",
    )
    bad_resp = client.post(
        "/feedback",
        data={"csrf_token": csrf_token, "text": "Here is my note."},
        files={"screenshot": bad_file},
    )
    assert bad_resp.status_code == 422
    assert "image" in bad_resp.text.lower()

    # 2. Overlong file (> 5 MB) is rejected
    huge_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_SCREENSHOT_SIZE_BYTES + 1024)
    huge_resp = client.post(
        "/feedback",
        data={"csrf_token": csrf_token, "text": "Here is my note."},
        files={"screenshot": ("huge.png", huge_bytes, "image/png")},
    )
    assert huge_resp.status_code == 422
    assert "exceed" in huge_resp.text.lower() or "limit" in huge_resp.text.lower()

    # 3. Valid PNG is stored by SHA-256
    valid_png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    valid_sha = sha256_hex(valid_png_bytes)

    good_resp = client.post(
        "/feedback",
        data={"csrf_token": csrf_token, "text": "Screenshot attached."},
        files={"screenshot": ("my_custom_filename.png", valid_png_bytes, "image/png")},
    )
    assert good_resp.status_code == 200

    store = FeedbackStore(run_log.database_url)
    (fb,) = store.list_feedback()
    assert fb.screenshot_sha256 == valid_sha

    # 4. View screenshot safely
    shot_resp = client.get(f"/feedback/screenshots/{valid_sha}")
    assert shot_resp.status_code == 200
    assert shot_resp.content == valid_png_bytes
    # Must use system-detected MIME type, never client-supplied
    assert shot_resp.headers["content-type"].startswith("image/png")
    # Must NOT use client-supplied filename ("my_custom_filename.png")
    assert "my_custom_filename.png" not in shot_resp.headers.get("content-disposition", "")
    assert shot_resp.headers.get("x-content-type-options") == "nosniff"


def test_screen_open_redirect_is_prevented(client: TestClient, run_log: RunLog) -> None:
    # 1. GET with javascript: URL -> Cancel link must fall back to "/"
    resp_js = client.get("/feedback?screen=javascript:alert(1)")
    assert resp_js.status_code == 200
    assert 'href="javascript:alert(1)"' not in resp_js.text
    assert 'href="/"' in resp_js.text

    # 2. GET with absolute external URL -> Cancel link must fall back to "/"
    resp_evil = client.get("/feedback?screen=https://evil.example/phish")
    assert resp_evil.status_code == 200
    assert 'href="https://evil.example/phish"' not in resp_evil.text
    assert 'href="/"' in resp_evil.text

    # 3. GET with protocol-relative URL -> Cancel link must fall back to "/"
    resp_proto = client.get("/feedback?screen=//evil.example")
    assert resp_proto.status_code == 200
    assert 'href="//evil.example"' not in resp_proto.text
    assert 'href="/"' in resp_proto.text

    # 4. POST with evil screen -> success Return link must be "/", stored screen must be "/"
    csrf = _extract_csrf(resp_js.text)
    post_resp = client.post(
        "/feedback",
        data={"csrf_token": csrf, "text": "Valid note.", "screen": "https://evil.example"},
    )
    assert post_resp.status_code == 200
    assert 'href="https://evil.example"' not in post_resp.text
    assert 'href="/"' in post_resp.text

    store = FeedbackStore(run_log.database_url)
    (fb,) = store.list_feedback()
    assert fb.screen == "/"

    # 5. Legitimate relative path is preserved
    resp_valid = client.get("/feedback?screen=/reports/abc")
    assert resp_valid.status_code == 200
    assert 'href="/reports/abc"' in resp_valid.text


def test_untrusted_unbounded_context_and_fake_run_id(client: TestClient, run_log: RunLog, recordings_dir: Path) -> None:
    form_page = client.get("/feedback")
    csrf = _extract_csrf(form_page.text)

    # 1. 50,000-character screen is rejected (422)
    resp_huge_screen = client.post(
        "/feedback",
        data={"csrf_token": csrf, "text": "Valid note.", "screen": "/" + "a" * 50000},
    )
    assert resp_huge_screen.status_code == 422
    assert "screen" in resp_huge_screen.text.lower() or "long" in resp_huge_screen.text.lower()

    # 2. Fake / unknown run_id is rejected (422)
    fake_run_id = str(uuid.uuid4())
    resp_fake_run = client.post(
        "/feedback",
        data={
            "csrf_token": csrf,
            "text": "Valid note.",
            "run_id": fake_run_id,
            "incident_id": "made-up-incident",
            "report_version": "made-up-version",
        },
    )
    assert resp_fake_run.status_code == 422
    assert "run" in resp_fake_run.text.lower()

    # 3. Known valid run: incident_id and report_version are taken from DB, ignoring client-supplied values
    _record(recordings_dir, synthetic_video(b"valid-run-context-test"), "Valid report context.")
    rep_resp = client.post(
        "/reports",
        data={"role": "exterior"},
        files={"video": ("camera.mp4", synthetic_video(b"valid-run-context-test"), "video/mp4")},
    )
    assert rep_resp.status_code == 200
    (run,) = run_log.list_runs()

    good_resp = client.post(
        "/feedback",
        data={
            "csrf_token": csrf,
            "text": "Checking server-derived context.",
            "run_id": str(run.run_id),
            "incident_id": "spoofed_incident_id",
            "report_version": "spoofed_version",
            "screen": f"/reports/{run.run_id}",
        },
        headers={"User-Agent": "A" * 1000},
    )
    assert good_resp.status_code == 200

    store = FeedbackStore(run_log.database_url)
    (fb,) = store.list_feedback()
    # Verified server-side: took values from stored run
    assert fb.run_id == run.run_id
    assert fb.incident_id == run.input_hash
    assert fb.incident_id != "spoofed_incident_id"
    assert fb.report_version == (run.report.prompt_version if run.report else None)
    assert fb.report_version != "spoofed_version"
    # User-Agent is capped at 500 characters
    assert fb.browser is not None
    assert len(fb.browser) <= 500


def test_control_characters_and_nul_bytes_rejected(client: TestClient) -> None:
    form_page = client.get("/feedback")
    csrf = _extract_csrf(form_page.text)

    # 1. NUL byte in text -> 422 (not 500)
    resp_nul_text = client.post(
        "/feedback",
        data={"csrf_token": csrf, "text": "hello\x00world"},
    )
    assert resp_nul_text.status_code == 422
    assert (
        "control" in resp_nul_text.text.lower()
        or "character" in resp_nul_text.text.lower()
        or "invalid" in resp_nul_text.text.lower()
    )

    # 2. NUL byte in name -> 422
    resp_nul_name = client.post(
        "/feedback",
        data={"csrf_token": csrf, "text": "Valid text.", "name": "Officer\x00Smith"},
    )
    assert resp_nul_name.status_code == 422

    # 3. Other control characters in name (e.g. \x1b ESC) -> 422
    resp_ctrl_name = client.post(
        "/feedback",
        data={"csrf_token": csrf, "text": "Valid text.", "name": "Officer\x1bSmith"},
    )
    assert resp_ctrl_name.status_code == 422

    # 4. Valid text with newlines and tabs -> 200
    resp_valid_multiline = client.post(
        "/feedback",
        data={"csrf_token": csrf, "text": "Line 1\nLine 2\r\n\tIndented line."},
    )
    assert resp_valid_multiline.status_code == 200


def test_screenshot_route_serves_only_feedback_screenshots_not_video_blobs(client: TestClient, run_log: RunLog) -> None:
    store = client.app.state.evidence  # type: ignore[attr-defined]
    assert store is not None

    # 1. Store a video blob in the evidence store
    video_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00isommp42" + b"\x00" * 100
    video_sha = sha256_hex(video_bytes)
    store.put(io.BytesIO(video_bytes), sha256=video_sha)
    assert store.exists(video_sha)

    # 2. Querying screenshot endpoint for the video hash must 404 (not serve video!)
    resp_video = client.get(f"/feedback/screenshots/{video_sha}")
    assert resp_video.status_code == 404

    # 3. Querying for an unrecorded PNG hash also must 404
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    unrecorded_sha = sha256_hex(png_bytes)
    store.put(io.BytesIO(png_bytes), sha256=unrecorded_sha)
    assert client.get(f"/feedback/screenshots/{unrecorded_sha}").status_code == 404


def test_screenshot_refused_when_no_evidence_store_configured(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INCIDENT_EVIDENCE_DIR", raising=False)
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=None)
    client = authorized_client(app, run_log.database_url)

    form_page = client.get("/feedback")
    csrf = _extract_csrf(form_page.text)

    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    resp = client.post(
        "/feedback",
        data={"csrf_token": csrf, "text": "Screenshot with unconfigured store."},
        files={"screenshot": ("shot.png", png_bytes, "image/png")},
    )
    assert resp.status_code in (503, 422)
    assert "storage" in resp.text.lower() or "configured" in resp.text.lower()


class SpyAIConnection(AIConnection):
    """Spy connection capturing every AIRequest passed to analyse, repair, and analyse_window."""

    def __init__(self, inner: AIConnection) -> None:
        self.inner = inner
        self.analyse_requests: list[AIRequest] = []
        self.repair_requests: list[AIRequest] = []
        self.dense_requests: list[AIRequest] = []

    def analyse(self, request: AIRequest) -> AIResponse:
        self.analyse_requests.append(request)
        return self.inner.analyse(request)

    def repair(self, request: AIRequest, response: AIResponse, errors: Sequence[str]) -> AIResponse:
        self.repair_requests.append(request)
        if hasattr(self.inner, "repair"):
            repair_fn = self.inner.repair
            return cast(AIResponse, repair_fn(request, response, errors))
        return response

    def analyse_window(self, request: AIRequest, window: Any) -> tuple[Any, tuple[Any, ...]]:
        self.dense_requests.append(request)
        if hasattr(self.inner, "analyse_window"):
            fn = self.inner.analyse_window
            return cast(tuple[Any, tuple[Any, ...]], fn(request, window))
        from incident_report.facts import OverviewFacts

        return OverviewFacts(
            parties=[], events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
        ), ()


def test_feedback_text_is_strictly_isolated_from_ai_analysis(
    recordings_dir: Path, run_log: RunLog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE.md / RPT-43 guardrail: prove feedback text never reaches any AIRequest (across analyse, repair, and dense analyse_window). Note: AIRequest carries view hashes and metadata, not rendered provider prompts."""
    evidence_dir = tmp_path / "evidence_spy"
    evidence_dir.mkdir()
    monkeypatch.setenv("INCIDENT_EVIDENCE_DIR", str(evidence_dir))
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    from incident_report.config import Settings

    settings = Settings.from_env({"DATABASE_URL": run_log.database_url})
    store = create_evidence_store(settings)

    spy_ai = SpyAIConnection(ReplayAIConnection(recordings_dir))
    app = create_app(ai=spy_ai, run_log=run_log, evidence=store)
    client = authorized_client(app, run_log.database_url)

    # 1. User submits feedback with unique canary text
    canary = "CANARY_SECRET_FEEDBACK_NOTE_OFFICER_CONFIDENTIAL_12345"
    form_page = client.get("/feedback")
    csrf_token = _extract_csrf(form_page.text)

    fb_resp = client.post(
        "/feedback",
        data={"csrf_token": csrf_token, "text": canary, "category": "confusing"},
    )
    assert fb_resp.status_code == 200

    # Verify feedback was stored in the DB
    fb_store = FeedbackStore(run_log.database_url)
    assert any(canary in f.text for f in fb_store.list_feedback())

    # 2. Trigger an analysis through the web upload route (with candidate collision so dense pass runs)
    clip = synthetic_video(b"spy-video-clip")
    _record_with_collision(recordings_dir, clip, "Spy summary result.")
    upload_resp = client.post(
        "/reports",
        data={"role": "exterior"},
        files={"video": ("camera.mp4", clip, "video/mp4")},
    )
    assert upload_resp.status_code == 200, upload_resp.text

    # 3. Assert canary text is absent from all captured AIRequest objects (including dense)
    assert len(spy_ai.analyse_requests) >= 1
    assert len(spy_ai.dense_requests) >= 1
    all_requests = spy_ai.analyse_requests + spy_ai.repair_requests + spy_ai.dense_requests
    for req in all_requests:
        # Check input_hash
        assert canary not in req.input_hash
        # Check prompt_version
        assert canary not in req.prompt_version
        # Check views
        for v in req.views:
            assert canary not in v.sha256
            assert canary not in str(v.role)
        # Check full string / JSON representation of AIRequest
        assert canary not in str(req)
        assert canary not in json.dumps(dataclasses.asdict(req), default=str)


def test_feedback_content_length_above_limit_returns_413_and_leaves_store_unchanged(
    client: TestClient, tmp_path: Path
) -> None:
    """RPT-52 carry-over: Content-Length above MAX_FEEDBACK_BODY_SIZE_BYTES is rejected with 413, store unchanged."""
    evidence_dir = tmp_path / "evidence"
    files_before = list(evidence_dir.iterdir()) if evidence_dir.exists() else []

    oversized_length = MAX_FEEDBACK_BODY_SIZE_BYTES + 1024
    dummy_body = b"csrf_token=dummy&text=hello"

    resp = client.post(
        "/feedback",
        content=dummy_body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-length": str(oversized_length),
        },
    )
    assert resp.status_code == 413
    assert "exceed" in resp.text.lower() or "limit" in resp.text.lower()

    # Evidence store remains untouched - nothing written
    files_after = list(evidence_dir.iterdir()) if evidence_dir.exists() else []
    assert files_after == files_before


def test_feedback_chunked_body_above_limit_returns_413(client: TestClient) -> None:
    """RPT-52 carry-over: Chunked stream without Content-Length is stopped with 413 when exceeding body limit."""
    form_page = client.get("/feedback")
    csrf_token = _extract_csrf(form_page.text)

    chunk_size = 64 * 1024
    total_chunks = (MAX_FEEDBACK_BODY_SIZE_BYTES // chunk_size) + 2

    def streaming_body() -> Iterator[bytes]:
        yield f"csrf_token={csrf_token}&text=".encode()
        for _ in range(total_chunks):
            yield b"a" * chunk_size

    resp = client.post(
        "/feedback",
        content=streaming_body(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 413
    assert "exceed" in resp.text.lower() or "limit" in resp.text.lower()


def test_feedback_bad_content_length_returns_400(client: TestClient) -> None:
    """RPT-52 carry-over: Non-integer or negative Content-Length returns 400."""
    # 1. Non-integer Content-Length
    resp_alpha = client.post(
        "/feedback",
        content=b"csrf_token=dummy&text=hello",
        headers={"content-type": "application/x-www-form-urlencoded", "content-length": "not-a-number"},
    )
    assert resp_alpha.status_code == 400
    assert "content-length" in resp_alpha.text.lower() or "invalid" in resp_alpha.text.lower()

    # 2. Negative Content-Length
    resp_neg = client.post(
        "/feedback",
        content=b"csrf_token=dummy&text=hello",
        headers={"content-type": "application/x-www-form-urlencoded", "content-length": "-1"},
    )
    assert resp_neg.status_code == 400
    assert "content-length" in resp_neg.text.lower() or "invalid" in resp_neg.text.lower()


def test_feedback_exactly_at_body_limit_succeeds(client: TestClient, run_log: RunLog) -> None:
    """RPT-52 carry-over: A valid submission exactly at MAX_FEEDBACK_BODY_SIZE_BYTES succeeds (200)."""
    form_page = client.get("/feedback")
    csrf_token = _extract_csrf(form_page.text)

    boundary = "----TestBoundaryExactLimit12345"
    valid_png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"

    part_csrf = (f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n{csrf_token}\r\n').encode()
    part_text = (f'--{boundary}\r\nContent-Disposition: form-data; name="text"\r\n\r\nValid feedback text\r\n').encode()
    part_shot_prefix = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="screenshot"; filename="shot.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode()
    screenshot_bytes = valid_png_header + b"\x00" * (1024 * 1024 - len(valid_png_header))
    part_shot = part_shot_prefix + screenshot_bytes + b"\r\n"
    closing = f"--{boundary}--\r\n".encode()

    current_len = len(part_csrf) + len(part_text) + len(part_shot) + len(closing)
    extra_field_prefix = f'--{boundary}\r\nContent-Disposition: form-data; name="pad"\r\n\r\n'.encode()
    extra_field_suffix = b"\r\n"

    needed_pad = MAX_FEEDBACK_BODY_SIZE_BYTES - (current_len + len(extra_field_prefix) + len(extra_field_suffix))
    assert needed_pad > 0
    extra_field = extra_field_prefix + (b"0" * needed_pad) + extra_field_suffix

    exact_body = part_csrf + part_text + part_shot + extra_field + closing
    assert len(exact_body) == MAX_FEEDBACK_BODY_SIZE_BYTES

    resp = client.post(
        "/feedback",
        content=exact_body,
        headers={
            "content-type": f"multipart/form-data; boundary={boundary}",
            "content-length": str(len(exact_body)),
        },
    )
    assert resp.status_code == 200
    assert "Thank You" in resp.text or "recorded" in resp.text.lower() or "received" in resp.text.lower()
