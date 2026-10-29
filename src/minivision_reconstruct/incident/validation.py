"""Evaluation of incident runs against scripted incident ground truth (README §11.10): detection, impact time and
location, collision type, PET, and contribution agreement, written as one immutable evaluation directory."""

from collections import defaultdict
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from typing import get_args

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.incident.models import (
    ConflictRecord, ContributionAssessment, ContributionRule, IncidentConfig, IncidentError, IncidentEvent, IncidentManifest,
)
from minivision_reconstruct.incident.staged_fixture import GroundTruthConflict, IncidentGroundTruth, incident_ground_truth_path
from minivision_reconstruct.movement.models import MovementEvent
from minivision_reconstruct.movement.validation import _match, _summary, _write_csv
from minivision_reconstruct.reconstruction.models import require_child, require_identifier
from minivision_reconstruct.reconstruction.storage import read_record, read_records, verify_hash


RULES: tuple[str, ...] = get_args(ContributionRule)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    return None if not precision or not recall else 2 * precision * recall / (precision + recall)


def true_pet_at(truth: IncidentGroundTruth, estimate: ConflictRecord) -> float | None:
    """PET from the true per-frame positions, measured with the estimator's own disc and entry/exit convention."""
    def passage(track: int, near_frame: int) -> tuple[int, int] | None:
        positions = next(user.positions for user in truth.road_users if user.track_id == track)
        inside = [frame for frame, x, y in positions if math.dist((x, y), estimate.location) <= estimate.radius]
        if not inside:
            return None
        runs: list[list[int]] = []
        for frame in inside:
            if not runs or frame != runs[-1][-1] + 1:
                runs.append([])
            runs[-1].append(frame)
        run = min(runs, key=lambda item: abs(item[0] - near_frame))
        last_visible = positions[-1][0]
        return run[0], run[-1] if run[-1] == last_visible else run[-1] + 1

    first, second = passage(estimate.track_first, estimate.first_enter_frame), passage(estimate.track_second, estimate.second_enter_frame)
    return None if first is None or second is None else (second[0] - first[1]) / truth.frame_rate


