"""Heading, visible stopping, and turning from displacement tests against position uncertainty.

No rate of travel is computed. A road user is "stopped" when its contact point shows no
displacement beyond its own position uncertainty for a minimum duration.
"""

from dataclasses import dataclass
import math

import numpy as np
from numpy.typing import NDArray

from minivision_reconstruct.domain.models import CalibrationDocument
from minivision_reconstruct.movement.models import (
    DistanceUnit, EventConfidence, EventType, MotionConfig, MovementError, MovementEvent, MovementState, SmoothedPoint,
)
from minivision_reconstruct.movement.uncertainty import world_units_per_pixel


@dataclass(frozen=True)
class SegmentMotion:
    """Per-frame heading and state for one segment plus its proposed events."""

    headings: dict[int, tuple[float, float] | None]
    states: dict[int, MovementState]
    events: tuple[MovementEvent, ...]


@dataclass(frozen=True)
class MotionContext:
    case_id: str
    frame_period: float
    unit: DistanceUnit
    right_handed: bool
    created_by: str


def segment_confidence(points: tuple[SmoothedPoint, ...]) -> EventConfidence:
    """Lower confidence when any supporting point is reconstructed or the segment is unsmoothed."""
    if any(not point.smoothed for point in points):
        return "low"
    if any(point.state == "interpolated" or point.provenance != "automatic" for point in points):
        return "medium"
    return "high"


def make_event(
    context: MotionContext, event_type: EventType, track_ids: tuple[int, ...], points: tuple[SmoothedPoint, ...],
    start: int, peak: int, end: int, window: tuple[float, float], signals: tuple[str, ...],
    provenance: str, from_feature: str | None, to_feature: str | None,
    location: tuple[float, float] | None, location_uncertainty: float | None, support: tuple[SmoothedPoint, ...],
) -> MovementEvent:
    """Build one proposed event from segment indices; times come from source frames."""
    peak_time = points[peak].source_time
    lower, upper = min(window[0], peak_time - context.frame_period / 2), max(window[1], peak_time + context.frame_period / 2)
    track_text = "-".join(str(item) for item in track_ids)
    return MovementEvent(
        event_id=f"{event_type}-t{track_text}-f{points[peak].source_frame}", case_id=context.case_id, event_type=event_type,
        track_ids=track_ids, start_frame=points[start].source_frame, peak_frame=points[peak].source_frame, end_frame=points[end].source_frame,
        start_time=points[start].source_time, peak_time=peak_time, end_time=points[end].source_time,
        time_window=(max(0.0, lower), upper), location=location, location_uncertainty=location_uncertainty, unit=context.unit,
        from_feature=from_feature, to_feature=to_feature, evidence_signals=signals, confidence=segment_confidence(support),
        provenance="calculated" if provenance == "calculated" else "inferred", status="PROPOSED_BY_SYSTEM", created_by=context.created_by,
    )


