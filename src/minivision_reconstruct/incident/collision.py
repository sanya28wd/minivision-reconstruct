"""Collision, near-miss, and single-vehicle candidates fused from proximity and consequence signals.

A two-party candidate needs a proximity signal: contact points within contact distance plus their combined
uncertainty (S1), or both road users inside one conflict area at once (S2, PET <= 0). It becomes a collision
candidate only with enough consequence signals for either participant soon after: a visible stop (S3), a heading
jump (S4), or a stop inside a lane long enough to obstruct it (S6). Image box overlap (S5) is recorded as
supporting evidence and never creates or upgrades a candidate on its own. Only time and position are used.

S1 is computed here from per-frame separations rather than read from Week 7 closest_approach events, because
after an impact both road users usually stay at rest side by side and the separation never rises again; a Week 7
closest_approach event is cited as supporting evidence when one exists.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
import math

from shapely.geometry import Point as ShapelyPoint, Polygon

from minivision_reconstruct.incident.models import (
    SIGNAL_KINDS, CollisionType, ConflictRecord, EvidenceSignal, IncidentConfig, IncidentError, IncidentEvent, PhaseBounds,
)
from minivision_reconstruct.movement.models import EventConfidence, MovementEvent, MovementObservation, SmoothedPoint
from minivision_reconstruct.movement.motion import MotionContext, segment_confidence
from minivision_reconstruct.movement.precision import display_value
from minivision_reconstruct.reconstruction.models import Observation


RANK: dict[EventConfidence, int] = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class IncidentInputs:
    smoothed: tuple[SmoothedPoint, ...]
    movement: tuple[MovementObservation, ...]
    events: tuple[MovementEvent, ...]
    conflicts: tuple[ConflictRecord, ...]
    observations: tuple[Observation, ...]
    lanes: dict[str, Polygon]


@dataclass
class _Index:
    """Per-track lookups over one movement run."""

    points: dict[int, dict[int, SmoothedPoint]] = field(default_factory=lambda: defaultdict(dict))
    unavailable: dict[int, set[int]] = field(default_factory=lambda: defaultdict(set))
    motion: dict[int, dict[int, MovementObservation]] = field(default_factory=lambda: defaultdict(dict))
    stops: dict[int, list[MovementEvent]] = field(default_factory=lambda: defaultdict(list))
    turns: dict[int, list[MovementEvent]] = field(default_factory=lambda: defaultdict(list))
    approaches: dict[tuple[int, int], list[MovementEvent]] = field(default_factory=lambda: defaultdict(list))
    boxes: dict[tuple[int, int], tuple[float, float, float, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Encounter:
    """One spell of proximity between two road users, before it is classified."""

    tracks: tuple[int, int]
    frames: tuple[int, ...]
    onset: int
    closest: int
    separation: float | None
    uncertainty: float | None
    conflict: ConflictRecord | None


def _index(inputs: IncidentInputs) -> _Index:
    index = _Index()
    for point in inputs.smoothed:
        if point.smoothed_position is None:
            index.unavailable[point.track_id].add(point.source_frame)
        else:
            index.points[point.track_id][point.source_frame] = point
    for item in inputs.movement:
        index.motion[item.track_id][item.source_frame] = item
    for event in inputs.events:
        if event.event_type == "stop":
            index.stops[event.track_ids[0]].append(event)
        elif event.event_type in {"turn_left", "turn_right"}:
            index.turns[event.track_ids[0]].append(event)
        elif event.event_type == "closest_approach":
            index.approaches[tuple(sorted(event.track_ids))].append(event)
    for observation in inputs.observations:
        if observation.bbox is not None:
            index.boxes[(observation.track_id, observation.source_frame)] = observation.bbox
    return index


def _encounters(index: _Index, conflicts: tuple[ConflictRecord, ...], config: IncidentConfig, merge_gap: int) -> list[_Encounter]:
    """S1 runs of frames within contact distance plus uncertainty, merged with PET <= 0 conflicts.

    A separation hovering at the threshold flickers in and out; runs less than `merge_gap` frames apart are one encounter.
    """
    found: list[_Encounter] = []
    for a, b in combinations(sorted(index.points), 2):
        frames = sorted(index.points[a].keys() & index.points[b].keys())
        runs: list[list[tuple[int, float, float]]] = []
        for frame in frames:
            first, second = index.points[a][frame], index.points[b][frame]
            separation = math.dist(first.smoothed_position, second.smoothed_position)
            uncertainty = math.hypot(first.position_uncertainty, second.position_uncertainty)
            if separation > config.collision.contact_distance + uncertainty:
                continue
            if not runs or frame - runs[-1][-1][0] > merge_gap:
                runs.append([])
            runs[-1].append((frame, separation, uncertainty))
        pair = [item for item in conflicts if {item.track_first, item.track_second} == {a, b}]
        for run in runs:
            closest = min(run, key=lambda item: item[1])
            # After contact the points drift slowly while both stop; the first frame indistinguishable from the
            # minimum is taken as the onset of contact.
            onset = next(item for item in run if item[1] <= closest[1] + closest[2])
            span = (run[0][0], run[-1][0])
            attached = [item for item in pair if span[0] <= item.second_enter_frame <= span[1] or span[0] <= item.first_exit_frame <= span[1]]
            found.append(_Encounter((a, b), tuple(item[0] for item in run), onset[0], closest[0], closest[1], closest[2],
                                    min(attached, key=lambda item: item.pet_seconds) if attached else None))
        spans = [(run[0][0], run[-1][0]) for run in runs]
        for conflict in pair:
            # A shared-area conflict that begins inside a proximity run is part of that encounter, attached or not.
            if conflict.simultaneous_occupancy and not any(item.conflict == conflict for item in found) \
                    and not any(start <= conflict.second_enter_frame <= end for start, end in spans):
                frames_inside = tuple(range(conflict.second_enter_frame, conflict.first_exit_frame + 1))
                found.append(_Encounter((a, b), frames_inside, conflict.second_enter_frame, conflict.second_enter_frame, None, None, conflict))
    return found


def _heading_jump(index: _Index, track: int, frames: range, config: IncidentConfig) -> tuple[int, float, float] | None:
    """First frame where the heading changes by at least the threshold over the configured frame span.

    Heading change inside a detected turn is a manoeuvre, not a jump: smoothed contact-point headings cannot tell
    post-impact rotation from an ordinary turn by angle alone.
    """
    span = config.collision.heading_jump_window_frames
    motion = index.motion[track]
    for frame in frames:
        if any(turn.start_frame <= frame + span and frame <= turn.end_frame for turn in index.turns[track]):
            continue
        before, after = motion.get(frame), motion.get(frame + span)
        if before is None or after is None or before.heading_degrees is None or after.heading_degrees is None:
            continue
        change = abs((after.heading_degrees - before.heading_degrees + 180.0) % 360.0 - 180.0)
        if change >= config.collision.heading_jump_degrees:
            spread = math.hypot(before.heading_uncertainty_degrees or 0.0, after.heading_uncertainty_degrees or 0.0)
            return frame, change, spread
    return None


def _obstruction(index: _Index, track: int, frames: range, lanes: dict[str, Polygon], minimum_frames: int) -> tuple[int, int, str] | None:
    """First visible stop that starts in the frame range and stays inside one lane for the minimum duration."""
    motion = index.motion[track]
    for start in frames:
        item = motion.get(start)
        if item is None or item.movement_state != "stopped" or (motion.get(start - 1) and motion[start - 1].movement_state == "stopped"):
            continue
        lane = next((lane_id for lane_id, polygon in sorted(lanes.items()) if item.world_x is not None and polygon.contains(ShapelyPoint(item.world_x, item.world_y))), None)
        if lane is None:
            continue
        end = start
        while (following := motion.get(end + 1)) is not None and following.movement_state == "stopped" and following.world_x is not None \
                and lanes[lane].contains(ShapelyPoint(following.world_x, following.world_y)):
            end += 1
        if end - start + 1 >= minimum_frames:
            return start, end, lane
    return None


def _consequences(index: _Index, tracks: tuple[int, ...], window: range, lanes: dict[str, Polygon], config: IncidentConfig,
                  context: MotionContext) -> list[EvidenceSignal]:
    """S3, S4, and S6 for any of the tracks, starting inside the frame window; one signal per kind."""
    signals: list[EvidenceSignal] = []
    stops = [event for track in tracks for event in index.stops[track] if event.peak_frame in window]
    if stops:
        signals.append(_signal("post_event_stop", tuple(sorted({event.track_ids[0] for event in stops})), tuple(event.peak_frame for event in stops),
                               tuple(event.event_id for event in stops),
                               "; ".join(f"track {event.track_ids[0]} comes to a visible stop at {event.peak_time:.2f} s" for event in stops)))
    jumps = {track: jump for track in tracks if (jump := _heading_jump(index, track, window, config)) is not None}
    if jumps:
        span = config.collision.heading_jump_window_frames
        signals.append(_signal("heading_jump", tuple(jumps), tuple(frame for start, _, _ in jumps.values() for frame in (start, start + span)), (),
                               "; ".join(f"track {track} heading changes {display_value(change, max(spread, 1.0), 'degrees')} (combined heading uncertainty) "
                                         f"between frames {start} and {start + span}" for track, (start, change, spread) in jumps.items())))
    minimum = max(1, round(config.collision.obstruction_seconds / context.frame_period))
    blocks = {track: block for track in tracks if (block := _obstruction(index, track, window, lanes, minimum)) is not None}
    if blocks:
        signals.append(_signal("post_event_obstruction", tuple(blocks), tuple(frame for start, end, _ in blocks.values() for frame in (start, end)), (),
                               "; ".join(f"track {track} stays stopped inside {lane} from frame {start} to {end}" for track, (start, end, lane) in blocks.items())))
    return signals


def _signal(name: str, tracks: tuple[int, ...], frames: tuple[int, ...], event_ids: tuple[str, ...], description: str) -> EvidenceSignal:
    return EvidenceSignal(name=name, kind=SIGNAL_KINDS[name], track_ids=tracks, source_frames=tuple(sorted(set(frames))),
                          supporting_event_ids=event_ids, description=description)


def _last_heading(index: _Index, track: int, frame: int) -> float | None:
    """Most recent known heading at or before the frame; a road user at rest keeps the heading it arrived with."""
    known = [item for source, item in index.motion[track].items() if source <= frame and item.heading_degrees is not None]
    return max(known, key=lambda item: item.source_frame).heading_degrees if known else None


def collision_type(index: _Index, tracks: tuple[int, int], frame: int, config: IncidentConfig) -> CollisionType:
    """Type from the relative heading at impact; same-direction contacts split by lateral offset of the contact points."""
    headings = [_last_heading(index, track, frame) for track in tracks]
    points = [index.points[track].get(frame) for track in tracks]
    if any(heading is None for heading in headings) or any(point is None for point in points):
        return "unknown"
    difference = abs(headings[0] - headings[1]) % 360.0
    relative = min(difference, 360.0 - difference)
    bands = config.collision_type
    if relative <= bands.same_direction_max_degrees:
        mean = math.radians(headings[0]) + math.radians(((headings[1] - headings[0] + 180.0) % 360.0 - 180.0) / 2)
        dx, dy = (points[1].smoothed_position[0] - points[0].smoothed_position[0], points[1].smoothed_position[1] - points[0].smoothed_position[1])
        lateral = abs(math.cos(mean) * dy - math.sin(mean) * dx)
        return "rear_end" if lateral <= bands.rear_end_max_lateral_offset else "sideswipe"
    if bands.t_bone_min_degrees <= relative <= bands.t_bone_max_degrees:
        return "t_bone"
    return "head_on" if relative >= bands.head_on_min_degrees else "unknown"


def _peak_window(index: _Index, tracks: tuple[int, int], peak: int, limit: int) -> tuple[int, int] | None:
    """Frames around the peak whose separation is indistinguishable from the peak separation, at most `limit` frames each way.

    Where either road user is unavailable next to the window, the window extends across the gap (up to the limit):
    the event may have happened while it was hidden.
    """
    def separation(frame: int) -> tuple[float, float] | None:
        first, second = index.points[tracks[0]].get(frame), index.points[tracks[1]].get(frame)
        if first is None or second is None:
            return None
        return math.dist(first.smoothed_position, second.smoothed_position), math.hypot(first.position_uncertainty, second.position_uncertainty)

    reference = separation(peak)
    if reference is None:
        return None
    bounds = []
    for step in (-1, 1):
        edge = peak
        while abs(edge + step - peak) <= limit:
            following = separation(edge + step)
            if following is None:
                gap = edge + step
                while abs(gap + step - peak) <= limit and separation(gap) is None and gap + step >= 0:
                    gap += step
                edge = gap
                break
            if abs(following[0] - reference[0]) > reference[1]:
                break
            edge += step
        bounds.append(max(edge, 0))
    return bounds[0], bounds[1]


def _boxes_overlap(index: _Index, tracks: tuple[int, ...], frame: int) -> bool:
    boxes = [index.boxes.get((track, frame)) for track in tracks]
    if any(box is None for box in boxes):
        return False
    (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) = boxes
    return ax1 < bx2 and bx1 < ax2 and ay1 < by2 and by1 < ay2


def _phases(index: _Index, tracks: tuple[int, ...], window: tuple[float, float]) -> PhaseBounds:
    times = [point.source_time for track in tracks for point in index.points[track].values()]
    return PhaseBounds(pre_event=(min(min(times), window[0]), window[0]), event=window, post_event=(window[1], max(max(times), window[1])))


def _quality(index: _Index, tracks: tuple[int, ...], frames: range) -> EventConfidence:
    """segment_confidence over the supporting points, and low whenever a participant is unavailable in the window."""
    if any(frame in index.unavailable[track] for track in tracks for frame in frames):
        return "low"
    support = tuple(index.points[track][frame] for track in tracks for frame in frames if frame in index.points[track])
    return segment_confidence(support) if support else "low"


def propose_incidents(inputs: IncidentInputs, config: IncidentConfig, context: MotionContext) -> tuple[IncidentEvent, ...]:
    """Classify every proximity encounter and look for single-vehicle candidates; ordered by peak time."""
    index = _index(inputs)
    post = max(1, round(config.collision.post_window_seconds / context.frame_period))
    unit_text = "m" if context.unit == "m" else "relative units"
    proposals: list[IncidentEvent] = []
    explained: dict[int, list[range]] = defaultdict(list)
    for encounter in _encounters(index, inputs.conflicts, config, post):
        tracks = encounter.tracks
        window_frames = range(encounter.frames[0], encounter.onset + post + 1)
        proximity: list[EvidenceSignal] = []
        if encounter.separation is not None:
            approach_ids = tuple(event.event_id for event in index.approaches[tracks] if encounter.frames[0] <= event.peak_frame <= encounter.frames[-1])
            proximity.append(_signal(
                "closest_approach", tracks, (encounter.onset, encounter.closest), approach_ids,
                f"minimum contact-point separation {display_value(encounter.separation, encounter.uncertainty, unit_text)} at frame {encounter.closest}, "
                f"within contact distance {config.collision.contact_distance} plus combined uncertainty; contact from frame {encounter.onset}; not a vehicle-body gap",
            ))
        if encounter.conflict is not None and encounter.conflict.simultaneous_occupancy:
            conflict = encounter.conflict
            proximity.append(_signal("simultaneous_occupancy", tracks, (conflict.second_enter_frame, conflict.first_exit_frame), (),
                                     f"both road users inside conflict area {conflict.conflict_id} at once (PET {conflict.pet_seconds:.2f} s <= 0)"))
        consequences = _consequences(index, tracks, window_frames, inputs.lanes, config, context)
        pet = encounter.conflict.pet_seconds if encounter.conflict is not None else None
        if len(consequences) >= config.collision.minimum_consequence_signals:
            event_type, peak = "collision_candidate", encounter.onset
        elif (pet is not None and 0 < pet <= config.near_miss.near_miss_pet_seconds) or \
                (encounter.separation is not None and encounter.separation < config.near_miss.near_miss_distance):
            event_type, peak = "near_miss_candidate", encounter.closest
        else:
            continue
        supporting = [_signal("image_box_overlap", tracks, (peak,), (), f"image boxes overlap at frame {peak}; supporting only, never sufficient")] \
            if _boxes_overlap(index, tracks, peak) else []
        strength: EventConfidence = "high" if event_type == "collision_candidate" and len(consequences) >= 2 else "medium"
        proposals.append(_event(
            context, event_type, tracks, encounter.frames[0], peak, encounter.frames[-1],
            _peak_window(index, tracks, peak, post) or (encounter.onset, encounter.closest),
            tuple(proximity + consequences + supporting), index, encounter.conflict,
            collision_type(index, tracks, peak, config) if event_type == "collision_candidate" else "unknown",
            # A participant lost just before the encounter means its true onset may be hidden, so look back one post window too.
            min(_quality(index, tracks, range(encounter.frames[0] - post, window_frames.stop)), strength, key=RANK.__getitem__),
        ))
        for track in tracks:
            explained[track].append(window_frames)
    for track in sorted(index.points):
        proposals.extend(_single_vehicle(index, track, explained[track], inputs.lanes, config, context, post))
    return tuple(sorted(proposals, key=lambda event: (event.peak_time, event.event_id)))


def _single_vehicle(index: _Index, track: int, explained: list[range], lanes: dict[str, Polygon], config: IncidentConfig,
                    context: MotionContext, post: int) -> list[IncidentEvent]:
    """A heading jump followed by a stop or obstruction with no other road user within the exclusion distance."""
    frames = sorted(index.points[track])
    proposals: list[IncidentEvent] = []
    cursor = frames[0] if frames else 0
    while frames and (jump := _heading_jump(index, track, range(cursor, frames[-1] + 1), config)) is not None:
        start = jump[0]
        cursor = start + post + 1
        if any(start in window for window in explained):
            continue
        consequences = _consequences(index, (track,), range(start, start + post + 1), lanes, config, context)
        if not any(signal.name in {"post_event_stop", "post_event_obstruction"} for signal in consequences):
            continue
        position = index.points[track][start].smoothed_position if start in index.points[track] else None
        others = [other for other in index.points if other != track and start in index.points[other]]
        if position is None or any(math.dist(position, index.points[other][start].smoothed_position) < config.single_vehicle.partner_exclusion_distance for other in others):
            continue
        end = start + config.collision.heading_jump_window_frames
        proposals.append(_event(context, "single_vehicle_candidate", (track,), start, start, end, (start, end), tuple(consequences),
                                index, None, "single", "low"))
    return proposals


def _event(
    context: MotionContext, event_type: str, tracks: tuple[int, ...], start: int, peak: int, end: int, window_frames: tuple[int, int],
    signals: tuple[EvidenceSignal, ...], index: _Index, conflict: ConflictRecord | None, predicted: CollisionType, confidence: EventConfidence,
) -> IncidentEvent:
    """Assemble one proposal; times come from source frames and the window is padded by one frame period."""
    def time(frame: int) -> float:
        known = next((index.points[track][frame] for track in tracks if frame in index.points[track]), None)
        return known.source_time if known is not None else frame * context.frame_period

    window = (max(0.0, time(window_frames[0]) - context.frame_period), time(window_frames[1]) + context.frame_period)
    points = [index.points[track].get(peak) for track in tracks]
    if all(point is not None for point in points):
        location = (sum(point.smoothed_position[0] for point in points) / len(points), sum(point.smoothed_position[1] for point in points) / len(points))
        spread = math.hypot(*(point.position_uncertainty for point in points)) / len(points)
    elif conflict is not None:
        location, spread = conflict.location, conflict.radius
    else:
        raise IncidentError(f"Tracks {tracks} have no position at frame {peak} and no conflict area to locate the event.")
    return IncidentEvent(
        event_id=f"{event_type}-t{'-'.join(str(track) for track in tracks)}-f{peak}", case_id=context.case_id, event_type=event_type,
        track_ids=tracks, start_frame=start, peak_frame=peak, end_frame=end, start_time=time(start), peak_time=time(peak), end_time=time(end),
        time_window=window, location=location, location_uncertainty=spread, unit=context.unit, signals=signals,
        conflict_id=None if conflict is None else conflict.conflict_id, phases=_phases(index, tracks, window),
        predicted_collision_type=predicted, confidence=confidence, provenance="inferred", status="PROPOSED_BY_SYSTEM", created_by=context.created_by,
    )
