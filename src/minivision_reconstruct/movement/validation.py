"""Ground-truth schema and timing/position validation of movement runs against annotated staged motion."""

from collections import defaultdict
import csv
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from typing import Literal

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pydantic import Field
from scipy.optimize import linear_sum_assignment
from scipy.stats import kendalltau

from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.movement.models import (
    DistanceUnit, EventType, MovementConfig, MovementError, MovementEvent, MovementManifest, SmoothedPoint,
)
from minivision_reconstruct.reconstruction.models import Artifact, require_child, require_identifier
from minivision_reconstruct.reconstruction.storage import read_record, read_records, verify_hash


class GroundTruthEvent(Artifact):
    event_id: str
    event_type: EventType
    track_ids: tuple[int, ...]
    start_time: float | None
    peak_time: float = Field(ge=0)
    end_time: float | None
    from_feature: str | None
    to_feature: str | None
    note: str


class GroundTruthTrack(Artifact):
    track_id: int = Field(ge=1)
    class_name: str
    scenario: str
    positions: tuple[tuple[int, float, float], ...]


class MovementGroundTruth(Artifact):
    """Exhaustive labels for the declared event types; hand-annotated staged videos use the same schema."""

    schema_version: Literal["1.0"]
    case_id: str
    source: Literal["controlled_synthetic", "staged_physical"]
    annotator: str
    unit: DistanceUnit
    frame_rate: float = Field(gt=0)
    annotated_event_types: tuple[EventType, ...]
    tracks: tuple[GroundTruthTrack, ...]
    events: tuple[GroundTruthEvent, ...]
    lane_sequences: dict[str, tuple[str, ...]]
    notes: tuple[str, ...]


def ground_truth_path(cases_root: Path, case_id: str) -> Path:
    return cases_root / case_id / "annotations" / "movement_ground_truth.json"