def analyze_segment(points: tuple[SmoothedPoint, ...], calibration: CalibrationDocument, config: MotionConfig, context: MotionContext) -> SegmentMotion:
    """Derive heading, stationary intervals, and turns for one contiguous segment."""
    count = len(points)
    positions: NDArray[np.float64] = np.asarray([point.smoothed_position for point in points], dtype=np.float64)
    relative = np.asarray([point.relative_uncertainty for point in points], dtype=np.float64)
    floors = np.asarray([config.minimum_stationary_extent_pixels * world_units_per_pixel(calibration, point.footpoint_pixel) for point in points if point.footpoint_pixel is not None], dtype=np.float64)
    if len(floors) != count:
        raise MovementError(f"Track {points[0].track_id} has an available point without its image contact point.")
    thresholds = np.maximum(config.stationary_extent_factor * relative, floors)
    stopped_runs = _stopped_runs(positions, thresholds, config, context.frame_period)
    unresolved = max(1, round(config.stationary_window_seconds / context.frame_period / 2))
    headings = _headings(positions, relative, config)
    states: dict[int, MovementState] = {}
    events: list[MovementEvent] = []
    stopped = np.zeros(count, dtype=bool)
    for first, last in stopped_runs:
        anchor = np.median(positions[first:last + 1], axis=0)
        limit = float(np.median(thresholds[first:last + 1]))
        distance = np.hypot(*(positions - anchor).T)
        boundary = limit * config.rest_boundary_fraction
        stop_index = _scan(distance, first, -1, boundary)
        start_index = _scan(distance, last, 1, boundary)
        stopped[stop_index:start_index + 1] = True
        location = (float(anchor[0]), float(anchor[1]))
        uncertainty = float(np.median([point.position_uncertainty for point in points[stop_index:start_index + 1]]))
        support = points[max(0, stop_index - 1):min(count, start_index + 2)]
        signals = (
            f"contact point stayed within its position uncertainty for {points[start_index].source_time - points[stop_index].source_time:.1f} s",
            "creeping below the displacement resolution cannot be excluded within the reported time window",
            "no rate of travel was calculated",
        )
        if stop_index > 0:
            outer = _scan(distance, first, -1, limit)
            events.append(make_event(context, "stop", (points[0].track_id,), points, stop_index, stop_index, start_index,
                                     (points[outer].source_time, points[min(stop_index + unresolved, start_index)].source_time), signals, "inferred", None, None, location, uncertainty, support))
        if start_index < count - 1:
            outer = _scan(distance, last, 1, limit)
            events.append(make_event(context, "start", (points[0].track_id,), points, stop_index, start_index, start_index,
                                     (points[max(start_index - unresolved, stop_index)].source_time, points[outer].source_time), signals, "inferred", None, None, location, uncertainty, support))
    turning = np.zeros(count, dtype=bool)
    for event, first, last in _turns(points, headings, config, context, stopped):
        events.append(event)
        turning[first:last + 1] = True
    heading_map: dict[int, tuple[float, float] | None] = {}
    for index, point in enumerate(points):
        heading = None if stopped[index] else headings[index]
        heading_map[point.source_frame] = None if heading is None else (_wrap(heading[0]), heading[1])
        states[point.source_frame] = "stopped" if stopped[index] else "turning" if turning[index] else "moving" if heading is not None else "unknown"
    return SegmentMotion(heading_map, states, tuple(events))


def _stopped_runs(positions: NDArray[np.float64], thresholds: NDArray[np.float64], config: MotionConfig, frame_period: float) -> tuple[tuple[int, int], ...]:
    count = len(positions)
    half = max(1, round(config.stationary_window_seconds / frame_period / 2))
    minimum = max(1, math.ceil(config.minimum_stop_seconds / frame_period))
    stationary = np.zeros(count, dtype=bool)
    if count < 2 * half + 1:
        return ()
    for index in range(half, count - half):
        window = positions[index - half:index + half + 1]
        extent = float(np.hypot(*(window - window.mean(axis=0)).T).max())
        stationary[index] = extent <= float(thresholds[index - half:index + half + 1].mean())
    runs: list[tuple[int, int]] = []
    index = 0
    while index < count:
        if not stationary[index]:
            index += 1
            continue
        end = index
        while end + 1 < count and stationary[end + 1]:
            end += 1
        first, last = index, end
        if first == half:
            first = 0
        if last == count - half - 1:
            last = count - 1
        if (last - first + 1) >= minimum:
            runs.append((first, last))
        index = end + 1
    return tuple(runs)


def _scan(distance: NDArray[np.float64], origin: int, step: int, limit: float) -> int:
    """Move outward from a stationary run while the contact point remains within the limit of rest."""
    index = origin
    while 0 <= index + step < len(distance) and distance[index + step] <= limit:
        index += step
    return index


