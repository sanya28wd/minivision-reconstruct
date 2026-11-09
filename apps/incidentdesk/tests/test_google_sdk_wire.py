"""The real google-genai SDK against an in-memory HTTP transport: no network, no key, no model call.

Pins the request shapes the installed SDK actually sends (verified against google-genai 2.31.0 source).
"""

import json
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest
from google import genai
from google.genai.types import HttpOptionsDict

from incident_report.ai import AIRequest, AIViewRef
from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo, TransportError
from incident_report.ai.google_video import GoogleVideoTransport, sdk_http_options
from incident_report.domain import Role
from incident_report.hashing import incident_input_hash, sha256_hex

_BASE = "https://generativelanguage.googleapis.com"
_FILE = {"name": "files/synthetic", "uri": f"{_BASE}/v1beta/files/synthetic", "mimeType": "video/mp4"}
_BYTES = b"synthetic video bytes with audio"
_ANSWER: dict[str, list[str]] = {"parties": [], "events": [], "candidate_collisions": []}
_OK: dict[str, object] = {
    "candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(_ANSWER)}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2},
    "modelVersion": "gemini-3.8-flash",
}


class FakeGoogle:
    """Answers the Files API resumable upload, polling, generateContent and delete endpoints."""

    def __init__(self, generate: tuple[int, Mapping[str, object]] = (200, _OK)) -> None:
        self.requests: list[httpx.Request] = []
        self.generate = generate

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/upload/v1beta/files":
            headers = {"x-goog-upload-url": f"{_BASE}/upload/v1beta/files/synthetic?upload_id=1"}
            return httpx.Response(200, headers=headers, json={})
        if request.method == "POST" and path == "/upload/v1beta/files/synthetic":
            return httpx.Response(
                200, headers={"x-goog-upload-status": "final"}, json={"file": {**_FILE, "state": "PROCESSING"}}
            )
        if request.method == "GET" and path == "/v1beta/files/synthetic":
            return httpx.Response(200, json={**_FILE, "state": "ACTIVE"})
        if request.method == "POST" and path.endswith(":generateContent"):
            status, body = self.generate
            return httpx.Response(status, json=body)
        if request.method == "DELETE" and path == "/v1beta/files/synthetic":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": {"code": 404, "message": "unexpected", "status": "NOT_FOUND"}})

    def calls(self, method: str, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]


def _transport(fake: FakeGoogle, settings: GeminiSettings, *, inline_enabled: bool = False) -> GoogleVideoTransport:
    options: HttpOptionsDict = {**sdk_http_options(settings), "client_args": {"transport": httpx.MockTransport(fake)}}
    client = genai.Client(api_key="unit-test-placeholder", vertexai=False, http_options=options)
    return GoogleVideoTransport(client, sleep=lambda _s: None, inline_enabled=inline_enabled)


def _video(tmp_path: Path) -> PublicResearchVideo:
    path = tmp_path / "public.mp4"
    path.write_bytes(_BYTES)
    return PublicResearchVideo(path, True)


@pytest.mark.parametrize("shape", ["single", "main", "recovery", "dense"])
def test_real_sdk_inline_preserves_parts_and_metadata_without_files(tmp_path: Path, shape: str) -> None:
    import base64

    from google.genai.types import Part

    from incident_report.ai.dense import DenseSettings
    from incident_report.ai.google_video import counted_inline_request_bytes
    from incident_report.ai.overview11 import main_prompt, recovery_prompt

    settings = GeminiSettings(model="gemini-3.8-flash", fps=2)
    prompt = (
        main_prompt(30, settings)
        if shape == "main"
        else recovery_prompt(30, settings)
        if shape == "recovery"
        else "observe"
    )
    fake = FakeGoogle()
    transport = _transport(fake, settings, inline_enabled=True)
    metadata: dict[str, object] = {"fps": settings.fps}
    if shape == "dense":
        metadata = {"fps": 20.0, "start_offset": "1.500s", "end_offset": "4.500s"}
        raw = transport.generate_window(
            _video(tmp_path),
            prompt,
            DenseSettings(model=settings.model, fps=20),
            start_offset_seconds=1.5,
            end_offset_seconds=4.5,
        )
    else:
        with transport.report_scope():
            raw = transport.generate(_video(tmp_path), prompt, settings)
    assert len(fake.requests) == 1  # no upload, get or delete HTTP request
    (request,) = fake.requests
    body = json.loads(request.content)
    parts = body["contents"][0]["parts"]
    videos = [p for p in parts if "inlineData" in p]
    expected_metadata = [p.metadata() for p in prompt.video_parts] if hasattr(prompt, "video_parts") else [metadata]
    assert len(videos) == len(expected_metadata)
    for part, expected in zip(videos, expected_metadata, strict=True):
        assert part["inlineData"] == {"data": base64.b64encode(_BYTES).decode(), "mime_type": "video/mp4"}
        # Validate the documented Python dictionary form against the installed SDK, too.
        validated = Part.model_validate(
            {"inline_data": {"data": _BYTES, "mime_type": "video/mp4"}, "video_metadata": expected}
        )
        assert validated.inline_data is not None and validated.inline_data.data == _BYTES
        actual = {"".join(c for c in k.lower() if c != "_"): v for k, v in part["videoMetadata"].items()}
        assert actual == {k.replace("_", ""): v for k, v in expected.items()}
    assert parts[-1] == {"text": str(prompt)}
    assert len(request.content) < counted_inline_request_bytes(len(_BYTES), prompt, settings.model, metadata)
    assert json.loads(raw)["candidates"][0]["finish_reason"] == "STOP"


