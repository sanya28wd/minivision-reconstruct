"""Typed, immutable movement artifacts; deliberately without speed or acceleration fields."""

from typing import Literal, get_args

from pydantic import Field

from minivision_reconstruct.domain.models import ReviewProvenance, TrackStateName
from minivision_reconstruct.reconstruction.models import Artifact, ArtifactHash, Point, ReconstructionError


MovementState = Literal["moving", "stopped", "turning", "unknown", "unavailable"]
DistanceUnit = Literal["m", "relative_units"]
EventType = Literal[
    "first_observed", "last_observed", "stop", "start", "turn_left", "turn_right",
    "lane_change", "lane_entry", "stop_line_crossing", "zone_entry", "zone_exit", "closest_approach",
]
EventProvenance = Literal["calculated", "inferred"]
EventConfidence = Literal["high", "medium", "low"]
EventStatus = Literal["PROPOSED_BY_SYSTEM", "CONFIRMED_BY_REVIEWER", "REJECTED_BY_REVIEWER", "MODIFIED_BY_REVIEWER"]
EVENT_TYPES: tuple[str, ...] = get_args(EventType)


class MovementError(ReconstructionError):
    """Invalid movement configuration, inputs, ground truth, or saved artifacts."""


class SmoothingConfig(Artifact):
    window_frames: int = Field(ge=3)
    polynomial_order: int = Field(ge=1)


class UncertaintyConfig(Artifact):
    footpoint_sigma_fraction_of_box_height: float = Field(ge=0)
    minimum_footpoint_sigma_pixels: float = Field(gt=0)
    low_confidence_threshold: float = Field(ge=0, le=1)
    low_confidence_multiplier: float = Field(ge=1)
    interpolated_multiplier: float = Field(ge=1)
    reviewer_multiplier: float = Field(ge=1)
    coverage_factor: float = Field(gt=0)


class MotionConfig(Artifact):
    stationary_window_seconds: float = Field(gt=0)
    stationary_extent_factor: float = Field(gt=0)
    minimum_stationary_extent_pixels: float = Field(gt=0)
    minimum_stop_seconds: float = Field(gt=0)
    rest_boundary_fraction: float = Field(gt=0, le=1)
    heading_half_window_frames: int = Field(ge=1)
    heading_displacement_factor: float = Field(gt=0)
    turn_half_window_frames: int = Field(ge=1)
    turn_change_threshold_degrees: float = Field(gt=0)
    turn_minimum_degrees: float = Field(gt=0, lt=360)
    turn_boundary_fraction: float = Field(gt=0, lt=0.5)
    turn_reference_frames: int = Field(ge=1)


class RoadUseConfig(Artifact):
    lane_persistence_frames: int = Field(ge=1)
    parallel_lane_tolerance_degrees: float = Field(gt=0, lt=90)


class SeparationConfig(Artifact):
    sample_every_frames: int = Field(ge=1)
    minimum_overlap_frames: int = Field(ge=2)


class ValidationConfig(Artifact):
    timing_tolerance_seconds: dict[EventType, float]
    matching_gate_factor: float = Field(ge=1)


class MovementConfig(Artifact):
    schema_version: Literal["1.0"]
    config_id: str
    smoothing: SmoothingConfig
    uncertainty: UncertaintyConfig
    motion: MotionConfig
    road_use: RoadUseConfig
    separation: SeparationConfig
    validation: ValidationConfig


class SmoothedPoint(Artifact):
    """Raw projected position retained beside its segment-local smoothed position."""

    track_id: int = Field(ge=1)
    source_frame: int = Field(ge=0)
    source_time: float = Field(ge=0)
    class_name: str
    state: TrackStateName
    provenance: ReviewProvenance
    segment_id: int | None
    raw_position: Point | None
    smoothed_position: Point | None
    smoothed: bool
    footpoint_pixel: Point | None
    relative_uncertainty: float | None = Field(ge=0)
    position_uncertainty: float | None = Field(ge=0)
    unit: DistanceUnit


class MovementObservation(Artifact):
    """README §6.5 movement observation; movement_state describes visible behavior only."""

    track_id: int = Field(ge=1)
    source_frame: int = Field(ge=0)
    timestamp_seconds: float = Field(ge=0)
    world_x: float | None
    world_y: float | None
    heading_degrees: float | None
    heading_uncertainty_degrees: float | None = Field(ge=0)
    position_uncertainty: float | None = Field(ge=0)
    movement_state: MovementState
    lane_or_zone_id: str | None
    lane_candidates: tuple[str, ...]
    source_quality: Literal["observed", "recovered", "interpolated", "reviewer", "unavailable"]
    unit: DistanceUnit


class MovementEvent(Artifact):
    event_id: str
    case_id: str
    event_type: EventType
    track_ids: tuple[int, ...]
    start_frame: int = Field(ge=0)
    peak_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    start_time: float = Field(ge=0)
    peak_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    time_window: tuple[float, float]
    location: Point | None
    location_uncertainty: float | None = Field(ge=0)
    unit: DistanceUnit
    from_feature: str | None
    to_feature: str | None
    evidence_signals: tuple[str, ...]
    confidence: EventConfidence
    provenance: EventProvenance
    status: EventStatus
    created_by: str


class PairSeparation(Artifact):
    """Distance between road-contact points, not between vehicle bodies."""

    track_a: int = Field(ge=1)
    track_b: int = Field(ge=1)
    source_frame: int = Field(ge=0)
    source_time: float = Field(ge=0)
    separation: float = Field(ge=0)
    uncertainty: float = Field(ge=0)
    unit: DistanceUnit
    physical_distance: bool


class SequenceEntry(Artifact):
    rank: int = Field(ge=1)
    event_id: str
    event_type: EventType
    track_ids: tuple[int, ...]
    peak_time: float
    time_window: tuple[float, float]
    order_unresolved_with: tuple[str, ...]


class MovementSequence(Artifact):
    schema_version: Literal["1.0"]
    case_id: str
    entries: tuple[SequenceEntry, ...]


class MovementManifest(Artifact):
    schema_version: Literal["1.0"]
    case_id: str
    run_id: str
    reconstruction_run: str
    source_reconstruction_manifest_sha256: str
    source_calibration_path: str
    source_calibration_sha256: str
    config_id: str
    config_sha256: str
    pipeline_version: str
    calibration_mode: Literal["metric", "non_metric"]
    unit: DistanceUnit
    world_is_right_handed: bool
    track_count: int
    event_count: int
    created_at: str
    limitations: tuple[str, ...]
    artifacts: tuple[ArtifactHash, ...]
