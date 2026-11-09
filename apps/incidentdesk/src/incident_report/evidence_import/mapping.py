"""Translate producer dictionaries once at the import boundary."""

from typing import Any

from pydantic import TypeAdapter

from .models import TIMESTAMP_TOLERANCE_SECONDS
from .payloads import Payload

_ADAPTER = TypeAdapter(Payload)


def payload(
    kind: str,
    row: dict[str, Any],
    source: Any,
    producer: str,
    case_id: str,
    movement: dict[str, Any],
    track: Any,
    sequence: dict[str, Any],
    assessments: list[dict[str, Any]],
    chain: list[str],
    frames: dict[int, float],
    events: dict[str, dict[str, Any]],
) -> Payload:
    def action(item: dict[str, Any]) -> dict[str, Any]:
        return {
            **{
                k: item[k]
                for k in ("rule", "description", "event_ids", "source_frames", "action_time", "seconds_before_peak")
            },
            "track_ref": track(item["track_id"]),
        }

    data: dict[str, Any] = {"kind": kind}
    if kind == "event_proposal":
        keys = (
            "event_id",
            "event_type",
            "start_frame",
            "peak_frame",
            "end_frame",
            "start_time",
            "peak_time",
            "end_time",
            "time_window",
            "location",
            "unit",
            "location_uncertainty",
            "confidence",
        )
        data.update({k: row[k] for k in keys})
        for boundary in ("start", "peak", "end"):
            canonical = frames.get(row[f"{boundary}_frame"])
            data[f"{boundary}_time"] = (
                canonical
                if canonical is not None and abs(canonical - row[f"{boundary}_time"]) <= TIMESTAMP_TOLERANCE_SECONDS
                else None
            )
        data["signals"] = [
            {
                "name": s["name"],
                "kind": s["kind"],
                "source_frames": s["source_frames"],
                "track_refs": [track(t) for t in s["track_ids"]],
                "description": s["description"],
            }
            for s in row.get("signals", [])
        ]
        if "evidence_signals" in row:
            data["signals"] = [
                {"name": s, "kind": None, "source_frames": None, "track_refs": [], "description": s}
                for s in row["evidence_signals"]
            ]
        assessment = next((a for a in assessments if a["event_id"] == row["event_id"]), None)
        data["supporting_evidence"] = (
            None if assessment is None else [action(i) for i in assessment["supporting_evidence"]]
        )
        data["contrary_evidence"] = None if assessment is None else [action(i) for i in assessment["counterevidence"]]
        data["order_unresolved_with"] = sequence.get(row["event_id"])
    elif kind == "geometry":
        point = (
            row.get("smoothed_position")
            if "smoothed_position" in row
            else None
            if row["world_x"] is None or row["world_y"] is None
            else (row["world_x"], row["world_y"])
        )
        data.update(
            relative_uncertainty=row.get("relative_uncertainty"),
            coordinates=point,
            raw_coordinates=row.get("raw_position"),
            unit=row["unit"],
            calibration_mode=movement.get("calibration_mode"),
            calibration_revision=source.calibration_revision,
            position_uncertainty=row.get("position_uncertainty"),
            heading_degrees=row.get("heading_degrees"),
            heading_uncertainty_degrees=row.get("heading_uncertainty_degrees"),
            source_quality=row.get("source_quality", row.get("state")),
        )
    elif kind == "track_reference":
        data["class_name"] = row.get("class_name")
        data.update({k: row[k] for k in ("track_id", "state", "position", "bbox", "provenance")})
        data.update(
            producer=producer,
            case_id=case_id,
            view_id=source.view_id,
            tracking_run=source.tracking_run,
            revision=source.track_revision,
        )
    elif kind == "review_overlay":
        data.update(
            {
                k: row[k]
                for k in (
                    "target_id",
                    "target_kind",
                    "action",
                    "reviewer",
                    "modified_peak_time",
                    "modified_location",
                    "modified_outcome",
                )
            }
        )
        data.update(
            reason=row["note"],
            revision=source.review_revision,
            revision_chain_hashes=chain,
            modified_contributing_track_refs=None
            if row["modified_contributing_track_ids"] is None
            else [track(t) for t in row["modified_contributing_track_ids"]],
        )
    elif kind == "assessment":
        data.update({k: row[k] for k in ("assessment_id", "event_id", "outcome", "lookback_window", "rule_source")})
        data.update(
            contributing_track_refs=[track(t) for t in row["contributing_track_ids"]],
            supporting_evidence=[action(i) for i in row["supporting_evidence"]],
            contrary_evidence=[action(i) for i in row["counterevidence"]],
            unresolved_facts=[
                {
                    **{k: i[k] for k in ("kind", "time_window", "description", "decisive")},
                    "track_refs": [track(t) for t in i["track_ids"]],
                }
                for i in row["unresolved_facts"]
            ],
        )
    elif kind == "sequence":
        data.update(
            {k: row[k] for k in ("event_id", "event_type", "rank", "peak_time", "time_window", "order_unresolved_with")}
        )
        event = events.get(row["event_id"])
        frame = None if event is None else event["peak_frame"]
        canonical = frames.get(frame) if frame is not None else None
        resolved = canonical is not None and abs(canonical - row["peak_time"]) <= TIMESTAMP_TOLERANCE_SECONDS
        data["peak_frame"] = frame if resolved else None
        data["peak_time"] = canonical if resolved else None
        if len(row["time_window"]) != 2 or row["time_window"][0] > row["time_window"][1]:
            data["time_window"] = (None, None)
            data["covering_frames"] = None
            return _ADAPTER.validate_python(data)
        window: list[float] = []
        for time in row["time_window"]:
            matches = [value for value in frames.values() if abs(value - time) <= TIMESTAMP_TOLERANCE_SECONDS]
            window.append(matches[0] if len(matches) == 1 else time)
        data["time_window"] = window
        inside = [(time, frame) for frame, time in frames.items() if window[0] <= time <= window[1]]
        inside.sort()
        data["covering_frames"] = (inside[0][1], inside[-1][1]) if inside else None

    else:
        data.update(
            producer_case_id=source.producer_case_id or case_id,
            video_metadata=source.video_metadata,
            transformations=source.transformations,
            frame_origin=source.frame_origin,
            calibration_revision=source.calibration_revision,
            reconstruction_manifest=source.reconstruction_manifest,
            movement_manifest=source.movement_manifest,
            incident_manifest=source.incident_manifest,
            reviewed_manifest=source.reviewed_manifest,
            original_video_sha256=source.original_video_sha256,
            tracking_run=source.tracking_run,
            track_revision=source.track_revision,
            reconstruction_run=source.reconstruction_run,
            movement_run=source.movement_run,
            incident_run=source.incident_run,
            review_revision=source.review_revision,
            source_frame=row.get("source_frame"),
            source_time=(
                frames.get(row["source_frame"])
                if "source_frame" in row
                and row["source_frame"] in frames
                and abs(frames[row["source_frame"]] - row["source_time"]) <= TIMESTAMP_TOLERANCE_SECONDS
                else None
            ),
            replay_frame=row.get("replay_frame"),
            replay_time=row.get("replay_time"),
            timestamp_provenance=row.get("timestamp_source"),
        )
    return _ADAPTER.validate_python(data)
