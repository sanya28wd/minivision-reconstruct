"""Rule-based contribution assessment for collision and near-miss candidates (README §6.7, §8.10).

Each rule is a small function over Week 7 movement events and PET conflicts. It returns supporting evidence for
the road user whose observed action meets the rule, and counterevidence where a similar action was observed but
the rule was not met (for example, a stop-line crossing after a visible stop). Every item cites event or conflict
IDs and source frames. No rule asserts a road-rule breach: there is no approved agency rule source.

Unresolved facts (unavailable, interpolated, or reviewer-created positions; mirrored coordinates; relative
calibration) are listed for every involved road user. One that falls inside the decisive window around the
candidate forces UNDETERMINED. A missing observation is never used as evidence against anyone.
"""

from collections import defaultdict
from dataclasses import dataclass, field
import math

from minivision_reconstruct.incident.models import (
    NOT_A_VIOLATION_FINDING, ActionEvidence, ConflictRecord, ContributionAssessment, IncidentConfig, IncidentEvent, UnresolvedFact,
)
from minivision_reconstruct.movement.models import EventConfidence, MovementEvent, MovementObservation, SmoothedPoint
from minivision_reconstruct.movement.motion import MotionContext


RANK: dict[EventConfidence, int] = {"low": 0, "medium": 1, "high": 2}
Rule = tuple[list[ActionEvidence], list[ActionEvidence]]


@dataclass(frozen=True)
class ContributionInputs:
    smoothed: tuple[SmoothedPoint, ...]
    movement: tuple[MovementObservation, ...]
    events: tuple[MovementEvent, ...]
    conflicts: tuple[ConflictRecord, ...]
    world_is_right_handed: bool
    metric: bool


@dataclass
class _Index:
    points: dict[int, list[SmoothedPoint]] = field(default_factory=lambda: defaultdict(list))
    motion: dict[int, dict[int, MovementObservation]] = field(default_factory=lambda: defaultdict(dict))
    events: dict[tuple[int, str], list[MovementEvent]] = field(default_factory=lambda: defaultdict(list))
    involving: dict[int, list[MovementEvent]] = field(default_factory=lambda: defaultdict(list))


def _index(inputs: ContributionInputs) -> _Index:
    index = _Index()
    for point in inputs.smoothed:
        index.points[point.track_id].append(point)
    for item in inputs.movement:
        index.motion[item.track_id][item.source_frame] = item
    for event in inputs.events:
        for track in event.track_ids:
            index.involving[track].append(event)
        if len(event.track_ids) == 1:
            index.events[(event.track_ids[0], event.event_type)].append(event)
    return index


def _evidence(rule: str, track: int, description: str, ids: tuple[str, ...], frames: tuple[int, ...], action_time: float, peak_time: float) -> ActionEvidence:
    return ActionEvidence(rule=rule, track_id=track, description=description, event_ids=ids, source_frames=tuple(sorted(set(frames))),
                          action_time=action_time, seconds_before_peak=max(0.0, peak_time - action_time))


def _heading(index: _Index, track: int, frame: int) -> float | None:
    """Most recent known heading at or before the frame; a road user at rest keeps the heading it arrived with."""
    known = [item for source, item in index.motion[track].items() if source <= frame and item.heading_degrees is not None]
    return max(known, key=lambda item: item.source_frame).heading_degrees if known else None


def _angle(first: float, second: float) -> float:
    difference = abs(first - second) % 360.0
    return min(difference, 360.0 - difference)


