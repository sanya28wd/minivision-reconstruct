"""Append-only human review events, deterministic track replay, and reviewed exports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from minivision_reconstruct.domain.errors import CaseNotFoundError, TrackReviewError
from minivision_reconstruct.domain.models import (
    EvidenceManifest,
    ReviewAction,
    ReviewEvent,
    ReviewedTrackRecord,
    TrackRecord,
    SUPPORTED_CLASS_NAMES,
)
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.perception.bytetrack import CLASS_IDS
from minivision_reconstruct.perception.tracking_workflow import _frame_rate, _load_case_manifest
from minivision_reconstruct.video.reader import iter_frames
from minivision_reconstruct.video.renderer import create_video_writer, render_tracking_frame, verify_annotated_video


REVIEW_EVENT_SCHEMA_VERSION = "1.0"
REVIEW_EXPORT_SCHEMA_VERSION = "1.0"
MAX_REPAIR_GAP_FRAMES = 5


@dataclass(frozen=True)
class ReviewContext:
    """Paths and integrity data for one selected case and automatic run."""

    case_id: str
    source_run_id: str
    case_directory: Path
    run_directory: Path
    evidence_path: Path
    evidence_manifest: EvidenceManifest
    source_records_path: Path
    source_records_sha256: str
    source_video_path: Path
    event_log_path: Path


def open_review_context(cases_root: Path, case_id: str, source_run_id: str) -> ReviewContext:
    """Resolve and verify immutable inputs for a review session."""
    case_directory = cases_root / case_id
    evidence_manifest = _load_case_manifest(case_directory, case_id)
    evidence_path = cases_root / evidence_manifest.preserved_relative_path
    if not evidence_path.is_file():
        raise CaseNotFoundError(f"Preserved video for case '{case_id}' is missing at '{evidence_path}'.")
    if sha256_file(evidence_path) != evidence_manifest.preserved_sha256:
        raise TrackReviewError(f"Preserved video hash mismatch for case '{case_id}': '{evidence_path}'.")
    run_directory = case_directory / "analysis" / "tracks" / source_run_id
    records_path = run_directory / "tracks.jsonl"
    run_manifest_path = run_directory / "run_manifest.json"
    if not records_path.is_file() or not run_manifest_path.is_file():
        raise CaseNotFoundError(f"Tracking run '{source_run_id}' is incomplete for case '{case_id}'.")
    run_manifest = _read_json(run_manifest_path)
    actual_tracks_hash = sha256_file(records_path)
    if run_manifest.get("tracks_sha256") != actual_tracks_hash:
        raise TrackReviewError(f"Automatic tracks hash mismatch for case '{case_id}', run '{source_run_id}'.")
    event_log_path = case_directory / "analysis" / "reviews" / source_run_id / "events.jsonl"
    return ReviewContext(
        case_id=case_id,
        source_run_id=source_run_id,
        case_directory=case_directory,
        run_directory=run_directory,
        evidence_path=evidence_path,
        evidence_manifest=evidence_manifest,
        source_records_path=records_path,
        source_records_sha256=actual_tracks_hash,
        source_video_path=evidence_path,
        event_log_path=event_log_path,
    )


def load_track_records(path: Path, case_id: str, run_id: str) -> tuple[TrackRecord, ...]:
    """Load and validate automatic JSONL track records."""
    if not path.is_file():
        raise CaseNotFoundError(f"Track records are missing: '{path}'.")
    records: list[TrackRecord] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                record = TrackRecord(**value)
                if record.case_id != case_id or record.run_id != run_id:
                    raise TrackReviewError(f"Track record at '{path}:{line_number}' does not match selected case/run.")
                records.append(record)
    except (json.JSONDecodeError, TypeError, KeyError) as error:
        raise TrackReviewError(f"Invalid track record in '{path}': {error}") from error
    return tuple(records)


def initial_review_records(records: tuple[TrackRecord, ...]) -> tuple[ReviewedTrackRecord, ...]:
    """Wrap automatic records with explicit origin metadata."""
    return tuple(ReviewedTrackRecord(record, "automatic") for record in records)


def read_review_events(path: Path, case_id: str, source_run_id: str) -> tuple[ReviewEvent, ...]:
    """Read an event stream and reject broken ordering or hash links."""
    if not path.exists():
        return ()
    events: list[ReviewEvent] = []
    previous_hash: str | None = None
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                event = ReviewEvent(**json.loads(line))
                if event.schema_version != REVIEW_EVENT_SCHEMA_VERSION:
                    raise TrackReviewError(f"Unsupported review event schema '{event.schema_version}' at '{path}:{line_number}'.")
                if event.sequence != len(events) + 1:
                    raise TrackReviewError(f"Review event sequence error at '{path}:{line_number}'.")
                if event.case_id != case_id or event.source_run_id != source_run_id:
                    raise TrackReviewError(f"Review event at '{path}:{line_number}' targets a different case or run.")
                if event.previous_event_sha256 != previous_hash:
                    raise TrackReviewError(f"Review event hash chain is broken at '{path}:{line_number}'.")
                if event.event_sha256 != _event_hash(event):
                    raise TrackReviewError(f"Review event hash is invalid at '{path}:{line_number}'.")
                previous_hash = event.event_sha256
                events.append(event)
    except (json.JSONDecodeError, TypeError, KeyError) as error:
        raise TrackReviewError(f"Invalid review event in '{path}': {error}") from error
    return tuple(events)


def append_review_event(
    context: ReviewContext,
    reviewer: str,
    action: ReviewAction,
    payload: dict[str, object],
) -> ReviewEvent:
    """Append one hash-linked immutable reviewer event."""
    normalized_reviewer = reviewer.strip()
    if not normalized_reviewer:
        raise TrackReviewError("Reviewer name cannot be empty.")
    _verify_review_tracks(context)
    path = context.event_log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0)
        existing_bytes = stream.read()
        existing_events = read_review_events_from_text(existing_bytes, context.case_id, context.source_run_id, path)
        previous_hash = existing_events[-1].event_sha256 if existing_events else None
        event = ReviewEvent(
            schema_version=REVIEW_EVENT_SCHEMA_VERSION,
            event_id=uuid4().hex,
            sequence=len(existing_events) + 1,
            case_id=context.case_id,
            source_run_id=context.source_run_id,
            reviewer=normalized_reviewer,
            occurred_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            action=action,
            payload=payload,
            previous_event_sha256=previous_hash,
            event_sha256="",
        )
        event = replace(event, event_sha256=_event_hash(event))
        automatic_records = load_track_records(context.source_records_path, context.case_id, context.source_run_id)
        rebuild_reviewed_tracks(automatic_records, (*existing_events, event))
        stream.seek(0, os.SEEK_END)
        stream.write(json.dumps(asdict(event), sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return event


def read_review_events_from_text(text: str, case_id: str, source_run_id: str, path: Path) -> tuple[ReviewEvent, ...]:
    """Validate event bytes already read under the append lock."""
    events: list[ReviewEvent] = []
    previous_hash: str | None = None
    try:
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            event = ReviewEvent(**json.loads(line))
            if event.schema_version != REVIEW_EVENT_SCHEMA_VERSION:
                raise TrackReviewError(f"Unsupported review event schema '{event.schema_version}' at '{path}:{line_number}'.")
            if event.sequence != len(events) + 1 or event.previous_event_sha256 != previous_hash:
                raise TrackReviewError(f"Review event order or hash link is broken at '{path}:{line_number}'.")
            if event.case_id != case_id or event.source_run_id != source_run_id or event.event_sha256 != _event_hash(event):
                raise TrackReviewError(f"Review event integrity check failed at '{path}:{line_number}'.")
            events.append(event)
            previous_hash = event.event_sha256
    except (json.JSONDecodeError, TypeError, KeyError) as error:
        raise TrackReviewError(f"Invalid review event in '{path}': {error}") from error
    return tuple(events)


def rebuild_reviewed_tracks(
    automatic_records: tuple[TrackRecord, ...], events: tuple[ReviewEvent, ...]
) -> tuple[ReviewedTrackRecord, ...]:
    """Replay validated review actions over automatic track records."""
    values = list(initial_review_records(automatic_records))
    for event in events:
        values = _apply_event(values, event)
    return tuple(sorted(values, key=lambda item: (item.record.frame_number, item.record.track_id)))


def undo_latest_review_event(
    context: ReviewContext,
    reviewer: str,
    automatic_records: tuple[TrackRecord, ...],
) -> ReviewEvent:
    """Append a compensating restore event for the latest action."""
    events = read_review_events(context.event_log_path, context.case_id, context.source_run_id)
    undone_event_ids = {
        event.payload.get("target_event_id")
        for event in events
        if event.action == "restore" and isinstance(event.payload.get("target_event_id"), str)
    }
    active_events = tuple(
        event for event in events if event.action != "restore" and event.event_id not in undone_event_ids
    )
    if not active_events:
        raise TrackReviewError("There is no review action available to undo.")
    target_event = active_events[-1]
    target_index = events.index(target_event)
    prior = rebuild_reviewed_tracks(automatic_records, events[:target_index])
    snapshot = [
        {**asdict(item.record), "provenance": item.provenance}
        for item in prior
    ]
    return append_review_event(context, reviewer, "restore", {"target_event_id": target_event.event_id, "records": snapshot})


def has_undoable_review_event(events: tuple[ReviewEvent, ...]) -> bool:
    """Report whether an active action remains to be compensated."""
    undone_event_ids = {
        event.payload.get("target_event_id")
        for event in events
        if event.action == "restore" and isinstance(event.payload.get("target_event_id"), str)
    }
    return any(event.action != "restore" and event.event_id not in undone_event_ids for event in events)


def export_reviewed_revision(
    context: ReviewContext,
    automatic_records: tuple[TrackRecord, ...],
) -> Path:
    """Write a non-overwriting numbered reviewed JSONL/video snapshot and manifest."""
    _verify_review_evidence(context)
    _verify_review_tracks(context)
    event_log_hash = sha256_file(context.event_log_path) if context.event_log_path.exists() else sha256_bytes(b"")
    events = read_review_events(context.event_log_path, context.case_id, context.source_run_id)
    reviewed = rebuild_reviewed_tracks(automatic_records, events)
    review_directory = context.event_log_path.parent
    review_directory.mkdir(parents=True, exist_ok=True)
    revision_number = _next_revision_number(review_directory)
    revision_id = f"revision-{revision_number:03d}"
    output_directory = review_directory / revision_id
    with TemporaryDirectory(prefix=f".{revision_id}-", dir=review_directory) as staging_name:
        staging_directory = Path(staging_name)
        records_path = staging_directory / "tracks.jsonl"
        with records_path.open("x", encoding="utf-8") as stream:
            for item in reviewed:
                value = _reviewed_record_json_value(item)
                stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        video_path = staging_directory / "reviewed.avi"
        _render_reviewed_video(context, reviewed, video_path)
        current_event_log_hash = sha256_file(context.event_log_path) if context.event_log_path.exists() else sha256_bytes(b"")
        if current_event_log_hash != event_log_hash:
            raise TrackReviewError("Review events changed while the export was being rendered; export again.")
        manifest = {
            "schema_version": REVIEW_EXPORT_SCHEMA_VERSION,
            "track_schema_version": automatic_records[0].schema_version if automatic_records else None,
            "case_id": context.case_id,
            "source_run_id": context.source_run_id,
            "revision_id": revision_id,
            "source_tracks_sha256": context.source_records_sha256,
            "review_events_sha256": event_log_hash,
            "review_event_count": len(events),
            "last_event_sha256": events[-1].event_sha256 if events else None,
            "reviewed_tracks_sha256": sha256_file(records_path),
            "reviewed_video_sha256": sha256_file(video_path),
            "records_relative_path": records_path.name,
            "video_relative_path": video_path.name,
            "total_records": len(reviewed),
            "track_count": len({item.record.track_id for item in reviewed}),
        }
        (staging_directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staging_directory.rename(output_directory)
    return output_directory


def _verify_review_evidence(context: ReviewContext) -> None:
    """Ensure preserved evidence still matches the ingestion manifest."""
    actual_evidence_hash = sha256_file(context.evidence_path)
    if actual_evidence_hash != context.evidence_manifest.preserved_sha256:
        raise TrackReviewError(
            f"Preserved video changed during review for case '{context.case_id}': "
            f"expected {context.evidence_manifest.preserved_sha256}, got {actual_evidence_hash}."
        )


def _verify_review_tracks(context: ReviewContext) -> None:
    """Ensure automatic track records still match the selected run manifest."""
    actual_records_hash = sha256_file(context.source_records_path)
    if actual_records_hash != context.source_records_sha256:
        raise TrackReviewError(
            f"Automatic track records changed during review for case '{context.case_id}', "
            f"run '{context.source_run_id}': expected {context.source_records_sha256}, got {actual_records_hash}."
        )


def _apply_event(
    current: list[ReviewedTrackRecord], event: ReviewEvent
) -> list[ReviewedTrackRecord]:
    payload = event.payload
    action = event.action
    if action == "restore":
        rows: list[ReviewedTrackRecord] = []
        for value in _required_list(payload, "records"):
            row = dict(value)
            provenance = row.pop("provenance")
            rows.append(ReviewedTrackRecord(TrackRecord(**row), provenance))
        return rows
    track_id = _required_int(payload, "track_id") if action not in {"merge"} else None
    if action == "correct":
        frame_number = _required_int(payload, "frame_number")
        index = _find_index(current, track_id, frame_number)
        record = current[index].record
        changes: dict[str, object] = {}
        if "bbox" in payload:
            changes.update(_event_bbox_fields(payload))
        if "class_name" in payload:
            class_name = _required_class(payload["class_name"])
            changes.update(class_name=class_name, class_id=CLASS_IDS[class_name])
        if "bbox" in payload and record.state == "lost":
            changes.update(state="manual", confidence=None)
        current[index] = ReviewedTrackRecord(replace(record, **changes), "reviewer")
        return current
    if action == "create_track":
        frame_number = _required_int(payload, "frame_number")
        if frame_number < 0:
            raise TrackReviewError("New track frame number must be non-negative.")
        if track_id < 1 or track_id in {item.record.track_id for item in current}:
            raise TrackReviewError(f"New track ID {track_id} is invalid or already in use.")
        class_name = _required_class(payload.get("class_name"))
        bbox = _event_bbox_fields(payload)
        timestamp = _required_float(payload, "timestamp_seconds")
        anchor = current[0].record if current else None
        if anchor is None:
            raise TrackReviewError("Cannot create a track without a source tracking record to inherit its schema.")
        current.append(ReviewedTrackRecord(TrackRecord(
            case_id=anchor.case_id, run_id=anchor.run_id, frame_number=frame_number,
            timestamp_seconds=timestamp, track_id=track_id, class_id=CLASS_IDS[class_name], class_name=class_name,
            confidence=None, bbox_x1=bbox["bbox_x1"], bbox_y1=bbox["bbox_y1"],
            bbox_x2=bbox["bbox_x2"], bbox_y2=bbox["bbox_y2"], state="manual",
            track_age_frames=1, gap_length_frames=0, schema_version=anchor.schema_version,
        ), "reviewer"))
        return current
    if action == "add":
        frame_number = _required_int(payload, "frame_number")
        class_name = _required_class(payload.get("class_name"))
        bbox = _event_bbox_fields(payload)
        existing_index = next((index for index, item in enumerate(current) if item.record.track_id == track_id and item.record.frame_number == frame_number), None)
        existing = current[existing_index] if existing_index is not None else None
        if existing is not None and _has_box(existing.record):
            raise TrackReviewError(f"Track {track_id} already has a boxed observation at frame {frame_number}.")
        anchor = existing.record if existing is not None else next((item.record for item in current if item.record.track_id == track_id), None)
        if anchor is None:
            raise TrackReviewError(f"Track {track_id} does not exist.")
        timestamp = _required_float(payload, "timestamp_seconds")
        manual_record = ReviewedTrackRecord(TrackRecord(
            case_id=anchor.case_id, run_id=anchor.run_id, frame_number=frame_number,
            timestamp_seconds=timestamp, track_id=track_id, class_id=CLASS_IDS[class_name], class_name=class_name,
            confidence=None, bbox_x1=bbox["bbox_x1"], bbox_y1=bbox["bbox_y1"], bbox_x2=bbox["bbox_x2"], bbox_y2=bbox["bbox_y2"],
            state="manual", track_age_frames=max(1, frame_number - min(row.record.frame_number for row in current if row.record.track_id == track_id) + 1),
            gap_length_frames=0, schema_version=anchor.schema_version,
        ), "reviewer")
        if existing_index is None:
            current.append(manual_record)
        else:
            current[existing_index] = manual_record
        return current
    if action == "delete_observation":
        frame_number = _required_int(payload, "frame_number")
        _find_index(current, track_id, frame_number)
        return [item for item in current if not (item.record.track_id == track_id and item.record.frame_number == frame_number)]
    if action == "delete_track":
        result = [item for item in current if item.record.track_id != track_id]
        if len(result) == len(current):
            raise TrackReviewError(f"Track {track_id} does not exist.")
        return result
    if action == "merge":
        primary_id = _required_int(payload, "primary_track_id")
        secondary_id = _required_int(payload, "secondary_track_id")
        return _merge_tracks(current, primary_id, secondary_id)
    if action == "split":
        frame_number = _required_int(payload, "frame_number")
        new_track_id = _required_int(payload, "new_track_id")
        if new_track_id in {item.record.track_id for item in current}:
            raise TrackReviewError(f"Split target track ID {new_track_id} already exists.")
        if not any(item.record.track_id == track_id and item.record.frame_number < frame_number for item in current):
            raise TrackReviewError(f"Track {track_id} has no records before frame {frame_number}.")
        if not any(item.record.track_id == track_id and item.record.frame_number >= frame_number for item in current):
            raise TrackReviewError(f"Track {track_id} has no observations at or after frame {frame_number}.")
        split_records = [
            ReviewedTrackRecord(replace(item.record, track_id=new_track_id), item.provenance)
            if item.record.track_id == track_id and item.record.frame_number >= frame_number else item
            for item in current
        ]
        return _recalculate_track_ages(split_records, {track_id, new_track_id})
    if action == "repair_gap":
        return _repair_gap(current, track_id, _required_int(payload, "start_frame"), _required_int(payload, "end_frame"), payload)
    raise TrackReviewError(f"Unsupported review action '{action}'.")


def _merge_tracks(current: list[ReviewedTrackRecord], primary_id: int, secondary_id: int) -> list[ReviewedTrackRecord]:
    if primary_id == secondary_id:
        raise TrackReviewError("Choose two different tracks to merge.")
    primary = [item for item in current if item.record.track_id == primary_id]
    secondary = [item for item in current if item.record.track_id == secondary_id]
    if not primary or not secondary:
        raise TrackReviewError("Both tracks must exist before merging.")
    primary_by_frame = {item.record.frame_number: item for item in primary}
    secondary_by_frame = {item.record.frame_number: item for item in secondary}
    overlap = set(primary_by_frame) & set(secondary_by_frame)
    for frame_number in overlap:
        if _has_box(primary_by_frame[frame_number].record) and _has_box(secondary_by_frame[frame_number].record):
            raise TrackReviewError(f"Tracks overlap with boxed observations at frame {frame_number}; resolve before merging.")
    output = [item for item in current if item.record.track_id not in {primary_id, secondary_id}]
    for frame_number in sorted(set(primary_by_frame) | set(secondary_by_frame)):
        first = primary_by_frame.get(frame_number)
        second = secondary_by_frame.get(frame_number)
        if first is not None and second is not None:
            chosen = first if _has_box(first.record) or not _has_box(second.record) else second
            output.append(ReviewedTrackRecord(replace(chosen.record, track_id=primary_id), chosen.provenance))
        else:
            chosen = first or second
            if chosen is None:
                raise TrackReviewError("Internal merge error: no record selected.")
            output.append(ReviewedTrackRecord(replace(chosen.record, track_id=primary_id), chosen.provenance))
    return _recalculate_track_ages(output, {primary_id})


def _repair_gap(
    current: list[ReviewedTrackRecord], track_id: int, start_frame: int, end_frame: int, payload: dict[str, object]
) -> list[ReviewedTrackRecord]:
    gap_length = end_frame - start_frame + 1
    if start_frame < 1 or gap_length < 1 or gap_length > MAX_REPAIR_GAP_FRAMES:
        raise TrackReviewError(f"Gap repair must cover 1 to {MAX_REPAIR_GAP_FRAMES} missing frames.")
    by_frame = {item.record.frame_number: item for item in current if item.record.track_id == track_id}
    first = by_frame.get(start_frame - 1)
    second = by_frame.get(end_frame + 1)
    if first is None or second is None or not _has_box(first.record) or not _has_box(second.record):
        raise TrackReviewError("Gap repair requires boxed observations immediately before and after the selected gap.")
    if first.record.class_name != second.record.class_name:
        raise TrackReviewError("Gap endpoints must have the same class before interpolation.")
    for frame_number in range(start_frame, end_frame + 1):
        if frame_number in by_frame and _has_box(by_frame[frame_number].record):
            raise TrackReviewError(f"Frame {frame_number} already has a boxed observation for track {track_id}.")
    timestamp_values = payload.get("timestamps", {})
    output = [item for item in current if not (item.record.track_id == track_id and start_frame <= item.record.frame_number <= end_frame)]
    for frame_number in range(start_frame, end_frame + 1):
        fraction = (frame_number - (start_frame - 1)) / (gap_length + 1)
        record = first.record
        coordinates = tuple(
            _linear(getattr(first.record, key), getattr(second.record, key), fraction)
            for key in ("bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2")
        )
        timestamp = _timestamp_for_frame(timestamp_values, frame_number, first.record, second.record, fraction)
        interpolated = replace(
            record,
            frame_number=frame_number,
            timestamp_seconds=timestamp,
            confidence=None,
            bbox_x1=coordinates[0], bbox_y1=coordinates[1], bbox_x2=coordinates[2], bbox_y2=coordinates[3],
            state="interpolated",
            track_age_frames=first.record.track_age_frames + frame_number - first.record.frame_number,
            gap_length_frames=0,
        )
        output.append(ReviewedTrackRecord(interpolated, "review_interpolation"))
    return output


def _render_reviewed_video(context: ReviewContext, records: tuple[ReviewedTrackRecord, ...], video_path: Path) -> None:
    grouped: dict[int, tuple[TrackRecord, ...]] = {}
    for item in records:
        grouped[item.record.frame_number] = (*grouped.get(item.record.frame_number, ()), item.record)
    manifest = context.evidence_manifest
    writer = create_video_writer(video_path, manifest.width or 0, manifest.height or 0, _frame_rate(manifest))
    tails: dict[int, list[tuple[int, int]]] = {}
    last_boxes: dict[int, tuple[int, int, int, int]] = {}
    try:
        for frame in iter_frames(context.source_video_path, _frame_rate(manifest)):
            writer.write(render_tracking_frame(frame.image, grouped.get(frame.number, ()), frame.number, frame.timestamp_seconds, tails, last_boxes))
    finally:
        writer.release()
    verify_annotated_video(video_path, manifest.width or 0, manifest.height or 0)


def _event_hash(event: ReviewEvent) -> str:
    value = asdict(event)
    value["event_sha256"] = ""
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha256_bytes(serialized)


def _reviewed_record_json_value(item: ReviewedTrackRecord) -> dict[str, object]:
    """Serialize one reviewed row with schema and source provenance."""
    return {
        **asdict(item.record),
        "provenance": item.provenance,
        "review_schema_version": REVIEW_EXPORT_SCHEMA_VERSION,
    }


def _next_revision_number(directory: Path) -> int:
    revisions = [
        int(path.name.removeprefix("revision-"))
        for path in directory.glob("revision-[0-9][0-9][0-9]")
        if path.is_dir()
    ]
    return max(revisions, default=0) + 1


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise TrackReviewError(f"Invalid JSON in '{path}': {error}") from error
    if not isinstance(value, dict):
        raise TrackReviewError(f"Expected a JSON object in '{path}'.")
    return value


def _find_index(records: list[ReviewedTrackRecord], track_id: int, frame_number: int) -> int:
    for index, item in enumerate(records):
        if item.record.track_id == track_id and item.record.frame_number == frame_number:
            return index
    raise TrackReviewError(f"Track {track_id} has no record at frame {frame_number}.")


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrackReviewError(f"Review event field '{key}' must be an integer.")
    return value


def _required_float(payload: dict[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TrackReviewError(f"Review event field '{key}' must be a finite number.")
    return float(value)


def _required_list(payload: dict[str, object], key: str) -> list[dict[str, object]]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise TrackReviewError(f"Review event field '{key}' must be a list of record objects.")
    return value


def _required_class(value: object) -> str:
    if not isinstance(value, str) or value not in SUPPORTED_CLASS_NAMES:
        raise TrackReviewError(f"Unsupported class '{value}'.")
    return value


def _bbox_fields(value: object) -> dict[str, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise TrackReviewError("A bounding box must contain x1, y1, x2, and y2 coordinates.")
    coordinates: list[float] = []
    for coordinate in value:
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)) or not math.isfinite(coordinate):
            raise TrackReviewError("Bounding box coordinates must be finite numbers.")
        coordinates.append(float(coordinate))
    x1, y1, x2, y2 = coordinates
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
        raise TrackReviewError("Bounding box must have non-negative origin and positive width and height.")
    return {"bbox_x1": x1, "bbox_y1": y1, "bbox_x2": x2, "bbox_y2": y2}


def _event_bbox_fields(payload: dict[str, object]) -> dict[str, float]:
    fields = _bbox_fields(payload.get("bbox"))
    frame_width = _required_int(payload, "frame_width")
    frame_height = _required_int(payload, "frame_height")
    if frame_width <= 0 or frame_height <= 0:
        raise TrackReviewError("Frame dimensions must be positive integers.")
    if fields["bbox_x2"] > frame_width or fields["bbox_y2"] > frame_height:
        raise TrackReviewError(f"Bounding box must remain inside the {frame_width}x{frame_height} frame.")
    return fields


def _has_box(record: TrackRecord) -> bool:
    return None not in (record.bbox_x1, record.bbox_y1, record.bbox_x2, record.bbox_y2)


def _recalculate_track_ages(
    records: list[ReviewedTrackRecord], track_ids: set[int]
) -> list[ReviewedTrackRecord]:
    first_frames = {
        track_id: min(item.record.frame_number for item in records if item.record.track_id == track_id)
        for track_id in track_ids
        if any(item.record.track_id == track_id for item in records)
    }
    return [
        ReviewedTrackRecord(
            replace(item.record, track_age_frames=item.record.frame_number - first_frames[item.record.track_id] + 1),
            item.provenance,
        )
        if item.record.track_id in first_frames else item
        for item in records
    ]


def _linear(first: float | None, second: float | None, fraction: float) -> float:
    if first is None or second is None:
        raise TrackReviewError("Interpolation requires boxed endpoints.")
    return first + (second - first) * fraction


def _timestamp_for_frame(
    timestamps: object, frame_number: int, first: TrackRecord, second: TrackRecord, fraction: float
) -> float:
    if isinstance(timestamps, dict):
        value = timestamps.get(str(frame_number), timestamps.get(frame_number))
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    return first.timestamp_seconds + (second.timestamp_seconds - first.timestamp_seconds) * fraction


def sha256_bytes(value: bytes) -> str:
    """Return a SHA-256 digest for canonical serialized content."""
    return hashlib.sha256(value).hexdigest()
