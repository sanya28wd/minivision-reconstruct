"""Bounded video streaming from report-owned evidence, through the existing access gate."""

import re
import uuid
from collections.abc import Iterator
from typing import BinaryIO

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from incident_report.domain import Run, RunStatus
from incident_report.runlog import RunLog
from incident_report.storage.errors import StorageError
from incident_report.storage.interface import EvidenceStore, validate_sha256_key


def accessible_report(log: RunLog, request: Request, run_id: uuid.UUID) -> Run:
    try:
        run = log.get(run_id)
    except KeyError:
        raise HTTPException(404, "No report with that id.") from None
    if run.status is not RunStatus.SUCCEEDED or run.report is None:
        raise HTTPException(404, "No report with that id.")
    request.state.run_id = run.run_id
    return run


def _chunks(stream: BinaryIO, remaining: int) -> Iterator[bytes]:
    try:
        while remaining:
            chunk = stream.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        stream.close()


class VideoResponse(StreamingResponse):
    """Own the evidence stream for the complete HTTP response lifecycle."""

    def __init__(
        self, stream: BinaryIO, remaining: int, *, status_code: int, media_type: str, headers: dict[str, str]
    ) -> None:
        self.evidence_stream = stream
        super().__init__(_chunks(stream, remaining), status_code=status_code, media_type=media_type, headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.evidence_stream.close()


def install_review(app: FastAPI, log: RunLog, evidence: EvidenceStore | None) -> None:
    @app.get("/reports/{run_id}/views/{view_id}")
    def video(request: Request, run_id: uuid.UUID, view_id: str) -> Response:
        run = accessible_report(log, request, run_id)
        if run.report is None:
            raise HTTPException(404, "No report with that id.")
        try:
            key = validate_sha256_key(view_id)
            if evidence is None or not any(view.sha256 == key for view in run.report.views):
                raise HTTPException(404, "Video not found.")
            stream = evidence.get(key)
        except (StorageError, OSError):
            raise HTTPException(404, "Video not found.") from None
        try:
            header = stream.read(16)
            # Sniff stored bytes; filename and browser-supplied MIME are never trusted.
            if header[4:8] == b"ftyp":
                mime = "video/mp4"
            elif header.startswith(b"\x1a\x45\xdf\xa3"):
                mime = "video/webm"
            elif header.startswith(b"RIFF") and header[8:12] == b"AVI ":
                mime = "video/x-msvideo"
            else:
                raise HTTPException(415, "This video format cannot be played here.")
            size = stream.seek(0, 2)
            start, end = 0, size - 1
            range_header = request.headers.get("range")
            if range_header:
                match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", range_header)
                if match is None or not any(match.groups()):
                    raise ValueError
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    suffix = int(last)
                    if suffix == 0:
                        raise ValueError
                    start = max(0, size - suffix)
                if start > end or start >= size:
                    raise ValueError
            stream.seek(start)
        except ValueError:
            stream.close()
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
        except BaseException:
            stream.close()
            raise
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "X-Content-Type-Options": "nosniff",
        }
        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return VideoResponse(
            stream, end - start + 1, status_code=206 if range_header else 200, media_type=mime, headers=headers
        )
