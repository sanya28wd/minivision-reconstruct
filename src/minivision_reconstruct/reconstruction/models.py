"""Typed, immutable reconstruction artifacts; no vehicle speed fields."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.domain.models import ReviewProvenance, TrackStateName


Point = tuple[float, float]
ShapeKind = Literal["surface", "marking", "label"]


class ReconstructionError(EvidenceError):
    """Invalid reconstruction inputs, geometry, or saved artifacts."""


class ReplayExportError(ReconstructionError):
    """A browser or encoder could not produce a verified presentation artifact."""


class Artifact(BaseModel):
    """Frozen JSON record with unexpected fields rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Observation(Artifact):
    source_frame: int = Field(ge=0)
    source_time: float = Field(ge=0)
    track_id: int = Field(ge=1)
    class_name: Literal["person", "bicycle", "car", "motorcycle", "bus", "truck"]
    position: Point | None
    bbox: tuple[float, float, float, float] | None
    state: TrackStateName
    provenance: ReviewProvenance
    detection_confidence: float | None = Field(ge=0, le=1)


class SourceFrame(Artifact):
    source_frame: int = Field(ge=0)
    source_time: float = Field(ge=0)
    timestamp_source: str


class ReplayFrame(Artifact):
    replay_frame: int = Field(ge=0)
    replay_time: float = Field(ge=0)
    source_frame: int = Field(ge=0)
    source_time: float = Field(ge=0)


class RoadReference(Artifact):
    feature_id: str
    name: str
    kind: Literal["lane", "stop_line", "crossing", "monitored_zone"]
    points: tuple[Point, ...]


class SceneShape(Artifact):
    shape_id: str
    kind: ShapeKind
    points: tuple[Point, ...]
    label: str


class SceneDocument(Artifact):
    schema_version: Literal["1.0"]
    calibration_id: str
    mode: Literal["metric", "non_metric"]
    display_unit: Literal["m", "relative_units"]
    bounds: tuple[float, float, float, float]
    rotation_degrees: float
    references: tuple[RoadReference, ...]
    shapes: tuple[SceneShape, ...]
    theme: Literal["warm"]


class Marker(Artifact):
    marker_id: str
    source_frame: int = Field(ge=0)
    source_time: float = Field(ge=0)
    position: Point | None
    track_ids: tuple[int, ...]
    label: str = Field(min_length=1, max_length=120)
    note: str = Field(max_length=2000)
    reviewer: str = Field(min_length=1, max_length=120)
    created_at: str


class MarkerDocument(Artifact):
    schema_version: Literal["1.0"]
    markers: tuple[Marker, ...]


class ArtifactHash(Artifact):
    filename: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReconstructionManifest(Artifact):
    schema_version: Literal["1.0"]
    case_id: str
    run_id: str
    tracking_run: str
    track_revision: str
    calibration_revision: str
    source_video_path: str
    source_video_sha256: str
    source_tracks_path: str
    source_tracks_sha256: str
    source_calibration_path: str
    source_calibration_sha256: str
    replay_fps: int
    proxy_width: int
    proxy_height: int
    source_width: int
    source_height: int
    source_frame_count: int
    replay_frame_count: int
    repeated_frames: int
    omitted_frames: int
    duration_seconds: float
    observation_count: int
    unavailable_observations: int
    created_at: str
    artifacts: tuple[ArtifactHash, ...]


class SceneRevision(Artifact):
    revision_id: str
    reviewer: str
    created_at: str
    previous_sha256: str
    document: SceneDocument


class MarkerRevision(Artifact):
    revision_id: str
    reviewer: str
    created_at: str
    previous_sha256: str
    document: MarkerDocument


class ReplayBundle(Artifact):
    manifest: ReconstructionManifest
    scene: SceneDocument
    markers: MarkerDocument
    observations: tuple[Observation, ...]
    source_frames: tuple[SourceFrame, ...]
    replay_frames: tuple[ReplayFrame, ...]


class ExportManifest(Artifact):
    schema_version: Literal["1.0"]
    source_manifest_sha256: str
    scene_revision: str
    scene_sha256: str
    marker_revision: str
    markers_sha256: str
    renderer_sha256: str
    frame_count: int
    frame_rate: int
    width: int
    height: int
    snapshot_source_frame: int
    created_at: str
    artifacts: tuple[ArtifactHash, ...]


def require_identifier(value: str, context: str) -> str:
    """Reject path traversal before resolving any caller-supplied identifier."""
    if not value or len(value) > 80 or not all(character.isascii() and (character.isalnum() or character in "-_") for character in value):
        raise ReconstructionError(f"Invalid {context} '{value}'; use ASCII letters, digits, hyphens or underscores.")
    return value


def require_child(path: Path, root: Path) -> Path:
    """Resolve one artifact and reject symlinks escaping its expected case root."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ReconstructionError(f"Artifact '{path}' escapes expected root '{root}'.")
    return resolved
