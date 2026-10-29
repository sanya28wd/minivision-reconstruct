"""Streamlit movement panel: linked interactive plots and an ordered, seekable event table."""

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
from uuid import uuid4

import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.movement.models import (
    MovementEvent, MovementManifest, MovementObservation, MovementSequence, PairSeparation, SmoothedPoint,
)
from minivision_reconstruct.movement.precision import display_time, display_value
from minivision_reconstruct.reconstruction.storage import read_record, read_records, verify_hash


SEEK_KEY = "movement-seek"
STATE_COLORS: dict[str, str] = {"moving": "#2F855A", "turning": "#B7791F", "stopped": "#C53030", "unknown": "#A0AEC0", "unavailable": "#E2E8F0"}


@dataclass(frozen=True)
class MovementRun:
    manifest: MovementManifest
    smoothed: tuple[SmoothedPoint, ...]
    movement: tuple[MovementObservation, ...]
    events: tuple[MovementEvent, ...]
    separation: tuple[PairSeparation, ...]
    sequence: MovementSequence
    lanes: dict[str, list[str]]


@st.cache_data(show_spinner="Verifying movement records…")
def load_movement_run(run_directory: str, manifest_sha256: str) -> MovementRun:
    """Hash-verified load; the manifest hash keys the cache so changed files are never shown."""
    directory = Path(run_directory)
    verify_hash(directory / "manifest.json", manifest_sha256)
    manifest = read_record(directory / "manifest.json", MovementManifest)
    for artifact in manifest.artifacts:
        verify_hash(directory / artifact.filename, artifact.sha256)
    return MovementRun(
        manifest=manifest,
        smoothed=read_records(directory / "smoothed.jsonl", SmoothedPoint),
        movement=read_records(directory / "movement_observations.jsonl", MovementObservation),
        events=read_records(directory / "events.jsonl", MovementEvent),
        separation=read_records(directory / "separation.jsonl", PairSeparation),
        sequence=read_record(directory / "sequence.json", MovementSequence),
        lanes=json.loads((directory / "lane_sequences.json").read_text(encoding="utf-8")),
    )


def movement_runs(case_root: Path, reconstruction_run: str) -> tuple[Path, ...]:
    """Movement runs derived from the selected reconstruction run only."""
    root = case_root / "analysis" / "movement"
    if not root.is_dir():
        return ()
    result: list[Path] = []
    for path in sorted(root.iterdir()):
        if (path / "manifest.json").is_file() and read_record(path / "manifest.json", MovementManifest).reconstruction_run == reconstruction_run:
            result.append(path)
    return tuple(result)


def seek_request() -> dict[str, object] | None:
    """Pending replay seek for the component; consumed by the browser once per action ID."""
    value: dict[str, object] | None = st.session_state.get(SEEK_KEY)
    return value


def render_movement_panel(case_root: Path, reconstruction_run: str, frame_period: float) -> None:
    """Show movement plots for one selected road user or all road users."""
    runs = movement_runs(case_root, reconstruction_run)
    st.subheader("Movement sequence and uncertainty")
    if not runs:
        st.info("No Week 7 movement run for this reconstruction. Create one with scripts/analyze_movement.py.")
        return
    run_directory = st.selectbox("Movement run", runs, format_func=lambda path: path.name, key=f"movement-run-{case_root.name}-{reconstruction_run}")
    run = load_movement_run(str(run_directory), sha256_file(run_directory / "manifest.json"))
    unit = "m" if run.manifest.unit == "m" else "relative units"
    tracks = sorted({point.track_id for point in run.smoothed})
    classes = {point.track_id: point.class_name for point in run.smoothed}
    choice = st.selectbox("Road user", ("All road users", *tracks), format_func=lambda value: value if isinstance(value, str) else f"#{value} · {classes[value]}",
                          key=f"movement-track-{case_root.name}")
    selected: tuple[int, ...] = tuple(tracks) if isinstance(choice, str) else (choice,)
    st.caption(f"Config {run.manifest.config_id} · {run.manifest.pipeline_version} · unit: {unit}. " + " ".join(run.manifest.limitations))
    events = tuple(event for event in run.events if set(event.track_ids) & set(selected))
    tabs = st.tabs(("Events", "Trajectory", "Heading", "Movement states", "Lane use", "Separation"))
    with tabs[0]:
        _events_tab(run, events, frame_period, unit, selected)
    with tabs[1]:
        _trajectory_tab(run, selected, events, unit)
    with tabs[2]:
        _heading_tab(run, selected, events)
    with tabs[3]:
        st.plotly_chart(_state_chart(run.movement, selected, "movement_state", "Visible movement state (no speed is calculated)"), width="stretch")
    with tabs[4]:
        vehicles = tuple(track for track in selected if str(track) in run.lanes)
        st.plotly_chart(_state_chart(run.movement, vehicles, "lane_or_zone_id", "Lane membership (blank = ambiguous or outside calibrated lanes)"), width="stretch")
        st.dataframe([{"road user": f"#{track}", "lane sequence": " → ".join(run.lanes[str(track)]) or "none persistent"} for track in vehicles], hide_index=True)
    with tabs[5]:
        _separation_tab(run, selected, unit, frame_period)


