"""Integrity-checked track projection and streaming 30 FPS replay preparation."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile
from typing import BinaryIO

import cv2
import numpy as np
from numpy import uint8
from numpy.typing import NDArray
from pydantic import TypeAdapter, ValidationError

from minivision_reconstruct.calibration import build_calibration, project_track_footpoint
from minivision_reconstruct.domain.models import (
    CalibrationDocument, EvidenceManifest, Frame, FrameRate, ReviewedTrackRecord, TrackRecord,
)
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.perception.review_workflow import (
    initial_review_records, load_track_records, open_review_context,
)
from minivision_reconstruct.reconstruction.models import (
    ArtifactHash, MarkerDocument, Observation, ReplayFrame, ReconstructionError,
    ReconstructionManifest, RoadReference, SceneDocument, SceneShape, SourceFrame,
    require_child, require_identifier,
)
from minivision_reconstruct.reconstruction.storage import verify_hash, write_record
from minivision_reconstruct.video.reader import iter_frames


@dataclass(frozen=True)
class TrackInput:
    """An explicitly selected automatic run or immutable reviewed export."""

    records: tuple[ReviewedTrackRecord, ...]
    path: Path
    sha256: str
    tracking_run: str
    revision: str


class FFmpegEncoder:
    """Streaming encoder connector with captured, actionable stderr."""

    def __init__(self, destination: Path, width: int, height: int, fps: int, command: str) -> None:
        self.destination = destination
        self.arguments = [
            command, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
            "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(destination),
        ]
        self.log: BinaryIO = TemporaryFile()
        try:
            self.process = subprocess.Popen(self.arguments, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log)
        except OSError as error:
            self.log.close()
            raise ReconstructionError(f"Could not start FFmpeg for '{destination}', command={self.arguments}: {error}") from error

    def write(self, image: NDArray[uint8]) -> None:
        if self.process.stdin is None:
            raise ReconstructionError(f"FFmpeg input pipe is unavailable for '{self.destination}'.")
        try:
            self.process.stdin.write(image.tobytes())
        except (BrokenPipeError, OSError) as error:
            self.abort()
            raise ReconstructionError(f"FFmpeg failed for '{self.destination}', command={self.arguments}, stderr={self._stderr()}") from error

    def finish(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            code = self.process.wait(timeout=120)
        except subprocess.TimeoutExpired as error:
            self.abort()
            raise ReconstructionError(f"FFmpeg finalization timed out for '{self.destination}', command={self.arguments}.") from error
        detail = self._stderr()
        self.log.close()
        if code != 0:
            raise ReconstructionError(f"FFmpeg exit status {code} for '{self.destination}', command={self.arguments}, stderr={detail}")

    def abort(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)

    def close(self) -> None:
        self.abort()
        if not self.log.closed:
            self.log.close()

    def _stderr(self) -> str:
        self.log.seek(0)
        return self.log.read().decode("utf-8", errors="replace")


def automatic_track_input(cases_root: Path, case_id: str, tracking_run: str) -> TrackInput:
    """Resolve an automatic run without silently selecting a reviewed replacement."""
    require_identifier(case_id, "case ID")
    require_identifier(tracking_run, "tracking run")
    context = open_review_context(cases_root, case_id, tracking_run)
    records = load_track_records(context.source_records_path, case_id, tracking_run)
    return TrackInput(initial_review_records(records), context.source_records_path, context.source_records_sha256, tracking_run, "automatic")


def reviewed_track_input(cases_root: Path, case_id: str, tracking_run: str, revision: str) -> TrackInput:
    """Read the selected frozen reviewed export with its declared track hash."""
    require_identifier(revision, "review revision")
    require_identifier(case_id, "case ID")
    require_identifier(tracking_run, "tracking run")
    context = open_review_context(cases_root, case_id, tracking_run)
    directory = require_child(context.case_directory / "analysis" / "reviews" / tracking_run / revision, context.case_directory)
    manifest = _json_object(directory / "manifest.json")
    if manifest.get("case_id") != case_id or manifest.get("source_run_id") != tracking_run or manifest.get("revision_id") != revision:
        raise ReconstructionError(f"Reviewed manifest does not match case/run/revision: '{directory}'.")
    if manifest.get("source_tracks_sha256") != context.source_records_sha256:
        raise ReconstructionError(f"Reviewed export references different automatic tracks: '{directory}'.")
    path = directory / "tracks.jsonl"
    declared_hash = _required_string(manifest, "reviewed_tracks_sha256", directory)
    verify_hash(path, declared_hash)
    records: list[ReviewedTrackRecord] = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                value: dict[str, object] = json.loads(line)
                provenance = TypeAdapter(ReviewedTrackRecord.__annotations__["provenance"]).validate_python(value["provenance"])
                record_value = {key: item for key, item in value.items() if key not in {"provenance", "review_schema_version"}}
                record = TypeAdapter(TrackRecord).validate_python(record_value)
            except (ValidationError, ValueError, KeyError, TypeError) as error:
                raise ReconstructionError(f"Invalid reviewed observation at '{path}:{number}': {error}") from error
            if record.case_id != case_id or record.run_id != tracking_run:
                raise ReconstructionError(f"Reviewed observation at '{path}:{number}' does not match selected case/run.")
            records.append(ReviewedTrackRecord(record, provenance))
    return TrackInput(tuple(records), path, declared_hash, tracking_run, revision)


def load_calibration(path: Path, evidence: EvidenceManifest, case_root: Path) -> CalibrationDocument:
    """Parse and independently check a Week 5 calibration before projection."""
    require_child(path, case_root)
    try:
        calibration = TypeAdapter(CalibrationDocument).validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise ReconstructionError(f"Invalid calibration '{path}': {error}") from error
    if calibration.case_id != evidence.case_id or calibration.source_video_sha256 != evidence.preserved_sha256:
        raise ReconstructionError(f"Calibration '{path}' references a different case or source video.")
    if not calibration.validation_passed or calibration.validation_max_error > calibration.validation_tolerance:
        raise ReconstructionError(f"Calibration '{path}' failed held-out validation: error={calibration.validation_max_error}, tolerance={calibration.validation_tolerance}.")
    if calibration.image_width != evidence.width or calibration.image_height != evidence.height:
        raise ReconstructionError(f"Calibration '{path}' image dimensions do not match the source footage.")
    if calibration.mode == "metric" and (not calibration.metric_claims_permitted or calibration.world_unit not in {"m", "ft"}):
        raise ReconstructionError(f"Calibration '{path}' does not permit metric geometry.")
    if calibration.mode == "non_metric" and calibration.world_unit != "relative_units":
        raise ReconstructionError(f"Non-metric calibration '{path}' must use relative units.")
    values = tuple(value for row in calibration.homography for value in row)
    if len(values) != 9 or not all(math.isfinite(value) for value in values):
        raise ReconstructionError(f"Calibration '{path}' has an invalid homography.")
    verified = build_calibration(
        calibration.case_id, calibration.source_video_sha256, calibration.image_width,
        calibration.image_height, calibration.mode, calibration.world_unit,
        calibration.control_points, calibration.validation_points, calibration.validation_tolerance,
        calibration.features,
    )
    if not verified.validation_passed or not np.allclose(calibration.homography, verified.homography, rtol=1e-6, atol=1e-8):
        raise ReconstructionError(f"Calibration '{path}' transform or held-out measurements do not match its control points.")
    for stored, projected in zip(calibration.features, verified.features, strict=True):
        if len(stored.world_points) != len(projected.world_points) or not np.allclose(stored.world_points, projected.world_points, rtol=1e-6, atol=1e-8):
            raise ReconstructionError(f"Calibration '{path}' road feature '{stored.feature_id}' does not match the saved transform.")
    return calibration


def project_observations(tracks: TrackInput, calibration: CalibrationDocument) -> tuple[Observation, ...]:
    """Map boxed contact observations and preserve explicit unavailable states."""
    factor = 0.3048 if calibration.world_unit == "ft" else 1.0
    result: list[Observation] = []
    seen: set[tuple[int, int]] = set()
    for item in sorted(tracks.records, key=lambda item: (item.record.frame_number, item.record.track_id)):
        record = item.record
        if record.schema_version != "1.0":
            raise ReconstructionError(f"Unsupported track schema '{record.schema_version}' for track {record.track_id}, frame {record.frame_number}.")
        key = (record.frame_number, record.track_id)
        if key in seen:
            raise ReconstructionError(f"Duplicate track {record.track_id}, frame {record.frame_number} in '{tracks.path}'.")
        seen.add(key)
        if record.state == "lost":
            position = None
            bbox = None
        else:
            if None in (record.bbox_x1, record.bbox_y1, record.bbox_x2, record.bbox_y2):
                raise ReconstructionError(f"Missing box for available track {record.track_id}, frame {record.frame_number}.")
            bbox = (float(record.bbox_x1), float(record.bbox_y1), float(record.bbox_x2), float(record.bbox_y2))
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                raise ReconstructionError(f"Invalid box {bbox} for track {record.track_id}, frame {record.frame_number}.")
            projected = project_track_footpoint(calibration, record)
            position = (projected[0] * factor, projected[1] * factor)
        try:
            result.append(Observation(
                source_frame=record.frame_number, source_time=record.timestamp_seconds,
                track_id=record.track_id, class_name=record.class_name,
                position=position, bbox=bbox, state=record.state,
                provenance=item.provenance, detection_confidence=record.confidence,
            ))
        except ValidationError as error:
            raise ReconstructionError(f"Invalid track {record.track_id}, frame {record.frame_number}: {error}") from error
    if not result:
        raise ReconstructionError(f"Selected track input '{tracks.path}' has no observations.")
    return tuple(result)


def initial_scene(calibration: CalibrationDocument) -> SceneDocument:
    """Build visual geometry from calibration, using control points when features are absent."""
    factor = 0.3048 if calibration.world_unit == "ft" else 1.0
    references = tuple(RoadReference(
        feature_id=feature.feature_id, name=feature.name, kind=feature.kind,
        points=tuple((x * factor, y * factor) for x, y in feature.world_points),
    ) for feature in calibration.features)
    points = tuple(point for feature in references for point in feature.points)
    if not points:
        points = tuple((point.world_x * factor, point.world_y * factor) for point in calibration.control_points)
    xs, ys = tuple(point[0] for point in points), tuple(point[1] for point in points)
    x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
    if x2 <= x1 or y2 <= y1:
        raise ReconstructionError("Calibration geometry must cover a positive road area.")
    shapes = tuple(SceneShape(shape_id=f"surface-{feature.feature_id}", kind="surface", points=feature.points, label="") for feature in references if feature.kind == "lane")
    return SceneDocument(
        schema_version="1.0", calibration_id=calibration.calibration_id,
        mode=calibration.mode, display_unit="m" if calibration.mode == "metric" else "relative_units",
        bounds=(x1 - (x2 - x1) * 0.1, y1 - (y2 - y1) * 0.1, x2 + (x2 - x1) * 0.1, y2 + (y2 - y1) * 0.1),
        rotation_degrees=0, references=references, shapes=shapes, theme="warm",
    )


def prepare_reconstruction(cases_root: Path, case_id: str, tracks: TrackInput, calibration_path: Path, run_id: str, ffmpeg_command: str) -> Path:
    """Prepare an immutable run with at most two decoded source frames in memory."""
    require_identifier(case_id, "case ID")
    require_identifier(run_id, "reconstruction run")
    case_root = cases_root / case_id
    require_child(tracks.path, case_root)
    try:
        evidence = TypeAdapter(EvidenceManifest).validate_json((case_root / "metadata" / "evidence_manifest.json").read_bytes())
    except (OSError, ValidationError) as error:
        raise ReconstructionError(f"Invalid evidence manifest for case '{case_id}': {error}") from error
    video_path = require_child(cases_root / evidence.preserved_relative_path, case_root)
    verify_hash(video_path, evidence.preserved_sha256)
    verify_hash(tracks.path, tracks.sha256)
    calibration_hash = sha256_file(calibration_path)
    calibration = load_calibration(calibration_path, evidence, case_root)
    observations = project_observations(tracks, calibration)
    root = case_root / "analysis" / "reconstruction"
    root.mkdir(exist_ok=True)
    destination = root / run_id
    if destination.exists():
        raise ReconstructionError(f"Reconstruction run '{destination}' already exists; choose a new run ID.")
    with TemporaryDirectory(prefix=".prepare-", dir=root) as temporary:
        staged = Path(temporary)
        source_frames, replay_frames, width, height = _build_proxy(video_path, evidence, staged / "replay.mp4", ffmpeg_command)
        for observation in observations:
            if observation.source_frame >= len(source_frames):
                raise ReconstructionError(f"Track {observation.track_id} references frame {observation.source_frame} beyond the video.")
            expected = source_frames[observation.source_frame].source_time
            if abs(expected - observation.source_time) > 0.001:
                raise ReconstructionError(f"Track {observation.track_id}, frame {observation.source_frame} timestamp {observation.source_time} differs from decoded source {expected}.")
        _write_jsonl(staged / "observations.jsonl", observations)
        _write_jsonl(staged / "source_frames.jsonl", source_frames)
        _write_jsonl(staged / "replay_frames.jsonl", replay_frames)
        write_record(staged / "scene.json", initial_scene(calibration))
        write_record(staged / "markers.json", MarkerDocument(schema_version="1.0", markers=()))
        used = {frame.source_frame for frame in replay_frames}
        manifest = ReconstructionManifest(
            schema_version="1.0", case_id=case_id, run_id=run_id,
            tracking_run=tracks.tracking_run, track_revision=tracks.revision,
            calibration_revision=calibration.calibration_id,
            source_video_path=str(video_path.relative_to(cases_root.resolve())), source_video_sha256=evidence.preserved_sha256,
            source_tracks_path=str(tracks.path.resolve().relative_to(cases_root.resolve())), source_tracks_sha256=tracks.sha256,
            source_calibration_path=str(calibration_path.resolve().relative_to(cases_root.resolve())), source_calibration_sha256=calibration_hash,
            replay_fps=30, proxy_width=width, proxy_height=height,
            source_width=evidence.width, source_height=evidence.height,
            source_frame_count=len(source_frames), replay_frame_count=len(replay_frames),
            repeated_frames=len(replay_frames) - len(used), omitted_frames=len(source_frames) - len(used),
            duration_seconds=len(replay_frames) / 30,
            observation_count=len(observations), unavailable_observations=sum(item.position is None for item in observations),
            created_at=datetime.now(UTC).isoformat(),
            artifacts=tuple(ArtifactHash(filename=path.name, sha256=sha256_file(path)) for path in sorted(staged.iterdir())),
        )
        verify_hash(video_path, evidence.preserved_sha256)
        verify_hash(tracks.path, tracks.sha256)
        verify_hash(calibration_path, calibration_hash)
        write_record(staged / "manifest.json", manifest)
        staged.rename(destination)
    return destination


def _build_proxy(video_path: Path, evidence: EvidenceManifest, destination: Path, ffmpeg: str) -> tuple[tuple[SourceFrame, ...], tuple[ReplayFrame, ...], int, int]:
    if evidence.width is None or evidence.height is None or evidence.frame_rate_numerator is None or evidence.frame_rate_denominator is None:
        raise ReconstructionError("Replay preparation requires source dimensions and a valid source frame rate.")
    frame_rate = FrameRate(evidence.frame_rate_numerator, evidence.frame_rate_denominator)
    if frame_rate.numerator <= 0 or frame_rate.denominator <= 0:
        raise ReconstructionError("Replay preparation requires a positive source frame rate.")
    scale = min(1280 / evidence.width, 720 / evidence.height, 1)
    width, height = max(2, int(evidence.width * scale) // 2 * 2), max(2, int(evidence.height * scale) // 2 * 2)
    source_frames: list[SourceFrame] = []
    replay_frames: list[ReplayFrame] = []
    decoder = iter_frames(video_path, frame_rate)
    first = next(decoder, None)
    if first is None:
        raise ReconstructionError(f"Video '{video_path}' contains no frames.")
    encoder = FFmpegEncoder(destination, width, height, 30, ffmpeg)
    previous = first
    origin = first.timestamp_seconds
    source_frames.append(_source_frame(first))
    try:
        for current in decoder:
            if current.timestamp_seconds <= previous.timestamp_seconds:
                raise ReconstructionError(f"Non-monotonic source time at frame {current.number}: {current.timestamp_seconds}.")
            while origin + len(replay_frames) / 30 < current.timestamp_seconds - 1e-9:
                replay_frames.append(_encode_proxy_frame(encoder, previous, len(replay_frames), width, height))
            source_frames.append(_source_frame(current))
            previous = current
        end = previous.timestamp_seconds + 1 / frame_rate.decimal
        if evidence.duration_seconds is not None and previous.timestamp_seconds < evidence.duration_seconds <= end + 1 / frame_rate.decimal:
            end = evidence.duration_seconds
        while origin + len(replay_frames) / 30 < end - 1e-9:
            replay_frames.append(_encode_proxy_frame(encoder, previous, len(replay_frames), width, height))
        encoder.finish()
    except (ReconstructionError, OSError, cv2.error):
        encoder.abort()
        raise
    finally:
        decoder.close()
        encoder.close()
    if len(source_frames) != evidence.decoded_frame_count:
        raise ReconstructionError(f"Decoded {len(source_frames)} source frames; ingestion recorded {evidence.decoded_frame_count}.")
    return tuple(source_frames), tuple(replay_frames), width, height


def _source_frame(frame: Frame) -> SourceFrame:
    return SourceFrame(source_frame=frame.number, source_time=frame.timestamp_seconds, timestamp_source=frame.timestamp_source)


def _encode_proxy_frame(encoder: FFmpegEncoder, frame: Frame, frame_number: int, width: int, height: int) -> ReplayFrame:
    encoder.write(cv2.resize(frame.image, (width, height), interpolation=cv2.INTER_AREA))
    return ReplayFrame(replay_frame=frame_number, replay_time=frame_number / 30, source_frame=frame.number, source_time=frame.timestamp_seconds)


def _write_jsonl(path: Path, records: tuple[Observation, ...] | tuple[SourceFrame, ...] | tuple[ReplayFrame, ...]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(record.model_dump_json() + "\n")


def _json_object(path: Path) -> dict[str, object]:
    try:
        value: object = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise ReconstructionError(f"Could not read manifest '{path}': {error}") from error
    if not isinstance(value, dict):
        raise ReconstructionError(f"Manifest '{path}' must contain an object.")
    return value


def _required_string(value: dict[str, object], key: str, context: Path) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise ReconstructionError(f"Manifest '{context}' requires string '{key}'.")
    return result
