"""Integrity-checked Week 7 movement analysis written as one immutable run directory."""

from collections import defaultdict
import csv
from datetime import UTC, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import TypeAdapter, ValidationError

from minivision_reconstruct import __version__
from minivision_reconstruct.domain.models import EvidenceManifest
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.movement.models import (
    DistanceUnit, MovementConfig, MovementError, MovementEvent, MovementManifest, MovementObservation, SmoothedPoint,
)
from minivision_reconstruct.movement.motion import MotionContext, analyze_segment, make_event
from minivision_reconstruct.movement.precision import round_to_uncertainty
from minivision_reconstruct.movement.road_use import analyze_lane_use, road_geometry, stop_line_crossings, zone_transitions
from minivision_reconstruct.movement.separation import pair_separations
from minivision_reconstruct.movement.sequence import order_events
from minivision_reconstruct.movement.smoothing import smooth_tracks
from minivision_reconstruct.movement.uncertainty import world_is_right_handed
from minivision_reconstruct.reconstruction.models import ArtifactHash, require_child, require_identifier
from minivision_reconstruct.reconstruction.pipeline import load_calibration
from minivision_reconstruct.reconstruction.storage import load_bundle, read_record, verify_hash, write_record


PIPELINE_VERSION = f"{__version__}+movement-1"
LIMITATIONS: tuple[str, ...] = (
    "Positions are bottom-centre box contact approximations, not vehicle footprints.",
    "Separation is measured between contact points, not between vehicle bodies.",
    "No speed, velocity, or acceleration is calculated; 'stopped' means no displacement beyond position uncertainty.",
    "Movement during unavailable (lost) frames is not inferred, and transitions across gaps are not proposed.",
    "Every event is a system proposal for reviewer confirmation, not a determination.",
    "Lane membership is not assigned to pedestrians.",
)


def load_movement_config(path: Path) -> MovementConfig:
    """Read the explicit movement configuration; no field has a default."""
    return read_record(path, MovementConfig)


def analyze_movement(cases_root: Path, case_id: str, reconstruction_run: str, config_path: Path, run_id: str) -> Path:
    """Derive smoothed trajectories, movement states, events, separation, and order for one reconstruction run."""
    require_identifier(case_id, "case ID")
    require_identifier(reconstruction_run, "reconstruction run")
    require_identifier(run_id, "movement run")
    cases_root = cases_root.resolve()
    case_root = cases_root / case_id
    run_directory = require_child(case_root / "analysis" / "reconstruction" / reconstruction_run, case_root)
    bundle = load_bundle(run_directory, "initial", "initial")
    manifest_hash = sha256_file(run_directory / "manifest.json")
    try:
        evidence = TypeAdapter(EvidenceManifest).validate_json((case_root / "metadata" / "evidence_manifest.json").read_bytes())
    except (OSError, ValidationError) as error:
        raise MovementError(f"Invalid evidence manifest for case '{case_id}': {error}") from error
    if evidence.frame_rate_decimal is None or evidence.frame_rate_decimal <= 0:
        raise MovementError(f"Case '{case_id}' has no valid frame rate; event timing cannot be bounded.")
    calibration_path = require_child(cases_root / bundle.manifest.source_calibration_path, case_root)
    verify_hash(calibration_path, bundle.manifest.source_calibration_sha256)
    calibration = load_calibration(calibration_path, evidence, case_root)
    config = load_movement_config(config_path)
    config_hash = sha256_file(config_path)
    unit: DistanceUnit = "m" if calibration.mode == "metric" else "relative_units"
    right_handed = world_is_right_handed(calibration)
    context = MotionContext(case_id, 1 / evidence.frame_rate_decimal, unit, right_handed, f"movement-pipeline {PIPELINE_VERSION}")
    smoothed = smooth_tracks(bundle.observations, calibration, config.smoothing, config.uncertainty, unit)
    geometry = road_geometry(calibration)
    by_track: dict[int, list[SmoothedPoint]] = defaultdict(list)
    for point in smoothed:
        by_track[point.track_id].append(point)
    events: list[MovementEvent] = []
    movement: list[MovementObservation] = []
    lane_sequences: dict[str, list[str]] = {}
    for track_id, track_points in sorted(by_track.items()):
        segments: dict[int, list[SmoothedPoint]] = defaultdict(list)
        for point in track_points:
            if point.segment_id is not None:
                segments[point.segment_id].append(point)
        headings: dict[int, tuple[float, float] | None] = {}
        states: dict[int, str] = {}
        lanes: dict[int, str | None] = {}
        candidates: dict[int, tuple[str, ...]] = {}
        sequence: list[str] = []
        for segment_id in sorted(segments):
            segment = tuple(segments[segment_id])
            motion = analyze_segment(segment, calibration, config.motion, context)
            headings.update(motion.headings)
            states.update(motion.states)
            events.extend((*motion.events, *stop_line_crossings(segment, geometry, context), *zone_transitions(segment, geometry, config.road_use, context)))
            if segment[0].class_name == "person":
                continue
            lane_use = analyze_lane_use(segment, geometry, config.road_use, context)
            lanes.update(lane_use.assignment)
            candidates.update(lane_use.candidates)
            events.extend(lane_use.events)
            for lane in lane_use.sequence:
                if not sequence or sequence[-1] != lane:
                    sequence.append(lane)
        if track_points[0].class_name != "person":
            lane_sequences[str(track_id)] = sequence
        available = tuple(point for point in track_points if point.smoothed_position is not None)
        if available:
            for event_type, point in (("first_observed", available[0]), ("last_observed", available[-1])):
                events.append(make_event(
                    context, event_type, (track_id,), (point,), 0, 0, 0, (point.source_time, point.source_time),
                    ("first available contact point in this run" if event_type == "first_observed" else "last available contact point in this run",),
                    "calculated", None, None, point.smoothed_position, point.position_uncertainty, (point,),
                ))
        for point in track_points:
            heading = headings.get(point.source_frame)
            movement.append(MovementObservation(
                track_id=track_id, source_frame=point.source_frame, timestamp_seconds=point.source_time,
                world_x=None if point.smoothed_position is None else point.smoothed_position[0],
                world_y=None if point.smoothed_position is None else point.smoothed_position[1],
                heading_degrees=None if heading is None else heading[0], heading_uncertainty_degrees=None if heading is None else heading[1],
                position_uncertainty=point.position_uncertainty,
                movement_state=states.get(point.source_frame, "unavailable") if point.smoothed_position is not None else "unavailable",
                lane_or_zone_id=lanes.get(point.source_frame), lane_candidates=candidates.get(point.source_frame, ()),
                source_quality=_source_quality(point), unit=unit,
            ))
    separations, approaches = pair_separations(smoothed, config.separation, context, calibration.mode == "metric")
    events.extend(approaches)
    if len({event.event_id for event in events}) != len(events):
        raise MovementError("Movement analysis produced duplicate event IDs; report this input for investigation.")
    ordered_events = tuple(sorted(events, key=lambda event: (event.peak_time, event.event_id)))
    order = order_events(case_id, ordered_events)
    root = case_root / "analysis" / "movement"
    root.mkdir(exist_ok=True)
    destination = root / run_id
    if destination.exists():
        raise MovementError(f"Movement run '{destination}' already exists; choose a new run ID.")
    with TemporaryDirectory(prefix=".movement-", dir=root) as temporary:
        staged = Path(temporary)
        _write_jsonl(staged / "smoothed.jsonl", smoothed)
        _write_jsonl(staged / "movement_observations.jsonl", tuple(movement))
        _write_jsonl(staged / "events.jsonl", ordered_events)
        _write_jsonl(staged / "separation.jsonl", separations)
        write_record(staged / "sequence.json", order)
        with (staged / "lane_sequences.json").open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(lane_sequences, indent=2, sort_keys=True) + "\n")
        (staged / "config.json").write_bytes(config_path.read_bytes())
        _write_trajectories_csv(staged / "trajectories.csv", smoothed, tuple(movement))
        limitations = LIMITATIONS + (() if calibration.mode == "metric" else ("Relative calibration: distances and separations are not physical measurements.",)) + (
            () if right_handed else ("Road-plane coordinates are mirrored; turn directions follow the mirrored convention and need review.",))
        manifest = MovementManifest(
            schema_version="1.0", case_id=case_id, run_id=run_id, reconstruction_run=reconstruction_run,
            source_reconstruction_manifest_sha256=manifest_hash,
            source_calibration_path=bundle.manifest.source_calibration_path, source_calibration_sha256=bundle.manifest.source_calibration_sha256,
            config_id=config.config_id, config_sha256=config_hash, pipeline_version=PIPELINE_VERSION,
            calibration_mode=calibration.mode, unit=unit, world_is_right_handed=right_handed,
            track_count=len(by_track), event_count=len(ordered_events), created_at=datetime.now(UTC).isoformat(),
            limitations=limitations,
            artifacts=tuple(ArtifactHash(filename=path.name, sha256=sha256_file(path)) for path in sorted(staged.iterdir())),
        )
        verify_hash(run_directory / "manifest.json", manifest_hash)
        write_record(staged / "manifest.json", manifest)
        staged.rename(destination)
    return destination


