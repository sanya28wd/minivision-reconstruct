"""Post-encroachment time at crossings of smoothed road-plane paths, from time and position only.

PET is the time between the first road user leaving a conflict area and the second entering it. No rate of
travel is computed. Paths are intersected one contiguous segment at a time, so a crossing is never inferred
inside an unavailable (lost) gap.

Frame convention: a road user "enters" at the first frame it is observed inside the conflict area and "exits"
at the first frame it is observed outside again. Both times are therefore late by less than one frame, so the
error on their difference stays below one frame period.
"""

from collections import defaultdict
from itertools import combinations
import math

import numpy as np
import shapely
from shapely.geometry import LineString, Point as ShapelyPoint

from minivision_reconstruct.incident.models import ConflictConfig, ConflictRecord, IncidentError
from minivision_reconstruct.movement.models import SmoothedPoint
from minivision_reconstruct.movement.motion import MotionContext, segment_confidence
from minivision_reconstruct.movement.precision import display_time, display_value


Segment = tuple[SmoothedPoint, ...]
Passage = tuple[int, int, bool, bool]  # enter index, exit index, entry censored, exit censored


def track_segments(points: tuple[SmoothedPoint, ...]) -> dict[int, tuple[Segment, ...]]:
    """Group available smoothed points into runs of consecutive frames within one smoothing segment."""
    by_track: dict[int, list[SmoothedPoint]] = defaultdict(list)
    for point in points:
        if point.smoothed_position is None:
            continue
        if point.position_uncertainty is None:
            raise IncidentError(f"Track {point.track_id}, frame {point.source_frame} has a position without uncertainty.")
        by_track[point.track_id].append(point)
    result: dict[int, tuple[Segment, ...]] = {}
    for track_id, items in sorted(by_track.items()):
        segments: list[list[SmoothedPoint]] = []
        previous: SmoothedPoint | None = None
        for point in sorted(items, key=lambda item: item.source_frame):
            if previous is None or point.source_frame != previous.source_frame + 1 or point.segment_id != previous.segment_id:
                segments.append([])
            segments[-1].append(point)
            previous = point
        result[track_id] = tuple(tuple(segment) for segment in segments if len(segment) >= 2)
    return result


def _path(segment: Segment) -> LineString:
    return LineString([point.smoothed_position for point in segment])


def _crossings(first: Segment, second: Segment) -> list[tuple[float, float]]:
    """Points where two segment paths meet, ordered along the first path; a shared stretch counts once, at its midpoint."""
    path = _path(first)
    shared = path.intersection(_path(second))
    if shared.is_empty:
        return []
    parts = shapely.get_parts(shared)
    stretches = [part for part in parts if part.geom_type == "LineString"]
    found = [(float(part.x), float(part.y)) for part in parts if part.geom_type == "Point"]
    if stretches:
        merged = shapely.line_merge(shapely.multilinestrings(stretches))
        found += [(float(mid.x), float(mid.y)) for mid in (line.interpolate(0.5, normalized=True) for line in shapely.get_parts(merged))]
    return sorted(found, key=lambda item: path.project(ShapelyPoint(item)))


def _index_after(segment: Segment, crossing: tuple[float, float]) -> int:
    """Index of the first sample at or after the crossing along the path."""
    xy = np.asarray([point.smoothed_position for point in segment], dtype=np.float64)
    arc = np.concatenate(([0.0], np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))))
    along = _path(segment).project(ShapelyPoint(crossing))
    return min(int(np.searchsorted(arc, along - 1e-9)), len(segment) - 1)


def _passage(segment: Segment, after: int, centre: tuple[float, float], radius: float) -> Passage:
    """The visit to the disc that contains the crossing; a pass between two samples enters and exits at `after`."""
    inside = [math.dist(point.smoothed_position, centre) <= radius for point in segment]
    seeds = [index for index in (after - 1, after) if index >= 0 and inside[index]]
    if not seeds:
        return after, after, False, False
    enter = seeds[0]
    while enter > 0 and inside[enter - 1]:
        enter -= 1
    last = seeds[-1]
    while last + 1 < len(segment) and inside[last + 1]:
        last += 1
    exit_censored = last == len(segment) - 1
    return enter, last if exit_censored else last + 1, enter == 0, exit_censored