def test_inline_error_never_records_echoed_video_bytes_or_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-placeholder")
    message = f"bad data {_BYTES!r} base64={base64.b64encode(_BYTES).decode()} key=unit-test-placeholder"
    fake = FakeGoogle(generate=(400, {"error": {"code": 400, "message": message, "status": "INVALID_ARGUMENT"}}))
    settings = GeminiSettings(model="gemini-3.8-flash")
    with pytest.raises(TransportError) as raised:
        _transport(fake, settings, inline_enabled=True).generate(_video(tmp_path), "observe", settings)
    assert raised.value.status_code == 400
    diagnostic = raised.value.provider_error
    assert diagnostic is not None and diagnostic.http_status == 400 and diagnostic.status == "INVALID_ARGUMENT"
    assert diagnostic.message is None
    assert _BYTES.decode() not in str(raised.value)
    assert "unit-test-placeholder" not in diagnostic.model_dump_json()
    assert len(fake.requests) == 1


def test_inline_error_drops_unrecognized_status_that_could_echo_data(tmp_path: Path) -> None:
    fake = FakeGoogle(generate=(400, {"error": {"code": 400, "message": "bad data", "status": "SECRETVIDEO"}}))
    settings = GeminiSettings(model="gemini-3.8-flash")
    with pytest.raises(TransportError) as raised:
        _transport(fake, settings, inline_enabled=True).generate(_video(tmp_path), "observe", settings)
    diagnostic = raised.value.provider_error
    assert diagnostic is not None
    assert diagnostic.model_dump() == {"http_status": 400, "status": None, "message": None}


def test_real_sdk_uploads_polls_generates_with_fps_and_schema_then_deletes(tmp_path: Path) -> None:
    fake = FakeGoogle()
    settings = GeminiSettings(model="gemini-3.8-flash", fps=2)
    raw = _transport(fake, settings).generate(_video(tmp_path), "observe", settings)

    start = fake.calls("POST", "/upload/v1beta/files")[0]
    assert start.headers["x-goog-upload-header-content-type"] == "video/mp4"
    assert start.headers["x-goog-upload-header-content-length"] == str(len(_BYTES))
    assert fake.calls("POST", "/files/synthetic")[0].content == _BYTES
    assert len(fake.calls("GET", "/files/synthetic")) == 1  # first state was PROCESSING, so polled once

    (generate,) = fake.calls("POST", ":generateContent")
    assert generate.url.path == "/v1beta/models/gemini-3.8-flash:generateContent"
    assert generate.headers["x-goog-api-key"] == "unit-test-placeholder"
    assert generate.headers["x-server-timeout"] == "60"  # 60000 ms HTTP timeout
    body = json.loads(generate.content)
    parts = body["contents"][0]["parts"]
    assert body["contents"][0]["role"] == "user"
    assert parts[0]["fileData"]["file_uri"] == _FILE["uri"]
    assert parts[0]["fileData"]["mime_type"] == "video/mp4"
    assert parts[0]["videoMetadata"] == {"fps": 2.0}
    assert parts[1] == {"text": "observe"}
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"]["type"] == "OBJECT"
    assert config["responseSchema"]["required"] == ["parties", "events", "candidate_collisions"]

    assert len(fake.calls("DELETE", "/files/synthetic")) == 1
    recorded = json.loads(raw)
    assert recorded["candidates"][0]["finish_reason"] == "STOP"
    assert recorded["usage_metadata"]["prompt_token_count"] == 7
    assert recorded["model_version"] == "gemini-3.8-flash"
    assert "sdk_http_response" not in recorded
    assert "unit-test-placeholder" not in raw