def _events_tab(run: MovementRun, events: tuple[MovementEvent, ...], frame_period: float, unit: str, selected: tuple[int, ...]) -> None:
    order = {entry.event_id: entry for entry in run.sequence.entries}
    rows = [{
        "rank": order[event.event_id].rank, "time": display_time(event.peak_time, event.time_window, frame_period),
        "event": event.event_type.replace("_", " "), "road users": ", ".join(f"#{track}" for track in event.track_ids),
        "frame": event.peak_frame,
        "location": "unavailable" if event.location is None or event.location_uncertainty is None else
        f"({display_value(event.location[0], event.location_uncertainty, '')}, {display_value(event.location[1], event.location_uncertainty, unit)})",
        "feature": " → ".join(item for item in (event.from_feature, event.to_feature) if item) or "",
        "confidence": event.confidence, "basis": event.provenance,
        "order unresolved with": len(order[event.event_id].order_unresolved_with), "status": event.status,
    } for event in events]
    st.caption("Proposed by the system for reviewer confirmation. Overlapping time windows mean order is not claimed. Click a point to inspect its source frame.")
    figure = go.Figure()
    by_type: dict[str, list[MovementEvent]] = defaultdict(list)
    for event in events:
        by_type[event.event_type].append(event)
    for event_type, items in sorted(by_type.items()):
        figure.add_trace(go.Scatter(
            x=[item.peak_time for item in items], y=["-".join(f"#{track}" for track in item.track_ids) for item in items], mode="markers", name=event_type.replace("_", " "),
            marker={"size": 10}, customdata=[[item.peak_frame, item.track_ids[0] if len(item.track_ids) == 1 else -1] for item in items],
            error_x={"type": "data", "symmetric": False, "array": [item.time_window[1] - item.peak_time for item in items],
                     "arrayminus": [item.peak_time - item.time_window[0] for item in items], "thickness": 1},
            hovertext=[f"{item.event_id} · window {item.time_window[0]:.2f}–{item.time_window[1]:.2f} s · {item.confidence}" for item in items],
        ))
    figure.update_layout(height=max(260, 34 * len({row["road users"] for row in rows}) + 120), xaxis_title="source time (s)", template="plotly_white", margin={"t": 20})
    selection = st.plotly_chart(figure, width="stretch", on_select="rerun", selection_mode="points", key=f"movement-events-{run.manifest.run_id}-{selected}")
    points = selection.selection.points if selection is not None and selection.selection is not None else []
    if points:
        frame, track = points[0]["customdata"]
        _request_seek(int(frame), None if int(track) < 0 else int(track))
    st.dataframe(rows, hide_index=True, width="stretch")
    if events:
        target = st.selectbox("Inspect event in replay", events, format_func=lambda item: f"{display_time(item.peak_time, item.time_window, frame_period)} · {item.event_type} · {item.track_ids}", key=f"movement-inspect-{run.manifest.run_id}")
        if st.button("Go to source frame", key=f"movement-go-{run.manifest.run_id}"):
            _request_seek(target.peak_frame, target.track_ids[0] if len(target.track_ids) == 1 else None)