def _conflict(
    segments: tuple[Segment, Segment], crossing: tuple[float, float], config: ConflictConfig, context: MotionContext,
) -> ConflictRecord | None:
    afters = tuple(_index_after(segment, crossing) for segment in segments)
    spreads = tuple(segment[index].position_uncertainty for segment, index in zip(segments, afters, strict=True))
    combined = math.hypot(*spreads)
    radius = max(config.conflict_radius_floor, config.uncertainty_coverage_factor * combined)

    def passages(offset: float) -> tuple[Passage, Passage]:
        return tuple(_passage(segment, after, crossing, max(radius + offset * spread, 0.0))  # type: ignore[return-value]
                     for segment, after, spread in zip(segments, afters, spreads, strict=True))

    def times(found: tuple[Passage, Passage], slot: int) -> tuple[float, float]:
        return segments[slot][found[slot][0]].source_time, segments[slot][found[slot][1]].source_time

    nominal = passages(0.0)
    first, second = sorted((0, 1), key=lambda slot: (*times(nominal, slot), segments[slot][0].track_id))

    def pet(found: tuple[Passage, Passage]) -> float:
        return times(found, second)[0] - times(found, first)[1]

    value = pet(nominal)
    if value > config.maximum_pet_seconds:
        return None
    # The disc boundary is only known to within each track's position uncertainty: a wider disc gives the
    # earliest entry and latest exit (smallest PET), a narrower one the opposite. Each time adds half a frame.
    window = (min(pet(passages(1.0)), value) - context.frame_period, max(pet(passages(-1.0)), value) + context.frame_period)
    (first_seg, second_seg), (first_pass, second_pass) = (segments[first], segments[second]), (nominal[first], nominal[second])
    track_first, track_second = first_seg[0].track_id, second_seg[0].track_id
    unit_text = "m" if context.unit == "m" else "relative units"
    signals = [
        f"smoothed paths of tracks {track_first} and {track_second} cross at "
        f"({display_value(crossing[0], combined, '')}, {display_value(crossing[1], combined, '')}) {unit_text}, "
        "inside segments with no unavailable frames",
        f"conflict area: disc of radius {radius:.2f} {unit_text} = max(floor {config.conflict_radius_floor}, "
        f"{config.uncertainty_coverage_factor} × combined position uncertainty {combined:.2f})",
        f"track {track_first} inside from frame {first_seg[first_pass[0]].source_frame}, outside again at frame "
        f"{first_seg[first_pass[1]].source_frame}; track {track_second} inside from frame {second_seg[second_pass[0]].source_frame}",
        f"PET {display_time(value, window, context.frame_period)} from source-frame times; no rate of travel used",
    ]
    if value <= 0:
        signals.append("both road users were inside the conflict area at the same time (PET ≤ 0)")
    censored = [f"track {segment[0].track_id} {edge}" for segment, found in ((first_seg, first_pass), (second_seg, second_pass))
                for edge, flag in (("entry", found[2]), ("exit", found[3])) if flag]
    if censored:
        signals.append(f"{', '.join(censored)} not observed: the segment begins or ends inside the conflict area")
    support = first_seg[first_pass[0]:first_pass[1] + 1] + second_seg[second_pass[0]:second_pass[1] + 1]
    return ConflictRecord(
        conflict_id=f"conflict-t{track_first}-{track_second}-f{second_seg[second_pass[0]].source_frame}", case_id=context.case_id,
        track_first=track_first, track_second=track_second, location=crossing, radius=radius, unit=context.unit,
        first_enter_frame=first_seg[first_pass[0]].source_frame, first_exit_frame=first_seg[first_pass[1]].source_frame,
        second_enter_frame=second_seg[second_pass[0]].source_frame, second_exit_frame=second_seg[second_pass[1]].source_frame,
        first_exit_time=times(nominal, first)[1], second_enter_time=times(nominal, second)[0],
        pet_seconds=value, pet_window=window, simultaneous_occupancy=value <= 0, evidence_signals=tuple(signals),
        confidence="low" if censored else segment_confidence(support),
    )


def post_encroachment(points: tuple[SmoothedPoint, ...], config: ConflictConfig, context: MotionContext) -> tuple[ConflictRecord, ...]:
    """One conflict record per distinct path crossing of each track pair, ordered by the second road user's entry."""
    segments = track_segments(points)
    records: list[ConflictRecord] = []
    for track_a, track_b in combinations(sorted(segments), 2):
        kept: list[ConflictRecord] = []
        for segment_a in segments[track_a]:
            for segment_b in segments[track_b]:
                for crossing in _crossings(segment_a, segment_b):
                    if any(math.dist(crossing, record.location) <= record.radius for record in kept):
                        continue
                    record = _conflict((segment_a, segment_b), crossing, config, context)
                    # Near-parallel jittery paths can cross twice around one passage; the second road user's entry identifies it.
                    if record is not None and all(record.conflict_id != other.conflict_id for other in kept):
                        kept.append(record)
        records.extend(kept)
    return tuple(sorted(records, key=lambda record: (record.second_enter_time, record.conflict_id)))
