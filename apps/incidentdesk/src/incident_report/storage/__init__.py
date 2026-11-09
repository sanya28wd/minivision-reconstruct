"""Evidence storage seam. See interface.py for the contract."""

from incident_report.config import Settings
from incident_report.storage.errors import (
    EvidenceConflict,
    EvidenceNotFound,
    HashMismatch,
    InvalidEvidenceKey,
    StorageConfigError,
    StorageError,
)
from incident_report.storage.interface import EvidenceStore, StoredObject, validate_sha256_key
from incident_report.storage.local import LocalEvidenceStore


def create_evidence_store(settings: Settings) -> EvidenceStore | None:
    """The evidence store the settings describe, or None when none is configured."""
    if settings.evidence_dir is None:
        return None
    return LocalEvidenceStore(settings.evidence_dir)


__all__ = [
    "create_evidence_store",
    "EvidenceConflict",
    "EvidenceNotFound",
    "EvidenceStore",
    "HashMismatch",
    "InvalidEvidenceKey",
    "LocalEvidenceStore",
    "StorageConfigError",
    "StorageError",
    "StoredObject",
    "validate_sha256_key",
]