def _source_quality(point: SmoothedPoint) -> str:
    if point.smoothed_position is None:
        return "unavailable"
    if point.provenance == "reviewer":
        return "reviewer"
    if point.state == "interpolated" or point.provenance == "review_interpolation":
        return "interpolated"
    return "recovered" if point.state == "recovered" else "observed"


def _write_jsonl(path: Path, records: tuple[object, ...]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(record.model_dump_json() + "\n")


def _write_trajectories_csv(path: Path, smoothed: tuple[SmoothedPoint, ...], movement: tuple[MovementObservation, ...]) -> None:
    """Reviewer-facing table rounded to each value's uncertainty; JSONL keeps full precision."""
    states = {(item.track_id, item.source_frame): item for item in movement}
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("track_id", "source_frame", "source_time_s", "class_name", "track_state", "provenance",
                         "raw_x", "raw_y", "smoothed_x", "smoothed_y", "position_uncertainty", "unit",
                         "movement_state", "heading_degrees", "heading_uncertainty_degrees", "lane_or_zone_id"))
        for point in smoothed:
            item = states[(point.track_id, point.source_frame)]
            row: list[object] = [point.track_id, point.source_frame, point.source_time, point.class_name, point.state, point.provenance]
            if point.smoothed_position is None or point.raw_position is None or point.position_uncertainty is None:
                row.extend(("", "", "", "", "", point.unit))
            else:
                values = [round_to_uncertainty(value, point.position_uncertainty)[0] for value in (*point.raw_position, *point.smoothed_position)]
                row.extend((*values, round_to_uncertainty(point.position_uncertainty, point.position_uncertainty)[1], point.unit))
            if item.heading_degrees is None or item.heading_uncertainty_degrees is None:
                row.extend((item.movement_state, "", ""))
            else:
                heading, spread = round_to_uncertainty(item.heading_degrees, max(item.heading_uncertainty_degrees, 1.0))
                row.extend((item.movement_state, heading, spread))
            row.append(item.lane_or_zone_id or "")
            writer.writerow(row)