def _headings(positions: NDArray[np.float64], relative: NDArray[np.float64], config: MotionConfig) -> tuple[tuple[float, float] | None, ...]:
    """Direction of centred displacement, unavailable when displacement is within uncertainty."""
    count = len(positions)
    half = config.heading_half_window_frames
    result: list[tuple[float, float] | None] = []
    for index in range(count):
        first, last = max(0, index - half), min(count - 1, index + half)
        delta = positions[last] - positions[first]
        length = float(np.hypot(*delta))
        noise = math.sqrt(2) * float(max(relative[first], relative[last]))
        if last == first or length < config.heading_displacement_factor * noise:
            result.append(None)
            continue
        result.append((math.degrees(math.atan2(float(delta[1]), float(delta[0]))), math.degrees(math.atan2(noise, length))))
    return tuple(result)


def _turns(
    points: tuple[SmoothedPoint, ...], headings: tuple[tuple[float, float] | None, ...], config: MotionConfig,
    context: MotionContext, stopped: NDArray[np.bool_],
) -> tuple[tuple[MovementEvent, int, int], ...]:
    results: list[tuple[MovementEvent, int, int]] = []
    half, reference = config.turn_half_window_frames, config.turn_reference_frames
    valid = [index for index, heading in enumerate(headings) if heading is not None and not stopped[index]]
    runs: list[list[int]] = []
    for index in valid:
        if runs and index - runs[-1][-1] <= half:
            runs[-1].append(index)
        else:
            runs.append([index])
    for run in runs:
        if len(run) < 2 * half + 1:
            continue
        indices = np.asarray(run)
        unwrapped = np.degrees(np.unwrap(np.radians([headings[index][0] for index in run])))
        uncertainty = np.asarray([headings[index][1] for index in run])
        changing = np.zeros(len(run), dtype=bool)
        for offset in range(half, len(run) - half):
            changing[offset] = abs(unwrapped[offset + half] - unwrapped[offset - half]) >= config.turn_change_threshold_degrees
        offset = 0
        while offset < len(run):
            if not changing[offset]:
                offset += 1
                continue
            end = offset
            while end + 1 < len(run) and (changing[end + 1] or (end + half < len(run) and changing[end + 1:end + half + 1].any())):
                end += 1
            lower, upper = max(0, offset - half), min(len(run) - 1, end + half)
            before = unwrapped[max(0, lower - reference):lower + 1]
            after = unwrapped[upper:min(len(run), upper + reference + 1)]
            initial, final = float(np.median(before)), float(np.median(after))
            net = final - initial
            if abs(net) >= config.turn_minimum_degrees:
                span = np.arange(lower, upper + 1)
                progress = np.abs(unwrapped[span] - initial) / abs(net)
                remaining = np.abs(unwrapped[span] - final) / abs(net)
                start = int(span[np.argmax(progress >= config.turn_boundary_fraction)])
                finish = int(span[len(span) - 1 - np.argmax(remaining[::-1] >= config.turn_boundary_fraction)])
                peak = int(span[np.argmax(progress >= 0.5)])
                margin = float(uncertainty[peak]) / abs(net)
                early = int(span[np.argmax(progress >= max(0.0, 0.5 - margin))])
                late_candidates = np.nonzero(progress >= min(1.0, 0.5 + margin))[0]
                late = int(span[late_candidates[0]]) if len(late_candidates) else finish
                left = (net > 0) == context.right_handed
                first, peak_index, last = int(indices[start]), int(indices[peak]), int(indices[finish])
                window = (points[int(indices[early])].source_time, points[int(indices[late])].source_time)
                signals = (
                    f"heading changed by {abs(net):.0f}° (each heading ±{float(np.median(uncertainty)):.0f}°)",
                    f"direction convention: {'right' if context.right_handed else 'mirrored'}-handed road-plane coordinates",
                )
                support = points[first:last + 1]
                location = points[peak_index].smoothed_position
                event = make_event(context, "turn_left" if left else "turn_right", (points[0].track_id,), points, first, peak_index, last,
                                   window, signals, "inferred", None, None, location, points[peak_index].position_uncertainty, support)
                results.append((event, first, last))
            offset = end + 1
    return tuple(results)


def _wrap(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0
