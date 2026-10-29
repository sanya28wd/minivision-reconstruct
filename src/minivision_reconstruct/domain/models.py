"""Immutable records used by the Week 1 evidence workflow."""

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from numpy import uint8
from numpy.typing import NDArray


FrameCountSource = Literal["reported", "derived", "unavailable"]
TimestampSource = Literal["presentation_timestamp", "frame_rate_fallback"]
TrackStateName = Literal["observed", "lost", "recovered", "interpolated", "manual"]
ReviewAction = Literal["correct", "add", "create_track", "delete_observation", "delete_track", "merge", "split", "repair_gap", "restore"]
ReviewProvenance = Literal["automatic", "reviewer", "review_interpolation"]
CalibrationMode = Literal["metric", "non_metric"]
CalibrationPointRole = Literal["control", "validation"]
RoadFeatureKind = Literal["lane", "stop_line", "crossing", "monitored_zone"]
SUPPORTED_CLASS_NAMES = frozenset({"person", "bicycle", "car", "motorcycle", "bus", "truck"})


@dataclass(frozen=True)
class FrameRate:
    """An exact rational frame rate reported by ffprobe."""

    numerator: int
    denominator: int

    @property
    def decimal(self) -> float:
        """Return the decimal frames-per-second representation."""
        return self.numerator / self.denominator


@dataclass(frozen=True)
class VideoMetadata:
    """Video metadata extracted from ffprobe without fabricated values."""

    original_filename: str
    duration_seconds: float | None
    frame_rate: FrameRate | None
    width: int | None
    height: int | None
    codec: str | None
    reported_frame_count: int | None
    frame_count_source: FrameCountSource
    creation_time: str | None
    extraction_tool: str


@dataclass(frozen=True)
class Frame:
    """One decoded image and its zero-based frame number and timestamp."""

    number: int
    timestamp_seconds: float
    timestamp_source: TimestampSource
    image: NDArray[uint8]


@dataclass(frozen=True)
class EvidenceManifest:
    """Portable, JSON-serializable summary of preserved evidence."""

    schema_version: str
    case_id: str
    original_source_filename: str
    preserved_relative_path: str
    source_sha256: str
    preserved_sha256: str
    file_size_bytes: int
    duration_seconds: float | None
    frame_rate_numerator: int | None
    frame_rate_denominator: int | None
    frame_rate_decimal: float | None
    width: int | None
    height: int | None
    codec: str | None
    reported_frame_count: int | None
    decoded_frame_count: int
    frame_count_source: FrameCountSource
    first_decoded_timestamp_seconds: float | None
    last_decoded_timestamp_seconds: float | None
    metadata_extraction_tool: str
    ingested_at: str


@dataclass(frozen=True)
class IngestedCase:
    """Paths and records created by one successful ingestion."""

    case_id: str
    case_directory: Path
    preserved_video_path: Path
    metadata_path: Path
    manifest_path: Path
    metadata: VideoMetadata
    manifest: EvidenceManifest


@dataclass(frozen=True)
class DetectorConfiguration:
    """Explicit settings and provenance for one pretrained detector."""

    model_name: str
    weights_path: Path
    weight_source: str
    model_family: str
    model_license: str
    device: str
    confidence_threshold: float
    inference_image_size: int
    frame_stride: int


@dataclass(frozen=True)
class DetectorProvenance:
    """Resolved detector identity recorded with a detection run."""

    model_family: str
    model_name: str
    model_version: str
    library_version: str
    weight_source: str
    weight_sha256: str
    device: str
    model_license: str
    model_size_bytes: int


@dataclass(frozen=True)
class RawDetection:
    """One detector result before supported-class filtering and clipping."""

    label: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True)
class DetectionRecord:
    """One independent supported road-user detection; never a track."""

    case_id: str
    frame_number: int
    timestamp_seconds: float
    class_id: int
    class_name: str
    confidence: float
    bbox_x1: float
    bbox_y1: float
    bbox_x2: float
    bbox_y2: float
    frame_width: int
    frame_height: int
    model_identifier: str
    model_version: str
    inference_duration_seconds: float
    schema_version: str


@dataclass(frozen=True)
class TrackerConfiguration:
    """Explicit ByteTrack association settings for one tracking run."""

    tracker_type: Literal["bytetrack"]
    track_high_thresh: float
    track_low_thresh: float
    new_track_thresh: float
    track_buffer: int
    match_thresh: float
    fuse_score: bool
    interpolation_max_gap_frames: int


@dataclass(frozen=True)
class TrackRecord:
    """One auditable state of a track for one source frame."""

    case_id: str
    run_id: str
    frame_number: int
    timestamp_seconds: float
    track_id: int
    class_id: int
    class_name: str
    confidence: float | None
    bbox_x1: float | None
    bbox_y1: float | None
    bbox_x2: float | None
    bbox_y2: float | None
    state: TrackStateName
    track_age_frames: int
    gap_length_frames: int
    schema_version: str


@dataclass(frozen=True)
class ReviewEvent:
    """One immutable, hash-linked reviewer action against an automatic run."""

    schema_version: str
    event_id: str
    sequence: int
    case_id: str
    source_run_id: str
    reviewer: str
    occurred_at: str
    action: ReviewAction
    payload: dict[str, object]
    previous_event_sha256: str | None
    event_sha256: str


@dataclass(frozen=True)
class ReviewedTrackRecord:
    """One track state plus its provenance in a reviewed export."""

    record: TrackRecord
    provenance: ReviewProvenance


@dataclass(frozen=True)
class CalibrationPoint:
    """Image/world coordinate pair used to fit or validate a homography."""

    point_id: str
    image_x: float
    image_y: float
    world_x: float
    world_y: float
    role: CalibrationPointRole


@dataclass(frozen=True)
class RoadFeature:
    """A lane or road marking stored in source-image and projected coordinates."""

    feature_id: str
    name: str
    kind: RoadFeatureKind
    image_points: tuple[tuple[float, float], ...]
    world_points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class CalibrationDocument:
    """Versioned case calibration, validation report, and road geometry."""

    schema_version: str
    calibration_id: str
    case_id: str
    source_video_sha256: str
    image_width: int
    image_height: int
    mode: CalibrationMode
    world_unit: str
    metric_claims_permitted: bool
    control_points: tuple[CalibrationPoint, ...]
    validation_points: tuple[CalibrationPoint, ...]
    homography: tuple[tuple[float, float, float], ...]
    control_rmse: float
    validation_rmse: float
    validation_max_error: float
    validation_tolerance: float
    validation_passed: bool
    features: tuple[RoadFeature, ...]


def record_to_json_value(record: VideoMetadata | EvidenceManifest) -> dict[str, object]:
    """Convert a typed record to JSON-compatible primitive values."""
    value: dict[str, object] = asdict(record)
    return value


def utc_timestamp(value: datetime) -> str:
    """Format an aware UTC instant for a portable evidence record."""
    return value.isoformat().replace("+00:00", "Z")
