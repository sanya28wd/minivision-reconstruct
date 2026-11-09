"""Local-disk evidence backend: for development and tests ONLY.

On Cloud Run the container filesystem is in-memory and does not persist when an instance stops (ADR 0011),
so evidence written here would be lost on the first restart. Staging and production must use the Cloud
Storage backend (not built yet, PENDING-OWNER); `incident_report.config.Settings` refuses the local backend there.

Layout under the root (which is configured outside the app, never inside the package directory):
    objects/<first two hex>/<sha256>   the write-once objects (mode 0444)
    staging/                            in-flight uploads; moved into objects/ only after the hash checks out
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, cast

import incident_report
from incident_report.storage.errors import EvidenceConflict, EvidenceNotFound, HashMismatch, StorageConfigError
from incident_report.storage.interface import StoredObject, validate_sha256_key

# Read size while hashing and copying. An engineering choice with no effect on any result.
_CHUNK_BYTES = 1024 * 1024
_PACKAGE_DIR = Path(incident_report.__file__).resolve().parent


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Evidence storage stage timed out.")


def _hashes_to(path: Path, key: str, deadline: float | None = None) -> bool:
    """Stream the file through SHA-256 in chunks (never whole in memory) and compare with the key."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            _check_deadline(deadline)
            chunk = handle.read(_CHUNK_BYTES)
            _check_deadline(deadline)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest() == key


class LocalEvidenceStore:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise StorageConfigError(f"The evidence directory must be an absolute path, got {str(root)!r}.")
        resolved = root.resolve()
        if resolved == _PACKAGE_DIR or _PACKAGE_DIR in resolved.parents:
            raise StorageConfigError(
                "The evidence directory must not be inside the app's package directory; "
                "choose a folder outside the source tree."
            )
        self._root = resolved
        self._objects = resolved / "objects"
        self._staging = resolved / "staging"
        self._objects.mkdir(parents=True, exist_ok=True)
        self._staging.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """The configured evidence directory; exports use it only to refuse leaking it."""
        return self._root

    @contextlib.contextmanager
    def stage(self) -> Iterator[BinaryIO]:
        with tempfile.TemporaryFile(mode="w+b", buffering=0, dir=self._staging) as stream:
            yield cast(BinaryIO, stream)

    def _path(self, key: str) -> Path:
        return self._objects / key[:2] / key

    def exists(self, sha256: str) -> bool:
        return self._path(validate_sha256_key(sha256)).is_file()

    def get(self, sha256: str) -> BinaryIO:
        path = self._path(validate_sha256_key(sha256))
        try:
            return path.open("rb")
        except FileNotFoundError:
            raise EvidenceNotFound("No evidence is stored under that key.") from None

    def put(self, stream: BinaryIO, *, sha256: str, deadline: float | None = None) -> StoredObject:
        key = validate_sha256_key(sha256)
        _check_deadline(deadline)
        fd, staged_name = tempfile.mkstemp(dir=self._staging, prefix=key[:8] + ".")
        staged = Path(staged_name)
        try:
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    _check_deadline(deadline)
                    chunk = stream.read(_CHUNK_BYTES)
                    _check_deadline(deadline)
                    if not chunk:
                        break
                    digest.update(chunk)
                    _check_deadline(deadline)
                    out.write(chunk)
                    size += len(chunk)
                _check_deadline(deadline)
                out.flush()
                _check_deadline(deadline)
                os.fsync(out.fileno())
                _check_deadline(deadline)
            if digest.hexdigest() != key:
                raise HashMismatch("The content does not match the SHA-256 it was offered under; nothing was stored.")
            final = self._path(key)
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                # A hard link fails if the name exists, so an object can never be replaced (unlike rename).
                _check_deadline(deadline)
                os.link(staged, final)
            except FileExistsError:
                if not _hashes_to(final, key, deadline):
                    raise EvidenceConflict(
                        "An object with this key already exists but does not match its SHA-256 "
                        "(damaged or tampered with); it was not replaced."
                    ) from None
                return StoredObject(sha256=key, size=size, created=False)
            staged.unlink()  # drop the staging name first: a read-only file cannot be unlinked on Windows
            final.chmod(0o444)
            return StoredObject(sha256=key, size=size, created=True)
        finally:
            with contextlib.suppress(FileNotFoundError):
                staged.unlink()
