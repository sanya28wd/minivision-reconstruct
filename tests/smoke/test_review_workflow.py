"""Smoke coverage for audit replay, fragmentation repair, and reviewed exports."""

from dataclasses import asdict
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from minivision_reconstruct.domain.errors import TrackReviewError
from minivision_reconstruct.domain.models import EvidenceManifest, TrackRecord
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.perception.review_workflow import (
    ReviewContext,
    append_review_event,
    export_reviewed_revision,
    has_undoable_review_event,
    open_review_context,
    read_review_events,
    rebuild_reviewed_tracks,
)


def test_fragmented_track_merge_gap_repair_and_immutable_export(tmp_path: Path) -> None:
    cases_root = tmp_path / "cases"
    case_directory = cases_root / "case-a"
    original_directory = case_directory / "original"
    metadata_directory = case_directory / "metadata"
    track_directory = case_directory / "analysis" / "tracks" / "auto-1"
    original_directory.mkdir(parents=True)
    metadata_directory.mkdir(parents=True)
    track_directory.mkdir(parents=True)
    video_path = original_directory / "source.avi"
    _write_video(video_path)
    video_hash = sha256_file(video_path)
    manifest = EvidenceManifest(
        schema_version="1.0", case_id="case-a", original_source_filename="source.avi",
        preserved_relative_path="case-a/original/source.avi", source_sha256=video_hash, preserved_sha256=video_hash,
        file_size_bytes=video_path.stat().st_size, duration_seconds=1.0, frame_rate_numerator=5,
        frame_rate_denominator=1, frame_rate_decimal=5.0, width=32, height=24, codec="mjpeg",
        reported_frame_count=5, decoded_frame_count=5, frame_count_source="reported",
        first_decoded_timestamp_seconds=0.0, last_decoded_timestamp_seconds=0.8,
        metadata_extraction_tool="test", ingested_at="2026-01-01T00:00:00Z",
    )
    (metadata_directory / "evidence_manifest.json").write_text(json.dumps(asdict(manifest)), encoding="utf-8")
    automatic = (
        _record(0, 1, 0.0), _record(1, 1, 10.0), _record(3, 2, 30.0), _record(4, 2, 40.0),
    )
    tracks_path = track_directory / "tracks.jsonl"
    tracks_path.write_text("".join(json.dumps(asdict(record), sort_keys=True) + "\n" for record in automatic), encoding="utf-8")
    (track_directory / "run_manifest.json").write_text(json.dumps({"tracks_sha256": sha256_file(tracks_path)}), encoding="utf-8")
    context = open_review_context(cases_root, "case-a", "auto-1")

    append_review_event(context, "Reviewer One", "merge", {"primary_track_id": 1, "secondary_track_id": 2})
    append_review_event(context, "Reviewer One", "repair_gap", {"track_id": 1, "start_frame": 2, "end_frame": 2})
    events = read_review_events(context.event_log_path, "case-a", "auto-1")
    reviewed = rebuild_reviewed_tracks(automatic, events)
    repaired = next(item for item in reviewed if item.record.track_id == 1 and item.record.frame_number == 2)
    assert repaired.record.state == "interpolated"
    assert repaired.record.bbox_x1 == 20.0
    assert repaired.provenance == "review_interpolation"
    assert all(record.track_id in {1, 2} for record in automatic)
    assert [record.track_id for record in automatic] == [1, 1, 2, 2]

    revision = export_reviewed_revision(context, automatic)
    exported_records = [json.loads(line) for line in (revision / "tracks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {(value["track_id"], value["frame_number"]) for value in exported_records} == {
        (1, 0), (1, 1), (1, 2), (1, 3), (1, 4),
    }
    assert all(value["review_schema_version"] == "1.0" for value in exported_records)
    assert (revision / "reviewed.avi").is_file()
    export_manifest = json.loads((revision / "manifest.json").read_text(encoding="utf-8"))
    assert export_manifest["source_tracks_sha256"] == sha256_file(tracks_path)
    assert export_manifest["review_event_count"] == 2
    assert sha256_file(video_path) == video_hash
    assert sha256_file(tracks_path) == context.source_records_sha256


def test_review_replay_supports_split_correction_add_and_delete(tmp_path: Path) -> None:
    automatic = (_record(0, 4, 2.0), _record(1, 4, 4.0), _record(2, 4, 6.0))
    context = _context(tmp_path, automatic)
    append_review_event(context, "Reviewer", "correct", {
        "track_id": 4, "frame_number": 0, "bbox": [3, 2, 12, 12], "frame_width": 32, "frame_height": 24,
        "class_name": "truck",
    })
    append_review_event(context, "Reviewer", "split", {"track_id": 4, "frame_number": 2, "new_track_id": 5})
    append_review_event(context, "Reviewer", "add", {
        "track_id": 4, "frame_number": 3, "timestamp_seconds": 0.6, "class_name": "car",
        "bbox": [8, 4, 18, 18], "frame_width": 32, "frame_height": 24,
    })
    append_review_event(context, "Reviewer", "delete_observation", {"track_id": 4, "frame_number": 1})
    reviewed = rebuild_reviewed_tracks(automatic, read_review_events(context.event_log_path, "case-a", "auto-1"))
    assert {(item.record.track_id, item.record.frame_number) for item in reviewed} == {(4, 0), (5, 2), (4, 3)}
    corrected = next(item for item in reviewed if item.record.frame_number == 0)
    assert corrected.record.class_name == "truck"
    assert corrected.record.bbox_x1 == 3
    assert corrected.provenance == "reviewer"
    manual = next(item for item in reviewed if item.record.frame_number == 3)
    assert manual.record.state == "manual"
    assert manual.record.confidence is None


def test_merge_rejects_overlapping_boxed_observations(tmp_path: Path) -> None:
    automatic = (_record(0, 1, 1), _record(0, 2, 2))
    context = _context(tmp_path, automatic)
    with pytest.raises(TrackReviewError, match="overlap"):
        append_review_event(context, "Reviewer", "merge", {"primary_track_id": 1, "secondary_track_id": 2})


def test_gap_repair_rejects_more_than_five_frames(tmp_path: Path) -> None:
    automatic = (_record(0, 1, 1), _record(7, 1, 10))
    context = _context(tmp_path, automatic)
    with pytest.raises(TrackReviewError, match="1 to 5"):
        append_review_event(context, "Reviewer", "repair_gap", {"track_id": 1, "start_frame": 1, "end_frame": 6})
    assert not read_review_events(context.event_log_path, "case-a", "auto-1")


def test_correction_rejects_box_outside_frame(tmp_path: Path) -> None:
    automatic = (_record(0, 1, 1),)
    context = _context(tmp_path, automatic)
    with pytest.raises(TrackReviewError, match="inside the 32x24 frame"):
        append_review_event(context, "Reviewer", "correct", {
            "track_id": 1, "frame_number": 0, "bbox": [2, 2, 40, 12],
            "frame_width": 32, "frame_height": 24,
        })


def test_manual_box_fills_a_lost_state(tmp_path: Path) -> None:
    lost = _lost_record(1, 1)
    automatic = (_record(0, 1, 1), lost, _record(2, 1, 5))
    context = _context(tmp_path, automatic)
    append_review_event(context, "Reviewer", "add", {
        "track_id": 1, "frame_number": 1, "timestamp_seconds": 0.2, "class_name": "car",
        "bbox": [3, 2, 11, 12], "frame_width": 32, "frame_height": 24,
    })
    reviewed = rebuild_reviewed_tracks(automatic, read_review_events(context.event_log_path, "case-a", "auto-1"))
    manual = next(item for item in reviewed if item.record.frame_number == 1)
    assert manual.record.state == "manual"
    assert manual.record.bbox_x1 == 3


def test_create_new_track_when_object_has_never_been_tracked(tmp_path: Path) -> None:
    automatic = (_record(0, 1, 1),)
    context = _context(tmp_path, automatic)
    append_review_event(context, "Reviewer", "create_track", {
        "track_id": 2, "frame_number": 0, "timestamp_seconds": 0.0, "class_name": "car",
        "bbox": [15, 2, 24, 12], "frame_width": 32, "frame_height": 24,
    })
    events = read_review_events(context.event_log_path, "case-a", "auto-1")
    reviewed = rebuild_reviewed_tracks(automatic, events)
    created = next(item for item in reviewed if item.record.track_id == 2)
    assert created.record.frame_number == 0
    assert created.record.state == "manual"
    assert created.record.bbox_x1 == 15
    assert created.provenance == "reviewer"
    assert automatic == (_record(0, 1, 1),)


def test_correcting_lost_box_changes_state_to_manual(tmp_path: Path) -> None:
    automatic = (_record(0, 1, 1), _lost_record(1, 1), _record(2, 1, 5))
    context = _context(tmp_path, automatic)
    append_review_event(context, "Reviewer", "correct", {
        "track_id": 1, "frame_number": 1, "bbox": [3, 2, 11, 12],
        "frame_width": 32, "frame_height": 24,
    })
    reviewed = rebuild_reviewed_tracks(automatic, read_review_events(context.event_log_path, "case-a", "auto-1"))
    manual = next(item for item in reviewed if item.record.frame_number == 1)
    assert manual.record.state == "manual"
    assert manual.record.confidence is None


def test_event_log_rejects_tampering(tmp_path: Path) -> None:
    automatic = (_record(0, 1, 2.0),)
    context = _context(tmp_path, automatic)
    append_review_event(context, "Reviewer", "delete_track", {"track_id": 1})
    content = context.event_log_path.read_text(encoding="utf-8").replace("delete_track", "delete_object")
    context.event_log_path.write_text(content, encoding="utf-8")
    with pytest.raises(TrackReviewError, match="hash"):
        read_review_events(context.event_log_path, "case-a", "auto-1")


def test_undo_appends_compensating_events(tmp_path: Path) -> None:
    from minivision_reconstruct.perception.review_workflow import undo_latest_review_event

    automatic = (_record(0, 1, 2.0),)
    context = _context(tmp_path, automatic)
    append_review_event(context, "Reviewer", "correct", {
        "track_id": 1, "frame_number": 0, "class_name": "truck",
    })
    append_review_event(context, "Reviewer", "correct", {
        "track_id": 1, "frame_number": 0, "class_name": "bus",
    })
    undo_latest_review_event(context, "Reviewer", automatic)
    events = read_review_events(context.event_log_path, "case-a", "auto-1")
    assert has_undoable_review_event(events)
    assert rebuild_reviewed_tracks(automatic, events)[0].record.class_name == "truck"
    undo_latest_review_event(context, "Reviewer", automatic)
    events = read_review_events(context.event_log_path, "case-a", "auto-1")
    assert not has_undoable_review_event(events)
    assert rebuild_reviewed_tracks(automatic, events)[0].record.class_name == "car"


def _record(frame_number: int, track_id: int, x1: float) -> TrackRecord:
    return TrackRecord(
        case_id="case-a", run_id="auto-1", frame_number=frame_number, timestamp_seconds=frame_number / 5,
        track_id=track_id, class_id=2, class_name="car", confidence=0.9,
        bbox_x1=x1, bbox_y1=2, bbox_x2=x1 + 8, bbox_y2=12,
        state="observed", track_age_frames=frame_number + 1, gap_length_frames=0, schema_version="1.0",
    )


def _lost_record(frame_number: int, track_id: int) -> TrackRecord:
    return TrackRecord(
        case_id="case-a", run_id="auto-1", frame_number=frame_number, timestamp_seconds=frame_number / 5,
        track_id=track_id, class_id=2, class_name="car", confidence=None,
        bbox_x1=None, bbox_y1=None, bbox_x2=None, bbox_y2=None,
        state="lost", track_age_frames=frame_number, gap_length_frames=1, schema_version="1.0",
    )


def _write_video(path: Path) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 5.0, (32, 24))
    if not writer.isOpened():
        pytest.skip("OpenCV MJPEG writer is unavailable in this environment.")
    try:
        for number in range(5):
            writer.write(np.full((24, 32, 3), number * 20, dtype=np.uint8))
    finally:
        writer.release()


