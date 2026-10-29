"""Pairwise road-contact separation with combined uncertainty and closest-approach proposals."""

from collections import defaultdict
from itertools import combinations
import math

from minivision_reconstruct.movement.models import MovementEvent, PairSeparation, SeparationConfig, SmoothedPoint
from minivision_reconstruct.movement.motion import MotionContext
from minivision_reconstruct.movement.precision import display_value


def pair_separations(
    points: tuple[SmoothedPoint, ...], config: SeparationConfig, context: MotionContext, physical: bool,
) -> tuple[tuple[PairSeparation, ...], tuple[MovementEvent, ...]]:
    """Sampled separations for co-visible pairs plus an approach-then-recede minimum per pair."""
    by_track: dict[int, dict[int, SmoothedPoint]] = defaultdict(dict)
    for point in points:
        if point.smoothed_position is not None:
            by_track[point.track_id][point.source_frame] = point
    records: list[PairSeparation] = []
    events: list[MovementEvent] = []
    for first, second in combinations(sorted(by_track), 2):
        frames = sorted(by_track[first].keys() & by_track[second].keys())
        if len(frames) < config.minimum_overlap_frames:
            continue
        series: list[PairSeparation] = []
        for frame in frames:
            a, b = by_track[first][frame], by_track[second][frame]
            series.append(PairSeparation(
                track_a=first, track_b=second, source_frame=frame, source_time=a.source_time,
                separation=math.dist(a.smoothed_position, b.smoothed_position),
                uncertainty=math.hypot(a.position_uncertainty, b.position_uncertainty), unit=context.unit, physical_distance=physical,
            ))
        minimum = min(range(len(series)), key=lambda index: series[index].separation)
        records.extend(item for index, item in enumerate(series) if index % config.sample_every_frames == 0 or index == minimum)
        closest = series[minimum]
        ceiling = closest.separation + closest.uncertainty
        if series[0].separation <= ceiling or series[-1].separation <= ceiling:
            continue
        lower, upper = minimum, minimum
        while lower - 1 >= 0 and series[lower - 1].separation <= ceiling and series[lower - 1].source_frame == series[lower].source_frame - 1:
            lower -= 1
        while upper + 1 < len(series) and series[upper + 1].separation <= ceiling and series[upper + 1].source_frame == series[upper].source_frame + 1:
            upper += 1
        a, b = by_track[first][closest.source_frame], by_track[second][closest.source_frame]
        location = ((a.smoothed_position[0] + b.smoothed_position[0]) / 2, (a.smoothed_position[1] + b.smoothed_position[1]) / 2)
        support = tuple(by_track[track][series[index].source_frame] for track in (first, second) for index in range(lower, upper + 1))
        confidence = "low" if any(not item.smoothed for item in support) else "medium" if any(item.state == "interpolated" or item.provenance != "automatic" for item in support) else "high"
        unit_text = "m" if physical else "relative units"
        events.append(MovementEvent(
            event_id=f"closest_approach-t{first}-{second}-f{closest.source_frame}", case_id=context.case_id, event_type="closest_approach",
            track_ids=(first, second), start_frame=series[lower].source_frame, peak_frame=closest.source_frame, end_frame=series[upper].source_frame,
            start_time=series[lower].source_time, peak_time=closest.source_time, end_time=series[upper].source_time,
            time_window=(series[lower].source_time, series[upper].source_time), location=location, location_uncertainty=closest.uncertainty,
            unit=context.unit, from_feature=None, to_feature=None,
            evidence_signals=(
                f"minimum contact-point separation {display_value(closest.separation, closest.uncertainty, unit_text)}; not a vehicle-body gap",
                "separation decreased before and increased after the minimum by more than its uncertainty",
            ),
            confidence=confidence, provenance="calculated", status="PROPOSED_BY_SYSTEM", created_by=context.created_by,
        ))
    return tuple(records), tuple(events)