def test_a_429_is_sent_once_by_the_sdk_so_the_counted_retry_loop_owns_every_retry(tmp_path: Path) -> None:
    error = {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
    fake = FakeGoogle(generate=(429, error))
    settings = GeminiSettings(model="gemini-3.8-flash")
    with pytest.raises(TransportError) as raised:
        _transport(fake, settings).generate(_video(tmp_path), "observe", settings)
    assert raised.value.status_code == 429
    assert len(fake.calls("POST", ":generateContent")) == 1  # no hidden SDK retry
    assert len(fake.calls("DELETE", "/files/synthetic")) == 1  # uploaded file removed even on failure
    assert "quota" not in str(raised.value)


def test_truncated_model_output_is_returned_raw_for_recording_not_raised_by_the_sdk(tmp_path: Path) -> None:
    part = {"text": '{"parties": [{"desc'}
    truncated = {"candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "MAX_TOKENS"}]}
    settings = GeminiSettings(model="gemini-3.8-flash")
    raw = _transport(FakeGoogle(generate=(200, truncated)), settings).generate(_video(tmp_path), "observe", settings)
    assert json.loads(raw)["candidates"][0]["finish_reason"] == "MAX_TOKENS"


def test_a_blocked_prompt_without_candidates_is_recorded_then_refused_as_invalid(tmp_path: Path) -> None:
    blocked = {"promptFeedback": {"blockReason": "OTHER"}}
    settings = GeminiSettings(model="gemini-3.8-flash", max_retries=0)
    video = _video(tmp_path)
    digest = sha256_hex(_BYTES)
    request = AIRequest(
        incident_input_hash([(Role.EXTERIOR, digest)]), "overview-1", (AIViewRef(Role.EXTERIOR, digest),)
    )
    connection = GeminiAIConnection(
        settings, video, transport=_transport(FakeGoogle(generate=(200, blocked)), settings)
    )
    with pytest.raises(Exception, match="invalid overview facts") as raised:
        connection.analyse(request)
    (call,) = raised.value.calls  # type: ignore[attr-defined]
    assert call.raw_response is not None and "block_reason" in call.raw_response


def test_full_connection_over_the_real_sdk_records_the_call_and_parses_facts(tmp_path: Path) -> None:
    settings = GeminiSettings(model="gemini-3.8-flash")
    video = _video(tmp_path)
    digest = sha256_hex(_BYTES)
    request = AIRequest(
        incident_input_hash([(Role.EXTERIOR, digest)]), "overview-1", (AIViewRef(Role.EXTERIOR, digest),)
    )
    connection = GeminiAIConnection(settings, video, transport=_transport(FakeGoogle(), settings))
    response = connection.analyse(request)
    assert response.facts is not None and response.facts.events == []
    (call,) = response.calls
    assert call.status_code is None and call.retry_index == 0 and call.raw_response is not None


def test_real_sdk_dense_window_sends_start_and_end_offsets_and_fps(tmp_path: Path) -> None:
    from incident_report.ai.dense import DenseSettings

    fake = FakeGoogle()
    settings = DenseSettings(model="gemini-3.8-flash", fps=20.0)
    transport = _transport(fake, settings)  # type: ignore[arg-type]
    raw = transport.generate_window(
        _video(tmp_path), "observe dense window", settings, start_offset_seconds=1.5, end_offset_seconds=4.5
    )

    (generate,) = fake.calls("POST", ":generateContent")
    assert generate.url.path == "/v1beta/models/gemini-3.8-flash:generateContent"
    body = json.loads(generate.content)
    parts = body["contents"][0]["parts"]
    assert parts[0]["fileData"]["file_uri"] == _FILE["uri"]
    v_meta = parts[0]["videoMetadata"]
    assert v_meta["fps"] == 20.0
    # SDK serializes start_offset and end_offset
    assert v_meta.get("start_offset") == "1.500s" or v_meta.get("startOffset") == "1.500s"
    assert v_meta.get("end_offset") == "4.500s" or v_meta.get("endOffset") == "4.500s"
    assert parts[1] == {"text": "observe dense window"}
    assert len(fake.calls("DELETE", "/files/synthetic")) == 1
    assert json.loads(raw)["candidates"][0]["finish_reason"] == "STOP"
