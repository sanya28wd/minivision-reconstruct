"""Streaming, evidence-verified detection run workflow."""

from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import resource
from statistics import mean, median
from time import perf_counter

from minivision_reconstruct.domain.errors import CaseNotFoundError, DetectionRunExistsError, EvidenceIntegrityError, InvalidDetectionError
from minivision_reconstruct.domain.models import (
    DetectionRecord,
    DetectorConfiguration,
    DetectorProvenance,
    EvidenceManifest,
    RawDetection,
    FrameRate,
    SUPPORTED_CLASS_NAMES,
)
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.perception.detector import YoloDetector
from minivision_reconstruct.video.reader import iter_frames
from minivision_reconstruct.video.renderer import create_video_writer, render_frame, verify_annotated_video


DETECTION_SCHEMA_VERSION = "1.0"
INTERNAL_CLASS_IDS: dict[str, int] = {"person": 0, "bicycle": 1, "car": 2, "motorcycle": 3, "bus": 4, "truck": 5}


@dataclass(frozen=True)
class DetectionRunResult:
    """Summary and artifact paths from one completed independent detection run."""

    detection_records_path: Path
    annotated_video_path: Path
    run_manifest_path: Path
    total_frames: int
    source_frames_seen: int
    frames_with_detections: int
    total_detections: int
    per_class_detections: dict[str, int]
    mean_latency_seconds: float
    median_latency_seconds: float
    p95_latency_seconds: float
    processing_seconds: float
    effective_fps: float
    peak_memory_megabytes: float
    provenance: DetectorProvenance


def run_detection_case(cases_root: Path, case_id: str, run_id: str, configuration: DetectorConfiguration) -> DetectionRunResult:
    """Process an existing evidence case without modifying its preserved video."""
    case_directory = cases_root / case_id
    manifest = _load_case_manifest(case_directory, case_id)
    evidence_path = case_directory / manifest.preserved_relative_path.split("/", 1)[1]
    _verify_evidence_hash(evidence_path, manifest, case_id)
    run_directory = case_directory / "analysis" / "detections" / run_id
    try:
        run_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise DetectionRunExistsError(f"Detection run '{run_id}' already exists for case '{case_id}'.") from error
    detector = YoloDetector(configuration)
    records_path = run_directory / "detections.jsonl"
    annotated_path = run_directory / "annotated.avi"
    started_at = perf_counter()
    latencies: list[float] = []
    class_counts: Counter[str] = Counter()
    total_frames = 0
    source_frames_seen = 0
    frames_with_detections = 0
    with records_path.open("x", encoding="utf-8") as records_stream:
        writer = create_video_writer(annotated_path, manifest.width or 0, manifest.height or 0, _manifest_frame_rate(manifest))
        try:
            for frame in iter_frames(evidence_path, _manifest_frame_rate(manifest)):
                source_frames_seen += 1
                if frame.number % configuration.frame_stride != 0:
                    continue
                raw_detections, inference_seconds = detector.detect(frame.image, case_id, frame.number)
                records = _validated_records(raw_detections, case_id, frame.number, frame.timestamp_seconds, frame.image.shape[1], frame.image.shape[0], detector.provenance, inference_seconds)
                for record in records:
                    records_stream.write(json.dumps(asdict(record), sort_keys=True) + "\n")
                    class_counts[record.class_name] += 1
                writer.write(render_frame(frame.image, records, frame.number, frame.timestamp_seconds))
                total_frames += 1
                latencies.append(inference_seconds)
                if records:
                    frames_with_detections += 1
        finally:
            writer.release()
    _verify_evidence_hash(evidence_path, manifest, case_id)
    verify_annotated_video(annotated_path, manifest.width or 0, manifest.height or 0)
    processing_seconds = perf_counter() - started_at
    result = DetectionRunResult(
        detection_records_path=records_path,
        annotated_video_path=annotated_path,
        run_manifest_path=run_directory / "run_manifest.json",
        total_frames=total_frames,
        source_frames_seen=source_frames_seen,
        frames_with_detections=frames_with_detections,
        total_detections=sum(class_counts.values()),
        per_class_detections=dict(sorted(class_counts.items())),
        mean_latency_seconds=mean(latencies) if latencies else 0.0,
        median_latency_seconds=median(latencies) if latencies else 0.0,
        p95_latency_seconds=_percentile(latencies, 0.95),
        processing_seconds=processing_seconds,
        effective_fps=total_frames / processing_seconds if processing_seconds > 0 else 0.0,
        peak_memory_megabytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024),
        provenance=detector.provenance,
    )
    _write_run_manifest(result, case_id, run_id, manifest, configuration)
    return result


def _load_case_manifest(case_directory: Path, case_id: str) -> EvidenceManifest:
    path = case_directory / "metadata" / "evidence_manifest.json"
    if not path.is_file():
        raise CaseNotFoundError(f"Case '{case_id}' has no evidence manifest at '{path}'.")
    try:
        value: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return EvidenceManifest(**value)
    except (json.JSONDecodeError, TypeError) as error:
        raise CaseNotFoundError(f"Case '{case_id}' has an invalid evidence manifest at '{path}': {error}") from error


