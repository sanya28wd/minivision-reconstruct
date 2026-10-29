"""Validated reconstruction loading and immutable reviewer revision persistence."""

from datetime import UTC, datetime
import fcntl
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.reconstruction.models import (
    MarkerDocument, MarkerRevision, Observation, ReplayBundle, ReplayFrame,
    ReconstructionError, ReconstructionManifest, SceneDocument, SceneRevision, SourceFrame,
    require_child, require_identifier,
)


Record = TypeVar("Record", bound=BaseModel)


def read_record(path: Path, record_type: type[Record]) -> Record:
    """Read a typed JSON artifact with a contextual parse error."""
    try:
        return record_type.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise ReconstructionError(f"Could not read {record_type.__name__} from '{path}': {error}") from error


def read_records(path: Path, record_type: type[Record]) -> tuple[Record, ...]:
    """Read typed JSONL observations without discarding malformed lines."""
    records: list[Record] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    records.append(record_type.model_validate_json(line))
                except ValidationError as error:
                    raise ReconstructionError(f"Invalid {record_type.__name__} at '{path}:{number}': {error}") from error
    except OSError as error:
        raise ReconstructionError(f"Could not read records '{path}': {error}") from error
    return tuple(records)


def write_record(path: Path, record: BaseModel) -> None:
    """Create one new artifact; overwriting existing evidence is prohibited."""
    with path.open("x", encoding="utf-8") as stream:
        stream.write(record.model_dump_json(indent=2) + "\n")


def verify_hash(path: Path, expected: str) -> None:
    """Reject missing or changed inputs before displaying derived evidence."""
    try:
        actual = sha256_file(path)
    except OSError as error:
        raise ReconstructionError(f"Cannot hash '{path}': {error}") from error
    if actual != expected:
        raise ReconstructionError(f"Integrity mismatch for '{path}': expected SHA-256 {expected}, received {actual}.")


def load_bundle(run_directory: Path, scene_revision: str, marker_revision: str) -> ReplayBundle:
    """Open explicitly selected saved scene and marker revisions after integrity checks."""
    manifest = read_record(run_directory / "manifest.json", ReconstructionManifest)
    for item in manifest.artifacts:
        verify_hash(require_child(run_directory / item.filename, run_directory), item.sha256)
    scene = load_scene(run_directory, scene_revision)
    markers = load_markers(run_directory, marker_revision)
    bundle = ReplayBundle(
        manifest=manifest, scene=scene, markers=markers,
        observations=read_records(run_directory / "observations.jsonl", Observation),
        source_frames=read_records(run_directory / "source_frames.jsonl", SourceFrame),
        replay_frames=read_records(run_directory / "replay_frames.jsonl", ReplayFrame),
    )
    validate_scene(scene, read_record(run_directory / "scene.json", SceneDocument))
    validate_markers(markers, bundle)
    return bundle


def load_scene(run_directory: Path, revision: str) -> SceneDocument:
    require_identifier(revision, "scene revision")
    if revision == "initial":
        return read_record(run_directory / "scene.json", SceneDocument)
    path = _verified_revision_path(run_directory, "scenes", revision)
    return read_record(path, SceneRevision).document


def load_markers(run_directory: Path, revision: str) -> MarkerDocument:
    require_identifier(revision, "marker revision")
    if revision == "initial":
        return read_record(run_directory / "markers.json", MarkerDocument)
    path = _verified_revision_path(run_directory, "markers", revision)
    return read_record(path, MarkerRevision).document


def _verified_revision_path(run_directory: Path, category: str, revision: str) -> Path:
    path = require_child(run_directory / category / revision / "revision.json", run_directory)
    try:
        seal: dict[str, str] = json.loads((path.parent / "sha256.json").read_bytes())
        verify_hash(path, seal["revision_sha256"])
    except (OSError, ValueError, KeyError) as error:
        raise ReconstructionError(f"Invalid integrity seal for revision '{path}': {error}") from error
    return path


def revision_ids(run_directory: Path, category: str) -> tuple[str, ...]:
    """List numbered revisions, leaving source selection explicit in the UI."""
    return ("initial", *(path.name for path in sorted((run_directory / category).glob("revision-[0-9]*")) if path.is_dir()))