def _request_seek(frame: int, track: int | None) -> None:
    current = st.session_state.get(SEEK_KEY)
    if current is not None and current["source_frame"] == frame and current["track_id"] == track:
        return
    st.session_state[SEEK_KEY] = {"source_frame": frame, "track_id": track, "action_id": uuid4().hex}
    st.rerun()


def _segments(points: list[SmoothedPoint]) -> list[list[SmoothedPoint]]:
    groups: list[list[SmoothedPoint]] = []
    for point in points:
        if point.segment_id is None:
            continue
        if not groups or groups[-1][-1].segment_id != point.segment_id:
            groups.append([])
        groups[-1].append(point)
    return groups


def _trajectory_tab(run: MovementRun, selected: tuple[int, ...], events: tuple[MovementEvent, ...], unit: str) -> None:
    by_track: dict[int, list[SmoothedPoint]] = defaultdict(list)
    for point in run.smoothed:
        if point.track_id in selected:
            by_track[point.track_id].append(point)
    plan = go.Figure()
    series = make_subplots(rows=2, cols=1, shared_xaxes=True, subplot_titles=(f"x ({unit})", f"y ({unit})"), vertical_spacing=0.08)
    single = len(selected) == 1
    for track, points in sorted(by_track.items()):
        for index, segment in enumerate(_segments(points)):
            name, legend = f"#{track}", index == 0
            plan.add_trace(go.Scatter(x=[p.smoothed_position[0] for p in segment], y=[p.smoothed_position[1] for p in segment], mode="lines",
                                      name=name, legendgroup=name, showlegend=legend, line={"dash": "solid" if all(p.state != "interpolated" for p in segment) else "dot"}))
            if single:
                plan.add_trace(go.Scatter(x=[p.raw_position[0] for p in segment], y=[p.raw_position[1] for p in segment], mode="markers",
                                          name="raw contact points", showlegend=legend, marker={"size": 3, "color": "#718096", "opacity": 0.6}))
                for row, axis in ((1, 0), (2, 1)):
                    times = [p.source_time for p in segment]
                    upper = [p.smoothed_position[axis] + p.position_uncertainty for p in segment]
                    lower = [p.smoothed_position[axis] - p.position_uncertainty for p in segment]
                    series.add_trace(go.Scatter(x=times + times[::-1], y=upper + lower[::-1], fill="toself", fillcolor="rgba(43,108,176,0.15)", line={"width": 0},
                                                name="reported uncertainty", showlegend=legend and row == 1, hoverinfo="skip"), row=row, col=1)
                    series.add_trace(go.Scatter(x=times, y=[p.raw_position[axis] for p in segment], mode="markers", marker={"size": 3, "color": "#718096"},
                                                name="raw", showlegend=legend and row == 1), row=row, col=1)
                    series.add_trace(go.Scatter(x=times, y=[p.smoothed_position[axis] for p in segment], mode="lines", line={"color": "#2B6CB0"},
                                                name="smoothed", showlegend=legend and row == 1), row=row, col=1)
    plan.update_layout(title="Road-plane paths (gaps are not connected; dotted segments contain interpolation)", template="plotly_white", height=520,
                       xaxis_title=unit, yaxis_title=unit, yaxis={"scaleanchor": "x"})
    st.plotly_chart(plan, width="stretch")
    if single:
        for event in events:
            series.add_vline(x=event.peak_time, line={"width": 1, "dash": "dot", "color": "#C05621"})
        series.update_layout(height=520, template="plotly_white", xaxis2_title="source time (s)", title="Raw and smoothed contact point with uncertainty band; event times dotted")
        st.plotly_chart(series, width="stretch")
    else:
        st.caption("Select one road user to compare raw and smoothed coordinates with their uncertainty band.")


