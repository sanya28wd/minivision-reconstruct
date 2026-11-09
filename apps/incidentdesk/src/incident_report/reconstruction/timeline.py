"""Display-only timeline projections. No rules or AI consume these records."""

from dataclasses import dataclass
from typing import Any

from incident_report.evidence_import.models import ImportDecision
from incident_report.evidence_import.payloads import EventFields, ReviewFields, TrackFields

EVENT_LABELS = {
    "first_observed": "First seen",
    "last_observed": "Last seen",
    "stop": "Stops",
    "start": "Starts moving",
    "turn_left": "Turns left",
    "turn_right": "Turns right",
    "lane_change": "Changes lane",
    "lane_entry": "Enters lane",
    "stop_line_crossing": "Crosses stop line",
    "zone_entry": "Enters zone",
    "zone_exit": "Leaves zone",
    "closest_approach": "Closest approach",
    "collision_candidate": "Possible collision",
    "near_miss_candidate": "Possible near miss",
    "single_vehicle_candidate": "Possible single-vehicle event",
}
REVIEW_LABELS = {
    "PROPOSED_BY_SYSTEM": "Proposed",
    "CONFIRMED_BY_REVIEWER": "Confirmed by reconstruction reviewer",
    "MODIFIED_BY_REVIEWER": "Modified by reconstruction reviewer",
    "REJECTED_BY_REVIEWER": "Rejected by reconstruction reviewer",
}
MISSING_LABELS = {
    "no_incident_candidates": "no incident candidates were supplied; assessment is unavailable",
    "calibration_failed": "calibration failed",
    "calibration_unavailable": "calibration is unavailable",
    "source_timestamps_unavailable": "source timestamps are unavailable",
    "source_timestamps_incomplete": "source timestamps are incomplete",
    "upstream_artifacts_missing": "required reconstruction evidence is missing",
}


@dataclass(frozen=True)
class TimelineRow:
    key: str
    event_id: str
    text: str
    view_id: str
    source_frame: int | None
    source_time: float | None
    origin: str
    review_status: str
    rejected: bool
    needs_review: bool
    original_time: float | None
    requested_time: float | None
    reviewer: str | None
    reason: str | None
    track_refs: tuple[str, ...]
    track_states: tuple[str, ...]
    order_unresolved_with: tuple[str, ...]
    event_type: str = ""


@dataclass(frozen=True)
class TimelineGroup:
    rows: tuple[TimelineRow, ...]
    order_unresolved: bool


def timeline_groups(
    decision: ImportDecision | None, source_view: str, view_sha: str, app_moments: tuple[Any, ...]
) -> tuple[TimelineGroup, ...]:
    records = decision.records if decision else ()
    quarantined = decision.quarantined_records if decision else ()
    overlays = {
        (r.artifact_path, r.data.target_id): r
        for r in records
        if r.view_id == source_view and isinstance(r.data, ReviewFields) and r.data.target_kind == "event"
    }
    states: dict[str, set[str]] = {}
    for record in records:
        if record.view_id == source_view and isinstance(record.data, TrackFields):
            for ref in record.track_refs:
                states.setdefault(ref, set()).add(record.data.state)
    rows = []
    for record in records:
        if record.view_id != source_view or not isinstance(record.data, EventFields):
            continue
        event = record.data
        overlay = overlays.get((record.artifact_path, event.event_id))
        review = overlay.data if overlay is not None and isinstance(overlay.data, ReviewFields) else None
        modified = record.producer_status == "MODIFIED_BY_REVIEWER" or (
            review is not None and review.action == "modify"
        )
        if modified:
            citation = overlay if review is not None and review.action == "modify" else None
        else:
            citation = record
        rows.append(
            TimelineRow(
                record.record_id,
                event.event_id,
                EVENT_LABELS.get(event.event_type, event.event_type + " (reconstruction event)"),
                view_sha,
                citation.source_frame if citation else None,
                citation.source_time if citation else None,
                "Reconstruction proposal",
                REVIEW_LABELS.get(record.producer_status or "", "Review status unavailable"),
                record.producer_status == "REJECTED_BY_REVIEWER",
                record.needs_review
                or record.record_id in quarantined
                or (overlay is not None and (overlay.needs_review or overlay.record_id in quarantined))
                or (modified and citation is None),
                event.peak_time,
                review.modified_peak_time if review else None,
                review.reviewer if review else None,
                review.reason if review else None,
                record.track_refs,
                tuple(
                    sorted(
                        {
                            state
                            for ref in record.track_refs
                            for state in states.get(ref, set())
                            if state in {"lost", "interpolated", "recovered", "manual"}
                        }
                    )
                ),
                event.order_unresolved_with or (),
                event.event_type,
            )
        )
    for index, moment in enumerate(app_moments):
        if moment.view_id == view_sha:
            rows.append(
                TimelineRow(
                    f"app-{index}",
                    "",
                    moment.text,
                    view_sha,
                    None,
                    moment.timestamp_seconds,
                    "App cited moment",
                    "",
                    False,
                    False,
                    None,
                    None,
                    None,
                    None,
                    (),
                    (),
                    (),
                )
            )
    rows.sort(key=lambda row: (row.source_time is None, row.source_time if row.source_time is not None else 0, row.key))
    # Components are local to this producer view; camera clocks are never merged here.
    by_id = {row.event_id: row.key for row in rows if row.event_id}
    adjacency: dict[str, set[str]] = {row.key: set() for row in rows}
    for row in rows:
        for other_id in row.order_unresolved_with:
            other = by_id.get(other_id)
            if other is not None:
                adjacency[row.key].add(other)
                adjacency[other].add(row.key)
    pending = {row.key: row for row in rows}
    groups = []
    for row in rows:
        if row.key not in pending:
            continue
        todo = [row.key]
        keys = set()
        while todo:
            key = todo.pop()
            if key in keys:
                continue
            keys.add(key)
            todo.extend(adjacency[key] - keys)
        component = tuple(candidate for candidate in rows if candidate.key in keys)
        for key in keys:
            pending.pop(key, None)
        groups.append(TimelineGroup(component, len(component) > 1 or any(r.order_unresolved_with for r in component)))
    return tuple(groups)
