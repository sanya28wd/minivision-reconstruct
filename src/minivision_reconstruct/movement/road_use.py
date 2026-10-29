"""Lane membership, lane transitions, stop-line crossings, and zone entry/exit on calibrated geometry.

Lanes come from the same calibration as the projected contact points, so lane membership uses
relative (contact-point) uncertainty. A point whose uncertainty disc touches several lanes stays
ambiguous instead of being forced into one.
"""

from collections.abc import Callable
from dataclasses import dataclass
import math

import numpy as np
from shapely.geometry import LineString, Point as ShapelyPoint, Polygon

from minivision_reconstruct.domain.models import CalibrationDocument
from minivision_reconstruct.movement.models import MovementError, MovementEvent, RoadUseConfig, SmoothedPoint
from minivision_reconstruct.movement.motion import MotionContext, make_event
from minivision_reconstruct.movement.uncertainty import unit_factor


@dataclass(frozen=True)
class RoadGeometry:
    lanes: dict[str, Polygon]
    zones: dict[str, Polygon]
    stop_lines: dict[str, LineString]


@dataclass(frozen=True)
class LaneUse:
    assignment: dict[int, str | None]
    candidates: dict[int, tuple[str, ...]]
    sequence: tuple[str, ...]
    events: tuple[MovementEvent, ...]


def road_geometry(calibration: CalibrationDocument) -> RoadGeometry:
    """Convert calibrated road features into world-unit shapes."""
    factor = unit_factor(calibration)
    lanes: dict[str, Polygon] = {}
    zones: dict[str, Polygon] = {}
    stop_lines: dict[str, LineString] = {}
    for feature in calibration.features:
        points = tuple((x * factor, y * factor) for x, y in feature.world_points)
        if feature.kind == "stop_line":
            stop_lines[feature.feature_id] = LineString(points)
            continue
        polygon = Polygon(points)
        if not polygon.is_valid or polygon.area <= 0:
            raise MovementError(f"Road feature '{feature.feature_id}' does not form a valid road-plane polygon.")
        (lanes if feature.kind == "lane" else zones)[feature.feature_id] = polygon
    return RoadGeometry(lanes, zones, stop_lines)


def lane_axis_degrees(polygon: Polygon) -> float:
    """Orientation of the polygon's longer minimum-rectangle side, modulo 180°."""
    corners = np.asarray(polygon.minimum_rotated_rectangle.exterior.coords)[:4]
    edges = np.diff(np.vstack([corners, corners[:1]]), axis=0)
    longest = edges[int(np.argmax(np.hypot(edges[:, 0], edges[:, 1])))]
    return math.degrees(math.atan2(float(longest[1]), float(longest[0]))) % 180.0


def parallel_adjacent(first: Polygon, second: Polygon, tolerance: float) -> bool:
    """True for side-by-side lanes: similar direction and negligible overlap."""
    difference = abs(lane_axis_degrees(first) - lane_axis_degrees(second))
    difference = min(difference, 180.0 - difference)
    overlap = first.intersection(second).area / min(first.area, second.area)
    return difference <= tolerance and overlap < 0.01


def analyze_lane_use(points: tuple[SmoothedPoint, ...], geometry: RoadGeometry, config: RoadUseConfig, context: MotionContext) -> LaneUse:
    """Assign lanes per point and propose persistent lane transitions within one segment."""
    assignment: dict[int, str | None] = {}
    candidates: dict[int, tuple[str, ...]] = {}
    labels: list[str | None] = []
    for point in points:
        location = ShapelyPoint(point.smoothed_position)
        nearby = tuple(sorted(lane for lane, polygon in geometry.lanes.items() if polygon.distance(location) <= point.relative_uncertainty))
        candidates[point.source_frame] = nearby
        label = nearby[0] if len(nearby) == 1 and geometry.lanes[nearby[0]].contains(location) else None
        assignment[point.source_frame] = label
        labels.append(label)
    runs = _persistent_runs(labels, config.lane_persistence_frames)
    events: list[MovementEvent] = []
    for (previous, _, previous_end), (current, current_start, _) in zip(runs, runs[1:]):
        if previous == current:
            continue
        before, after = geometry.lanes[previous], geometry.lanes[current]
        change = parallel_adjacent(before, after, config.parallel_lane_tolerance_degrees)
        transition = range(previous_end + 1, current_start + 1)
        peak = next((index for index in transition if after.contains(ShapelyPoint(points[index].smoothed_position)) and not before.contains(ShapelyPoint(points[index].smoothed_position))), current_start)
        signals = (
            f"contact point moved from {previous} to {current} and remained for at least {config.lane_persistence_frames} frames",
            "adjacent parallel lanes" if change else "lanes are not adjacent and parallel; recorded as lane entry",
        )
        events.append(make_event(
            context, "lane_change" if change else "lane_entry", (points[0].track_id,), points, previous_end, peak, current_start,
            (points[previous_end].source_time, points[current_start].source_time), signals, "calculated", previous, current,
            points[peak].smoothed_position, points[peak].position_uncertainty, points[previous_end:current_start + 1],
        ))
    sequence: list[str] = []
    for label, _, _ in runs:
        if not sequence or sequence[-1] != label:
            sequence.append(label)
    return LaneUse(assignment, candidates, tuple(sequence), tuple(events))


