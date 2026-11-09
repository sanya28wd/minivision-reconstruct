"""Typed contract payloads, mapped from the producer's immutable models.

Absent producer fields remain None; original payloads are never rewritten.
"""

from typing import Annotated, Literal

from pydantic import Field

from .models_base import FrozenRecord

Point = tuple[float, float]


class Signal(FrozenRecord):
    name: str
    kind: str | None
    source_frames: tuple[Annotated[int, Field(ge=0)], ...] | None
    track_refs: tuple[str, ...]
    description: str


class ActionEvidence(FrozenRecord):
    rule: str
    track_ref: str
    description: str
    event_ids: tuple[str, ...]
    source_frames: tuple[Annotated[int, Field(ge=0)], ...]
    action_time: float = Field(ge=0)
    seconds_before_peak: float = Field(ge=0)


class EventFields(FrozenRecord):
    kind: Literal["event_proposal"] = "event_proposal"
    event_id: str
    event_type: str
    start_frame: int = Field(ge=0)
    peak_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    start_time: float | None
    peak_time: float | None
    end_time: float | None
    time_window: tuple[float, float]
    location: Point | None
    unit: Literal["m", "relative_units"]
    location_uncertainty: float | None = Field(ge=0)
    signals: tuple[Signal, ...]
    supporting_evidence: tuple[ActionEvidence, ...] | None
    contrary_evidence: tuple[ActionEvidence, ...] | None
    order_unresolved_with: tuple[str, ...] | None
    confidence: str


class GeometryFields(FrozenRecord):
    kind: Literal["geometry"] = "geometry"
    coordinates: Point | None
    raw_coordinates: Point | None
    unit: Literal["m", "relative_units"]
    calibration_mode: Literal["metric", "non_metric"] | None
    calibration_revision: str
    relative_uncertainty: float | None = Field(ge=0)
    position_uncertainty: float | None = Field(ge=0)
    heading_degrees: float | None
    heading_uncertainty_degrees: float | None
    source_quality: str


class TrackFields(FrozenRecord):
    class_name: str | None = None
    kind: Literal["track_reference"] = "track_reference"
    producer: str
    case_id: str
    view_id: str
    tracking_run: str
    revision: str
    track_id: int = Field(ge=1)
    state: Literal[
        "observed",
        "lost",
        "recovered",
        "interpolated",
        "manual",
        "estimated",
        "reviewer",
        "unavailable",
        "tentative",
        "confirmed",
        "deleted",
    ]
    position: Point | None
    bbox: tuple[float, float, float, float] | None
    provenance: str


class ReviewFields(FrozenRecord):
    kind: Literal["review_overlay"] = "review_overlay"
    target_id: str
    target_kind: Literal["event", "assessment"]
    action: Literal["confirm", "reject", "modify"]
    reviewer: str
    reason: str
    reviewer_authenticated: Literal[False] = False
    revision: str
    revision_chain_hashes: tuple[str, ...]
    modified_peak_time: float | None
    modified_location: Point | None
    modified_outcome: str | None
    modified_contributing_track_refs: tuple[str, ...] | None


class AssessmentFields(FrozenRecord):
    kind: Literal["assessment"] = "assessment"
    assessment_id: str
    event_id: str
    outcome: Literal["ONE_CONTRIBUTOR", "MULTIPLE_CONTRIBUTORS", "NO_SUPPORTED_CONTRIBUTOR", "UNDETERMINED"]
    contributing_track_refs: tuple[str, ...]
    lookback_window: tuple[float, float]
    supporting_evidence: tuple[ActionEvidence, ...]
    contrary_evidence: tuple[ActionEvidence, ...]
    unresolved_facts: tuple["UnresolvedFact", ...]
    rule_source: None


class UnresolvedFact(FrozenRecord):
    kind: str
    track_refs: tuple[str, ...]
    time_window: tuple[float, float] | None
    description: str
    decisive: bool


class SequenceFields(FrozenRecord):
    covering_frames: tuple[int, int] | None = None
    kind: Literal["sequence"] = "sequence"
    event_id: str
    event_type: str
    rank: int = Field(ge=1)
    peak_frame: int | None = Field(default=None, ge=0)
    peak_time: float | None
    time_window: tuple[float | None, float | None]
    order_unresolved_with: tuple[str, ...]


class Transformation(FrozenRecord):
    operation: Literal["producer reconstruction replay"]
    mapping: str


class FrameRate(FrozenRecord):
    numerator: int = Field(gt=0)
    denominator: int = Field(gt=0)


class VideoMetadata(FrozenRecord):
    original_filename: str
    duration_seconds: float | None = Field(ge=0)
    frame_rate: FrameRate | None
    width: int | None = Field(gt=0)
    height: int | None = Field(gt=0)
    codec: str | None
    reported_frame_count: int | None = Field(ge=0)
    frame_count_source: Literal["reported", "derived", "unavailable"]
    creation_time: str | None
    extraction_tool: str


class SourceFields(FrozenRecord):
    producer_case_id: str | None = None
    kind: Literal["source_binding"] = "source_binding"
    video_metadata: VideoMetadata
    transformations: tuple[Transformation, ...]
    frame_origin: Literal[0]
    calibration_revision: str
    reconstruction_manifest: str
    movement_manifest: str
    incident_manifest: str
    reviewed_manifest: str
    original_video_sha256: str
    tracking_run: str
    track_revision: str
    reconstruction_run: str
    movement_run: str
    incident_run: str
    review_revision: str
    source_frame: int | None
    source_time: float | None
    replay_frame: int | None
    replay_time: float | None
    timestamp_provenance: str | None


Payload = Annotated[
    EventFields | GeometryFields | TrackFields | ReviewFields | AssessmentFields | SequenceFields | SourceFields,
    Field(discriminator="kind"),
]
