"""Run YOLOv8 plus ByteTrack over prepared UA-DETRAC image sequences."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import cv2
import numpy as np
from PIL import Image

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.perception.bytetrack import ByteTrackConnector
from minivision_reconstruct.perception.configuration import load_detector_configuration
from minivision_reconstruct.perception.detector import YoloDetector
from minivision_reconstruct.perception.track_states import interpolate_short_gaps
from minivision_reconstruct.perception.tracking_configuration import load_tracker_configuration


SEQUENCES = {
    "tuning": ("MVI_39781", "MVI_40732"),
    "evaluation": ("MVI_39851", "MVI_40181", "MVI_40213", "MVI_41073", "MVI_63552", "MVI_63561"),
}


def main(arguments: list[str]) -> int:
    """Track one prepared UA-DETRAC sequence or every sequence in a split."""
    parser = argparse.ArgumentParser(description="Run all-frame ByteTrack over prepared UA-DETRAC JPEG sequences.")
    parser.add_argument("--frames-root", required=True, type=Path)
    parser.add_argument("--split", required=True, choices=("tuning", "evaluation"))
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--detector-config", required=True, type=Path)
    parser.add_argument("--tracker-config", required=True, type=Path)
    parser.add_argument("--sequence")
    parsed = parser.parse_args(arguments)
    try:
        detector_configuration = load_detector_configuration(parsed.detector_config)
        tracker_configuration = load_tracker_configuration(parsed.tracker_config)
        if detector_configuration.frame_stride != 1:
            raise EvidenceError("UA-DETRAC tracking requires frame_stride=1.")
        sequences = (parsed.sequence,) if parsed.sequence else SEQUENCES[parsed.split]
        for sequence in sequences:
            _track_sequence(parsed.frames_root / sequence, parsed.output_root / parsed.split / sequence, detector_configuration, tracker_configuration)
    except (EvidenceError, OSError, ValueError) as error:
        print(f"UA-DETRAC tracking failed: {error}", file=sys.stderr)
        return 1
    print(f"Completed {len(sequences)} UA-DETRAC {parsed.split} sequence(s) in '{parsed.output_root}'.")
    return 0


def _track_sequence(
    image_directory: Path, output_directory: Path, detector_configuration: object, tracker_configuration: object,
) -> None:
    images = tuple(sorted(image_directory.glob("*.jpg")))
    if not images:
        raise EvidenceError(f"No JPEG frames found for sequence '{image_directory}'.")
    if output_directory.exists():
        raise EvidenceError(f"Refusing to overwrite existing sequence output '{output_directory}'.")
    output_directory.mkdir(parents=True, exist_ok=False)
    detector = YoloDetector(detector_configuration)
    tracker = ByteTrackConnector(tracker_configuration)
    records = []
    for position, image_path in enumerate(images):
        image = _read_image(image_path)
        frame_number = _frame_number(image_path, position)
        detections, _ = detector.detect(image, image_directory.name, frame_number)
        records.extend(tracker.update(detections, "ua-detrac", image_directory.name, frame_number, (frame_number / 25.0)))
        if (position + 1) % 100 == 0:
            print(f"{image_directory.name}: {position + 1}/{len(images)} frames", file=sys.stderr, flush=True)
    finalized = interpolate_short_gaps(tuple(records), tracker_configuration.interpolation_max_gap_frames)
    with (output_directory / "tracks.jsonl").open("x", encoding="utf-8") as stream:
        for record in finalized:
            stream.write(json.dumps(asdict(record), sort_keys=True) + "\n")
    (output_directory / "sequence_manifest.json").write_text(json.dumps({"sequence": image_directory.name, "frames": len(images), "records": len(finalized), "fps": 25.0}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _frame_number(path: Path, fallback: int) -> int:
    digits = "".join(character for character in path.stem if character.isdigit())
    return int(digits) - 1 if digits else fallback


def _read_image(path: Path) -> np.ndarray:
    """Decode a UA-DETRAC frame, using macOS ImageIO for legacy JPEG variants."""
    image = cv2.imread(str(path))
    if image is not None:
        return image
    image = _read_with_sips(path)
    if image is not None:
        return image
    try:
        with Image.open(path) as decoded:
            return cv2.cvtColor(np.asarray(decoded.convert("RGB")), cv2.COLOR_RGB2BGR)
    except (OSError, ValueError) as error:
        raise EvidenceError(f"Could not decode UA-DETRAC frame '{path}' with OpenCV, macOS ImageIO, or Pillow: {error}") from error


def _read_with_sips(path: Path) -> np.ndarray | None:
    """Use the system JPEG decoder only when OpenCV rejects a frame."""
    descriptor, output_name = tempfile.mkstemp(suffix=".jpg")
    os.close(descriptor)
    os.unlink(output_name)
    output_path = Path(output_name)
    try:
        result = subprocess.run(
            ("sips", "-s", "format", "jpeg", str(path), "--out", str(output_path)),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return cv2.imread(str(output_path))
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        output_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