def stop_line_crossings(points: tuple[SmoothedPoint, ...], geometry: RoadGeometry, context: MotionContext) -> tuple[MovementEvent, ...]:
    """Crossings between consecutive frames of one segment; repeated crossings within uncertainty merge."""
    events: list[MovementEvent] = []
    for feature_id, line in sorted(geometry.stop_lines.items()):
        def distance(point: SmoothedPoint) -> float:
            return float(line.distance(ShapelyPoint(point.smoothed_position)))
        groups: list[list[int]] = []
        for index in range(len(points) - 1):
            step = LineString((points[index].smoothed_position, points[index + 1].smoothed_position))
            if step.length == 0 or not step.intersects(line):
                continue
            if groups and all(distance(points[other]) <= points[other].relative_uncertainty for other in range(groups[-1][-1], index + 2)):
                groups[-1].append(index)
            else:
                groups.append([index])
        for group in groups:
            peak = group[len(group) // 2]
            lower, upper = _uncertainty_window(points, distance, peak, peak + 1)
            signals = (
                f"contact-point path crossed {feature_id} between frames {points[peak].source_frame} and {points[peak + 1].source_frame}",
                f"{len(group)} crossings inside position uncertainty were merged" if len(group) > 1 else "single crossing",
            )
            events.append(make_event(
                context, "stop_line_crossing", (points[0].track_id,), points, peak, peak, peak + 1,
                (points[lower].source_time, points[upper].source_time), signals, "calculated", feature_id, None,
                points[peak].smoothed_position, points[peak].position_uncertainty, points[lower:upper + 1],
            ))
    return tuple(events)


def zone_transitions(points: tuple[SmoothedPoint, ...], geometry: RoadGeometry, config: RoadUseConfig, context: MotionContext) -> tuple[MovementEvent, ...]:
    """Entry and exit of crossings or monitored zones that persist beyond the configured frames."""
    events: list[MovementEvent] = []
    for zone_id, polygon in sorted(geometry.zones.items()):
        def distance(point: SmoothedPoint) -> float:
            return float(polygon.exterior.distance(ShapelyPoint(point.smoothed_position)))
        inside = [zone_id if polygon.contains(ShapelyPoint(point.smoothed_position)) else None for point in points]
        for label, first, last in _persistent_runs(inside, config.lane_persistence_frames):
            if label is None:
                continue
            for event_type, index in (("zone_entry", first), ("zone_exit", last + 1)):
                if index <= 0 or index >= len(points):
                    continue
                lower, upper = _uncertainty_window(points, distance, index - 1, index)
                verb = "entered" if event_type == "zone_entry" else "left"
                events.append(make_event(
                    context, event_type, (points[0].track_id,), points, index, index, index,
                    (points[lower].source_time, points[upper].source_time),
                    (f"contact point {verb} {zone_id} and the state persisted for at least {config.lane_persistence_frames} frames",), "calculated",
                    zone_id if event_type == "zone_exit" else None, zone_id if event_type == "zone_entry" else None,
                    points[index].smoothed_position, points[index].position_uncertainty, points[lower:upper + 1],
                ))
    return tuple(events)


def _persistent_runs(labels: list[str | None], persistence: int) -> tuple[tuple[str, int, int], ...]:
    """Runs of one non-empty label lasting at least the persistence threshold."""
    runs: list[tuple[str, int, int]] = []
    index = 0
    while index < len(labels):
        label = labels[index]
        end = index
        while end + 1 < len(labels) and labels[end + 1] == label:
            end += 1
        if label is not None and end - index + 1 >= persistence:
            runs.append((label, index, end))
        index = end + 1
    return tuple(runs)


def _uncertainty_window(points: tuple[SmoothedPoint, ...], distance: Callable[[SmoothedPoint], float], first: int, last: int) -> tuple[int, int]:
    """Widen [first, last] while neighbouring contact points remain within uncertainty of the boundary."""
    while first - 1 >= 0 and distance(points[first - 1]) <= points[first - 1].relative_uncertainty:
        first -= 1
    while last + 1 < len(points) and distance(points[last + 1]) <= points[last + 1].relative_uncertainty:
        last += 1
    return first, last
