"""Aggregate vehicle evaluation for the declared UA-DETRAC-derived subset."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path

import cv2

from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.domain.models import DetectorConfiguration, RawDetection, SUPPORTED_CLASS_NAMES
from minivision_reconstruct.perception.detector import YoloDetector


@dataclass(frozen=True)
class BoundingBox:
    """One pixel-space ground-truth or predicted box."""

    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True)
class AggregateEvaluation:
    """Vehicle-level metrics for a fixed labelled image subset."""

    images_evaluated: int
    ground_truth_boxes: int
    predicted_boxes: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    iou_threshold: float
    detector: dict[str, object]


def evaluate_subset(
    images_directory: Path,
    labels_directory: Path,
    configuration: DetectorConfiguration,
    iou_threshold: float,
    output_path: Path,
) -> AggregateEvaluation:
    """Evaluate supported detections as aggregate vehicles against YOLO labels."""
    if not 0 < iou_threshold <= 1:
        raise EvidenceError(f"IoU threshold must be in (0, 1], got {iou_threshold}.")
    image_paths = tuple(sorted(images_directory.glob("*.jpg")))
    if not image_paths:
        raise EvidenceError(f"No JPEG images found in '{images_directory}'.")
    detector = YoloDetector(configuration)
    true_positives = 0
    false_positives = 0
    false_negatives = 0
    ground_truth_count = 0
    prediction_count = 0
    for image_path in image_paths:
        label_path = labels_directory / f"{image_path.stem}.txt"
        if not label_path.is_file():
            raise EvidenceError(f"Missing ground-truth label for '{image_path}': '{label_path}'.")
        image = cv2.imread(str(image_path))
        if image is None:
            raise EvidenceError(f"Could not decode evaluation image '{image_path}'.")
        ground_truth = _read_yolo_boxes(label_path, image.shape[1], image.shape[0])
        raw_detections, _ = detector.detect(image, "ua-detrac-roboflow-subset", 0)
        predictions = _supported_boxes(raw_detections, image.shape[1], image.shape[0])
        matched_true_positives, image_false_positives, image_false_negatives = _match_boxes(predictions, ground_truth, iou_threshold)
        true_positives += matched_true_positives
        false_positives += image_false_positives
        false_negatives += image_false_negatives
        ground_truth_count += len(ground_truth)
        prediction_count += len(predictions)
    precision = true_positives / (true_positives + false_positives) if true_positives + false_positives else 0.0
    recall = true_positives / (true_positives + false_negatives) if true_positives + false_negatives else 0.0
    result = AggregateEvaluation(
        images_evaluated=len(image_paths),
        ground_truth_boxes=ground_truth_count,
        predicted_boxes=prediction_count,
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        precision=precision,
        recall=recall,
        iou_threshold=iou_threshold,
        detector=asdict(detector.provenance),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(asdict(result), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def _read_yolo_boxes(path: Path, width: int, height: int) -> tuple[BoundingBox, ...]:
    boxes: list[BoundingBox] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        values = line.split()
        if len(values) != 5:
            raise EvidenceError(f"Invalid YOLO label at '{path}', line {line_number}.")
        class_id, center_x, center_y, box_width, box_height = (float(value) for value in values)
        if class_id != 0:
            raise EvidenceError(f"Unsupported label class {class_id} at '{path}', line {line_number}; expected aggregate vehicle class 0.")
        x1 = (center_x - box_width / 2) * width
        y1 = (center_y - box_height / 2) * height
        x2 = (center_x + box_width / 2) * width
        y2 = (center_y + box_height / 2) * height
        boxes.append(BoundingBox(x1, y1, x2, y2))
    return tuple(boxes)


def _supported_boxes(raw_detections: tuple[RawDetection, ...], width: int, height: int) -> tuple[BoundingBox, ...]:
    boxes: list[BoundingBox] = []
    for raw in raw_detections:
        if raw.label not in SUPPORTED_CLASS_NAMES:
            continue
        boxes.append(BoundingBox(max(0.0, raw.x1), max(0.0, raw.y1), min(float(width), raw.x2), min(float(height), raw.y2)))
    return tuple(box for box in boxes if box.x1 < box.x2 and box.y1 < box.y2)


def _match_boxes(predictions: tuple[BoundingBox, ...], ground_truth: tuple[BoundingBox, ...], threshold: float) -> tuple[int, int, int]:
    unmatched_ground_truth = set(range(len(ground_truth)))
    true_positives = 0
    for prediction in predictions:
        candidate = max(unmatched_ground_truth, key=lambda index: _iou(prediction, ground_truth[index]), default=None)
        if candidate is not None and _iou(prediction, ground_truth[candidate]) >= threshold:
            unmatched_ground_truth.remove(candidate)
            true_positives += 1
    return true_positives, len(predictions) - true_positives, len(unmatched_ground_truth)


def _iou(first: BoundingBox, second: BoundingBox) -> float:
    intersection_width = max(0.0, min(first.x2, second.x2) - max(first.x1, second.x1))
    intersection_height = max(0.0, min(first.y2, second.y2) - max(first.y1, second.y1))
    intersection = intersection_width * intersection_height
    first_area = (first.x2 - first.x1) * (first.y2 - first.y1)
    second_area = (second.x2 - second.x1) * (second.y2 - second.y1)
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0