def _verify_evidence_hash(path: Path, manifest: EvidenceManifest, case_id: str) -> None:
    if not path.is_file():
        raise CaseNotFoundError(f"Preserved evidence is missing for case '{case_id}': '{path}'.")
    actual_hash = sha256_file(path)
    if actual_hash != manifest.preserved_sha256:
        raise EvidenceIntegrityError(
            f"Preserved evidence hash mismatch for case '{case_id}', path '{path}': expected {manifest.preserved_sha256}, got {actual_hash}."
        )


def _manifest_frame_rate(manifest: EvidenceManifest) -> FrameRate | None:
    if manifest.frame_rate_numerator is None or manifest.frame_rate_denominator is None:
        return None
    return FrameRate(manifest.frame_rate_numerator, manifest.frame_rate_denominator)


def _validated_records(
    raw_detections: tuple[RawDetection, ...],
    case_id: str,
    frame_number: int,
    timestamp_seconds: float,
    frame_width: int,
    frame_height: int,
    provenance: DetectorProvenance,
    inference_seconds: float,
) -> tuple[DetectionRecord, ...]:
    records: list[DetectionRecord] = []
    for raw in raw_detections:
        if raw.label not in SUPPORTED_CLASS_NAMES:
            continue
        x1, y1, x2, y2 = _clip_and_validate(raw, frame_width, frame_height, case_id, frame_number)
        records.append(
            DetectionRecord(
                case_id=case_id,
                frame_number=frame_number,
                timestamp_seconds=timestamp_seconds,
                class_id=INTERNAL_CLASS_IDS[raw.label],
                class_name=raw.label,
                confidence=raw.confidence,
                bbox_x1=x1,
                bbox_y1=y1,
                bbox_x2=x2,
                bbox_y2=y2,
                frame_width=frame_width,
                frame_height=frame_height,
                model_identifier=provenance.model_name,
                model_version=provenance.model_version,
                inference_duration_seconds=inference_seconds,
                schema_version=DETECTION_SCHEMA_VERSION,
            )
        )
    return tuple(records)


def _clip_and_validate(raw: RawDetection, width: int, height: int, case_id: str, frame_number: int) -> tuple[float, float, float, float]:
    values = (raw.confidence, raw.x1, raw.y1, raw.x2, raw.y2)
    if not all(value == value and value not in (float("inf"), float("-inf")) for value in values):
        raise InvalidDetectionError(f"Non-finite detection for case '{case_id}', frame {frame_number}, class '{raw.label}'.")
    if not 0 <= raw.confidence <= 1:
        raise InvalidDetectionError(f"Invalid confidence {raw.confidence} for case '{case_id}', frame {frame_number}.")
    x1, x2 = max(0.0, min(float(width), raw.x1)), max(0.0, min(float(width), raw.x2))
    y1, y2 = max(0.0, min(float(height), raw.y1)), max(0.0, min(float(height), raw.y2))
    if x1 >= x2 or y1 >= y2:
        raise InvalidDetectionError(f"Invalid clipped box for case '{case_id}', frame {frame_number}, class '{raw.label}'.")
    return x1, y1, x2, y2


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


def _write_run_manifest(
    result: DetectionRunResult,
    case_id: str,
    run_id: str,
    evidence_manifest: EvidenceManifest,
    configuration: DetectorConfiguration,
) -> None:
    value = {
        "schema_version": DETECTION_SCHEMA_VERSION,
        "case_id": case_id,
        "run_id": run_id,
        "evidence_sha256": evidence_manifest.preserved_sha256,
        "detector": asdict(result.provenance),
        "configuration": {**asdict(configuration), "weights_path": str(configuration.weights_path)},
        "records_relative_path": result.detection_records_path.name,
        "annotated_video_relative_path": result.annotated_video_path.name,
        "total_frames": result.total_frames,
        "source_frames_seen": result.source_frames_seen,
        "frame_stride": configuration.frame_stride,
        "frames_with_detections": result.frames_with_detections,
        "total_detections": result.total_detections,
        "per_class_detections": result.per_class_detections,
        "mean_inference_seconds": result.mean_latency_seconds,
        "median_inference_seconds": result.median_latency_seconds,
        "p95_inference_seconds": result.p95_latency_seconds,
        "end_to_end_processing_seconds": result.processing_seconds,
        "effective_fps": result.effective_fps,
        "peak_process_memory_megabytes": result.peak_memory_megabytes,
        "memory_measurement": "ru_maxrss of the Python process on macOS, including model load and rendering",
        "warmup_frames_excluded": 0,
        "source_video_size_bytes": evidence_manifest.file_size_bytes,
        "annotated_video_size_bytes": result.annotated_video_path.stat().st_size,
    }
    result.run_manifest_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
