"""Evidence-verified, all-frame detection and persistent-tracking workflow."""

from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from time import perf_counter

from minivision_reconstruct.domain.errors import CaseNotFoundError, EvidenceIntegrityError, TrackingError, TrackingRunExistsError
from minivision_reconstruct.domain.models import DetectorConfiguration, DetectorProvenance, EvidenceManifest, FrameRate, TrackRecord, TrackerConfiguration
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.perception.bytetrack import ByteTrackConnector
from minivision_reconstruct.perception.detector import YoloDetector
from minivision_reconstruct.perception.track_states import interpolate_short_gaps
from minivision_reconstruct.video.reader import iter_frames
from minivision_reconstruct.video.renderer import create_video_writer, render_tracking_frame, verify_annotated_video


TRACK_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class TrackingRunResult:
    """Summary and artifact paths from one completed tracking run."""

    track_records_path: Path
    tracking_video_path: Path
    run_manifest_path: Path
    total_frames: int
    total_tracks: int
    state_counts: dict[str, int]
    processing_seconds: float
    provenance: DetectorProvenance


def run_tracking_case(
    cases_root: Path, case_id: str, run_id: str, detector_configuration: DetectorConfiguration, tracker_configuration: TrackerConfiguration,
) -> TrackingRunResult:
    """Track supported road users across every evidence frame without altering evidence."""
    if detector_configuration.frame_stride != 1:
        raise TrackingError("Tracking requires detector frame_stride=1; sampled detections cannot be associated safely.")
    case_directory = cases_root / case_id
    manifest = _load_case_manifest(case_directory, case_id)
    evidence_path = case_directory / manifest.preserved_relative_path.split("/", 1)[1]
    _verify_evidence_hash(evidence_path, manifest, case_id)
    run_directory = case_directory / "analysis" / "tracks" / run_id
    try:
        run_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise TrackingRunExistsError(f"Tracking run '{run_id}' already exists for case '{case_id}'.") from error
    started = perf_counter()
    detector = YoloDetector(detector_configuration)
    tracker = ByteTrackConnector(tracker_configuration)
    raw_records: list[TrackRecord] = []
    total_frames = 0
    for frame in iter_frames(evidence_path, _frame_rate(manifest)):
        detections, _ = detector.detect(frame.image, case_id, frame.number)
        raw_records.extend(tracker.update(detections, case_id, run_id, frame.number, frame.timestamp_seconds))
        total_frames += 1
    records = interpolate_short_gaps(tuple(raw_records), tracker_configuration.interpolation_max_gap_frames)
    tracks_path = run_directory / "tracks.jsonl"
    with tracks_path.open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(asdict(record), sort_keys=True) + "\n")
    video_path = run_directory / "tracking.avi"
    _render_video(evidence_path, manifest, records, video_path)
    _verify_evidence_hash(evidence_path, manifest, case_id)
    state_counts: Counter[str] = Counter(record.state for record in records)
    result = TrackingRunResult(
        track_records_path=tracks_path, tracking_video_path=video_path, run_manifest_path=run_directory / "run_manifest.json",
        total_frames=total_frames, total_tracks=len({record.track_id for record in records}), state_counts=dict(sorted(state_counts.items())),
        processing_seconds=perf_counter() - started, provenance=detector.provenance,
    )
    _write_manifest(result, case_id, run_id, manifest, detector_configuration, tracker_configuration)
    return result


def _render_video(evidence_path: Path, manifest: EvidenceManifest, records: tuple[TrackRecord, ...], output_path: Path) -> None:
    grouped: dict[int, tuple[TrackRecord, ...]] = {}
    for record in records:
        grouped[record.frame_number] = (*grouped.get(record.frame_number, ()), record)
    writer = create_video_writer(output_path, manifest.width or 0, manifest.height or 0, _frame_rate(manifest))
    tails: dict[int, list[tuple[int, int]]] = {}
    last_boxes: dict[int, tuple[int, int, int, int]] = {}
    try:
        for frame in iter_frames(evidence_path, _frame_rate(manifest)):
            writer.write(render_tracking_frame(frame.image, grouped.get(frame.number, ()), frame.number, frame.timestamp_seconds, tails, last_boxes))
    finally:
        writer.release()
    verify_annotated_video(output_path, manifest.width or 0, manifest.height or 0)


def _load_case_manifest(case_directory: Path, case_id: str) -> EvidenceManifest:
    path = case_directory / "metadata" / "evidence_manifest.json"
    if not path.is_file():
        raise CaseNotFoundError(f"Case '{case_id}' has no evidence manifest at '{path}'.")
    try:
        return EvidenceManifest(**json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError) as error:
        raise CaseNotFoundError(f"Case '{case_id}' has an invalid evidence manifest at '{path}': {error}") from error


def _verify_evidence_hash(path: Path, manifest: EvidenceManifest, case_id: str) -> None:
    if not path.is_file():
        raise CaseNotFoundError(f"Preserved evidence is missing for case '{case_id}': '{path}'.")
    actual = sha256_file(path)
    if actual != manifest.preserved_sha256:
        raise EvidenceIntegrityError(f"Preserved evidence hash mismatch for case '{case_id}': expected {manifest.preserved_sha256}, got {actual}.")


def _frame_rate(manifest: EvidenceManifest) -> FrameRate | None:
    if manifest.frame_rate_numerator is None or manifest.frame_rate_denominator is None:
        return None
    return FrameRate(manifest.frame_rate_numerator, manifest.frame_rate_denominator)


def _write_manifest(
    result: TrackingRunResult, case_id: str, run_id: str, evidence: EvidenceManifest,
    detector: DetectorConfiguration, tracker: TrackerConfiguration,
) -> None:
    value = {
        "schema_version": TRACK_SCHEMA_VERSION, "case_id": case_id, "run_id": run_id,
        "evidence_sha256": evidence.preserved_sha256, "detector": asdict(result.provenance),
        "detector_configuration": {**asdict(detector), "weights_path": str(detector.weights_path)},
        "tracker_configuration": asdict(tracker), "records_relative_path": result.track_records_path.name,
        "tracking_video_relative_path": result.tracking_video_path.name, "tracks_sha256": sha256_file(result.track_records_path),
        "tracking_video_sha256": sha256_file(result.tracking_video_path), "total_frames": result.total_frames,
        "total_tracks": result.total_tracks, "state_counts": result.state_counts,
        "end_to_end_processing_seconds": result.processing_seconds,
        "identity_scope": "Track IDs are stable only within this case and run; they are not cross-camera identities.",
    }
    result.run_manifest_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
