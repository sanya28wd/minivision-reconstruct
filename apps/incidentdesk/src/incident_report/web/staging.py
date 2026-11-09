"""Owner-bound temporary uploads. No evidence publication until normal intake at submit."""

from __future__ import annotations

import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import BinaryIO, cast

from incident_report.ai.google_video import PreparedVideo
from incident_report.domain import InvalidIncident
from incident_report.hashing import hash_stream
from incident_report.intake import validate_staged_video
from incident_report.intake_limits import IntakeLimits
from incident_report.storage import EvidenceStore


class StagingUnavailable(InvalidIncident):
    """A retry with retained browser bytes is safe: no intake has started."""


@dataclass
class StagedUpload:
    owner: str
    filename: str
    sha256: str
    size: int
    expires_at: float
    prepared: PreparedVideo | None = None
    timer: threading.Timer | None = None
    handle: BinaryIO | None = field(default=None, repr=False)

    def detach_ready_preparation(self) -> PreparedVideo | None:
        lease = self.prepared
        if lease is not None and lease.ready(lease.transport, self.sha256) is not None:
            self.prepared = None
            return lease
        if lease is not None:
            lease.close()
            self.prepared = None
        return None

    def close(self) -> None:
        if self.timer:
            self.timer.cancel()
        if self.prepared:
            self.prepared.close()
            self.prepared = None
        if self.handle:
            self.handle.close()
            self.handle = None


class UploadStages:
    """Process-local capability registry with OS delete-on-close temporary files.

    This intentionally fails closed across workers/restarts: an unknown id falls back
    to the browser's retained bytes. TTL is an engineering assumption, not a legal limit.
    """

    def __init__(
        self,
        evidence: EvidenceStore,
        limits: IntakeLimits,
        prepare: Callable[[str], PreparedVideo | None] = lambda _: None,
    ) -> None:
        self.evidence, self.limits = evidence, limits
        self.prepare = prepare
        self._items: dict[str, StagedUpload] = {}
        self._lock = threading.RLock()

    def add(self, stream: BinaryIO, *, owner: str, filename: str) -> str:
        # OS temporary file: no application-addressable staging pathname is retained.
        # The provider reads an independently verified snapshot of the declared public path.
        handle = cast(BinaryIO, tempfile.TemporaryFile(mode="w+b", buffering=0))
        try:
            digest, size = hash_stream(
                stream,
                destination=handle,
                max_bytes=self.limits.max_upload_bytes,
                deadline=time.monotonic() + self.limits.hash_timeout_seconds,
            )
            handle.flush()
            validate_staged_video(handle, self.evidence, self.limits, digest)
            with self._lock:
                owned = [s for s in self._items.values() if s.owner == owner]
                if (
                    len(owned) >= self.limits.max_files
                    or sum(s.size for s in owned) + size > self.limits.max_total_upload_bytes
                    or len(self._items) >= self.limits.max_files * self.limits.stage_workers
                ):
                    raise InvalidIncident("Temporary upload capacity reached. Submit normally or try later.")
                token = secrets.token_urlsafe(32)
                staged = StagedUpload(
                    owner,
                    filename,
                    digest,
                    size,
                    time.monotonic() + self.limits.staging_ttl_seconds,
                    handle=handle,
                )
                staged.prepared = self.prepare(digest)
                timer = threading.Timer(self.limits.staging_ttl_seconds, self.discard, args=(token,))
                timer.daemon = True
                staged.timer = timer
                self._items[token] = staged
                timer.start()
                return token
        except BaseException:
            handle.close()
            raise

    def discard(self, token: str) -> None:
        with self._lock:
            staged = self._items.pop(token, None)
        if staged:
            staged.close()

    @contextmanager
    def consume(self, tokens: list[str], owner: str) -> Iterator[list[StagedUpload]]:
        with self._lock:
            if len(tokens) != len(set(tokens)):
                raise InvalidIncident("Send each temporary upload only once.")
            stages = [self._items.get(token) for token in tokens]
            if any(s is None or s.owner != owner or s.expires_at <= time.monotonic() for s in stages):
                raise StagingUnavailable("Temporary upload unavailable. Select the files again or submit normally.")
            accepted = [s for s in stages if s is not None]
            for token in tokens:
                del self._items[token]
            for staged in accepted:
                if staged.timer:
                    staged.timer.cancel()
                if staged.handle is None:
                    raise InvalidIncident("Temporary upload is closed.")
                staged.handle.seek(0)
        try:
            yield accepted
        finally:
            for staged in accepted:
                staged.close()

    def close(self) -> None:
        with self._lock:
            tokens = list(self._items)
        for token in tokens:
            self.discard(token)
