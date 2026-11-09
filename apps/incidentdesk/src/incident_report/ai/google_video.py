"""Official Google Gen AI SDK transport. Construction never sends a model request.

Sources and verification caveats: docs/research/RPT07_GEMINI_SDK.md.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import warnings
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Any, BinaryIO, cast

from incident_report.ai.dense import DenseSettings
from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import (
    COLLISION_PROMPT,
    DRIVER_PROMPT,
    DROWSINESS_PROMPT,
    LOCATION_PROMPT,
    PROMPT,
    REFERRAL_PROMPT,
    GeminiSettings,
    PublicResearchVideo,
    TransportError,
)
from incident_report.ai.provider_error import sanitize_provider_error
from incident_report.facts import (
    LEGACY_OVERVIEW_SCHEMA,
    OVERVIEW_3_SCHEMA,
    OVERVIEW_4_SCHEMA,
    OVERVIEW_5_SCHEMA,
    OVERVIEW_6_SCHEMA,
    OVERVIEW_7_SCHEMA,
    OVERVIEW_SCHEMA,
)
from incident_report.hashing import hash_stream
from incident_report.timings import capture_timings, stage_timer, timed_call

if TYPE_CHECKING:
    from google.genai.types import HttpOptionsDict

# Stricter Gemini prose (despite its <100MB table); also Vertex Flex PayGo's body cap.
# https://ai.google.dev/gemini-api/docs/video-understanding#inline-video
# https://docs.cloud.google.com/vertex-ai/generative-ai/docs/flex-paygo#request-payload-limit
# Use decimal MB conservatively for both backends; transport never selects the backend.
INLINE_REQUEST_LIMIT_BYTES = 20_000_000
# Canonical Google RPC statuses: https://cloud.google.com/apis/design/errors#handling_errors
_PROVIDER_STATUSES = frozenset(
    {
        "OK",
        "CANCELLED",
        "UNKNOWN",
        "INVALID_ARGUMENT",
        "DEADLINE_EXCEEDED",
        "NOT_FOUND",
        "ALREADY_EXISTS",
        "PERMISSION_DENIED",
        "UNAUTHENTICATED",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "ABORTED",
        "OUT_OF_RANGE",
        "UNIMPLEMENTED",
        "INTERNAL",
        "UNAVAILABLE",
        "DATA_LOSS",
    }
)


def counted_inline_request_bytes(video_bytes: int, prompt: str, model: str, video_metadata: dict[str, object]) -> int:
    """Conservative JSON body count, including every base64 copy, config/schema and 4 KiB envelope margin.

    Empty data placeholders avoid allocating/encoding video for the size decision. ASCII-escaped JSON
    conservatively counts Unicode and quotes. The margin covers SDK wire naming/envelope differences;
    the SDK wire tests also check that this count exceeds the actual HTTP body.
    """
    contents = _contents(b"", prompt, video_metadata)
    copies = 0
    for part in contents:
        if "inline_data" in part:
            part["inline_data"] = {"data": "", "mime_type": "video/mp4"}
            copies += 1
    body = {"model": model, "contents": contents, "config": _generate_config(prompt)}
    return len(json.dumps(body, ensure_ascii=True).encode("utf-8")) + copies * (4 * ((video_bytes + 2) // 3)) + 4096


def sdk_http_options(settings: GeminiSettings | DenseSettings) -> HttpOptionsDict:
    # HttpOptions.timeout is milliseconds; retry_options.attempts includes the original request, so 1
    # disables the SDK's hidden retries and every 429 reaches the counted retry loop in gemini.py.
    # Verified against google-genai 2.31.0: types.HttpOptions, types.HttpRetryOptions, _api_client.retry_args.
    return {"timeout": settings.http_timeout_ms, "retry_options": {"attempts": 1}}


def _generate_config(prompt: str) -> dict[str, Any]:
    """The generate config for a prompt.

    overview-10 prompts carry their own system instruction and schema (chosen by prompt version in
    ai.overview10). Every older prompt keeps the exact startswith choice below, so older behaviour is unchanged.
    """
    schema = getattr(prompt, "response_schema", None)
    if schema is not None:
        config: dict[str, Any] = {
            "response_mime_type": "application/json",
            "system_instruction": getattr(prompt, "system_instruction", ""),
            "response_schema": schema,
        }
        level = getattr(prompt, "thinking_level", None)
        if level is not None:
            # overview-11 sets its thinking level explicitly; every older version sends none, exactly as before.
            config["thinking_config"] = {"thinking_level": level}
        return config
    return {
        "response_mime_type": "application/json",
        "response_schema": (
            OVERVIEW_7_SCHEMA
            if prompt.startswith(DROWSINESS_PROMPT)
            else OVERVIEW_6_SCHEMA
            if prompt.startswith(REFERRAL_PROMPT)
            else OVERVIEW_5_SCHEMA
            if prompt.startswith(COLLISION_PROMPT)
            else OVERVIEW_4_SCHEMA
            if prompt.startswith(DRIVER_PROMPT)
            else OVERVIEW_3_SCHEMA
            if prompt.startswith(LOCATION_PROMPT)
            else OVERVIEW_SCHEMA
            if prompt.startswith(PROMPT)
            else LEGACY_OVERVIEW_SCHEMA
        ),
    }


def _contents(video: str | bytes, prompt: str, video_metadata: dict[str, object]) -> list[dict[str, Any]]:
    """The request contents. An overview-11 prompt lists its video parts: each is a text line naming the part, then the
    same uploaded file with that part's own video_metadata (fps, and start/end offsets for the detail window), and the
    user text comes last. Every other prompt is one video part and the text, as before."""
    media = (
        {"inline_data": {"data": video, "mime_type": "video/mp4"}}
        if isinstance(video, bytes)
        else {"file_data": {"file_uri": video, "mime_type": "video/mp4"}}
    )
    parts = getattr(prompt, "video_parts", None)
    if parts is None:
        return [{**media, "video_metadata": video_metadata}, {"text": str(prompt)}]
    contents: list[dict[str, Any]] = []
    for part in parts:
        contents.append({"text": part.intro})
        piece: dict[str, Any] = {**media, "video_metadata": part.metadata()}
        if part.media_resolution == "high":
            # google.genai.types.Part.media_resolution (PartMediaResolution.level); per-part, Gemini 3/4 models.
            # https://ai.google.dev/gemini-api/docs/media-resolution
            piece["media_resolution"] = {"level": "MEDIA_RESOLUTION_HIGH"}
        contents.append(piece)
    contents.append({"text": str(prompt)})
    return contents


_logger = logging.getLogger(__name__)


_report_files: ContextVar[dict[int, _ReportFiles] | None] = ContextVar("report_files", default=None)


class _ReportFiles:
    """One Report's files; a late upload after timeout is deleted by its worker."""

    def __init__(self, transport: GoogleVideoTransport) -> None:
        self.transport = transport
        self.lock = Lock()
        self.closed = False
        self.names: set[str] = set()
        self.files: dict[str, Future[Any]] = {}
        self.active_files: dict[str, Future[Any]] = {}

    def register(self, name: str) -> None:
        with self.lock:
            closed = self.closed
            if not closed:
                self.names.add(name)
        if closed:
            self.transport._delete(name)
            raise TransportError()

    def check_open(self) -> None:
        with self.lock:
            if self.closed:
                raise TransportError()

    def _once(self, cache: dict[str, Future[Any]], key: str, operation: Callable[[], Any]) -> Any:
        with self.lock:
            if self.closed:
                raise TransportError()
            owner = key not in cache
            future = cache.setdefault(key, Future())
        if owner:
            try:
                future.set_result(operation())
            except Exception as exc:
                future.set_exception(exc)
                # Existing waiters see this failure; a subsequent counted retry can recover.
                with self.lock:
                    if cache.get(key) is future:
                        del cache[key]
        result = future.result()
        self.check_open()
        return result

    def get(self, video: PublicResearchVideo, timeout: float, poll: float) -> Any:
        key = str(video.path.resolve())
        uploaded = self._once(
            self.files, key, lambda: self.transport._upload_file(video, self.register, self.check_open)
        )
        return self._once(
            self.active_files,
            key,
            lambda: self.transport._wait_for_active(uploaded, timeout, poll, self.check_open),
        )

    def close(self) -> None:
        with self.lock:
            self.closed = True
            names = tuple(self.names)
            self.names.clear()
        for name in names:
            self.transport._delete(name)


