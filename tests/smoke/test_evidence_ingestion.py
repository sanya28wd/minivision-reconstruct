"""End-to-end smoke coverage for byte-preserving video evidence ingestion."""

import json
from pathlib import Path
import shutil

import cv2
import numpy as np
import pytest

from minivision_reconstruct.domain.errors import (
    CaseAlreadyExistsError,
    SourceVideoInvalidError,
    SourceVideoNotFoundError,
    VideoDecodingError,
    VideoMetadataError,
)
from minivision_reconstruct.evidence.case_store import ingest_case
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.video.reader import iter_frames


FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(FFPROBE is None, reason="ffprobe must be installed for the evidence ingestion smoke workflow")


def test_ingests_video_and_preserves_original(tmp_path: Path) -> None:
    source = tmp_path / "fixture.avi"
    _write_fixture_video(source)
    cases_root = tmp_path / "cases"
    source_hash_before = sha256_file(source)

    result = ingest_case(source, cases_root, "fixture-case", _ffprobe_path())

    assert result.manifest.source_sha256 == result.manifest.preserved_sha256 == source_hash_before
    assert sha256_file(source) == source_hash_before
    assert result.manifest.decoded_frame_count == 6
    assert result.manifest.reported_frame_count in (None, 6)
    assert result.metadata_path.is_file()
    assert result.manifest_path.is_file()
    assert json.loads(result.metadata_path.read_text(encoding="utf-8"))["width"] == 32
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["preserved_relative_path"] == "fixture-case/original/fixture.avi"
    frames = list(iter_frames(result.preserved_video_path, result.metadata.frame_rate))
    assert [frame.number for frame in frames] == list(range(6))
    assert all(right.timestamp_seconds > left.timestamp_seconds for left, right in zip(frames, frames[1:]))
    assert sha256_file(result.preserved_video_path) == source_hash_before


def test_rejects_missing_empty_and_reused_cases(tmp_path: Path) -> None:
    cases_root = tmp_path / "cases"
    with pytest.raises(SourceVideoNotFoundError):
        ingest_case(tmp_path / "missing.avi", cases_root, "missing-case", _ffprobe_path())
    empty_video = tmp_path / "empty.avi"
    empty_video.touch()
    with pytest.raises(SourceVideoInvalidError):
        ingest_case(empty_video, cases_root, "empty-case", _ffprobe_path())
    source = tmp_path / "fixture.avi"
    _write_fixture_video(source)
    ingest_case(source, cases_root, "existing-case", _ffprobe_path())
    with pytest.raises(CaseAlreadyExistsError):
        ingest_case(source, cases_root, "existing-case", _ffprobe_path())


def test_rejects_corrupt_nonempty_video(tmp_path: Path) -> None:
    corrupt_video = tmp_path / "corrupt.avi"
    corrupt_video.write_bytes(b"not a video")
    with pytest.raises((VideoMetadataError, VideoDecodingError)) as error:
        ingest_case(corrupt_video, tmp_path / "cases", "corrupt-case", _ffprobe_path())
    assert "ffprobe" in str(error.value) or "OpenCV" in str(error.value)


def _write_fixture_video(destination: Path) -> None:
    writer = cv2.VideoWriter(str(destination), cv2.VideoWriter_fourcc(*"MJPG"), 5.0, (32, 24))
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not create the deterministic MJPEG smoke fixture.")
    for frame_number in range(6):
        image = np.full((24, 32, 3), frame_number * 30, dtype=np.uint8)
        writer.write(image)
    writer.release()


def _ffprobe_path() -> str:
    if FFPROBE is None:
        raise RuntimeError("ffprobe is unavailable despite the smoke test skip condition.")
    return FFPROBE