def did_not_stop_behind_stopped_road_user(event: IncidentEvent, index: _Index, config: IncidentConfig) -> Rule:
    """R1: one road user was at rest when proximity began; the other closed on it from behind without a visible stop."""
    support: list[ActionEvidence] = []
    counter: list[ActionEvidence] = []
    for leader, follower in (event.track_ids, event.track_ids[::-1]):
        # The end of a rest is only known within its start event's time window; an impact shove can end it a few frames early.
        rest_end = {start.peak_time: start.time_window[1] for start in index.events[(leader, "start")]}
        resting = [stop for stop in index.events[(leader, "stop")]
                   if stop.peak_time < event.peak_time and rest_end.get(stop.end_time, stop.end_time) >= event.start_time]
        if not resting:
            continue
        stop = max(resting, key=lambda item: item.peak_time)
        own_stops = [item for item in index.events[(follower, "stop")] if stop.peak_time <= item.peak_time < event.time_window[0]]
        if own_stops:
            counter.append(_evidence("did_not_stop_behind_stopped_road_user", follower,
                                     f"track {follower} came to a visible stop at {own_stops[0].peak_time:.2f} s behind track {leader}, which was at rest",
                                     (stop.event_id, own_stops[0].event_id), (stop.peak_frame, own_stops[0].peak_frame), own_stops[0].peak_time, event.peak_time))
            continue
        heading = _heading(index, follower, event.start_frame)
        here, there = index.motion[follower].get(event.start_frame), index.motion[leader].get(event.start_frame)
        if heading is None or here is None or there is None or here.world_x is None or there.world_x is None:
            continue
        bearing = math.degrees(math.atan2(there.world_y - here.world_y, there.world_x - here.world_x))
        if _angle(heading, bearing) <= config.collision_type.same_direction_max_degrees:
            support.append(_evidence(
                "did_not_stop_behind_stopped_road_user", follower,
                f"track {leader} was at rest from {stop.peak_time:.2f} s; track {follower} approached it from behind "
                f"(heading within {_angle(heading, bearing):.0f} degrees of the direction to it) with no visible stop before contact",
                (stop.event_id,), (stop.peak_frame, event.start_frame, event.peak_frame), event.time_window[0], event.peak_time,
            ))
    return support, counter


def lane_change_into_occupied_lane(event: IncidentEvent, index: _Index, config: IncidentConfig) -> Rule:
    """R2: a lane change shortly before the conflict into the lane the other road user occupied or entered before it."""
    support: list[ActionEvidence] = []
    counter: list[ActionEvidence] = []
    for changer, other in (event.track_ids, event.track_ids[::-1]):
        for change in index.events[(changer, "lane_change")]:
            if not event.peak_time - config.contribution.lane_change_window_seconds <= change.peak_time <= event.peak_time:
                continue
            occupied = sorted(frame for frame, item in index.motion[other].items()
                              if change.start_frame <= frame <= event.peak_frame and item.lane_or_zone_id == change.to_feature)
            if occupied:
                support.append(_evidence("lane_change_into_occupied_lane", changer,
                                         f"track {changer} changed from {change.from_feature} to {change.to_feature} at {change.peak_time:.2f} s; "
                                         f"track {other} was in {change.to_feature} from frame {occupied[0]}",
                                         (change.event_id,), (change.peak_frame, occupied[0]), change.peak_time, event.peak_time))
            else:
                counter.append(_evidence("lane_change_into_occupied_lane", changer,
                                         f"track {changer} changed into {change.to_feature}, which track {other} was not seen in before the conflict",
                                         (change.event_id,), (change.peak_frame,), change.peak_time, event.peak_time))
    return support, counter


def entered_occupied_conflict_area(event: IncidentEvent, index: _Index, conflicts: tuple[ConflictRecord, ...], config: IncidentConfig) -> Rule:
    """R3: the second road user entered a crossing conflict area while the first was still inside (PET <= 0)."""
    support: list[ActionEvidence] = []
    counter: list[ActionEvidence] = []
    for conflict in conflicts:
        if {conflict.track_first, conflict.track_second} != set(event.track_ids) or not conflict.simultaneous_occupancy:
            continue
        if not event.peak_time - config.contribution.lookback_seconds <= conflict.second_enter_time <= event.time_window[1]:
            continue
        headings = [_heading(index, track, conflict.second_enter_frame) for track in (conflict.track_first, conflict.track_second)]
        # Road users following on the same path also "cross"; only a real crossing angle makes this a conflict-area entry.
        if None in headings or _angle(*headings) <= config.collision_type.same_direction_max_degrees:
            continue
        support.append(_evidence("entered_occupied_conflict_area", conflict.track_second,
                                 f"track {conflict.track_second} entered conflict area {conflict.conflict_id} at {conflict.second_enter_time:.2f} s "
                                 f"while track {conflict.track_first} was still inside (PET {conflict.pet_seconds:.2f} s)",
                                 (conflict.conflict_id,), (conflict.second_enter_frame, conflict.first_exit_frame), conflict.second_enter_time, event.peak_time))
    return support, counter