_preuploaded: ContextVar[tuple[PreparedVideo, ...]] = ContextVar("preuploaded", default=())


class PreparedVideo:
    """A cancellable upload lease. Only ACTIVE files from this transport may be borrowed."""

    def __init__(
        self,
        transport: GoogleVideoTransport,
        video: PublicResearchVideo,
        sha256: str,
        settings: GeminiSettings,
        on_done: Callable[[], None] = lambda: None,
    ) -> None:
        self.transport, self.sha256 = transport, sha256
        self.session = _ReportFiles(transport)
        self.done = Event()
        self.active: Any = None
        self.timings: dict[str, float] = {}

        def prepare() -> None:
            try:
                with capture_timings() as timings:
                    self._prepare_snapshot(transport, video, sha256, settings)
                self.timings = dict(timings)
            except Exception as exc:
                error = sanitize_provider_error(exc)
                _logger.warning(
                    "Optional video pre-upload failed: status=%s code=%s",
                    error.status if error.status in _PROVIDER_STATUSES else None,
                    getattr(exc, "code", None) if isinstance(getattr(exc, "code", None), int) else None,
                )
                self.session.close()
            finally:
                self.done.set()
                on_done()

        Thread(target=prepare, name="video-preupload", daemon=True).start()

    def _prepare_snapshot(
        self, transport: GoogleVideoTransport, video: PublicResearchVideo, sha256: str, settings: GeminiSettings
    ) -> None:
        # Hash and send the same private snapshot; never reopen a mutable path
        # after accepting its hash. No upload of mismatched declared bytes.
        with tempfile.TemporaryFile(mode="w+b", buffering=0) as snapshot:
            with video.path.open("rb") as stream:
                digest, _ = hash_stream(stream, destination=cast(BinaryIO, snapshot))
            if digest != sha256:
                return
            snapshot.seek(0)
            uploaded = transport._upload_stream(
                getattr(snapshot, "file", snapshot), self.session.register, self.session.check_open
            )
            self.active = transport._wait_for_active(
                uploaded, settings.processing_timeout_seconds, settings.poll_seconds, self.session.check_open
            )

    def wait(self) -> None:
        """Wait for preparation (offline verification/benchmark harness only)."""
        self.done.wait()

    def ready(self, transport: GoogleVideoTransport, sha256: str) -> Any:
        if transport is not self.transport or sha256 != self.sha256 or not self.done.is_set():
            return None
        with self.session.lock:
            if self.session.closed or self.active is None:
                return None
            return self.active if self.active.state and self.active.state.name == "ACTIVE" else None

    def close(self) -> None:
        self.session.close()