def evaluate_movement(cases_root: Path, case_ids: tuple[str, ...], movement_run: str, evaluation_root: Path, evaluation_id: str) -> Path:
    """Match proposed to annotated events per case and write metrics, matches, failures, and plots."""
    require_identifier(evaluation_id, "evaluation ID")
    require_identifier(movement_run, "movement run")
    if not case_ids:
        raise MovementError("Evaluation requires at least one case with movement ground truth.")
    cases_root = cases_root.resolve()
    destination = evaluation_root / evaluation_id
    if destination.exists():
        raise MovementError(f"Evaluation '{destination}' already exists; choose a new evaluation ID.")
    matches: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    position_rows: list[tuple[float, float, float]] = []
    lane_results: list[bool] = []
    window_hits: list[bool] = []
    order_pairs: list[tuple[float, float]] = []
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"ground_truth": 0, "predicted": 0, "within_tolerance": 0, "gated": 0})
    configs: set[tuple[str, str]] = set()
    plot_cases: list[tuple[str, tuple[GroundTruthEvent, ...], tuple[MovementEvent, ...]]] = []
    config: MovementConfig | None = None
    truth_hashes: dict[str, str] = {}
    for case_id in case_ids:
        require_identifier(case_id, "case ID")
        case_root = cases_root / case_id
        truth = read_record(ground_truth_path(cases_root, case_id), MovementGroundTruth)
        truth_hashes[case_id] = sha256_file(ground_truth_path(cases_root, case_id))
        if truth.case_id != case_id:
            raise MovementError(f"Ground truth for '{case_id}' names case '{truth.case_id}'.")
        run = require_child(case_root / "analysis" / "movement" / movement_run, case_root)
        manifest = read_record(run / "manifest.json", MovementManifest)
        for artifact in manifest.artifacts:
            verify_hash(run / artifact.filename, artifact.sha256)
        config = read_record(run / "config.json", MovementConfig)
        configs.add((manifest.config_id, manifest.config_sha256))
        events = tuple(item for item in read_records(run / "events.jsonl", MovementEvent) if item.event_type in truth.annotated_event_types)
        truth_events = tuple(item for item in truth.events if item.event_type in truth.annotated_event_types)
        plot_cases.append((case_id, truth_events, events))
        for event_type in truth.annotated_event_types:
            tolerance = config.validation.timing_tolerance_seconds.get(event_type)
            if tolerance is None:
                raise MovementError(f"No declared timing tolerance for annotated event type '{event_type}'.")
            gate = tolerance * config.validation.matching_gate_factor
            expected = [item for item in truth_events if item.event_type == event_type]
            proposed = [item for item in events if item.event_type == event_type]
            counts[event_type]["ground_truth"] += len(expected)
            counts[event_type]["predicted"] += len(proposed)
            pairs = _match(expected, proposed, gate)
            matched_truth, matched_proposed = {index for index, _ in pairs}, {index for _, index in pairs}
            for truth_index, proposed_index in pairs:
                reference, candidate = expected[truth_index], proposed[proposed_index]
                peak_error = candidate.peak_time - reference.peak_time
                within = abs(peak_error) <= tolerance
                counts[event_type]["gated"] += 1
                counts[event_type]["within_tolerance"] += int(within)
                window_hits.append(candidate.time_window[0] <= reference.peak_time <= candidate.time_window[1])
                order_pairs.append((reference.peak_time, candidate.peak_time))
                matches.append({
                    "case_id": case_id, "event_type": event_type, "track_ids": "-".join(map(str, reference.track_ids)),
                    "ground_truth_event": reference.event_id, "predicted_event": candidate.event_id,
                    "ground_truth_peak_s": reference.peak_time, "predicted_peak_s": candidate.peak_time,
                    "peak_error_s": peak_error, "peak_error_frames": peak_error * truth.frame_rate,
                    "start_error_s": None if reference.start_time is None or event_type not in {"turn_left", "turn_right", "lane_change"} else candidate.start_time - reference.start_time,
                    "end_error_s": None if reference.end_time is None or event_type not in {"turn_left", "turn_right", "lane_change"} else candidate.end_time - reference.end_time,
                    "tolerance_s": tolerance, "within_tolerance": within,
                    "ground_truth_in_reported_window": window_hits[-1], "confidence": candidate.confidence,
                })
                if not within:
                    failures.append({"case_id": case_id, "kind": "timing_outside_tolerance", "event_type": event_type, "event": reference.event_id,
                                     "detail": f"peak error {peak_error:+.2f} s exceeds ±{tolerance} s"})
            for index, reference in enumerate(expected):
                if index not in matched_truth:
                    failures.append({"case_id": case_id, "kind": "missed", "event_type": event_type, "event": reference.event_id,
                                     "detail": f"no proposal within ±{gate:.2f} s of {reference.peak_time:.2f} s ({reference.note})"})
            for index, candidate in enumerate(proposed):
                if index not in matched_proposed:
                    failures.append({"case_id": case_id, "kind": "false_alarm", "event_type": event_type, "event": candidate.event_id,
                                     "detail": f"no annotated event within ±{gate:.2f} s of {candidate.peak_time:.2f} s"})
        smoothed = read_records(run / "smoothed.jsonl", SmoothedPoint)
        lookup = {(item.track_id, item.source_frame): item for item in smoothed if item.smoothed_position is not None}
        for track in truth.tracks:
            for frame, x, y in track.positions:
                point = lookup.get((track.track_id, frame))
                if point is not None:
                    position_rows.append((math.dist(point.raw_position, (x, y)), math.dist(point.smoothed_position, (x, y)), point.position_uncertainty))
        actual_lanes = json.loads((run / "lane_sequences.json").read_text())
        for track_id, sequence in truth.lane_sequences.items():
            matched = tuple(actual_lanes.get(track_id, ())) == sequence
            lane_results.append(matched)
            if not matched:
                failures.append({"case_id": case_id, "kind": "lane_sequence", "event_type": "lane_sequence", "event": f"track-{track_id}",
                                 "detail": f"expected {list(sequence)}, received {actual_lanes.get(track_id, [])}"})
    if config is None:
        raise MovementError("No case was evaluated.")
    if len(configs) != 1:
        raise MovementError(f"Evaluated runs used different configurations: {sorted(configs)}; freeze one configuration first.")
    metrics = _metrics(counts, matches, position_rows, lane_results, window_hits, order_pairs, case_ids, movement_run, evaluation_id, next(iter(configs)), config)
    metrics["ground_truth_sha256"] = truth_hashes
    staging = evaluation_root / f".{evaluation_id}-staging"
    staging.mkdir(parents=True)
    (staging / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_csv(staging / "matches.csv", matches, ("case_id", "event_type", "track_ids", "ground_truth_event", "predicted_event", "ground_truth_peak_s", "predicted_peak_s",
                                                   "peak_error_s", "peak_error_frames", "start_error_s", "end_error_s", "tolerance_s", "within_tolerance",
                                                   "ground_truth_in_reported_window", "confidence"))
    _write_csv(staging / "failures.csv", failures, ("case_id", "kind", "event_type", "event", "detail"))
    (staging / "config_snapshot.json").write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
    _write_plots(staging / "plots.html", plot_cases, matches, position_rows)
    staging.rename(destination)
    return destination


def _match(expected: list[GroundTruthEvent], proposed: list[MovementEvent], gate: float) -> tuple[tuple[int, int], ...]:
    """Minimum-total-error assignment within the same road users and the matching gate."""
    if not expected or not proposed:
        return ()
    blocked = 1e9
    cost = np.full((len(expected), len(proposed)), blocked)
    for row, reference in enumerate(expected):
        for column, candidate in enumerate(proposed):
            error = abs(candidate.peak_time - reference.peak_time)
            if tuple(sorted(candidate.track_ids)) == tuple(sorted(reference.track_ids)) and error <= gate:
                cost[row, column] = error
    rows, columns = linear_sum_assignment(cost)
    return tuple((int(row), int(column)) for row, column in zip(rows, columns, strict=True) if cost[row, column] < blocked)


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "p90": None, "max": None}
    absolute = np.abs(np.asarray(values))
    return {"count": len(values), "mean": float(absolute.mean()), "median": float(np.median(absolute)),
            "p90": float(np.percentile(absolute, 90)), "max": float(absolute.max())}


