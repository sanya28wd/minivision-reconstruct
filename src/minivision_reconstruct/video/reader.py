"""Streaming frame decoder.

OpenCV's CAP_PROP_POS_MSEC is used as the presentation timestamp when it is
available and strictly monotonic.  OpenCV exposes this as a floating-point
decoder position, so it is not a bit-exact container PTS for every codec. If
it is unavailable or non-monotonic after the first frame, the timestamp is
calculated as frame_number / exact_frame_rate.
"""

from collections.abc import Iterator
from pathlib import Path

import cv2

from minivision_reconstruct.domain.errors import TimestampError, VideoDecodingError
from minivision_reconstruct.domain.models import Frame, FrameRate


def iter_frames(video_path: Path, frame_rate: FrameRate | None) -> Iterator[Frame]:
    """Yield decoded frames sequentially without retaining prior frames."""
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        raise VideoDecodingError(f"OpenCV could not open preserved video '{video_path}'.")
    frame_number = 0
    previous_timestamp: float | None = None
    try:
        while True:
            decoded, image = capture.read()
            if not decoded:
                _raise_unexpected_decode_failure(capture, frame_number, video_path)
                return
            raw_timestamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            timestamp, source = _frame_timestamp(
                raw_timestamp,
                previous_timestamp,
                frame_number,
                frame_rate,
                video_path,
            )
            yield Frame(number=frame_number, timestamp_seconds=timestamp, timestamp_source=source, image=image)
            previous_timestamp = timestamp
            frame_number += 1
    finally:
        capture.release()


def read_frame_at(video_path: Path, frame_rate: FrameRate | None, frame_number: int) -> Frame:
    """Decode one zero-based frame for interactive review navigation."""
    if frame_number < 0:
        raise VideoDecodingError(f"Frame number must be non-negative; received {frame_number}.")
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        raise VideoDecodingError(f"OpenCV could not open preserved video '{video_path}'.")
    try:
        if not capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number):
            raise VideoDecodingError(f"OpenCV could not seek to frame {frame_number} of '{video_path}'.")
        decoded, image = capture.read()
        if not decoded:
            raise VideoDecodingError(f"Frame {frame_number} could not be decoded from '{video_path}'.")
        raw_timestamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if raw_timestamp > 0:
            timestamp, source = raw_timestamp, "presentation_timestamp"
        elif frame_rate is not None:
            timestamp, source = frame_number / frame_rate.decimal, "frame_rate_fallback"
        else:
            raise TimestampError(
                f"Frame {frame_number} of '{video_path}' has no usable timestamp and no valid frame rate."
            )
        return Frame(frame_number, timestamp, source, image)
    finally:
        capture.release()


def _raise_unexpected_decode_failure(capture: cv2.VideoCapture, frame_number: int, video_path: Path) -> None:
    reported_count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
    if reported_count > 0 and frame_number < int(round(reported_count)):
        raise VideoDecodingError(
            f"Decoding stopped unexpectedly at frame {frame_number} of approximately {reported_count:g} for '{video_path}'."
        )


def _frame_timestamp(
    raw_timestamp: float,
    previous_timestamp: float | None,
    frame_number: int,
    frame_rate: FrameRate | None,
    video_path: Path,
) -> tuple[float, str]:
    if raw_timestamp >= 0 and (previous_timestamp is None or raw_timestamp > previous_timestamp):
        return raw_timestamp, "presentation_timestamp"
    if frame_rate is None:
        raise TimestampError(
            f"Frame {frame_number} of '{video_path}' has no usable presentation timestamp and ffprobe reported no valid frame rate."
        )
    return frame_number / frame_rate.decimal, "frame_rate_fallback"