class GoogleVideoTransport:
    def __init__(
        self,
        client: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        inline_enabled: bool | None = None,
    ) -> None:
        self.client = client
        self.sleep = sleep
        self.clock = clock
        self.inline_enabled = (
            os.environ.get("GEMINI_VIDEO_INLINE", "1").strip().lower() not in {"0", "false", "no", "off"}
            if inline_enabled is None
            else inline_enabled
        )

    @classmethod
    def from_env(cls, settings: GeminiSettings | DenseSettings) -> GoogleVideoTransport:
        # Only environment credentials; never serialize Client, requests, headers or provider exceptions.
        key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise AIConnectionError(
                "Set GEMINI_API_KEY or GOOGLE_API_KEY in the environment before the owner smoke call."
            )
        try:
            from google import genai
        except ImportError:
            raise AIConnectionError(
                "The official google-genai SDK is not installed. See the RPT-07 dependency blocker."
            ) from None
        try:
            client = genai.Client(api_key=key, vertexai=False, http_options=sdk_http_options(settings))
        except Exception:
            raise AIConnectionError("The Gemini API client could not be configured.") from None
        return cls(client)

    def preupload(
        self,
        video: PublicResearchVideo,
        sha256: str,
        settings: GeminiSettings,
        *,
        on_done: Callable[[], None] = lambda: None,
    ) -> PreparedVideo:
        # Files is Developer API only. Unknown/Vertex clients must never enter this path.
        if video.public_research is not True or getattr(self.client, "vertexai", None) is not False:
            raise AIConnectionError("Provider pre-upload is unavailable for this backend.")
        return PreparedVideo(self, video, sha256, settings, on_done)

    @contextmanager
    def preuploaded_scope(self, prepared: list[PreparedVideo]) -> Iterator[None]:
        token = _preuploaded.set(tuple(prepared))
        try:
            yield
        finally:
            _preuploaded.reset(token)

    def close(self) -> None:
        self.client.close()

    def _generate_with_metadata(
        self,
        video: PublicResearchVideo,
        prompt: str,
        model: str,
        processing_timeout_seconds: float,
        poll_seconds: float,
        video_metadata: dict[str, object],
    ) -> str:
        session = (_report_files.get() or {}).get(id(self))
        name = None
        inline: bytes | None = None
        try:
            if session is not None:
                session.check_open()
            media: str | bytes
            prepared_file = None
            if _preuploaded.get():
                with video.path.open("rb") as stream:
                    digest = hash_stream(stream)[0]
                for prepared in _preuploaded.get():
                    prepared_file = prepared.ready(self, digest)
                    if prepared_file is not None:
                        break
            if (
                prepared_file is None
                and self.inline_enabled
                and counted_inline_request_bytes(video.path.stat().st_size, prompt, model, video_metadata)
                < INLINE_REQUEST_LIMIT_BYTES
            ):
                inline = video.path.read_bytes()
                # Recheck the bytes actually read if a local file changed after stat.
                if (
                    counted_inline_request_bytes(len(inline), prompt, model, video_metadata)
                    >= INLINE_REQUEST_LIMIT_BYTES
                ):
                    inline = None
            if prepared_file is not None:
                media = prepared_file.uri
            elif inline is not None:
                media = inline
                if session is not None:
                    session.check_open()
            elif session is not None:
                uploaded = session.get(video, processing_timeout_seconds, poll_seconds)
                media = uploaded.uri
            else:

                def remember(uploaded_name: str) -> None:
                    nonlocal name
                    name = uploaded_name

                uploaded = self._upload(video, processing_timeout_seconds, poll_seconds, remember)
                media = uploaded.uri
            # Part/VideoMetadata and GenerateContentConfig accept these documented dictionary forms.
            response = timed_call(
                getattr(prompt, "generate_stage", "generate"),
                self.client.models.generate_content,
                model=model,
                contents=_contents(media, prompt, video_metadata),
                config=_generate_config(prompt),
            )
            # Preserve the complete response body (candidates, usage, model version), excluding HTTP metadata.
            raw: str = response.model_dump_json(exclude={"sdk_http_response"})
            return raw
        except TransportError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            # Only the sanitized fields leave this block; the exception itself (headers, URLs, key) is dropped.
            error = sanitize_provider_error(exc)
            if inline is not None:
                # Provider text can echo arbitrary fragments of inline data (raw or base64). Retain
                # only safe status fields instead of trying to recognize every encoding/fragment.
                error = error.model_copy(
                    update={"message": None, "status": error.status if error.status in _PROVIDER_STATUSES else None}
                )
            raise TransportError(code if isinstance(code, int) else None, error) from None
        finally:
            if name:
                self._delete(name)

    def _delete(self, name: str) -> None:
        try:
            self.client.files.delete(name=name)
        except Exception:
            warnings.warn(
                "Gemini temporary-file cleanup failed; the owner should check Files API storage.",
                RuntimeWarning,
                stacklevel=2,
            )

    @contextmanager
    def report_scope(self) -> Iterator[None]:
        session = _ReportFiles(self)
        token = _report_files.set({**(_report_files.get() or {}), id(self): session})
        try:
            yield
        finally:
            _report_files.reset(token)
            session.close()

    def _upload(
        self,
        video: PublicResearchVideo,
        timeout: float,
        poll: float,
        register: Callable[[str], None],
        check_open: Callable[[], None] = lambda: None,
    ) -> Any:
        uploaded = self._upload_file(video, register, check_open)
        return self._wait_for_active(uploaded, timeout, poll, check_open)

    def _upload_file(
        self, video: PublicResearchVideo, register: Callable[[str], None], check_open: Callable[[], None]
    ) -> Any:
        check_open()
        with video.path.open("rb") as stream:
            return self._upload_stream(stream, register, check_open)

    def _upload_stream(self, stream: Any, register: Callable[[str], None], check_open: Callable[[], None]) -> Any:
        check_open()
        uploaded = timed_call("upload", self.client.files.upload, file=stream, config={"mime_type": "video/mp4"})
        if not uploaded.name:
            raise TransportError()
        register(uploaded.name)
        return uploaded

    def _wait_for_active(self, uploaded: Any, timeout: float, poll: float, check_open: Callable[[], None]) -> Any:
        name = uploaded.name
        with stage_timer("processing_wait"):
            deadline = self.clock() + timeout
            while not uploaded.state or uploaded.state.name != "ACTIVE":
                if uploaded.state and uploaded.state.name == "FAILED":
                    raise TransportError()
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TransportError()
                self.sleep(min(poll, remaining))
                check_open()
                uploaded = self.client.files.get(name=name)
        check_open()
        if not uploaded.uri:
            raise TransportError()
        return uploaded

    def generate(self, video: PublicResearchVideo, prompt: str, settings: GeminiSettings) -> str:
        return self._generate_with_metadata(
            video,
            prompt,
            settings.model,
            settings.processing_timeout_seconds,
            settings.poll_seconds,
            {"fps": settings.fps},
        )

    def generate_window(
        self,
        video: PublicResearchVideo,
        prompt: str,
        settings: DenseSettings,
        *,
        start_offset_seconds: float,
        end_offset_seconds: float,
    ) -> str:
        # ADR 0008, item 5: VideoMetadata start_offset and end_offset supported by official SDK.
        video_metadata: dict[str, object] = {
            "fps": settings.fps,
            "start_offset": f"{start_offset_seconds:.3f}s",
            "end_offset": f"{end_offset_seconds:.3f}s",
        }
        return self._generate_with_metadata(
            video,
            prompt,
            settings.model,
            settings.processing_timeout_seconds,
            settings.poll_seconds,
            video_metadata,
        )
