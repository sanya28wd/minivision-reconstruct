"""Streamlit host for verified replay selection, review persistence, and export."""

import argparse
from pathlib import Path
import sys
from typing import TypedDict
from uuid import uuid4

from pydantic import BaseModel, ValidationError
import streamlit as st

from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.incident.ui import render_incident_panel
from minivision_reconstruct.movement.ui import render_movement_panel, seek_request
from minivision_reconstruct.reconstruction.component import replay_component
from minivision_reconstruct.reconstruction.export import export_reconstruction, export_snapshot
from minivision_reconstruct.reconstruction.media import ReplayMediaConnector, ReplayMediaRegistryConnector
from minivision_reconstruct.reconstruction.models import MarkerDocument, SceneDocument, require_identifier
from minivision_reconstruct.reconstruction.storage import revision_ids, save_marker_revision, save_scene_revision


class SaveRequest(BaseModel):
    document: dict[str, object]
    reviewer: str
    action_id: str


class SnapshotRequest(BaseModel):
    source_frame: int
    action_id: str


class MediaSession(TypedDict):
    identity: tuple[str, str, str, str]
    connector: ReplayMediaConnector


@st.cache_resource
def media_registry(port: int) -> ReplayMediaRegistryConnector:
    """Own one local server across page reloads and independent browser sessions."""
    registry = ReplayMediaRegistryConnector(port)
    registry.start()
    return registry


