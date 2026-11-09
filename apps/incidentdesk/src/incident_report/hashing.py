"""Fingerprints. SHA-256 is hashed in-stream so a whole video is never held in memory."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable
from typing import BinaryIO

from incident_report.domain import Role

# Read size for in-stream hashing. An engineering choice with no effect on any result.
_CHUNK_BYTES = 1024 * 1024


def hash_stream(
    stream: BinaryIO,
    *,
    destination: BinaryIO | None = None,
    max_bytes: int | None = None,
    deadline: float | None = None,
) -> tuple[str, int]:
    """Return (sha256 hex digest, byte count) reading the stream once, in chunks."""
    digest = hashlib.sha256()
    size = 0
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Streaming stage timed out.")
        chunk = stream.read(_CHUNK_BYTES if max_bytes is None else min(_CHUNK_BYTES, max_bytes - size + 1))
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Streaming stage timed out.")
        if not chunk:
            break
        if max_bytes is not None and size + len(chunk) > max_bytes:
            raise ValueError("The upload exceeds the configured size limit.")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Streaming stage timed out.")
        size += len(chunk)
    return digest.hexdigest(), size


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def incident_input_hash(views: Iterable[tuple[Role, str]]) -> str:
    """Fingerprint of an incident's input: the (role, file sha256) pairs, order-independent.

    Labels, filenames and acquisition method are deliberately excluded: they do not change
    what the AI is shown, so they must not change the replay key.
    """
    pairs = sorted([role.value, sha256] for role, sha256 in views)
    canonical = json.dumps(pairs, separators=(",", ":"))
    return sha256_hex(canonical.encode("utf-8"))