def _context(tmp_path: Path, automatic_records: tuple[TrackRecord, ...]) -> ReviewContext:
    evidence_path = tmp_path / "source.avi"
    evidence_path.write_bytes(b"evidence")
    records_path = tmp_path / "tracks.jsonl"
    records_path.write_text(
        "".join(json.dumps(asdict(record), sort_keys=True) + "\n" for record in automatic_records),
        encoding="utf-8",
    )
    evidence_hash = sha256_file(evidence_path)
    return ReviewContext(
        case_id="case-a", source_run_id="auto-1", case_directory=tmp_path, run_directory=tmp_path,
        evidence_path=evidence_path, evidence_manifest=EvidenceManifest(
            schema_version="1.0", case_id="case-a", original_source_filename="source.avi",
            preserved_relative_path="case-a/original/source.avi", source_sha256=evidence_hash, preserved_sha256=evidence_hash,
            file_size_bytes=1, duration_seconds=1.0, frame_rate_numerator=5, frame_rate_denominator=1,
            frame_rate_decimal=5.0, width=32, height=24, codec="mjpeg", reported_frame_count=5,
            decoded_frame_count=5, frame_count_source="reported", first_decoded_timestamp_seconds=0.0,
            last_decoded_timestamp_seconds=0.8, metadata_extraction_tool="test", ingested_at="2026-01-01T00:00:00Z",
        ), source_records_path=records_path, source_records_sha256=sha256_file(records_path), source_video_path=evidence_path,
        event_log_path=tmp_path / "events.jsonl",
    )
