"""ffprobe metadata extraction with explicit validation failures."""

import json
import subprocess
from pathlib import Path

from minivision_reconstruct.domain.errors import VideoMetadataError
from minivision_reconstruct.domain.models import FrameRate, VideoMetadata


def extract_video_metadata(video_path: Path, ffprobe_command: str) -> VideoMetadata:
    """Read metadata from ffprobe and retain unavailable fields as None."""
    command: list[str] = [
        ffprobe_command,
        "-v",
        "error",
        "-show_entries",
        "format=duration:format_tags=creation_time:stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise VideoMetadataError(
            f"ffprobe executable '{ffprobe_command}' was not found while reading '{video_path}'. "
            "Install FFmpeg or pass a valid --ffprobe command."
        ) from error
    if completed.returncode != 0:
        raise VideoMetadataError(
            f"ffprobe failed for '{video_path}' with exit code {completed.returncode}: "
            f"{completed.stderr.strip() or 'no stderr output'}"
        )
    try:
        payload: dict[str, object] = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise VideoMetadataError(
            f"ffprobe returned invalid JSON for '{video_path}': {error.msg}. "
            f"stderr: {completed.stderr.strip() or 'no stderr output'}"
        ) from error
    streams_value = payload.get("streams")
    if not isinstance(streams_value, list):
        raise VideoMetadataError(f"ffprobe returned no stream list for '{video_path}'.")
    video_stream = next(
        (stream for stream in streams_value if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    if not isinstance(video_stream, dict):
        raise VideoMetadataError(f"ffprobe found no video stream in '{video_path}'.")
    format_value = payload.get("format")
    format_record = format_value if isinstance(format_value, dict) else {}
    duration = _optional_float(format_record.get("duration"), "duration", video_path)
    width = _optional_positive_int(video_stream.get("width"), "width", video_path)
    height = _optional_positive_int(video_stream.get("height"), "height", video_path)
    frame_rate = _parse_frame_rate(video_stream, video_path)
    reported_frame_count = _optional_positive_int(video_stream.get("nb_frames"), "nb_frames", video_path)
    tags_value = format_record.get("tags")
    tags = tags_value if isinstance(tags_value, dict) else {}
    creation_value = tags.get("creation_time")
    creation_time = creation_value if isinstance(creation_value, str) else None
    codec_value = video_stream.get("codec_name")
    codec = codec_value if isinstance(codec_value, str) else None
    return VideoMetadata(
        original_filename=video_path.name,
        duration_seconds=duration,
        frame_rate=frame_rate,
        width=width,
        height=height,
        codec=codec,
        reported_frame_count=reported_frame_count,
        frame_count_source="reported" if reported_frame_count is not None else "unavailable",
        creation_time=creation_time,
        extraction_tool="ffprobe",
    )


def _optional_float(value: object, field_name: str, video_path: Path) -> float | None:
    if value is None or value == "N/A":
        return None
    try:
        parsed = float(str(value))
    except ValueError as error:
        raise VideoMetadataError(f"ffprobe returned invalid {field_name} '{value}' for '{video_path}'.") from error
    if parsed < 0:
        raise VideoMetadataError(f"ffprobe returned negative {field_name} '{value}' for '{video_path}'.")
    return parsed


def _optional_positive_int(value: object, field_name: str, video_path: Path) -> int | None:
    if value is None or value == "N/A":
        return None
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise VideoMetadataError(f"ffprobe returned invalid {field_name} '{value}' for '{video_path}'.") from error
    if parsed <= 0:
        raise VideoMetadataError(f"ffprobe returned non-positive {field_name} '{value}' for '{video_path}'.")
    return parsed


def _parse_frame_rate(stream: dict[str, object], video_path: Path) -> FrameRate | None:
    average_value = stream.get("avg_frame_rate")
    nominal_value = stream.get("r_frame_rate")
    raw_value = average_value if average_value not in (None, "0/0", "N/A") else nominal_value
    if raw_value is None or raw_value == "0/0" or raw_value == "N/A":
        return None
    try:
        numerator_text, denominator_text = str(raw_value).split("/", maxsplit=1)
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except ValueError as error:
        raise VideoMetadataError(f"ffprobe returned invalid frame rate '{raw_value}' for '{video_path}'.") from error
    if numerator <= 0 or denominator <= 0:
        raise VideoMetadataError(f"ffprobe returned invalid frame rate '{raw_value}' for '{video_path}'.")
    return FrameRate(numerator=numerator, denominator=denominator)