def evaluate_incident(cases_root: Path, case_ids: tuple[str, ...], incident_run: str, evaluation_root: Path, evaluation_id: str) -> Path:
    """Match proposals to scripted incidents per case and write metrics, matches, failures, and plots."""
    require_identifier(evaluation_id, "evaluation ID")
    require_identifier(incident_run, "incident run")
    if not case_ids:
        raise IncidentError("Evaluation requires at least one case with incident ground truth.")
    cases_root = cases_root.resolve()
    destination = evaluation_root / evaluation_id
    if destination.exists():
        raise IncidentError(f"Evaluation '{destination}' already exists; choose a new evaluation ID.")
    matches: list[dict[str, object]] = []
    pet_rows: list[dict[str, object]] = []
    contribution_rows: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"ground_truth": 0, "predicted": 0, "matched_same_type": 0})
    rule_counts: dict[str, dict[str, int]] = {rule: {"true_positive": 0, "false_positive": 0, "false_negative": 0} for rule in RULES}
    links: list[bool] = []
    configs: set[tuple[str, str]] = set()
    truth_hashes: dict[str, str] = {}
    config: IncidentConfig | None = None
    unannotated_conflicts = 0
    predicted_total = matched_total = truth_total = 0
    for case_id in case_ids:
        require_identifier(case_id, "case ID")
        case_root = cases_root / case_id
        truth_path = incident_ground_truth_path(cases_root, case_id)
        truth = read_record(truth_path, IncidentGroundTruth)
        truth_hashes[case_id] = sha256_file(truth_path)
        if truth.case_id != case_id:
            raise IncidentError(f"Ground truth for '{case_id}' names case '{truth.case_id}'.")
        run = require_child(case_root / "analysis" / "incident" / incident_run, case_root)
        manifest = read_record(run / "manifest.json", IncidentManifest)
        for artifact in manifest.artifacts:
            verify_hash(run / artifact.filename, artifact.sha256)
        config = read_record(run / "config.json", IncidentConfig)
        configs.add((manifest.config_id, manifest.config_sha256))
        tolerance = config.validation
        gate = tolerance.impact_time_tolerance_seconds * tolerance.matching_gate_factor
        events = read_records(run / "events.jsonl", IncidentEvent)
        assessments = {item.event_id: item for item in read_records(run / "assessments.jsonl", ContributionAssessment)}
        conflicts = read_records(run / "conflicts.jsonl", ConflictRecord)
        movement_ids = {item.event_id for item in read_records(case_root / "analysis" / "movement" / manifest.movement_run / "events.jsonl", MovementEvent)}
        citable = movement_ids | {item.conflict_id for item in conflicts}
        expected = list(truth.incidents)
        proposed = list(events)
        truth_total += len(expected)
        predicted_total += len(proposed)
        for item in expected:
            counts[item.event_type]["ground_truth"] += 1
        for item in proposed:
            counts[item.event_type]["predicted"] += 1
        pairs = _match(expected, proposed, gate)  # type: ignore[arg-type]
        matched_total += len(pairs)
        paired = {truth_index: proposed_index for truth_index, proposed_index in pairs}
        for truth_index, reference in enumerate(expected):
            candidate = proposed[paired[truth_index]] if truth_index in paired else None
            if candidate is None:
                failures.append({"case_id": case_id, "scenario": truth.scenario, "kind": "missed", "item": reference.incident_id,
                                 "detail": f"no proposal for tracks {reference.track_ids} within ±{gate:.2f} s of {reference.peak_time:.2f} s"})
            else:
                time_error = candidate.peak_time - reference.peak_time
                location_error = math.dist(candidate.location, reference.location)
                same_type = candidate.event_type == reference.event_type
                type_correct = None if reference.collision_type is None or not same_type else candidate.predicted_collision_type == reference.collision_type
                counts[reference.event_type]["matched_same_type"] += int(same_type)
                matches.append({
                    "case_id": case_id, "scenario": truth.scenario, "truth_incident": reference.incident_id, "predicted_event": candidate.event_id,
                    "truth_type": reference.event_type, "predicted_type": candidate.event_type, "event_type_correct": same_type,
                    "truth_peak_s": reference.peak_time, "predicted_peak_s": candidate.peak_time, "peak_error_s": time_error,
                    "peak_error_frames": time_error * truth.frame_rate, "within_time_tolerance": abs(time_error) <= tolerance.impact_time_tolerance_seconds,
                    "truth_in_reported_window": candidate.time_window[0] <= reference.peak_time <= candidate.time_window[1],
                    "location_error": location_error, "within_location_tolerance": location_error <= tolerance.impact_location_tolerance,
                    "truth_collision_type": reference.collision_type, "predicted_collision_type": candidate.predicted_collision_type,
                    "collision_type_correct": type_correct, "confidence": candidate.confidence,
                })
                for kind, failed, detail in (
                    ("wrong_event_type", not same_type, f"{candidate.event_type} proposed for {reference.event_type}"),
                    ("timing_outside_tolerance", abs(time_error) > tolerance.impact_time_tolerance_seconds, f"peak error {time_error:+.2f} s"),
                    ("location_outside_tolerance", location_error > tolerance.impact_location_tolerance, f"location error {location_error:.2f} {truth.unit}"),
                    ("wrong_collision_type", type_correct is False, f"{candidate.predicted_collision_type} proposed for {reference.collision_type}"),
                ):
                    if failed:
                        failures.append({"case_id": case_id, "scenario": truth.scenario, "kind": kind, "item": reference.incident_id, "detail": detail})
            if reference.contribution is None:
                continue
            assessment = assessments.get(candidate.event_id) if candidate is not None else None
            truth_actions = {(action.track_id, action.rule): action for action in reference.contribution.actions}
            predicted_actions = {(item.track_id, item.rule): item for item in assessment.supporting_evidence} if assessment else {}
            for key in truth_actions.keys() | predicted_actions.keys():
                outcome = "true_positive" if key in truth_actions and key in predicted_actions else "false_negative" if key in truth_actions else "false_positive"
                rule_counts[key[1]][outcome] += 1
                if outcome == "true_positive":
                    item, action = predicted_actions[key], truth_actions[key]
                    links.append(set(item.event_ids) <= citable and bool(item.source_frames) and (
                        action.action_time is None or abs(item.action_time - action.action_time) <= tolerance.impact_time_tolerance_seconds))
            agreed = assessment is not None and assessment.outcome == reference.contribution.outcome \
                and assessment.contributing_track_ids == reference.contribution.contributing_track_ids
            contribution_rows.append({
                "case_id": case_id, "scenario": truth.scenario, "truth_incident": reference.incident_id,
                "truth_outcome": reference.contribution.outcome, "predicted_outcome": assessment.outcome if assessment else "NOT_DETECTED",
                "truth_contributors": "-".join(map(str, reference.contribution.contributing_track_ids)),
                "predicted_contributors": "-".join(map(str, assessment.contributing_track_ids)) if assessment else "",
                "truth_rules": " ".join(sorted(f"{track}:{rule}" for track, rule in truth_actions)),
                "predicted_rules": " ".join(sorted(f"{track}:{rule}" for track, rule in predicted_actions)), "agreed": agreed,
            })
            if not agreed:
                failures.append({"case_id": case_id, "scenario": truth.scenario, "kind": "contribution_mismatch", "item": reference.incident_id,
                                 "detail": f"expected {reference.contribution.outcome} {reference.contribution.contributing_track_ids}, "
                                           f"received {contribution_rows[-1]['predicted_outcome']} {contribution_rows[-1]['predicted_contributors']}"})
        for index, candidate in enumerate(proposed):
            if index not in paired.values():
                failures.append({"case_id": case_id, "scenario": truth.scenario, "kind": "false_alarm", "item": candidate.event_id,
                                 "detail": f"{candidate.event_type} at {candidate.peak_time:.2f} s matches no scripted incident"})
        used: set[str] = set()
        for reference in truth.conflicts:
            candidates = [item for item in conflicts if {item.track_first, item.track_second} == {reference.track_first, reference.track_second}
                          and item.conflict_id not in used and math.dist(item.location, reference.location) <= tolerance.impact_location_tolerance]
            if not candidates:
                failures.append({"case_id": case_id, "scenario": truth.scenario, "kind": "pet_missed", "item": f"{reference.track_first}-{reference.track_second}",
                                 "detail": f"no estimated conflict within {tolerance.impact_location_tolerance} of the true crossing"})
                continue
            estimate = min(candidates, key=lambda item: math.dist(item.location, reference.location))
            used.add(estimate.conflict_id)
            pet_rows.append(_pet_row(case_id, truth, reference, estimate, tolerance.pet_tolerance_seconds))
            if not pet_rows[-1]["within_tolerance"]:
                failures.append({"case_id": case_id, "scenario": truth.scenario, "kind": "pet_outside_tolerance", "item": estimate.conflict_id,
                                 "detail": f"PET {estimate.pet_seconds:.2f} s vs {pet_rows[-1]['truth_pet_at_radius_s']} s at the same conflict radius"})
        unannotated_conflicts += len(conflicts) - len(used)
    if config is None:
        raise IncidentError("No case was evaluated.")
    if len(configs) != 1:
        raise IncidentError(f"Evaluated runs used different configurations: {sorted(configs)}; freeze one configuration first.")
    metrics = _metrics(evaluation_id, incident_run, case_ids, next(iter(configs)), counts, predicted_total, matched_total, truth_total,
                       matches, pet_rows, unannotated_conflicts, contribution_rows, rule_counts, links)
    metrics["ground_truth_sha256"] = truth_hashes
    staging = evaluation_root / f".{evaluation_id}-staging"
    staging.mkdir(parents=True)
    (staging / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_csv(staging / "matches.csv", matches, tuple(matches[0]) if matches else ("case_id",))
    _write_csv(staging / "pet.csv", pet_rows, tuple(pet_rows[0]) if pet_rows else ("case_id",))
    _write_csv(staging / "contributions.csv", contribution_rows, tuple(contribution_rows[0]) if contribution_rows else ("case_id",))
    _write_csv(staging / "failures.csv", failures, ("case_id", "scenario", "kind", "item", "detail"))
    (staging / "config_snapshot.json").write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
    _write_plots(staging / "plots.html", matches, pet_rows, contribution_rows)
    staging.rename(destination)
    return destination


def _pet_row(case_id: str, truth: IncidentGroundTruth, reference: GroundTruthConflict, estimate: ConflictRecord, tolerance: float) -> dict[str, object]:
    at_radius = true_pet_at(truth, estimate)
    error = None if at_radius is None else estimate.pet_seconds - at_radius
    return {
        "case_id": case_id, "scenario": truth.scenario, "conflict_id": estimate.conflict_id, "radius": estimate.radius,
        "estimated_pet_s": estimate.pet_seconds, "truth_pet_at_radius_s": at_radius, "truth_point_pet_s": reference.point_pet_seconds,
        "error_s": error, "within_tolerance": error is not None and abs(error) <= tolerance,
        "truth_in_reported_window": at_radius is not None and estimate.pet_window[0] <= at_radius <= estimate.pet_window[1],
        "location_error": math.dist(estimate.location, reference.location), "confidence": estimate.confidence,
    }


def _metrics(
    evaluation_id: str, incident_run: str, case_ids: tuple[str, ...], identity: tuple[str, str], counts: dict[str, dict[str, int]],
    predicted: int, matched: int, truth: int, matches: list[dict[str, object]], pets: list[dict[str, object]], unannotated: int,
    contributions: list[dict[str, object]], rules: dict[str, dict[str, int]], links: list[bool],
) -> dict[str, object]:
    precision, recall = _rate(matched, predicted), _rate(matched, truth)
    per_type = {}
    for event_type, count in sorted(counts.items()):
        type_precision, type_recall = _rate(count["matched_same_type"], count["predicted"]), _rate(count["matched_same_type"], count["ground_truth"])
        per_type[event_type] = {**count, "precision": type_precision, "recall": type_recall, "f1": _f1(type_precision, type_recall)}
    typed = [row for row in matches if row["collision_type_correct"] is not None]
    per_rule = {}
    for rule, count in rules.items():
        rule_precision = _rate(count["true_positive"], count["true_positive"] + count["false_positive"])
        rule_recall = _rate(count["true_positive"], count["true_positive"] + count["false_negative"])
        per_rule[rule] = {**count, "precision": rule_precision, "recall": rule_recall}
    detected = [row for row in contributions if row["predicted_outcome"] != "NOT_DETECTED"]
    scenarios = sorted({str(row["scenario"]) for row in matches + contributions})
    occluded = [row for row in contributions if row["scenario"] == "occluded_impact"]
    return {
        "evaluation_id": evaluation_id, "incident_run": incident_run, "cases": list(case_ids), "config_id": identity[0], "config_sha256": identity[1],
        "created_at": datetime.now(UTC).isoformat(), "ground_truth_kind": "controlled synthetic incident scenarios; not real CCTV",
        "detection": {"ground_truth": truth, "predicted": predicted, "matched": matched, "precision": precision, "recall": recall, "f1": _f1(precision, recall),
                      "false_alarms_per_clip": (predicted - matched) / len(case_ids), "per_event_type": per_type,
                      "event_type_accuracy": _rate(sum(bool(row["event_type_correct"]) for row in matches), len(matches))},
        "impact_time_error_s": _summary([float(row["peak_error_s"]) for row in matches]),
        "impact_time_within_tolerance": _rate(sum(bool(row["within_time_tolerance"]) for row in matches), len(matches)),
        "truth_time_inside_reported_window": _rate(sum(bool(row["truth_in_reported_window"]) for row in matches), len(matches)),
        "impact_location_error": _summary([float(row["location_error"]) for row in matches]),
        "impact_location_within_tolerance": _rate(sum(bool(row["within_location_tolerance"]) for row in matches), len(matches)),
        "collision_type_accuracy": _rate(sum(bool(row["collision_type_correct"]) for row in typed), len(typed)),
        "pet": {"matched_true_crossings": len(pets), "unannotated_estimated_conflicts": unannotated,
                "error_at_matched_radius_s": _summary([float(row["error_s"]) for row in pets if row["error_s"] is not None]),
                "within_tolerance": _rate(sum(bool(row["within_tolerance"]) for row in pets), len(pets)),
                "truth_inside_reported_window": _rate(sum(bool(row["truth_in_reported_window"]) for row in pets), len(pets)),
                "estimated_minus_point_pet_s": _summary([float(row["estimated_pet_s"]) - float(row["truth_point_pet_s"]) for row in pets])},
        "contribution": {"truth_assessments": len(contributions),
                         "agreement": _rate(sum(bool(row["agreed"]) for row in contributions), len(contributions)),
                         "agreement_given_detection": _rate(sum(bool(row["agreed"]) for row in detected), len(detected)),
                         "per_rule": per_rule,
                         "undetermined_rate_predicted": _rate(sum(row["predicted_outcome"] == "UNDETERMINED" for row in detected), len(detected)),
                         "undetermined_rate_truth": _rate(sum(row["truth_outcome"] == "UNDETERMINED" for row in contributions), len(contributions)),
                         "evidence_link_accuracy": _rate(sum(links), len(links))},
        "per_scenario": {scenario: {
            "matched": sum(row["scenario"] == scenario for row in matches),
            "peak_error_s": [round(float(row["peak_error_s"]), 3) for row in matches if row["scenario"] == scenario],
            "contribution_agreed": [bool(row["agreed"]) for row in contributions if row["scenario"] == scenario],
        } for scenario in scenarios},
        "gate": {
            "event_timing_error_measured": bool(matches),
            "occluded_scenarios_undetermined": bool(occluded) and all(row["predicted_outcome"] == "UNDETERMINED" for row in occluded),
            "every_matched_contributor_links_to_timed_action": bool(links) and all(links),
        },
    }


def _write_plots(path: Path, matches: list[dict[str, object]], pets: list[dict[str, object]], contributions: list[dict[str, object]]) -> None:
    """Standalone interactive report: impact timing and location error, PET agreement, and contribution agreement by scenario."""
    figure = make_subplots(rows=4, cols=1, vertical_spacing=0.08, subplot_titles=(
        "Impact time error by scenario (proposed − scripted)", "Impact location error by scenario",
        "Estimated PET vs true PET at the same conflict radius", "Contribution outcome agreement by scenario"))
    figure.add_trace(go.Box(x=[row["scenario"] for row in matches], y=[row["peak_error_s"] for row in matches], boxpoints="all",
                            marker={"color": "#2B6CB0"}, name="time error", showlegend=False), row=1, col=1)
    figure.update_yaxes(title_text="seconds", row=1, col=1)
    figure.add_trace(go.Box(x=[row["scenario"] for row in matches], y=[row["location_error"] for row in matches], boxpoints="all",
                            marker={"color": "#2F855A"}, name="location error", showlegend=False), row=2, col=1)
    figure.update_yaxes(title_text="calibration units", row=2, col=1)
    usable = [row for row in pets if row["truth_pet_at_radius_s"] is not None]
    figure.add_trace(go.Scatter(x=[row["truth_pet_at_radius_s"] for row in usable], y=[row["estimated_pet_s"] for row in usable], mode="markers",
                                marker={"color": "#C05621", "size": 9}, hovertext=[f"{row['case_id']} {row['conflict_id']}" for row in usable],
                                name="PET", showlegend=False), row=3, col=1)
    if usable:
        low = min(min(float(row["truth_pet_at_radius_s"]), float(row["estimated_pet_s"])) for row in usable)
        high = max(max(float(row["truth_pet_at_radius_s"]), float(row["estimated_pet_s"])) for row in usable)
        figure.add_trace(go.Scatter(x=[low, high], y=[low, high], mode="lines", line={"dash": "dash", "color": "#718096"}, showlegend=False), row=3, col=1)
    figure.update_xaxes(title_text="true PET at radius (s)", row=3, col=1)
    figure.update_yaxes(title_text="estimated PET (s)", row=3, col=1)
    scenarios = sorted({str(row["scenario"]) for row in contributions})
    for agreed, color in ((True, "#2F855A"), (False, "#C53030")):
        figure.add_trace(go.Bar(x=scenarios, y=[sum(row["scenario"] == scenario and bool(row["agreed"]) == agreed for row in contributions) for scenario in scenarios],
                                marker={"color": color}, name="agreed" if agreed else "disagreed"), row=4, col=1)
    figure.update_layout(barmode="stack", height=1700, template="plotly_white", title="Week 8 incident validation (controlled synthetic ground truth)")
    path.write_text(figure.to_html(full_html=True, include_plotlyjs=True), encoding="utf-8")