def _metrics(
    counts: dict[str, dict[str, int]], matches: list[dict[str, object]], positions: list[tuple[float, float, float]],
    lanes: list[bool], windows: list[bool], order: list[tuple[float, float]], case_ids: tuple[str, ...],
    movement_run: str, evaluation_id: str, config_identity: tuple[str, str], config: MovementConfig,
) -> dict[str, object]:
    per_type: dict[str, object] = {}
    for event_type, count in sorted(counts.items()):
        rows = [row for row in matches if row["event_type"] == event_type]
        precision = count["within_tolerance"] / count["predicted"] if count["predicted"] else None
        recall = count["within_tolerance"] / count["ground_truth"] if count["ground_truth"] else None
        f1 = None if not precision or not recall else 2 * precision * recall / (precision + recall)
        per_type[event_type] = {
            **count, "precision": precision, "recall": recall, "f1": f1,
            "tolerance_s": config.validation.timing_tolerance_seconds[event_type],
            "peak_error_s": _summary([float(row["peak_error_s"]) for row in rows]),
            "start_error_s": _summary([float(row["start_error_s"]) for row in rows if row["start_error_s"] is not None]),
            "end_error_s": _summary([float(row["end_error_s"]) for row in rows if row["end_error_s"] is not None]),
            "mean_signed_peak_error_s": float(np.mean([float(row["peak_error_s"]) for row in rows])) if rows else None,
            "ground_truth_inside_reported_window": float(np.mean([bool(row["ground_truth_in_reported_window"]) for row in rows])) if rows else None,
        }
    raw, smooth, uncertainty = (np.asarray(column) for column in zip(*positions, strict=True)) if positions else (np.asarray([]),) * 3
    tau = kendalltau([pair[0] for pair in order], [pair[1] for pair in order]).statistic if len(order) >= 2 else None
    maneuvers = [row for row in matches if row["event_type"] in {"stop", "start", "turn_left", "turn_right", "lane_change"}]
    return {
        "evaluation_id": evaluation_id, "movement_run": movement_run, "cases": list(case_ids),
        "config_id": config_identity[0], "config_sha256": config_identity[1], "created_at": datetime.now(UTC).isoformat(),
        "ground_truth_kind": "see per-case annotations; controlled synthetic fixtures are not real CCTV",
        "per_event_type": per_type,
        "maneuver_and_stop_peak_error_s": _summary([float(row["peak_error_s"]) for row in maneuvers]),
        "all_matched_peak_error_s": _summary([float(row["peak_error_s"]) for row in matches]),
        "ground_truth_time_inside_reported_window": float(np.mean(windows)) if windows else None,
        "movement_order_kendall_tau": None if tau is None or math.isnan(tau) else float(tau),
        "lane_sequence_exact_match": float(np.mean(lanes)) if lanes else None,
        "position_samples": len(positions),
        "position_rmse_raw": float(np.sqrt(np.mean(raw ** 2))) if positions else None,
        "position_rmse_smoothed": float(np.sqrt(np.mean(smooth ** 2))) if positions else None,
        "true_position_inside_reported_uncertainty": float(np.mean(smooth <= uncertainty)) if positions else None,
        "gate": {
            "timing_error_reported_against_ground_truth": bool(matches),
            "all_matched_events_within_tolerance": bool(matches) and all(bool(row["within_tolerance"]) for row in matches),
        },
    }


