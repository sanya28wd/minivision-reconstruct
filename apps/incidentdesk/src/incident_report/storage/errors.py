from __future__ import annotations


class StorageError(Exception):
    """Evidence storage could not do what was asked. The message is plain and safe to show."""


class StorageConfigError(StorageError):
    """The storage backend is not configured safely (for example a root inside the app directory)."""


class InvalidEvidenceKey(StorageError):
    """The key is not a lowercase hex SHA-256 (64 characters)."""


class EvidenceNotFound(StorageError):
    """No object is stored under this key."""


class HashMismatch(StorageError):
    """The bytes streamed in do not hash to the key they were offered under. Nothing was stored."""


class EvidenceConflict(StorageError):
    """An object already exists under this key but does not match the content offered.

    The existing object is never replaced; this means it was damaged or tampered with and needs a person.
    """