def validate_scene(scene: SceneDocument, original: SceneDocument) -> None:
    """Allow appearance edits while keeping the calibrated road geometry locked."""
    if (scene.calibration_id, scene.mode, scene.display_unit, scene.references) != (
        original.calibration_id, original.mode, original.display_unit, original.references
    ):
        raise ReconstructionError("Scene edits cannot change calibration, coordinate units, or locked road references.")
    x1, y1, x2, y2 = scene.bounds
    if x2 <= x1 or y2 <= y1 or not all(math.isfinite(value) for value in scene.bounds):
        raise ReconstructionError(f"Scene bounds must describe a finite, positive area: {scene.bounds}.")
    if len({shape.shape_id for shape in scene.shapes}) != len(scene.shapes):
        raise ReconstructionError("Scene shape IDs must be unique.")
    for shape in scene.shapes:
        minimum = {"surface": 3, "marking": 2, "label": 1}[shape.kind]
        if len(shape.points) < minimum or (shape.kind == "label" and len(shape.points) != 1):
            raise ReconstructionError(f"Scene {shape.kind} '{shape.shape_id}' requires {minimum} points.")
        if shape.kind == "label" and not shape.label.strip():
            raise ReconstructionError(f"Scene label '{shape.shape_id}' requires text.")


def validate_markers(document: MarkerDocument, bundle: ReplayBundle) -> None:
    """Require exact source-frame timing and known road-user identities."""
    if len({marker.marker_id for marker in document.markers}) != len(document.markers):
        raise ReconstructionError("Reviewer marker IDs must be unique.")
    tracks = {observation.track_id for observation in bundle.observations}
    for marker in document.markers:
        if marker.source_frame >= len(bundle.source_frames):
            raise ReconstructionError(f"Marker '{marker.marker_id}' points outside the source video.")
        frame = bundle.source_frames[marker.source_frame]
        if abs(frame.source_time - marker.source_time) > 0.000001:
            raise ReconstructionError(f"Marker '{marker.marker_id}' has incorrect source timestamp {marker.source_time}; expected {frame.source_time}.")
        if not set(marker.track_ids).issubset(tracks):
            raise ReconstructionError(f"Marker '{marker.marker_id}' references unknown tracks: {marker.track_ids}.")
        if not marker.label.strip() or not marker.reviewer.strip():
            raise ReconstructionError("Every marker requires a label and reviewer name.")


def save_scene_revision(run_directory: Path, document: SceneDocument, reviewer: str) -> Path:
    """Save an immutable visual scene revision without modifying calibration."""
    validate_scene(document, read_record(run_directory / "scene.json", SceneDocument))
    return _save_revision(run_directory, "scenes", document, reviewer)


def save_marker_revision(run_directory: Path, document: MarkerDocument, reviewer: str, bundle: ReplayBundle) -> Path:
    """Save an immutable, attributed marker revision with validated evidence references."""
    validate_markers(document, bundle)
    return _save_revision(run_directory, "markers", document, reviewer)


def _save_revision(run_directory: Path, category: str, document: SceneDocument | MarkerDocument, reviewer: str) -> Path:
    if not reviewer.strip():
        raise ReconstructionError("Enter a reviewer name before saving a revision.")
    root = run_directory / category
    root.mkdir(exist_ok=True)
    with (root / ".revision.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = revision_ids(run_directory, category)[1:]
        number = max((int(name.removeprefix("revision-")) for name in existing), default=0) + 1
        revision_id = f"revision-{number:03d}"
        initial_filename = "scene.json" if category == "scenes" else "markers.json"
        previous_path = root / existing[-1] / "revision.json" if existing else run_directory / initial_filename
        previous_hash = sha256_file(previous_path)
        created = datetime.now(UTC).isoformat()
        if isinstance(document, SceneDocument):
            revision: SceneRevision | MarkerRevision = SceneRevision(
                revision_id=revision_id, reviewer=reviewer.strip(), created_at=created,
                previous_sha256=previous_hash, document=document,
            )
        else:
            revision = MarkerRevision(
                revision_id=revision_id, reviewer=reviewer.strip(), created_at=created,
                previous_sha256=previous_hash, document=document,
            )
        destination = root / revision_id
        with TemporaryDirectory(prefix=".revision-", dir=root) as temporary:
            staged = Path(temporary)
            write_record(staged / "revision.json", revision)
            (staged / "sha256.json").write_text(json.dumps({"revision_sha256": sha256_file(staged / "revision.json")}) + "\n")
            staged.rename(destination)
    return destination