def no_visible_stop_at_stop_line(event: IncidentEvent, index: _Index, config: IncidentConfig) -> Rule:
    """R4: a stop-line crossing shortly before the conflict with no visible stop before it. An observation, not a violation."""
    support: list[ActionEvidence] = []
    counter: list[ActionEvidence] = []
    window = config.contribution.stop_line_window_seconds
    for track in event.track_ids:
        for crossing in index.events[(track, "stop_line_crossing")]:
            if not event.peak_time - window <= crossing.peak_time <= event.peak_time:
                continue
            line = crossing.from_feature or crossing.to_feature
            prior = [stop for stop in index.events[(track, "stop")] if stop.peak_time <= crossing.peak_time and stop.end_time >= crossing.peak_time - window]
            if prior:
                counter.append(_evidence("no_visible_stop_at_stop_line", track,
                                         f"track {track} came to a visible stop at {prior[-1].peak_time:.2f} s before crossing {line} at {crossing.peak_time:.2f} s",
                                         (prior[-1].event_id, crossing.event_id), (prior[-1].peak_frame, crossing.peak_frame), crossing.peak_time, event.peak_time))
            else:
                support.append(_evidence("no_visible_stop_at_stop_line", track,
                                         f"track {track} crossed {line} at {crossing.peak_time:.2f} s with no visible stop in the preceding {window:g} s. {NOT_A_VIOLATION_FINDING}",
                                         (crossing.event_id,), (crossing.peak_frame,), crossing.peak_time, event.peak_time))
    return support, counter


def turned_across_path(event: IncidentEvent, index: _Index, config: IncidentConfig) -> Rule:
    """R5: one road user turned shortly before the conflict while the other kept its path; if both turned, neither is supported."""
    window = config.contribution.turn_window_seconds
    turns = {track: [turn for kind in ("turn_left", "turn_right") for turn in index.events[(track, kind)]
                     if turn.start_time <= event.peak_time and turn.end_time >= event.peak_time - window] for track in event.track_ids}
    support: list[ActionEvidence] = []
    counter: list[ActionEvidence] = []
    for track, other in (event.track_ids, event.track_ids[::-1]):
        for turn in turns[track]:
            description = f"track {track} made a {turn.event_type.replace('_', ' ')} from {turn.start_time:.2f} s to {turn.end_time:.2f} s"
            if turns[other]:
                counter.append(_evidence("turned_across_path", track, f"{description}; track {other} also turned, so neither kept its path",
                                         (turn.event_id,), (turn.peak_frame,), turn.peak_time, event.peak_time))
            else:
                support.append(_evidence("turned_across_path", track, f"{description} across the path of track {other}, which did not turn",
                                         (turn.event_id,), (turn.start_frame, turn.peak_frame), turn.peak_time, event.peak_time))
    return support, counter