def _write_csv(path: Path, rows: list[dict[str, object]], columns: tuple[str, ...]) -> None:
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("" if value is None else round(value, 3) if isinstance(value, float) else value) for key, value in row.items()})


def _write_plots(path: Path, cases: list[tuple[str, tuple[GroundTruthEvent, ...], tuple[MovementEvent, ...]]], matches: list[dict[str, object]], positions: list[tuple[float, float, float]]) -> None:
    """Standalone interactive validation report: annotated vs proposed timing and error distributions."""
    figure = make_subplots(rows=len(cases) + 2, cols=1, vertical_spacing=0.06,
                           subplot_titles=(*[f"{case_id}: annotated (○) vs proposed (●) event times" for case_id, _, _ in cases],
                                           "Peak timing error by event type (proposed − annotated)", "Contact-point error vs reported uncertainty"))
    for row, (case_id, truth, events) in enumerate(cases, 1):
        figure.add_trace(go.Scatter(
            x=[item.peak_time for item in truth], y=[f"#{'-'.join(map(str, item.track_ids))} {item.event_type}" for item in truth], mode="markers",
            marker={"symbol": "circle-open", "size": 12, "color": "#2B6CB0"}, name="annotated", legendgroup="annotated", showlegend=row == 1,
            hovertext=[item.note for item in truth]), row=row, col=1)
        figure.add_trace(go.Scatter(
            x=[item.peak_time for item in events], y=[f"#{'-'.join(map(str, item.track_ids))} {item.event_type}" for item in events], mode="markers",
            marker={"symbol": "circle", "size": 7, "color": "#C05621"}, name="proposed", legendgroup="proposed", showlegend=row == 1,
            error_x={"type": "data", "symmetric": False, "array": [item.time_window[1] - item.peak_time for item in events],
                     "arrayminus": [item.peak_time - item.time_window[0] for item in events], "thickness": 1},
            hovertext=[f"{item.event_id} · {item.confidence}" for item in events]), row=row, col=1)
        figure.update_xaxes(title_text="source time (s)", row=row, col=1)
    figure.add_trace(go.Box(x=[row["event_type"] for row in matches], y=[row["peak_error_s"] for row in matches], boxpoints="all",
                            marker={"color": "#2F855A"}, name="peak error", showlegend=False), row=len(cases) + 1, col=1)
    figure.update_yaxes(title_text="seconds", row=len(cases) + 1, col=1)
    if positions:
        figure.add_trace(go.Scattergl(x=[item[2] for item in positions], y=[item[1] for item in positions], mode="markers",
                                      marker={"size": 3, "opacity": 0.35, "color": "#6B46C1"}, name="smoothed error", showlegend=False), row=len(cases) + 2, col=1)
        limit = max(max(item[2] for item in positions), max(item[1] for item in positions))
        figure.add_trace(go.Scatter(x=[0, limit], y=[0, limit], mode="lines", line={"dash": "dash", "color": "#718096"}, name="error = uncertainty", showlegend=False), row=len(cases) + 2, col=1)
        figure.update_xaxes(title_text="reported position uncertainty", row=len(cases) + 2, col=1)
        figure.update_yaxes(title_text="smoothed position error", row=len(cases) + 2, col=1)
    figure.update_layout(height=420 * (len(cases) + 2), template="plotly_white", title="Week 7 movement validation (controlled ground truth)")
    path.write_text(figure.to_html(full_html=True, include_plotlyjs=True), encoding="utf-8")

