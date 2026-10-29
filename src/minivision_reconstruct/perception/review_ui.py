"""Local Streamlit interface for reviewing one automatic tracking run."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image

from minivision_reconstruct.domain.errors import EvidenceError, TrackReviewError
from minivision_reconstruct.domain.models import SUPPORTED_CLASS_NAMES
from minivision_reconstruct.perception.review_workflow import (
    MAX_REPAIR_GAP_FRAMES,
    append_review_event,
    export_reviewed_revision,
    has_undoable_review_event,
    initial_review_records,
    load_track_records,
    open_review_context,
    read_review_events,
    rebuild_reviewed_tracks,
    undo_latest_review_event,
)
from minivision_reconstruct.perception.tracking_workflow import _frame_rate
from minivision_reconstruct.video.reader import read_frame_at
from minivision_reconstruct.video.renderer import render_tracking_frame


def run_review_app() -> None:
    """Launch a local browser based track-review application."""
    import streamlit as st
    from streamlit_drawable_canvas import st_canvas

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cases-root", type=Path, default=Path("cases"))
    parsed, _ = parser.parse_known_args(_streamlit_arguments())
    cases_root = parsed.cases_root
    st.set_page_config(page_title="MiniVision Track Review", layout="wide")
    st.title("Human track review")
    st.caption("Review edits are appended to a hash-linked event log. Automatic tracking outputs stay unchanged.")

    case_ids = _available_case_ids(cases_root)
    if not case_ids:
        st.error(f"No cases with tracking runs were found under '{cases_root}'.")
        return
    case_column, run_column, reviewer_column = st.columns((1, 1, 1))
    with case_column:
        selected_case = st.selectbox("Case", case_ids)
    run_ids = _available_run_ids(cases_root / selected_case)
    if not run_ids:
        st.error(f"Case '{selected_case}' has no complete tracking runs.")
        return
    with run_column:
        selected_run = st.selectbox("Automatic tracking run", run_ids)

    context_key = f"review-context-{selected_case}-{selected_run}"
    automatic_key = f"automatic-records-{selected_case}-{selected_run}"
    try:
        if context_key not in st.session_state:
            st.session_state[context_key] = open_review_context(cases_root, selected_case, selected_run)
        context = st.session_state[context_key]
        if automatic_key not in st.session_state:
            st.session_state[automatic_key] = load_track_records(context.source_records_path, selected_case, selected_run)
        automatic = st.session_state[automatic_key]
        events = read_review_events(context.event_log_path, selected_case, selected_run)
        reviewed = rebuild_reviewed_tracks(automatic, events)
        frame_rate = _frame_rate(context.evidence_manifest)
    except EvidenceError as error:
        st.error(str(error))
        return

    with reviewer_column:
        reviewer = st.text_input("Reviewer name", key=f"reviewer-{selected_case}-{selected_run}").strip()
    frame_count = context.evidence_manifest.decoded_frame_count
    frame_key = f"frame-{selected_case}-{selected_run}"
    if frame_key not in st.session_state:
        st.session_state[frame_key] = 0
    navigation_left, navigation_center, navigation_right = st.columns((1, 5, 1))
    if navigation_left.button("← Previous", disabled=st.session_state[frame_key] <= 0):
        st.session_state[frame_key] -= 1
    if navigation_right.button("Next →", disabled=st.session_state[frame_key] >= frame_count - 1):
        st.session_state[frame_key] += 1
    navigation_center.number_input(
        "Frame number (zero based)", min_value=0, max_value=max(frame_count - 1, 0),
        step=1, key=frame_key,
    )
    frame_number = int(st.session_state[frame_key])
    try:
        frame = read_frame_at(context.source_video_path, frame_rate, int(frame_number))
    except EvidenceError as error:
        st.error(str(error))
        return

    frame_records = tuple(item.record for item in reviewed if item.record.frame_number == int(frame_number))
    annotated = render_tracking_frame(frame.image, frame_records, frame.number, frame.timestamp_seconds, {}, {})
    all_track_ids = sorted({item.record.track_id for item in reviewed})
    visible_ids = sorted({record.track_id for record in frame_records})
    view_column, action_column = st.columns((3, 2))
    with view_column:
        st.image(cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB), caption=f"Frame {frame.number} · {frame.timestamp_seconds:.3f}s", use_container_width=True)
        st.write(f"Tracks on this frame: {', '.join(str(value) for value in visible_ids) or 'none'}")
        if not all_track_ids:
            st.info("This run has no tracks to review yet.")
    with action_column:
        operation = st.selectbox(
            "Observation action",
            ("Correct box or class", "Add observation", "Create new track", "Delete this observation", "Delete whole track"),
            key=f"operation-{selected_case}-{selected_run}",
        )
        track_to_review = (
            st.selectbox(
                "Selected track", all_track_ids, disabled=operation == "Create new track",
                key=f"track-{selected_case}-{selected_run}",
            )
            if all_track_ids else None
        )
        event_track_ids = {
            value
            for event in events
            for key in ("track_id", "new_track_id", "primary_track_id", "secondary_track_id")
            if isinstance((value := event.payload.get(key)), int) and not isinstance(value, bool)
        }
        next_track_id = max((*all_track_ids, *event_track_ids), default=0) + 1
        effective_track_id = next_track_id if operation == "Create new track" else track_to_review
        selected_record = next(
            (item.record for item in reviewed if item.record.track_id == effective_track_id and item.record.frame_number == int(frame_number)),
            None,
        )
        if operation == "Correct box or class" and selected_record is None:
            st.info("Choose a track with an existing box on this frame, or add a missing observation to an existing track.")
        if operation == "Create new track":
            st.info(f"Draw a box for an object with no existing track ID. It will be assigned track {next_track_id}.")
        if operation == "Add observation" and selected_record is not None and selected_record.state != "lost" and selected_record.bbox_x1 is not None:
            st.info("This track already has a box on this frame. Choose Correct box or class, or select a track with a missing box.")
        bbox = _box_editor(st, st_canvas, frame.image, selected_record, operation, selected_case, selected_run)
        class_name = st.selectbox(
            "Class", sorted(SUPPORTED_CLASS_NAMES),
            index=sorted(SUPPORTED_CLASS_NAMES).index(selected_record.class_name) if selected_record and selected_record.class_name in SUPPORTED_CLASS_NAMES else sorted(SUPPORTED_CLASS_NAMES).index("car"),
            key=f"class-{selected_case}-{selected_run}-{frame_number}",
        )
        save_disabled = not reviewer or effective_track_id is None
        if st.button("Save observation edit", disabled=save_disabled, key=f"save-observation-{selected_case}-{selected_run}"):
            try:
                _save_observation_action(context, reviewer, operation, int(effective_track_id), int(frame_number), frame.timestamp_seconds, frame.image.shape[1], frame.image.shape[0], bbox, class_name, selected_record)
                st.rerun()
            except TrackReviewError as error:
                st.error(str(error))

        st.divider()
        _render_track_actions(st, context, reviewer, reviewed, int(frame_number), selected_case, selected_run)
        st.divider()
        action_col, export_col = st.columns(2)
        with action_col:
            if st.button("Undo latest action", disabled=not reviewer or not has_undoable_review_event(events), key=f"undo-{selected_case}-{selected_run}"):
                try:
                    undo_latest_review_event(context, reviewer, automatic)
                    st.rerun()
                except TrackReviewError as error:
                    st.error(str(error))
        with export_col:
            if st.button("Export reviewed revision", key=f"export-{selected_case}-{selected_run}"):
                try:
                    output = export_reviewed_revision(context, automatic)
                    st.success(f"Reviewed revision saved to {output}")
                except EvidenceError as error:
                    st.error(str(error))
        st.caption(f"{len(events)} audit events · {len(reviewed)} reviewed frame records · automatic tracks: {context.source_records_sha256[:12]}")
        if events:
            with st.expander("Audit log"):
                for event in reversed(events):
                    st.write(f"{event.sequence}. {event.occurred_at} · {event.reviewer} · {event.action} · {event.event_sha256[:12]}")


def _box_editor(
    st: object,
    st_canvas: object,
    image: np.ndarray,
    selected_record: object,
    operation: str,
    case_id: str,
    run_id: str,
) -> tuple[float, float, float, float] | None:
    height, width = image.shape[:2]
    scale = min(1000 / width, 700 / height, 1.0)
    canvas_width, canvas_height = max(1, round(width * scale)), max(1, round(height * scale))
    canvas_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).resize((canvas_width, canvas_height))
    mode = "rect"
    initial_drawing = None
    if selected_record is not None and operation == "Correct box or class":
        record = selected_record
        if None not in (record.bbox_x1, record.bbox_y1, record.bbox_x2, record.bbox_y2):
            initial_drawing = {"version": "7.0.0", "objects": [{
                "type": "rect", "left": record.bbox_x1 * scale, "top": record.bbox_y1 * scale,
                "width": (record.bbox_x2 - record.bbox_x1) * scale,
                "height": (record.bbox_y2 - record.bbox_y1) * scale,
                "fill": "rgba(0, 0, 0, 0)", "stroke": "#ff3300", "strokeWidth": 2,
            }]}
    drawing = st_canvas(
        fill_color="rgba(255, 80, 0, 0.18)", stroke_width=2, stroke_color="#ff3300",
        background_image=canvas_image, update_streamlit=True, height=canvas_height, width=canvas_width,
        drawing_mode=mode, initial_drawing=initial_drawing,
        key=f"canvas-{case_id}-{run_id}-{st.session_state.get(f'frame-{case_id}-{run_id}', 0)}-{getattr(selected_record, 'track_id', 'new')}-{operation}",
    )
    if selected_record is not None and operation == "Correct box or class":
        st.caption("Use the canvas edit toggle to move or resize the existing box, or draw a replacement box.")
    if drawing.json_data is None:
        return None
    objects = drawing.json_data.get("objects", [])
    if not objects:
        return None
    shape = objects[-1]
    left = float(shape.get("left", 0)) / scale
    top = float(shape.get("top", 0)) / scale
    box_width = float(shape.get("width", 0)) * float(shape.get("scaleX", 1)) / scale
    box_height = float(shape.get("height", 0)) * float(shape.get("scaleY", 1)) / scale
    return left, top, left + box_width, top + box_height


def _save_observation_action(
    context: object,
    reviewer: str,
    operation: str,
    track_id: int,
    frame_number: int,
    timestamp_seconds: float,
    frame_width: int,
    frame_height: int,
    bbox: tuple[float, float, float, float] | None,
    class_name: str,
    selected_record: object,
) -> None:
    if operation == "Correct box or class":
        if selected_record is None:
            raise TrackReviewError(f"Track {track_id} has no observation at frame {frame_number} to correct.")
        payload: dict[str, object] = {"track_id": track_id, "frame_number": frame_number, "class_name": class_name}
        if bbox is not None:
            payload["bbox"] = bbox
            payload["frame_width"] = frame_width
            payload["frame_height"] = frame_height
        append_review_event(context, reviewer, "correct", payload)
    elif operation in {"Add observation", "Create new track"}:
        if bbox is None:
            raise TrackReviewError("Draw a bounding box before adding an observation.")
        append_review_event(context, reviewer, "create_track" if operation == "Create new track" else "add", {
            "track_id": track_id, "frame_number": frame_number, "timestamp_seconds": timestamp_seconds,
            "class_name": class_name, "bbox": bbox, "frame_width": frame_width, "frame_height": frame_height,
        })
    elif operation == "Delete this observation":
        append_review_event(context, reviewer, "delete_observation", {"track_id": track_id, "frame_number": frame_number})
    elif operation == "Delete whole track":
        append_review_event(context, reviewer, "delete_track", {"track_id": track_id})
    else:
        raise TrackReviewError(f"Unsupported observation action '{operation}'.")


def _render_track_actions(
    st: object, context: object, reviewer: str, reviewed: tuple[object, ...], frame_number: int, case_id: str, run_id: str
) -> None:
    track_ids = sorted({item.record.track_id for item in reviewed})
    last_frame = max((item.record.frame_number for item in reviewed), default=frame_number)
    if len(track_ids) < 1:
        return
    merge_tab, split_tab, repair_tab = st.tabs(("Merge", "Split", "Repair gap"))
    with merge_tab:
        if len(track_ids) < 2:
            st.info("At least two tracks are needed to merge.")
        else:
            primary_id = st.selectbox("Keep this track ID", track_ids, key=f"merge-primary-{case_id}-{run_id}")
            secondary_options = [value for value in track_ids if value != primary_id]
            secondary_id = st.selectbox("Merge this track into it", secondary_options, key=f"merge-secondary-{case_id}-{run_id}")
            if st.button("Merge tracks", disabled=not reviewer, key=f"merge-button-{case_id}-{run_id}"):
                try:
                    append_review_event(context, reviewer, "merge", {"primary_track_id": primary_id, "secondary_track_id": secondary_id})
                    st.rerun()
                except TrackReviewError as error:
                    st.error(str(error))
    with split_tab:
        track_id = st.selectbox("Track to split", track_ids, key=f"split-track-{case_id}-{run_id}")
        split_frame = st.number_input("First frame for new track ID", min_value=0, max_value=max(last_frame, 0), value=min(frame_number, last_frame), step=1, key=f"split-frame-{case_id}-{run_id}")
        if st.button("Split track", disabled=not reviewer, key=f"split-button-{case_id}-{run_id}"):
            new_track_id = max(track_ids) + 1
            try:
                append_review_event(context, reviewer, "split", {"track_id": track_id, "frame_number": int(split_frame), "new_track_id": new_track_id})
                st.rerun()
            except TrackReviewError as error:
                st.error(str(error))
    with repair_tab:
        track_id = st.selectbox("Track to repair", track_ids, key=f"repair-track-{case_id}-{run_id}")
        start_frame, end_frame = st.columns(2)
        repair_start = start_frame.number_input("First missing frame", min_value=1, max_value=max(last_frame, 1), value=min(max(1, frame_number), max(last_frame, 1)), step=1, key=f"repair-start-{case_id}-{run_id}")
        repair_end = end_frame.number_input("Last missing frame", min_value=1, max_value=max(last_frame, 1), value=min(max(1, frame_number), max(last_frame, 1)), step=1, key=f"repair-end-{case_id}-{run_id}")
        st.caption(f"The repair can fill at most {MAX_REPAIR_GAP_FRAMES} frames between boxed endpoints.")
        if st.button("Interpolate gap", disabled=not reviewer, key=f"repair-button-{case_id}-{run_id}"):
            try:
                append_review_event(context, reviewer, "repair_gap", {
                    "track_id": track_id, "start_frame": int(repair_start), "end_frame": int(repair_end),
                })
                st.rerun()
            except TrackReviewError as error:
                st.error(str(error))


def _available_case_ids(cases_root: Path) -> list[str]:
    if not cases_root.is_dir():
        return []
    return sorted(path.name for path in cases_root.iterdir() if path.is_dir() and (path / "analysis" / "tracks").is_dir())


def _available_run_ids(case_directory: Path) -> list[str]:
    tracks_directory = case_directory / "analysis" / "tracks"
    return sorted(path.name for path in tracks_directory.iterdir() if path.is_dir() and (path / "tracks.jsonl").is_file() and (path / "run_manifest.json").is_file())


def _streamlit_arguments() -> list[str]:
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1 :]
    return sys.argv[1:]