def _heading_tab(run: MovementRun, selected: tuple[int, ...], events: tuple[MovementEvent, ...]) -> None:
    figure = go.Figure()
    by_track: dict[int, list[MovementObservation]] = defaultdict(list)
    for item in run.movement:
        if item.track_id in selected:
            by_track[item.track_id].append(item)
    for track, items in sorted(by_track.items()):
        figure.add_trace(go.Scatter(
            x=[item.timestamp_seconds for item in items], y=[item.heading_degrees for item in items], mode="lines", connectgaps=False, name=f"#{track}",
            error_y={"type": "data", "array": [item.heading_uncertainty_degrees or 0 for item in items], "thickness": 0.5, "width": 0} if len(selected) == 1 else None,
        ))
    if len(selected) == 1:
        for event in events:
            figure.add_vline(x=event.peak_time, line={"width": 1, "dash": "dot", "color": "#C05621"})
    figure.update_layout(height=420, template="plotly_white", xaxis_title="source time (s)", yaxis_title="heading (degrees, road-plane)",
                         title="Direction of displacement; blank where displacement is within uncertainty or the road user is stopped")
    st.plotly_chart(figure, width="stretch")


def _state_chart(movement: tuple[MovementObservation, ...], selected: tuple[int, ...], field: str, title: str) -> go.Figure:
    intervals: list[tuple[int, str, float, float]] = []
    by_track: dict[int, list[MovementObservation]] = defaultdict(list)
    for item in movement:
        if item.track_id in selected:
            by_track[item.track_id].append(item)
    for track, items in sorted(by_track.items()):
        start = items[0]
        for previous, current in zip(items, items[1:] + [None]):
            value = getattr(previous, field)
            if current is None or getattr(current, field) != value or current.source_frame != previous.source_frame + 1:
                intervals.append((track, value or "ambiguous / outside", start.timestamp_seconds, previous.timestamp_seconds))
                start = current
    figure = go.Figure()
    for label in sorted({item[1] for item in intervals}):
        rows = [item for item in intervals if item[1] == label]
        figure.add_trace(go.Bar(base=[row[2] for row in rows], x=[max(row[3] - row[2], 0.04) for row in rows], y=[f"#{row[0]}" for row in rows],
                                orientation="h", name=label, marker={"color": STATE_COLORS.get(label)}))
    figure.update_layout(barmode="overlay", height=max(240, 40 * len(by_track) + 120), template="plotly_white", title=title, xaxis_title="source time (s)")
    return figure


def _separation_tab(run: MovementRun, selected: tuple[int, ...], unit: str, frame_period: float) -> None:
    pairs = sorted({(item.track_a, item.track_b) for item in run.separation if item.track_a in selected or item.track_b in selected})
    if not pairs:
        st.info("No co-visible pair for the selected road user.")
        return
    approaches = {tuple(event.track_ids): event for event in run.events if event.event_type == "closest_approach"}
    pair = st.selectbox("Pair", pairs, format_func=lambda value: f"#{value[0]} and #{value[1]}" + (" · closest approach" if value in approaches else ""),
                        key=f"movement-pair-{run.manifest.run_id}-{selected}")
    rows = sorted((item for item in run.separation if (item.track_a, item.track_b) == pair), key=lambda item: item.source_frame)
    times = [item.source_time for item in rows]
    figure = go.Figure()
    figure.add_trace(go.Scatter(x=times + times[::-1], y=[item.separation + item.uncertainty for item in rows] + [max(0.0, item.separation - item.uncertainty) for item in rows][::-1],
                                fill="toself", fillcolor="rgba(107,70,193,0.15)", line={"width": 0}, name="uncertainty", hoverinfo="skip"))
    figure.add_trace(go.Scatter(x=times, y=[item.separation for item in rows], mode="lines+markers", marker={"size": 4}, name="contact-point separation"))
    event = approaches.get(pair)
    if event is not None:
        figure.add_vrect(x0=event.time_window[0], x1=event.time_window[1], fillcolor="rgba(192,86,33,0.12)", line_width=0)
        figure.add_vline(x=event.peak_time, line={"dash": "dot", "color": "#C05621"})
        st.caption(f"Closest approach at {display_time(event.peak_time, event.time_window, frame_period)}: {event.evidence_signals[0]}")
    figure.update_layout(height=420, template="plotly_white", xaxis_title="source time (s)", yaxis_title=f"separation ({unit})",
                         title="Separation between road-contact points (sampled; not vehicle-body gap)" + ("" if run.manifest.unit == "m" else " — relative units, not physical"))
    st.plotly_chart(figure, width="stretch")
