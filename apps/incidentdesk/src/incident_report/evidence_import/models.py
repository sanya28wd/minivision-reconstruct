"""Evidence contract v1. Limits are project assumptions, to be validated."""

from typing import Any, Literal, Self

from pydantic import Field, model_validator

from .models_base import FrozenRecord
from .payloads import Payload, VideoMetadata

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 256 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_FILES = 512
MAX_RECORDS = 100_000
IMPORTER_VERSION = "1.4.0"
# Project assumption, to be validated: serialization noise only, never a frame-sized tolerance.
TIMESTAMP_TOLERANCE_SECONDS = 1e-9


class SourceBinding(FrozenRecord):
    producer_case_id: str | None = Field(default=None, min_length=1, max_length=200)
    view_id: str = Field(min_length=1, max_length=200)
    original_video_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reconstruction_manifest: str
    movement_manifest: str
    incident_manifest: str
    reviewed_manifest: str
    tracking_run: str
    track_revision: str
    calibration_revision: str
    reconstruction_run: str
    movement_run: str
    incident_run: str
    review_revision: str
    video_metadata: VideoMetadata
    transformations: tuple[dict[str, Any], ...]
    frame_origin: Literal[0]


class Envelope(FrozenRecord):
    contract_version: Literal["1.0"]
    producer: Literal["minivision-reconstruct"]
    producer_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    case_id: str = Field(min_length=1, max_length=200)
    created_at: str
    processing_policy: Literal["local_only"]
    config_identifiers: tuple[str, ...]
    model_identifiers: tuple[str, ...]
    artifacts: dict[str, str]
    sources: tuple[SourceBinding, ...]


class NormalizedRecord(FrozenRecord):
    record_id: str
    kind: Literal[
        "source_binding", "track_reference", "event_proposal", "geometry", "review_overlay", "assessment", "sequence"
    ]
    view_id: str
    artifact_path: str
    data: Payload
    original: dict[str, Any]
    effective: dict[str, Any] | None = None
    source_frame: int | None = Field(default=None, ge=0)
    source_time: float | None = Field(default=None, ge=0)
    timestamp_provenance: str | None = None
    track_refs: tuple[str, ...] = ()
    producer_status: str | None = None
    parent_status: str | None = None
    needs_review: bool = False
    affirmative: Literal[False] = False
    officer_approved: Literal[False] = False
    rule_source: None = None

    @model_validator(mode="after")
    def check_kind(self) -> Self:
        if self.data.kind != self.kind:
            raise ValueError("record kind/payload mismatch")
        return self


class DerivedOmission(FrozenRecord):
    state: Literal["not_supplied_derived"] = "not_supplied_derived"
    artifact_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ImportDecision(FrozenRecord):
    missing_inputs: tuple[str, ...] = ()
    issue_codes: tuple[str, ...] = ()
    not_supplied_derived: tuple[DerivedOmission, ...] = ()
    read_complete: bool = True
    import_id: str
    importer_version: str = IMPORTER_VERSION
    provenance_state: Literal["verified", "incomplete_provenance", "rejected"]
    issues: tuple[str, ...]
    records: tuple[NormalizedRecord, ...] = ()
    structurally_accepted_records: tuple[str, ...] = ()
    quarantined_records: tuple[str, ...] = ()
    source_hashes: dict[str, str] = Field(default_factory=dict)
    envelope: Envelope | None = None
    raw_artifacts: dict[str, bytes] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="after")
    def expose_missing_inputs(self) -> Self:
        missing_codes = {
            "no_incident_candidates",
            "calibration_failed",
            "calibration_unavailable",
            "source_timestamps_unavailable",
            "source_timestamps_incomplete",
            "upstream_artifacts_missing",
        }
        object.__setattr__(
            self, "missing_inputs", tuple(sorted(set(self.missing_inputs) | (set(self.issue_codes) & missing_codes)))
        )
        return self


class LegacyImportSnapshot(FrozenRecord):
    """Historical decision, deliberately not a current typed evidence decision."""

    import_id: str
    importer_version: Literal["1.0.0"]
    original_decision: dict[str, Any]
    raw_envelope: bytes | None
    raw_artifacts: dict[str, bytes] = Field(exclude=True)
