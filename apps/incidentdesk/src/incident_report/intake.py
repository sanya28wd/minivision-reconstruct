"""Hash to external staging before examining content; keep originals write-once."""

from __future__ import annotations

import functools
import json
import math
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from incident_report.config import Environment, _check_storage_backend, _environment
from incident_report.domain import EvidenceCopy, InvalidIncident, ReportView, View
from incident_report.hashing import hash_stream
from incident_report.intake_limits import IntakeLimits
from incident_report.stages import run_stage
from incident_report.storage import EvidenceStore, LocalEvidenceStore, StorageError

# ffprobe options: https://ffmpeg.org/ffprobe.html#Main-options
# fd confines input to inherited stdin and preserves seeking for MP4s with moov at the end.
# https://ffmpeg.org/ffmpeg-protocols.html#fd ; no user text is a command argument.
PROBE_COMMAND = (
    "ffprobe",
    "-v",
    "error",
    "-protocol_whitelist",
    "fd",
    "-format_whitelist",
    "mov,matroska,webm,avi",
    "-show_format",
    "-show_streams",
    "-of",
    "json",
    "-i",
    "fd:",
)


def configured_store() -> EvidenceStore:
    environment = _environment(os.environ.get("INCIDENT_ENV", ""))
    directory = os.environ.get("INCIDENT_EVIDENCE_DIR", "").strip()
    _check_storage_backend(os.environ.get("INCIDENT_STORAGE_BACKEND", ""), bool(directory), environment)
    if not directory or environment is not Environment.DEV:
        raise InvalidIncident("Evidence storage is not configured. Ask the owner to configure it.")
    return LocalEvidenceStore(Path(directory))


def _hash_to_deadline(stream: BinaryIO, deadline: float) -> tuple[str, int]:
    return hash_stream(stream, deadline=deadline)


def _mime(head: bytes) -> str:
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/quicktime" if head[8:12] == b"qt  " else "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/x-matroska"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "video/x-msvideo"
    raise InvalidIncident("Choose a supported video container (MP4, MOV, WebM, Matroska or AVI).")


def _probe(
    staged: BinaryIO,
    evidence: EvidenceStore,
    limits: IntakeLimits,
    parent_sha256: str,
    on_output: Callable[[str | None, str | None, EvidenceCopy, EvidenceCopy], None],
    *,
    persist: bool = True,
) -> tuple[str, str, float, str]:
    staged.seek(0)
    with evidence.stage() as output, evidence.stage() as errors:
        failure = None
        returncode = -1
        try:
            result = subprocess.run(  # noqa: S603  fixed executable/options; input bytes only on stdin
                PROBE_COMMAND,
                stdin=staged,
                stdout=output,
                stderr=errors,
                timeout=limits.probe_timeout_seconds,
                check=False,
            )
            returncode = result.returncode
        except (OSError, subprocess.TimeoutExpired):
            failure = "Video metadata could not be read before the ffprobe timeout. Ask the owner to check ffprobe."
        copies = []
        sizes = []
        texts: list[str | None] = []
        for stream in (output, errors):
            size = stream.seek(0, os.SEEK_END)
            sizes.append(size)
            stream.seek(0)
            key, _ = run_stage(
                "probe-output",
                limits.copy_timeout_seconds,
                limits.stage_workers,
                functools.partial(_hash_to_deadline, stream),
            )
            copies.append(EvidenceCopy(sha256=key, parent_sha256=parent_sha256))
            stream.seek(0)
            try:
                texts.append(
                    stream.read(limits.max_probe_bytes).decode("utf-8") if size <= limits.max_probe_bytes else None
                )
            except UnicodeDecodeError:
                texts.append(None)
        # Preserve bounded text in PostgreSQL before attempting external artifact storage.
        on_output(texts[0], texts[1], copies[0], copies[1])
        if persist:
            for stream, copy in zip((output, errors), copies, strict=True):
                stream.seek(0)
                _store(stream, copy.sha256, evidence, limits)
        if any(size > limits.max_probe_bytes for size in sizes):
            raise InvalidIncident("The ffprobe metadata exceeds the configured size limit; raw output was retained.")
        # Decode only bounded metadata, never video. Preserve stdout/stderr including newlines.
        try:
            stdout, stderr = texts
            if stdout is None or stderr is None:
                raise ValueError
            if failure is not None:
                raise InvalidIncident(failure)
            data = json.loads(stdout)
            if returncode or not any(
                s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
                for s in data["streams"]
            ):
                raise ValueError
            duration = float(data["format"]["duration"])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError
            format_name = str(data["format"]["format_name"])
        except InvalidIncident:
            raise
        except (KeyError, TypeError, ValueError):
            raise InvalidIncident("The video is corrupt, has no video stream, or has no measurable duration.") from None
    return stdout, stderr, duration, format_name


