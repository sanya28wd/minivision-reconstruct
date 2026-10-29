"""Annotated video rendering for independent Week 2 detections."""

from pathlib import Path

import cv2
import numpy as np
from numpy import uint8
from numpy.typing import NDArray

from minivision_reconstruct.domain.errors import VideoWriterError
from minivision_reconstruct.domain.models import DetectionRecord, FrameRate, TrackRecord


CLASS_COLORS: dict[str, tuple[int, int, int]] = {
    "person": (255, 170, 0),
    "bicycle": (0, 220, 220),
    "car": (0, 220, 0),
    "motorcycle": (220, 0, 220),
    "bus": (0, 120, 255),
    "truck": (255, 80, 80),
}


def create_video_writer(path: Path, width: int, height: int, frame_rate: FrameRate | None) -> cv2.VideoWriter:
    """Create an MJPEG AVI writer with source dimensions and source FPS where known."""
    if frame_rate is None:
        raise VideoWriterError(f"Cannot create annotated video '{path}' without a valid source frame rate.")
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), frame_rate.decimal, (width, height))
    if not writer.isOpened():
        writer.release()
        raise VideoWriterError(f"OpenCV could not open annotated-video writer '{path}'.")
    return writer


def render_frame(
    image: NDArray[uint8],
    detections: tuple[DetectionRecord, ...],
    frame_number: int,
    timestamp_seconds: float,
) -> NDArray[uint8]:
    """Return an annotated copy of one source-resolution frame."""
    annotated = image.copy()
    for detection in detections:
        color = CLASS_COLORS[detection.class_name]
        start = (round(detection.bbox_x1), round(detection.bbox_y1))
        end = (round(detection.bbox_x2), round(detection.bbox_y2))
        label = f"{detection.class_name} {detection.confidence:.2f}"
        cv2.rectangle(annotated, start, end, color, 2)
        cv2.putText(annotated, label, (start[0], max(16, start[1] - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    frame_label = f"frame {frame_number} | {timestamp_seconds:.3f}s"
    cv2.putText(annotated, frame_label, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return annotated


def verify_annotated_video(path: Path, expected_width: int, expected_height: int) -> None:
    """Confirm that an annotated artifact is a non-empty decodable video."""
    if not path.is_file() or path.stat().st_size == 0:
        raise VideoWriterError(f"Annotated output is missing or empty: '{path}'.")
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise VideoWriterError(f"Annotated output is not decodable: '{path}'.")
    width = round(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    decoded, _ = capture.read()
    capture.release()
    if not decoded:
        raise VideoWriterError(f"Annotated output has no decodable frames: '{path}'.")
    if (width, height) != (expected_width, expected_height):
        raise VideoWriterError(
            f"Annotated output dimensions are {width}x{height}, expected {expected_width}x{expected_height}: '{path}'."
        )


def render_tracking_frame(
    image: NDArray[uint8], records: tuple[TrackRecord, ...], frame_number: int, timestamp_seconds: float,
    tails: dict[int, list[tuple[int, int]]], last_boxes: dict[int, tuple[int, int, int, int]],
) -> NDArray[uint8]:
    """Render one finalized tracking frame, including state-aware boxes and trajectory tails."""
    annotated = image.copy()
    for record in records:
        color = _track_color(record.track_id)
        if record.state == "lost":
            previous = last_boxes.get(record.track_id)
            if previous is not None:
                _dashed_rectangle(annotated, previous[:2], previous[2:], (120, 120, 120))
                cv2.putText(annotated, f"#{record.track_id} LOST +{record.gap_length_frames}", (previous[0], max(16, previous[1] - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1, cv2.LINE_AA)
            continue
        if None in (record.bbox_x1, record.bbox_y1, record.bbox_x2, record.bbox_y2):
            continue
        start = (round(record.bbox_x1 or 0), round(record.bbox_y1 or 0))
        end = (round(record.bbox_x2 or 0), round(record.bbox_y2 or 0))
        draw_color = (0, 180, 255) if record.state == "interpolated" else color
        if record.state == "interpolated":
            _dashed_rectangle(annotated, start, end, draw_color)
        else:
            cv2.rectangle(annotated, start, end, draw_color, 2)
        cv2.putText(annotated, f"{record.class_name} #{record.track_id} {record.state}", (start[0], max(16, start[1] - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, draw_color, 1, cv2.LINE_AA)
        point = (round((record.bbox_x1 + record.bbox_x2) / 2), round(record.bbox_y2))
        tails.setdefault(record.track_id, []).append(point)
        tails[record.track_id] = tails[record.track_id][-30:]
        last_boxes[record.track_id] = (start[0], start[1], end[0], end[1])
    for track_id, points in tails.items():
        if len(points) > 1:
            cv2.polylines(annotated, [np.asarray(points, dtype=np.int32)], False, _track_color(track_id), 2, cv2.LINE_AA)
    cv2.putText(annotated, f"frame {frame_number} | {timestamp_seconds:.3f}s", (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return annotated


def _track_color(track_id: int) -> tuple[int, int, int]:
    return ((37 * track_id + 80) % 255, (71 * track_id + 120) % 255, (113 * track_id + 160) % 255)


def _dashed_rectangle(image: NDArray[uint8], start: tuple[int, int], end: tuple[int, int], color: tuple[int, int, int]) -> None:
    x1, y1 = start
    x2, y2 = end
    for first, second in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
        _dashed_line(image, first, second, color)


def _dashed_line(image: NDArray[uint8], first: tuple[int, int], second: tuple[int, int], color: tuple[int, int, int]) -> None:
    distance = max(abs(second[0] - first[0]), abs(second[1] - first[1]))
    if distance == 0:
        return
    for offset in range(0, distance, 10):
        end_offset = min(offset + 5, distance)
        start = (round(first[0] + (second[0] - first[0]) * offset / distance), round(first[1] + (second[1] - first[1]) * offset / distance))
        end = (round(first[0] + (second[0] - first[0]) * end_offset / distance), round(first[1] + (second[1] - first[1]) * end_offset / distance))
        cv2.line(image, start, end, color, 2, cv2.LINE_AA)
