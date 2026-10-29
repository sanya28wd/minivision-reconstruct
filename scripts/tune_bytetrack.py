"""Cache UA-DETRAC detections and run the fixed ByteTrack tuning grid."""

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.domain.models import RawDetection
from minivision_reconstruct.perception.bytetrack import ByteTrackConnector
from minivision_reconstruct.perception.configuration import load_detector_configuration
from minivision_reconstruct.perception.detector import YoloDetector
from minivision_reconstruct.perception.track_states import interpolate_short_gaps
from minivision_reconstruct.perception.tracking_configuration import load_tracker_configuration

from track_ua_detrac import _frame_number, _read_image


TUNING_SEQUENCES = ("MVI_39781", "MVI_40732")
BUFFERS = (15, 30, 60)
MATCH_THRESHOLDS = (0.70, 0.80, 0.90)


def main(arguments: list[str]) -> int:
    """Run each prescribed ByteTrack trial from cached all-frame detections."""
    parser = argparse.ArgumentParser(description="Tune ByteTrack on the fixed UA-DETRAC tuning split.")
    parser.add_argument("--frames-root", required=True, type=Path)
    parser.add_argument("--detector-config", required=True, type=Path)
    parser.add_argument("--base-tracker-config", required=True, type=Path)
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    try:
        detector_configuration = load_detector_configuration(parsed.detector_config)
        base_configuration = load_tracker_configuration(parsed.base_tracker_config)
        if detector_configuration.frame_stride != 1:
            raise EvidenceError("Tuning requires detector frame_stride=1.")
        caches = _load_or_create_caches(parsed.frames_root, parsed.cache_root, detector_configuration)
        _run_trials(caches, parsed.output_root, base_configuration)
    except (EvidenceError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ByteTrack tuning failed: {error}", file=sys.stderr)
        return 1
    print(f"Completed {len(BUFFERS) * len(MATCH_THRESHOLDS)} tuning trials in '{parsed.output_root}'.")
    return 0


def _load_or_create_caches(frames_root: Path, cache_root: Path, detector_configuration: object) -> dict[str, tuple[tuple[int, tuple[RawDetection, ...]], ...]]:
    caches: dict[str, tuple[tuple[int, tuple[RawDetection, ...]], ...]] = {}
    detector: YoloDetector | None = None
    for sequence in TUNING_SEQUENCES:
        cache_path = cache_root / f"{sequence}.jsonl"
        if cache_path.is_file():
            caches[sequence] = _load_cache(cache_path)
            print(f"{sequence}: reused cached detections.")
            continue
        if detector is None:
            detector = YoloDetector(detector_configuration)
        caches[sequence] = _create_cache(frames_root / sequence, cache_path, detector)
    return caches


def _create_cache(image_directory: Path, cache_path: Path, detector: YoloDetector) -> tuple[tuple[int, tuple[RawDetection, ...]], ...]:
    images = tuple(sorted(image_directory.glob("*.jpg")))
    if not images:
        raise EvidenceError(f"No JPEG frames found for sequence '{image_directory}'.")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        raise EvidenceError(f"Refusing to overwrite detection cache '{cache_path}'.")
    rows: list[tuple[int, tuple[RawDetection, ...]]] = []
    with cache_path.open("x", encoding="utf-8") as stream:
        for position, image_path in enumerate(images):
            frame_number = _frame_number(image_path, position)
            detections, _ = detector.detect(_read_image(image_path), "ua-detrac", frame_number)
            rows.append((frame_number, detections))
            stream.write(json.dumps({"frame_number": frame_number, "detections": [asdict(item) for item in detections]}, sort_keys=True) + "\n")
            if (position + 1) % 100 == 0:
                print(f"{image_directory.name}: cached {position + 1}/{len(images)} frames", file=sys.stderr, flush=True)
    return tuple(rows)


def _load_cache(cache_path: Path) -> tuple[tuple[int, tuple[RawDetection, ...]], ...]:
    rows: list[tuple[int, tuple[RawDetection, ...]]] = []
    with cache_path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            value = json.loads(line)
            if "frame_number" not in value or "detections" not in value:
                raise EvidenceError(f"Invalid detection cache row {line_number} in '{cache_path}'.")
            detections = tuple(RawDetection(**item) for item in value["detections"])
            rows.append((int(value["frame_number"]), detections))
    if not rows:
        raise EvidenceError(f"Detection cache '{cache_path}' is empty.")
    return tuple(rows)


def _run_trials(caches: dict[str, tuple[tuple[int, tuple[RawDetection, ...]], ...]], output_root: Path, base_configuration: object) -> None:
    for buffer in BUFFERS:
        for match_threshold in MATCH_THRESHOLDS:
            trial_name = f"buffer-{buffer}-match-{match_threshold:.2f}".replace(".", "")
            trial_root = output_root / trial_name
            if trial_root.exists():
                raise EvidenceError(f"Refusing to overwrite existing tuning trial '{trial_root}'.")
            configuration = replace(base_configuration, track_buffer=buffer, match_thresh=match_threshold)
            for sequence, cache in caches.items():
                records = _track_cached_sequence(sequence, trial_name, cache, configuration)
                _write_trial_outputs(trial_root, sequence, records)


def _track_cached_sequence(sequence: str, trial_name: str, cache: tuple[tuple[int, tuple[RawDetection, ...]], ...], configuration: object) -> tuple[object, ...]:
    tracker = ByteTrackConnector(configuration)
    records = []
    for frame_number, detections in cache:
        records.extend(tracker.update(detections, "ua-detrac", trial_name, frame_number, frame_number / 25.0))
    return interpolate_short_gaps(tuple(records), configuration.interpolation_max_gap_frames)


def _write_trial_outputs(trial_root: Path, sequence: str, records: tuple[object, ...]) -> None:
    track_directory = trial_root / "tracks" / "tuning" / sequence
    mot_directory = trial_root / "mot" / sequence / "data"
    track_directory.mkdir(parents=True, exist_ok=False)
    mot_directory.mkdir(parents=True, exist_ok=False)
    with (track_directory / "tracks.jsonl").open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(asdict(record), sort_keys=True) + "\n")
    rows = []
    for record in records:
        if record.state not in {"observed", "recovered"}:
            continue
        if None in (record.bbox_x1, record.bbox_y1, record.bbox_x2, record.bbox_y2, record.confidence):
            raise EvidenceError(f"Scored track {record.track_id} in '{sequence}' has incomplete data.")
        rows.append(
            f"{record.frame_number + 1},{record.track_id},{record.bbox_x1:.6f},{record.bbox_y1:.6f},"
            f"{record.bbox_x2 - record.bbox_x1:.6f},{record.bbox_y2 - record.bbox_y1:.6f},{record.confidence:.6f},1,1,1"
        )
    (mot_directory / f"{sequence}.txt").write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    frames = max(record.frame_number for record in records) + 1
    (track_directory / "sequence_manifest.json").write_text(json.dumps({"sequence": sequence, "frames": frames, "records": len(records), "fps": 25.0}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