def run_reconstruction_app() -> None:
    """Select explicit immutable inputs and mount the browser-owned replay workspace."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--media-port", type=int, required=True)
    parser.add_argument("--ffmpeg", type=str, required=True)
    args, _ = parser.parse_known_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
    st.set_page_config(page_title="MiniVision · Reconstruction", layout="wide")
    st.title("Bird’s-eye reconstruction studio")
    st.caption("Explore the road scene, follow each participant, and review the synchronized source evidence.")
    pending: tuple[str, str] | None = st.session_state.pop("reconstruction-pending-choice", None)
    if pending is not None:
        st.session_state[pending[0]] = pending[1]
    notice: str | None = st.session_state.pop("reconstruction-notice", None)
    if notice is not None:
        st.success(notice)
    cases_root: Path = args.cases_root.resolve()
    cases = tuple(sorted(path.name for path in cases_root.iterdir() if (path / "analysis" / "reconstruction").is_dir())) if cases_root.is_dir() else ()
    if not cases:
        st.info("Prepare a Week 6 reconstruction run first using scripts/prepare_reconstruction.py.")
        return
    gallery: tuple[tuple[str, str, str], ...] = (
        ("demo-boulevard-crossing", "Boulevard crossing", "Two-way traffic, a zebra crossing, and a pedestrian."),
        ("demo-roundabout-circulation", "Roundabout circulation", "A landscaped island with circulating road users."),
        ("demo-intersection-mixed-traffic", "Mixed traffic intersection", "Crossing paths, multiple vehicle types, and track gaps."),
    )
    available = tuple(item for item in gallery if item[0] in cases)
    if available:
        with st.expander("Explore synthetic demo cases", expanded=True):
            columns = st.columns(len(available))
            for column, (demo_id, title, description) in zip(columns, available, strict=True):
                with column, st.container(border=True):
                    st.caption("SYNTHETIC DEMO · 12 SECONDS")
                    st.subheader(title)
                    st.write(description)
                    if st.button("Open scene", key=f"open-{demo_id}", use_container_width=True):
                        st.session_state["reconstruction-case"] = demo_id
    with st.sidebar:
        st.subheader("Case library")
        st.caption(f"{len(cases)} prepared cases available")
        case_id = st.selectbox("Case", cases, key="reconstruction-case")
        if case_id.startswith("demo-"):
            st.info("Synthetic demonstration with scripted tracks. This is not real CCTV or a validated accident assessment.")
        runs_root = cases_root / case_id / "analysis" / "reconstruction"
        runs = tuple(path.name for path in sorted(runs_root.iterdir()) if (path / "manifest.json").is_file())
        if not runs:
            st.error("This case has no complete reconstruction run.")
            return
        run_id = st.selectbox("Reconstruction run", runs)
        run_directory = runs_root / run_id
        scenes = revision_ids(run_directory, "scenes")
        scene_revision = st.selectbox("Scene revision", scenes, index=len(scenes) - 1 if case_id.startswith("demo-") else 0, key=f"scene-choice-{case_id}-{run_id}")
        marker_revision = st.selectbox("Marker revision", revision_ids(run_directory, "markers"), key=f"marker-choice-{case_id}-{run_id}")
        reviewer = st.text_input("Reviewer name", key="reconstruction-reviewer")
        st.caption("Saved revisions are immutable. Select a saved revision before making a presentation export.")
    identity = (case_id, run_id, scene_revision, marker_revision)
    try:
        registry = media_registry(args.media_port)
        session: MediaSession | None = st.session_state.get("reconstruction-media")
        if session is None or session["identity"] != identity:
            if session is not None:
                registry.unregister(session["connector"].token)
            connector = ReplayMediaConnector(cases_root, run_directory, scene_revision, marker_revision, args.media_port)
            registry.register(connector)
            session = {"identity": identity, "connector": connector}
            st.session_state["reconstruction-media"] = session
        media = session["connector"]
        result = replay_component(
            key=f"replay-{case_id}-{run_id}-{scene_revision}-{marker_revision}",
            data={"bundle_url": media.base_url + "/bundle", "video_url": media.base_url + "/video", "frame_url": media.base_url + "/frame/", "reviewer": reviewer, "seek": seek_request()},
            default={},
            on_scene_save_change=_component_changed,
            on_marker_save_change=_component_changed,
            on_snapshot_request_change=_component_changed,
        )
        _handle_component_requests(result, run_directory, media)
        summary = st.columns(4)
        summary[0].metric("Road users", len({item.track_id for item in media.bundle.observations}))
        summary[1].metric("Source duration", f"{media.bundle.manifest.duration_seconds:.1f} s")
        summary[2].metric("Source frames", media.bundle.manifest.source_frame_count)
        summary[3].metric("Coordinate system", "Metric" if media.bundle.scene.mode == "metric" else "Relative")
        frames = media.bundle.source_frames
        frame_period = (frames[-1].source_time - frames[0].source_time) / (len(frames) - 1) if len(frames) > 1 else 1.0
        render_movement_panel(cases_root / case_id, run_id, frame_period)
        render_incident_panel(cases_root / case_id, run_id, frame_period, reviewer)
        with st.expander("Presentation exports"):
            export_id = st.text_input("Export ID", value="presentation-001")
            snapshot_frame = int(st.number_input("Source frame included in export snapshot", min_value=0, max_value=media.bundle.manifest.source_frame_count - 1, step=1))
            if st.button("Export bird’s-eye and comparison MP4", type="primary"):
                require_identifier(export_id, "export ID")
                progress = st.progress(0.0, text="Rendering using the interactive Canvas renderer…")
                destination = run_directory / "exports" / export_id
                with st.spinner("Rendering and verifying presentation videos…"):
                    exported = export_reconstruction(media, destination, scene_revision, marker_revision, snapshot_frame, args.ffmpeg, lambda done, total: progress.progress(done / total, text=f"Rendering frame {done}/{total}"))
                st.success(f"Exported and verified: {exported}")
            if st.button("Export selected source frame as PNG"):
                destination = run_directory / "snapshots" / f"frame-{snapshot_frame}-{uuid4().hex[:8]}.png"
                export_snapshot(media, destination, snapshot_frame)
                st.session_state["reconstruction-snapshot"] = str(destination)
            snapshot_path = st.session_state.get("reconstruction-snapshot")
            if snapshot_path is not None:
                path = Path(snapshot_path)
                if path.is_relative_to(run_directory):
                    st.download_button("Download snapshot PNG", data=path.read_bytes(), file_name=path.name, mime="image/png")
    except (EvidenceError, ValidationError, OSError) as error:
        st.error(str(error))


def _component_changed() -> None:
    """Streamlit trigger callbacks request the normal rerun, with no frame traffic."""
    return None


def _handle_component_requests(result: object, run_directory: Path, media: ReplayMediaConnector) -> None:
    """Process intentional component writes once, keyed by action UUID."""
    if result is None:
        return
    for key in ("scene_save", "marker_save"):
        value = getattr(result, key, None)
        if value is None:
            continue
        request = SaveRequest.model_validate(value)
        if request.action_id == st.session_state.get(f"processed-{key}"):
            continue
        if key == "scene_save":
            destination = save_scene_revision(run_directory, SceneDocument.model_validate(request.document), request.reviewer)
        else:
            destination = save_marker_revision(run_directory, MarkerDocument.model_validate(request.document), request.reviewer, media.bundle)
        st.session_state[f"processed-{key}"] = request.action_id
        category = "scene" if key == "scene_save" else "marker"
        st.session_state["reconstruction-pending-choice"] = (f"{category}-choice-{media.bundle.manifest.case_id}-{media.bundle.manifest.run_id}", destination.name)
        st.session_state["reconstruction-notice"] = f"Saved {destination.name}. This revision is now selected for replay and export."
        st.rerun()
    snapshot_value = getattr(result, "snapshot_request", None)
    if snapshot_value is not None:
        request = SnapshotRequest.model_validate(snapshot_value)
        if request.action_id != st.session_state.get("processed-snapshot"):
            destination = run_directory / "snapshots" / f"frame-{request.source_frame}-{uuid4().hex[:8]}.png"
            export_snapshot(media, destination, request.source_frame)
            st.session_state["processed-snapshot"] = request.action_id
            st.session_state["reconstruction-snapshot"] = str(destination)
            st.success(f"Source-exact snapshot saved for frame {request.source_frame}.")
