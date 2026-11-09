"""The evidence storage seam (ADR 0011; spec 001, "Storage and integrity").

Evidence is original videos, working copies and Feedback screenshots. Rules every backend keeps:
- Objects are keyed by the lowercase hex SHA-256 of their bytes.
- `put` verifies that hash WHILE streaming (a whole video is never held in memory) and stores nothing
  when it does not match.
- Write-once: a stored object is never modified, replaced or deleted through this interface.
  Putting identical content again is a no-op that reports `created=False`.
- `get` returns a binary stream; the caller closes it.

Backends: `LocalEvidenceStore` (local.py; development and tests only). A Cloud Storage adapter (bucket with
object retention/versioning) is PENDING-OWNER: it needs a bucket and credentials that agents never hold.
It must implement this protocol and pass the same tests in tests/test_evidence_storage.py.
"""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from incident_report.storage.errors import InvalidEvidenceKey

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def validate_sha256_key(key: str) -> str:
    """Return the key if it is a lowercase hex SHA-256, else raise. Call before building any path or URL."""
    if not isinstance(key, str) or _SHA256_HEX.fullmatch(key) is None:
        raise InvalidEvidenceKey("An evidence key must be 64 lowercase hex characters (a SHA-256).")
    return key


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    size: int
    created: bool  # False when the identical object was already stored


class EvidenceStore(Protocol):
    def stage(self) -> AbstractContextManager[BinaryIO]:
        """Open a seekable, disk-backed staging stream; remove it on every exit.

        Local dev stages on its configured disk. A future GCS backend must stage in
        external storage, never the Cloud Run container filesystem (ADR 0011).
        """
        ...

    def put(self, stream: BinaryIO, *, sha256: str, deadline: float | None = None) -> StoredObject:
        """Store the stream's bytes under `sha256` after verifying they hash to it.

        A deadline is an absolute time.monotonic() value. Check it during copying,
        existing-object verification and immediately before publishing; expired operations
        raise TimeoutError before starting publication. An in-flight atomic OS call can
        complete after timeout; intake records its fingerprint before starting it.
        Backends also set native IO timeouts where supported.

        Raises HashMismatch (nothing stored), EvidenceConflict (an object already holds the key but no longer hashes to it: damage or tampering; it is never replaced),
        or InvalidEvidenceKey.
        """
        ...

    def get(self, sha256: str) -> BinaryIO:
        """Open the object for reading. Raises EvidenceNotFound or InvalidEvidenceKey."""
        ...

    def exists(self, sha256: str) -> bool: ...