def receive_view(
    view: View,
    evidence: EvidenceStore,
    *,
    session_id: str,
    limits: IntakeLimits,
    record: Callable[[ReportView], None],
) -> ReportView:
    uploaded_at = datetime.now(UTC)
    with evidence.stage() as staged:
        try:
            sha256, size = run_stage(
                "hash",
                limits.hash_timeout_seconds,
                limits.stage_workers,
                lambda deadline: hash_stream(
                    view.stream, destination=staged, max_bytes=limits.max_upload_bytes, deadline=deadline
                ),
            )
        except (TimeoutError, ValueError) as exc:
            raise InvalidIncident(str(exc)) from None
        metadata = ReportView(
            filename=view.filename,
            role=view.role,
            label=view.label,
            acquisition_method=view.acquisition_method,
            sha256=sha256,
            uploaded_at=uploaded_at,
            session_id=session_id,
            size_bytes=size,
        )

        record(metadata)

        def save_output(
            stdout: str | None, stderr: str | None, stdout_object: EvidenceCopy, stderr_object: EvidenceCopy
        ) -> None:
            nonlocal metadata
            metadata = metadata.model_copy(
                update={
                    "ffprobe_stdout": stdout,
                    "ffprobe_stderr": stderr,
                    "ffprobe_stdout_object": stdout_object,
                    "ffprobe_stderr_object": stderr_object,
                }
            )
            record(metadata)

        try:
            staged.seek(0)
            mime = _mime(staged.read(32))
            metadata = metadata.model_copy(update={"mime_type": mime})
            stdout, stderr, duration, format_name = _probe(staged, evidence, limits, sha256, save_output)
            if mime == "video/x-matroska" and "webm" in format_name:
                staged.seek(0)
                if b"webm" in staged.read(4096):
                    mime = "video/webm"
            metadata = metadata.model_copy(update={"mime_type": mime, "duration_seconds": duration})
            if duration > limits.max_duration_seconds:
                raise InvalidIncident("The video exceeds the configured duration limit.")
            record(metadata)  # durable before publication, including an OS call that finishes after timeout
            staged.seek(0)
            _store(staged, sha256, evidence, limits)
            # A byte-for-byte working copy is independently streamed and verified. Content-addressed
            # storage deduplicates identical bytes; provenance is a separate parent-linked reference.
            with evidence.stage() as working:
                staged.seek(0)
                try:
                    working_hash, _ = run_stage(
                        "copy",
                        limits.copy_timeout_seconds,
                        limits.stage_workers,
                        lambda deadline: hash_stream(staged, destination=working, deadline=deadline),
                    )
                except TimeoutError as exc:
                    raise InvalidIncident(str(exc)) from None
                working.seek(0)
                _store(working, working_hash, evidence, limits)
            metadata = metadata.model_copy(
                update={
                    "working_copy": EvidenceCopy(sha256=working_hash, parent_sha256=sha256),
                }
            )
        except InvalidIncident as exc:
            record(metadata.model_copy(update={"validation_error": str(exc)}))
            raise
        except (StorageError, OSError):
            record(metadata.model_copy(update={"validation_error": "Evidence storage was unavailable."}))
            raise
        record(metadata)
        return metadata


def _store(stream: BinaryIO, sha256: str, evidence: EvidenceStore, limits: IntakeLimits) -> None:
    run_stage(
        "storage",
        limits.store_timeout_seconds,
        limits.stage_workers,
        lambda deadline: evidence.put(stream, sha256=sha256, deadline=deadline),
    )


def validate_staged_video(stream: BinaryIO, evidence: EvidenceStore, limits: IntakeLimits, sha256: str) -> None:
    """Apply normal content/probe limits without publishing bytes or audit records."""
    stream.seek(0)
    _mime(stream.read(32))
    _, _, duration, _ = _probe(stream, evidence, limits, sha256, lambda *_: None, persist=False)
    if duration > limits.max_duration_seconds:
        raise InvalidIncident("The video exceeds the configured duration limit.")
    stream.seek(0)
