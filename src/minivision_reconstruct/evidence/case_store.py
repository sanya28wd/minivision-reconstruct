"""Case creation and immutable evidence ingestion."""

from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil
from tempfile import NamedTemporaryFile
from uuid import uuid4

from minivision_reconstruct.domain.errors import (
    CaseAlreadyExistsError,
    EvidenceIntegrityError,
    InvalidCaseIdError,
    SourceVideoInvalidError,
    SourceVideoNotFoundError,
    VideoDecodingError,
)
from minivision_reconstruct.domain.models import EvidenceManifest, IngestedCase, VideoMetadata, record_to_json_value, utc_timestamp
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.evidence.metadata import extract_video_metadata
from minivision_reconstruct.video.reader import iter_frames


CASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SCHEMA_VERSION = "1.0"


def ingest_case(source_path: Path, cases_root: Path, requested_case_id: str | None, ffprobe_command: str) -> IngestedCase:
    """Create one evidence case, preserving bytes before inspecting the copy."""
    _validate_source_file(source_path)
    case_id = _resolve_case_id(source_path, requested_case_id)
    case_directory = cases_root / case_id
    _create_case_directories(case_directory)
    preserved_video_path = case_directory / "original" / source_path.name
    source_hash = sha256_file(source_path)
    _copy_original(source_path, preserved_video_path)
    preserved_hash = sha256_file(preserved_video_path)
    if source_hash != preserved_hash:
        raise EvidenceIntegrityError(
            f"Preserved evidence hash does not match source for case '{case_id}': "
            f"source={source_hash}, preserved={preserved_hash}."
        )
    metadata = extract_video_metadata(preserved_video_path, ffprobe_command)
    _validate_decodable_metadata(metadata, preserved_video_path)
    metadata_path = case_directory / "metadata" / "video_metadata.json"
    _write_json_atomically(metadata_path, record_to_json_value(metadata))
    manifest = _build_manifest(
        case_id,
        source_path,
        cases_root,
        preserved_video_path,
        source_hash,
        preserved_hash,
        metadata,
    )
    manifest_path = case_directory / "metadata" / "evidence_manifest.json"
    _write_json_atomically(manifest_path, record_to_json_value(manifest))
    return IngestedCase(
        case_id=case_id,
        case_directory=case_directory,
        preserved_video_path=preserved_video_path,
        metadata_path=metadata_path,
        manifest_path=manifest_path,
        metadata=metadata,
        manifest=manifest,
    )


def _validate_source_file(source_path: Path) -> None:
    if not source_path.exists():
        raise SourceVideoNotFoundError(f"Input video does not exist: '{source_path}'.")
    if not source_path.is_file():
        raise SourceVideoInvalidError(f"Input video is not a regular file: '{source_path}'.")
    if source_path.stat().st_size == 0:
        raise SourceVideoInvalidError(f"Input video is empty: '{source_path}'.")


def _resolve_case_id(source_path: Path, requested_case_id: str | None) -> str:
    if requested_case_id is None:
        generated_stem = re.sub(r"[^A-Za-z0-9_-]+", "-", source_path.stem).strip("-") or "case"
        return f"{generated_stem[:48]}-{uuid4().hex[:12]}"
    if CASE_ID_PATTERN.fullmatch(requested_case_id) is None:
        raise InvalidCaseIdError(
            f"Invalid case ID '{requested_case_id}'. Use 1-64 ASCII letters, digits, hyphens, or underscores; start with a letter or digit."
        )
    return requested_case_id


def _create_case_directories(case_directory: Path) -> None:
    try:
        case_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise CaseAlreadyExistsError(
            f"Case '{case_directory.name}' already exists at '{case_directory}'. Existing evidence will not be overwritten."
        ) from error
    for directory_name in ("original", "metadata", "analysis", "outputs"):
        (case_directory / directory_name).mkdir()


def _copy_original(source_path: Path, preserved_video_path: Path) -> None:
    if preserved_video_path.exists():
        raise CaseAlreadyExistsError(f"Original evidence path already exists: '{preserved_video_path}'.")
    shutil.copyfile(source_path, preserved_video_path)


def _validate_decodable_metadata(metadata: VideoMetadata, video_path: Path) -> None:
    if metadata.width is None or metadata.height is None:
        raise VideoDecodingError(f"ffprobe could not determine video dimensions for '{video_path}'.")
    if metadata.duration_seconds is not None and metadata.duration_seconds == 0:
        raise VideoDecodingError(f"ffprobe reported zero duration for non-empty video '{video_path}'.")


def _build_manifest(
    case_id: str,
    source_path: Path,
    cases_root: Path,
    preserved_video_path: Path,
    source_hash: str,
    preserved_hash: str,
    metadata: VideoMetadata,
) -> EvidenceManifest:
    decoded_count, first_timestamp, last_timestamp = _decode_summary(preserved_video_path, metadata)
    frame_rate = metadata.frame_rate
    frame_count_source = metadata.frame_count_source if metadata.reported_frame_count is not None else "derived"
    return EvidenceManifest(
        schema_version=SCHEMA_VERSION,
        case_id=case_id,
        original_source_filename=source_path.name,
        preserved_relative_path=preserved_video_path.relative_to(cases_root).as_posix(),
        source_sha256=source_hash,
        preserved_sha256=preserved_hash,
        file_size_bytes=source_path.stat().st_size,
        duration_seconds=metadata.duration_seconds,
        frame_rate_numerator=frame_rate.numerator if frame_rate is not None else None,
        frame_rate_denominator=frame_rate.denominator if frame_rate is not None else None,
        frame_rate_decimal=frame_rate.decimal if frame_rate is not None else None,
        width=metadata.width,
        height=metadata.height,
        codec=metadata.codec,
        reported_frame_count=metadata.reported_frame_count,
        decoded_frame_count=decoded_count,
        frame_count_source=frame_count_source,
        first_decoded_timestamp_seconds=first_timestamp,
        last_decoded_timestamp_seconds=last_timestamp,
        metadata_extraction_tool=metadata.extraction_tool,
        ingested_at=utc_timestamp(datetime.now(UTC)),
    )


def _decode_summary(video_path: Path, metadata: VideoMetadata) -> tuple[int, float | None, float | None]:
    count = 0
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    for frame in iter_frames(video_path, metadata.frame_rate):
        if frame.number != count:
            raise VideoDecodingError(
                f"Decoder returned non-sequential frame number {frame.number}; expected {count} for '{video_path}'."
            )
        if last_timestamp is not None and frame.timestamp_seconds <= last_timestamp:
            raise VideoDecodingError(f"Decoder returned non-monotonic timestamps for '{video_path}' at frame {frame.number}.")
        if first_timestamp is None:
            first_timestamp = frame.timestamp_seconds
        last_timestamp = frame.timestamp_seconds
        count += 1
    if count == 0:
        raise VideoDecodingError(f"OpenCV decoded zero frames from non-empty video '{video_path}'.")
    return count, first_timestamp, last_timestamp


def _write_json_atomically(path: Path, value: dict[str, object]) -> None:
    serialized = json.dumps(value, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary.write(serialized)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)
