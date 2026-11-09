"""Per-request disk staging, with body caps and cancellation cleanup.

Starlette 1.7.0 formparsers.py on_headers_finished/parse and datastructures.py UploadFile
were inspected: callbacks queue writes, parse closes its files on BaseException,
and UploadFile treats a plain file as disk-backed. No parser globals are modified.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from typing import Any

from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request

from incident_report.domain import InvalidIncident
from incident_report.intake_limits import IntakeLimits
from incident_report.storage import EvidenceStore


class UploadFileTooLarge(MultiPartException):
    """Raised when an individual file part exceeds max_upload_bytes."""


class UploadTooLarge(InvalidIncident):
    """Raised when an upload exceeds configured size limits."""


class InvalidUploadForm(InvalidIncident):
    """Raised when an upload violates multipart form limits (max_files, max_fields, duplicate keys)."""


@asynccontextmanager
async def staged_form(request: Request, evidence: EvidenceStore, limits: IntakeLimits) -> AsyncIterator[FormData]:
    async def limited_body() -> AsyncGenerator[bytes, None]:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limits.max_total_upload_bytes + limits.max_form_bytes:
                raise UploadTooLarge("The upload exceeds the configured size limit.")
            yield chunk

    with ExitStack() as stack:
        # Pinned starlette<1.8 in pyproject.toml: this relies on the private MultiPartParser._current_part.
        class DiskParser(MultiPartParser):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self._current_file_size = 0

            def on_headers_finished(self) -> None:
                super().on_headers_finished()
                self._current_file_size = 0
                upload = self._current_part.file
                if upload is not None:
                    # The superclass allocated an EMPTY spool, before any file bytes arrived.
                    upload.file.close()
                    upload.file = stack.enter_context(evidence.stage())

            def on_part_data(self, data: bytes, start: int, end: int) -> None:
                super().on_part_data(data, start, end)
                if self._current_part.file is not None:
                    self._current_file_size += end - start
                    if self._current_file_size > limits.max_upload_bytes:
                        raise UploadFileTooLarge("The upload exceeds the configured size limit.")

        parser = DiskParser(
            request.headers,
            limited_body(),
            max_files=limits.max_files,
            max_fields=limits.max_fields,
            max_part_size=limits.max_form_bytes,
        )
        try:
            async with asyncio.timeout(limits.upload_timeout_seconds):
                form = await parser.parse()
        except TimeoutError:
            raise InvalidIncident("The upload stage timed out.") from None
        except UploadFileTooLarge as exc:
            raise UploadTooLarge(str(exc)) from None
        except MultiPartException:
            raise InvalidUploadForm("The upload form is invalid or exceeds the configured limits.") from None
        try:
            text_keys = [k for k, v in form.multi_items() if not hasattr(v, "file")]
            if len(text_keys) != len(set(text_keys)):
                raise InvalidUploadForm("Send each upload field only once.")
            yield form
        finally:
            await form.close()
