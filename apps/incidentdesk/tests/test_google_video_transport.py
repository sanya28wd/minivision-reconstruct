"""Official SDK wire boundary, exercised with an in-memory fake client only."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from incident_report.ai.gemini import GeminiSettings, PublicResearchVideo
from incident_report.ai.google_video import GoogleVideoTransport


class FakeClient:
    def __init__(self) -> None:
        self.uploaded: bytes | None = None
        self.generated: dict[str, Any] = {}
        self.deleted: list[str] = []
        self.files = self
        self.models = self

    def upload(self, *, file: Any, config: dict[str, Any]) -> SimpleNamespace:
        self.uploaded = file.read()
        assert config == {"mime_type": "video/mp4"}
        return SimpleNamespace(
            name="files/synthetic",
            uri="https://generativelanguage.googleapis.com/synthetic",
            state=SimpleNamespace(name="ACTIVE"),
        )

    def generate_content(self, **kwargs: Any) -> SimpleNamespace:
        self.generated = kwargs
        return SimpleNamespace(model_dump_json=lambda **_: '{"candidates":[]}')

    def delete(self, *, name: str) -> None:
        self.deleted.append(name)


def test_small_video_is_inline_without_file_lifecycle(tmp_path: Path) -> None:
    from incident_report.timings import capture_timings

    path = tmp_path / "public.mp4"
    path.write_bytes(b"original video and audio")
    client = FakeClient()
    transport = GoogleVideoTransport(client)
    with capture_timings() as timings, transport.report_scope():
        transport.generate(
            PublicResearchVideo(path, True), "observe", GeminiSettings(model="gemini-3.8-flash", fps=0.5)
        )
    assert client.generated["contents"] == [
        {
            "inline_data": {"data": b"original video and audio", "mime_type": "video/mp4"},
            "video_metadata": {"fps": 0.5},
        },
        {"text": "observe"},
    ]
    assert client.uploaded is None
    assert client.deleted == []
    assert set(timings) == {"generate"}


@pytest.mark.parametrize("extra", [-1, 0, 1])
@pytest.mark.parametrize("multi", [False, True])
def test_counted_total_request_boundary(tmp_path: Path, extra: int, multi: bool) -> None:
    from incident_report.ai.google_video import INLINE_REQUEST_LIMIT_BYTES, counted_inline_request_bytes
    from incident_report.ai.overview11 import MultiResPrompt, VideoPart

    settings = GeminiSettings(model="gemini-3.8-flash")
    path = tmp_path / "public.mp4"
    # 7 MB fits once, but almost fills the request when duplicated and base64 encoded.
    path.write_bytes(b"v" * 7_000_000)

    def prompt(text: str) -> str:
        if not multi:
            return text
        return MultiResPrompt(
            text,
            video_parts=(VideoPart("whole_clip", "whole", 1), VideoPart("detail_window", "detail", 15, "1s", "3s")),
            window=(1, 3),
            generate_stage="generate",
        )

    baseline = counted_inline_request_bytes(path.stat().st_size, prompt(""), settings.model, {"fps": settings.fps})
    request = prompt("x" * (INLINE_REQUEST_LIMIT_BYTES - baseline + extra))
    assert (
        counted_inline_request_bytes(path.stat().st_size, request, settings.model, {"fps": settings.fps})
        == 20_000_000 + extra
    )
    client = FakeClient()
    GoogleVideoTransport(client).generate(PublicResearchVideo(path, True), request, settings)
    assert (client.uploaded is None) is (extra < 0)
    assert ("inline_data" in client.generated["contents"][1 if multi else 0]) is (extra < 0)


def test_request_count_includes_base64_padding_system_and_all_parts() -> None:
    from incident_report.ai.google_video import counted_inline_request_bytes
    from incident_report.ai.overview11 import MultiResPrompt, VideoPart

    prompt = MultiResPrompt(
        "observe",
        video_parts=(VideoPart("whole_clip", "whole", 1), VideoPart("detail_window", "detail", 15)),
        window=None,
        generate_stage="generate",
    )
    empty = counted_inline_request_bytes(0, prompt, "model", {"fps": 1})
    # Two copies of one byte each require four base64 characters each, not 4/3 bytes.
    assert counted_inline_request_bytes(1, prompt, "model", {"fps": 1}) == empty + 8
    assert counted_inline_request_bytes(3, prompt, "model", {"fps": 1}) == empty + 8
    assert counted_inline_request_bytes(4, prompt, "model", {"fps": 1}) == empty + 16
    prompt.system_instruction += "a" * 1000
    assert counted_inline_request_bytes(0, prompt, "model", {"fps": 1}) == empty + 1000


def test_environment_off_switch_forces_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_VIDEO_INLINE", "0")
    path = tmp_path / "public.mp4"
    path.write_bytes(b"public")
    client = FakeClient()
    GoogleVideoTransport(client).generate(
        PublicResearchVideo(path, True), "observe", GeminiSettings(model="gemini-3.8-flash")
    )
    assert client.uploaded == b"public"
    assert "file_data" in client.generated["contents"][0]
    assert client.deleted == ["files/synthetic"]


def test_sdk_receives_original_video_with_fps_and_structured_schema(tmp_path: Path) -> None:
    path = tmp_path / "public.mp4"
    path.write_bytes(b"video and original audio")
    client = FakeClient()
    raw = GoogleVideoTransport(client, inline_enabled=False).generate(
        PublicResearchVideo(path, True), "observe", GeminiSettings(model="gemini-3.8-flash", fps=0.5)
    )
    assert raw == '{"candidates":[]}'
    assert client.uploaded == b"video and original audio"
    assert client.generated["model"] == "gemini-3.8-flash"
    assert client.generated["contents"][0]["video_metadata"] == {"fps": 0.5}
    assert client.generated["config"]["response_mime_type"] == "application/json"
    assert client.generated["config"]["response_schema"]["required"] == ["parties", "events", "candidate_collisions"]
    assert client.deleted == ["files/synthetic"]


def test_report_scope_reuses_upload_for_overview_and_dense_then_cleans_once(tmp_path: Path) -> None:
    from incident_report.ai.dense import DenseSettings

    path = tmp_path / "public.mp4"
    path.write_bytes(b"same video and audio")
    client = FakeClient()
    uploads = []
    original = client.upload

    def upload(**kwargs: Any) -> SimpleNamespace:
        uploads.append(True)
        return original(**kwargs)

    client.upload = upload  # type: ignore[method-assign]
    transport = GoogleVideoTransport(client, inline_enabled=False)
    video = PublicResearchVideo(path, True)
    settings = GeminiSettings(model="gemini-3.8-flash")
    with transport.report_scope():
        for _ in range(3):
            transport.generate(video, "observe", settings)
        transport.generate_window(
            video, "dense", DenseSettings(model=settings.model), start_offset_seconds=0, end_offset_seconds=1
        )
        assert client.deleted == []
    assert uploads == [True]
    assert client.deleted == ["files/synthetic"]
    with transport.report_scope():
        transport.generate(video, "observe", settings)
    assert len(uploads) == 2
    assert client.deleted == ["files/synthetic", "files/synthetic"]


def test_report_files_concurrent_calls_and_failure_cleanup(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    from threading import Barrier

    import pytest

    from incident_report.ai.gemini import TransportError

    path = tmp_path / "public.mp4"
    path.write_bytes(b"public")
    client = FakeClient()
    uploads = []
    barrier = Barrier(3)
    original = client.upload

    def upload(**kwargs: Any) -> SimpleNamespace:
        uploads.append(True)
        return original(**kwargs)

    def generate(**kwargs: Any) -> SimpleNamespace:
        barrier.wait(timeout=3)
        raise RuntimeError("synthetic failure")

    client.upload = upload  # type: ignore[method-assign]
    client.generate_content = generate  # type: ignore[method-assign]
    transport = GoogleVideoTransport(client, inline_enabled=False)
    with transport.report_scope(), ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(
                copy_context().run,
                transport.generate,
                PublicResearchVideo(path, True),
                "observe",
                GeminiSettings(model="gemini-3.8-flash"),
            )
            for _ in range(3)
        ]
        for future in futures:
            with pytest.raises(TransportError):
                future.result(timeout=4)
    assert uploads == [True]
    assert client.deleted == ["files/synthetic"]


def test_late_upload_after_report_exit_is_cleaned_without_generation(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    from threading import Event

    import pytest

    from incident_report.ai.gemini import TransportError

    path = tmp_path / "public.mp4"
    path.write_bytes(b"public")
    client = FakeClient()
    entered, release = Event(), Event()
    original = client.upload

    def upload(**kwargs: Any) -> SimpleNamespace:
        entered.set()
        assert release.wait(3)
        return original(**kwargs)

    client.upload = upload  # type: ignore[method-assign]
    transport = GoogleVideoTransport(client, inline_enabled=False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transport.report_scope():
            future = pool.submit(
                copy_context().run,
                transport.generate,
                PublicResearchVideo(path, True),
                "observe",
                GeminiSettings(model="gemini-3.8-flash"),
            )
            assert entered.wait(3)
        release.set()
        with pytest.raises(TransportError):
            future.result(timeout=3)
    assert client.deleted == ["files/synthetic"]
    assert client.generated == {}


def test_transport_timings_separate_upload_wait_and_generate(tmp_path: Path) -> None:
    from incident_report.timings import capture_timings

    path = tmp_path / "public.mp4"
    path.write_bytes(b"public")
    transport = GoogleVideoTransport(FakeClient(), inline_enabled=False)
    with capture_timings() as timings:
        transport.generate(PublicResearchVideo(path, True), "observe", GeminiSettings(model="gemini-3.8-flash"))
    assert set(timings) == {"upload", "processing_wait", "generate"}
    assert all(value >= 0 for value in timings.values())


@pytest.mark.parametrize("failed_stage", ["upload", "poll"])
def test_report_upload_retry_recovers_and_poll_retry_keeps_uploaded_file(tmp_path: Path, failed_stage: str) -> None:
    import pytest

    from incident_report.ai.gemini import TransportError

    path = tmp_path / "public.mp4"
    path.write_bytes(b"public")
    client = FakeClient()
    uploads = []
    polls = []
    original = client.upload

    def upload(**kwargs: Any) -> Any:
        uploads.append(True)
        if failed_stage == "upload" and len(uploads) == 1:
            raise TransportError(503)
        result = original(**kwargs)
        if failed_stage == "poll":
            result.state.name = "PROCESSING"
        return result

    def get(**kwargs: Any) -> Any:
        polls.append(True)
        if len(polls) == 1:
            raise TransportError(503)
        return SimpleNamespace(name="files/synthetic", uri="synthetic-uri", state=SimpleNamespace(name="ACTIVE"))

    client.upload = upload  # type: ignore[method-assign]
    client.get = get  # type: ignore[attr-defined]
    transport = GoogleVideoTransport(client, sleep=lambda _: None, inline_enabled=False)
    with transport.report_scope():
        with pytest.raises(TransportError) as exc:
            transport.generate(PublicResearchVideo(path, True), "observe", GeminiSettings(model="gemini-3.8-flash"))
        assert exc.value.status_code == 503
        assert transport.generate(PublicResearchVideo(path, True), "observe", GeminiSettings(model="gemini-3.8-flash"))
    assert len(uploads) == (2 if failed_stage == "upload" else 1)
    assert client.deleted == ["files/synthetic"]


def test_preuploaded_active_bytes_force_files_and_are_not_uploaded_twice(tmp_path: Path) -> None:
    from incident_report.hashing import sha256_hex

    path = tmp_path / "public.mp4"
    path.write_bytes(b"public video")
    client = FakeClient()
    client.vertexai = False  # type: ignore[attr-defined]
    transport = GoogleVideoTransport(client)
    video = PublicResearchVideo(path, True)
    prepared = transport.preupload(video, sha256_hex(b"public video"), GeminiSettings(model="gemini-3.8-flash"))
    prepared.wait()
    assert client.uploaded == b"public video"
    client.uploaded = None
    with transport.preuploaded_scope([prepared]), transport.report_scope():
        transport.generate(video, "observe", GeminiSettings(model="gemini-3.8-flash"))
        transport.generate(video, "observe again", GeminiSettings(model="gemini-3.8-flash"))
    assert client.uploaded is None
    assert "file_data" in client.generated["contents"][0]
    prepared.close()
    assert client.deleted == ["files/synthetic"]


@pytest.mark.parametrize("backend", [True, None])
def test_preupload_never_uses_files_for_vertex_or_unknown_backend(tmp_path: Path, backend: bool | None) -> None:
    from incident_report.ai import AIConnectionError
    from incident_report.hashing import sha256_hex

    path = tmp_path / "public.mp4"
    path.write_bytes(b"public")
    client = FakeClient()
    client.vertexai = backend  # type: ignore[attr-defined]
    with pytest.raises(AIConnectionError):
        GoogleVideoTransport(client).preupload(
            PublicResearchVideo(path, True), sha256_hex(b"public"), GeminiSettings(model="gemini-3.8-flash")
        )
    assert client.uploaded is None


def test_preupload_mismatch_is_local_only_and_late_completion_is_deleted(tmp_path: Path) -> None:
    from threading import Event

    from incident_report.hashing import sha256_hex

    class SlowClient(FakeClient):
        vertexai = False

        def upload(self, **kwargs: Any) -> SimpleNamespace:
            started.set()
            assert release.wait(2)
            return super().upload(**kwargs)

    path = tmp_path / "public.mp4"
    path.write_bytes(b"public")
    started, release = Event(), Event()
    client = SlowClient()
    transport = GoogleVideoTransport(client)
    settings = GeminiSettings(model="gemini-3.8-flash")
    mismatch = transport.preupload(PublicResearchVideo(path, True), sha256_hex(b"different"), settings)
    mismatch.wait()
    assert client.uploaded is None
    prepared = transport.preupload(PublicResearchVideo(path, True), sha256_hex(b"public"), settings)
    assert started.wait(1)
    prepared.close()
    release.set()
    prepared.wait()
    assert client.deleted == ["files/synthetic"]


def test_preupload_snapshot_satisfies_installed_sdk_stream_contract(tmp_path: Path) -> None:
    from google.genai import _extra_utils

    from incident_report.hashing import sha256_hex

    class SDKValidatingClient(FakeClient):
        vertexai = False

        def upload(self, *, file: Any, config: dict[str, Any]) -> SimpleNamespace:
            _, size, mime = _extra_utils.prepare_resumable_upload(file, user_mime_type=config["mime_type"])
            assert size == 6 and mime == "video/mp4"
            return super().upload(file=file, config=config)

    path = tmp_path / "public.mp4"
    path.write_bytes(b"public")
    client = SDKValidatingClient()
    transport = GoogleVideoTransport(client)
    prepared = transport.preupload(
        PublicResearchVideo(path, True), sha256_hex(b"public"), GeminiSettings(model="gemini-3.8-flash")
    )
    prepared.wait()
    try:
        assert prepared.ready(transport, sha256_hex(b"public")) is not None
        assert client.uploaded == b"public"
    finally:
        prepared.close()