def _unresolved(event: IncidentEvent, index: _Index, inputs: ContributionInputs, config: IncidentConfig, turn_supported: bool) -> list[UnresolvedFact]:
    """Data-quality facts in the lookback window; decisive when they touch the window just before and during the candidate."""
    lookback = (max(0.0, event.peak_time - config.contribution.lookback_seconds), event.time_window[1])
    decisive = (event.time_window[0] - config.collision.post_window_seconds, event.time_window[1])
    facts: list[UnresolvedFact] = []
    checks = (
        ("unavailable_frames", lambda point: point.smoothed_position is None, "position unavailable (lost)"),
        ("interpolated_frames", lambda point: point.state == "interpolated" or point.provenance == "review_interpolation", "position interpolated, not observed"),
        ("reviewer_track", lambda point: point.provenance == "reviewer", "position created by a reviewer"),
    )
    for track in event.track_ids:
        for kind, test, text in checks:
            times = [point.source_time for point in index.points[track] if lookback[0] <= point.source_time <= lookback[1] and test(point)]
            if times:
                span = (min(times), max(times))
                facts.append(UnresolvedFact(kind=kind, track_ids=(track,), time_window=span, decisive=span[0] <= decisive[1] and span[1] >= decisive[0],
                                            description=f"track {track}: {text} for {len(times)} frame(s) between {span[0]:.2f} s and {span[1]:.2f} s"))
    if not inputs.world_is_right_handed:
        facts.append(UnresolvedFact(kind="mirrored_coordinates", track_ids=event.track_ids, time_window=None, decisive=turn_supported,
                                    description="road-plane coordinates are mirrored, so turn directions are unreliable"))
    if not inputs.metric:
        facts.append(UnresolvedFact(kind="non_metric_calibration", track_ids=event.track_ids, time_window=None, decisive=False,
                                    description="relative calibration: distances are not physical measurements"))
    return facts


def assess_contributions(incidents: tuple[IncidentEvent, ...], inputs: ContributionInputs, config: IncidentConfig, context: MotionContext) -> tuple[ContributionAssessment, ...]:
    """One assessment per collision or near-miss candidate; single-vehicle candidates have no counterpart to assess."""
    index = _index(inputs)
    assessments: list[ContributionAssessment] = []
    for event in incidents:
        if event.event_type == "single_vehicle_candidate":
            continue
        results = (
            did_not_stop_behind_stopped_road_user(event, index, config), lane_change_into_occupied_lane(event, index, config),
            entered_occupied_conflict_area(event, index, inputs.conflicts, config), no_visible_stop_at_stop_line(event, index, config),
            turned_across_path(event, index, config),
        )
        support = [item for found, _ in results for item in found]
        counter = [item for _, found in results for item in found]
        facts = _unresolved(event, index, inputs, config, any(item.rule == "turned_across_path" for item in support))
        contributors = tuple(sorted({item.track_id for item in support}))
        if any(fact.decisive for fact in facts):
            outcome, confidence = "UNDETERMINED", "low"
            if support:
                facts = [fact.model_copy(update={"description": f"{fact.description}; would otherwise support: "
                                                 + ", ".join(sorted({f'{item.rule} (track {item.track_id})' for item in support}))}) if fact.decisive else fact for fact in facts]
            contributors, support = (), []
        elif contributors:
            outcome = "ONE_CONTRIBUTOR" if len(contributors) == 1 else "MULTIPLE_CONTRIBUTORS"
            confidence = min(event.confidence, "low" if facts else "medium", key=RANK.__getitem__)
        else:
            outcome = "UNDETERMINED" if facts else "NO_SUPPORTED_CONTRIBUTOR"
            confidence = "low" if facts else min(event.confidence, "medium", key=RANK.__getitem__)
        lookback = (max(0.0, event.peak_time - config.contribution.lookback_seconds), event.peak_time)
        observed = tuple(item.event_id for item in sorted({item.event_id: item for track in event.track_ids for item in index.involving[track]
                                                           if lookback[0] <= item.peak_time <= lookback[1]}.values(), key=lambda item: (item.peak_time, item.event_id)))
        cited = {event_id for item in support + counter for event_id in item.event_ids}
        features = {feature for track in event.track_ids for item in index.involving[track] if item.event_id in cited
                    for feature in (item.from_feature, item.to_feature) if feature is not None}
        assessments.append(ContributionAssessment(
            assessment_id=f"assessment-{event.event_id}", case_id=context.case_id, event_id=event.event_id, involved_track_ids=event.track_ids,
            contributing_track_ids=contributors, outcome=outcome, lookback_window=lookback, observed_actions=observed,
            supporting_evidence=tuple(support), counterevidence=tuple(counter), applicable_road_context_refs=tuple(sorted(features)),
            unresolved_facts=tuple(facts), rule_source=None, disclaimer=NOT_A_VIOLATION_FINDING, confidence=confidence,
            provenance="inferred", status="PROPOSED_BY_SYSTEM", created_by=context.created_by,
        ))
    return tuple(assessments)
