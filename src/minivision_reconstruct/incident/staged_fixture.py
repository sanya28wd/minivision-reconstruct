"""Controlled synthetic incident scenarios with exact collision, near-miss, PET, and contribution ground truth.

One case per scenario. Road users follow scripted road-plane paths on the Week 7 staged intersection, plus a
third northbound lane on the west side. At impact the scripted contact points stop abruptly along a short arc,
so a heading jump and an abrupt stop are both present. Contact points stand for whole vehicles: "impact" means
the two contact points reach a scripted contact separation, not that drawn rectangles touch.

Truth is computed from the scripts without rendering. The rendered video is visibly labelled as synthetic and
must never be presented as real CCTV.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import combinations
import math
from pathlib import Path
from typing import Literal, get_args

import numpy as np
from pydantic import Field
from shapely.geometry import LineString, Point as ShapelyPoint

from minivision_reconstruct.incident.models import CollisionType, ContributionOutcome, ContributionRule, IncidentError, IncidentEventType
from minivision_reconstruct.movement.models import DistanceUnit
from minivision_reconstruct.movement.staged_fixture import (
    FPS, LANES, Keyframe, Path2D, PathBuilder, RoadUser, box_geometry, fixture_homography, position_at, progress_at,
    render_staged_case, schedule, time_at_progress, visible_world,
)
from minivision_reconstruct.reconstruction.models import Artifact, Point, require_identifier


ScenarioName = Literal[
    "rear_end", "t_bone", "lane_change_sideswipe", "both_lane_change", "near_miss_crossing",
    "normal_crossing", "occluded_impact", "single_vehicle", "box_overlap_only", "pedestrian_near_miss",
]
SCENARIOS: tuple[str, ...] = get_args(ScenarioName)
RECONSTRUCTION_RUN = "week8-incident"
INCIDENT_LANES: dict[str, tuple[tuple[float, float], ...]] = {"lane-0": ((18.0, 0.0), (24.0, 0.0), (24.0, 60.0), (18.0, 60.0)), **LANES}
STOP_LINE_Y = 16.0
TRACE_STEP = 0.004
SCHEDULE_END = 60.0  # road users at rest stay scheduled (and visible) until after every clip ends


# --- Ground-truth schema ---


class GroundTruthRoadUser(Artifact):
    track_id: int = Field(ge=1)
    class_name: str
    role: str
    positions: tuple[tuple[int, float, float], ...] = Field(description="True contact point per visible frame, including occluded frames.")
    unavailable_window: tuple[float, float] | None = Field(description="Scripted occlusion; the tracker reports these frames as lost.")


class GroundTruthAction(Artifact):
    track_id: int = Field(ge=1)
    rule: ContributionRule
    action_time: float | None = Field(description="When the action happened; null for an absence, such as not stopping.")
    note: str


class GroundTruthContribution(Artifact):
    outcome: ContributionOutcome
    contributing_track_ids: tuple[int, ...]
    actions: tuple[GroundTruthAction, ...]
    note: str


class GroundTruthIncident(Artifact):
    incident_id: str
    event_type: IncidentEventType
    track_ids: tuple[int, ...] = Field(min_length=1)
    peak_time: float = Field(ge=0, description="Scripted impact; for a near miss, the time of minimum true separation.")
    peak_frame: int = Field(ge=0)
    location: Point = Field(description="Midpoint of the true contact points at peak_time; for a single vehicle, its contact point.")
    contact_separation: float | None = Field(description="True contact-point separation at peak_time; null for a single vehicle.")
    relative_heading_degrees: float | None
    collision_type: CollisionType | None = Field(description="Null for near misses.")
    contribution: GroundTruthContribution | None = Field(description="Null where no assessment is expected (single vehicle).")
    note: str


class GroundTruthConflict(Artifact):
    """A true crossing of two paths. PET is measured at the crossing point itself (a conflict area of radius zero)."""

    track_first: int = Field(ge=1)
    track_second: int = Field(ge=1)
    location: Point
    first_pass_time: float
    second_pass_time: float
    point_pet_seconds: float


class GroundTruthPair(Artifact):
    track_a: int = Field(ge=1)
    track_b: int = Field(ge=1)
    minimum_separation: float = Field(ge=0)
    minimum_separation_time: float = Field(ge=0)
    box_overlap_frames: tuple[int, ...] = Field(description="Frames where the noise-free image boxes of both visible road users overlap.")


class IncidentGroundTruth(Artifact):
    """Exhaustive incident labels for one scenario case; an empty incident list means nothing should trigger."""

    schema_version: Literal["1.0"]
    case_id: str
    scenario: ScenarioName
    description: str
    source: Literal["controlled_synthetic"]
    annotator: str
    unit: DistanceUnit
    frame_rate: float = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    road_users: tuple[GroundTruthRoadUser, ...]
    incidents: tuple[GroundTruthIncident, ...]
    conflicts: tuple[GroundTruthConflict, ...]
    pairs: tuple[GroundTruthPair, ...]
    notes: tuple[str, ...]


def incident_ground_truth_path(cases_root: Path, case_id: str) -> Path:
    return cases_root / case_id / "annotations" / "incident_ground_truth.json"


# --- Scenario scripts ---


@dataclass(frozen=True)
class IncidentSpec:
    event_type: IncidentEventType
    track_ids: tuple[int, ...]
    peak_time: float | None  # None: use the time of minimum true separation
    collision_type: CollisionType | None
    contribution: GroundTruthContribution | None
    note: str


@dataclass(frozen=True)
class ScenarioScript:
    description: str
    duration_seconds: int
    users: tuple[RoadUser, ...]
    incidents: tuple[IncidentSpec, ...]


def deflect(builder: PathBuilder, heading_degrees: float, turn_degrees: float, length: float) -> PathBuilder:
    """Short constant-radius arc from the current point (positive turns left): the post-impact heading jump."""
    x, y = builder.points[-1]
    side = math.copysign(90.0, turn_degrees)
    radius = length / math.radians(abs(turn_degrees))
    centre = (x + radius * math.cos(math.radians(heading_degrees + side)), y + radius * math.sin(math.radians(heading_degrees + side)))
    start = heading_degrees - side
    return builder.arc(centre, radius, start, start + turn_degrees)


def lane_change_until(builder: PathBuilder, lateral_end: float, distance: float, stop_x: float) -> float:
    """Northbound smoothstep lane change cut off where the contact point reaches stop_x; returns the heading there."""
    start = builder.points[-1]
    steps = max(2, math.ceil(distance / builder.resolution))
    for k in range(1, steps + 1):
        u = k / steps
        x = start[0] + (lateral_end - start[0]) * (3 * u ** 2 - 2 * u ** 3)
        builder.points.append((x, start[1] + distance * u))
        if (x - stop_x) * (lateral_end - start[0]) >= 0:
            break
    else:
        raise IncidentError(f"Lane change toward x={lateral_end} never reaches x={stop_x}.")
    (x0, y0), (x1, y1) = builder.points[-2], builder.points[-1]
    return math.degrees(math.atan2(y1 - y0, x1 - x0))


def first_progress(path: Path2D, test: Callable[[float, float], bool]) -> float:
    """Arc length of the first dense path point satisfying the test."""
    for (x, y), length in zip(path.points, path.lengths, strict=True):
        if test(float(x), float(y)):
            return float(length)
    raise IncidentError("Scripted path never satisfies the requested condition.")


def strike(impact_time: float, impact_progress: float, rate: float, braking: float, end: float) -> tuple[Keyframe, ...]:
    """Cruise into impact, then a uniform abrupt stop over the post-impact arc and rest until the end."""
    return (
        Keyframe(impact_time - impact_progress / rate, 0.0, rate), Keyframe(impact_time, impact_progress, rate),
        Keyframe(impact_time + 2 * braking / rate, impact_progress + braking, 0.0), Keyframe(end, impact_progress + braking, 0.0),
    )


def brake_after(keys: tuple[Keyframe, ...], braking: float, end: float) -> tuple[Keyframe, ...]:
    """Append an abrupt post-impact stop to a schedule whose last keyframe is the impact."""
    last = keys[-1]
    return (*keys, Keyframe(last.time + 2 * braking / last.rate, last.progress + braking, 0.0), Keyframe(end, last.progress + braking, 0.0))


def car(track_id: int, role: str, path: Path2D, keys: tuple[Keyframe, ...], lost: tuple[float, float] | None = None,
        class_name: str = "car", box_scale: tuple[float, float] = (1.0, 1.0)) -> RoadUser:
    return RoadUser(track_id, class_name, role, path, keys, lost, None, box_scale)


def contribution(outcome: ContributionOutcome, actions: tuple[GroundTruthAction, ...], note: str) -> GroundTruthContribution:
    return GroundTruthContribution(outcome=outcome, contributing_track_ids=tuple(sorted({action.track_id for action in actions})), actions=actions, note=note)


def _rear_end(rng: np.random.Generator, end: float) -> ScenarioScript:
    leader_rate, follower_rate = rng.uniform(5.5, 6.5), rng.uniform(5.5, 6.5)
    rest, shove = rng.uniform(3.0, 3.6), 0.8
    impact = rest + rng.uniform(2.0, 3.0)
    braking, stop_progress = 7.0, 26.5  # leader rests at y = 14.5, short of the stop line
    leader = deflect(PathBuilder((27.0, -12.0), 0.05).straight((27.0, 14.5)), 90.0, -15.0, shove).build()
    cruise_end = rest - 2 * braking / leader_rate
    leader_keys = (
        Keyframe(cruise_end - (stop_progress - braking) / leader_rate, 0.0, leader_rate), Keyframe(cruise_end, stop_progress - braking, leader_rate),
        Keyframe(rest, stop_progress, 0.0), Keyframe(impact, stop_progress, 0.0),
        Keyframe(impact + 0.5, stop_progress + shove, 0.0), Keyframe(end, stop_progress + shove, 0.0),
    )
    contact_y = 14.5 - rng.uniform(4.2, 4.6)
    follower = deflect(PathBuilder((27.0, -30.0), 0.05).straight((27.0, contact_y)), 90.0, 25.0, 1.2).build()
    return ScenarioScript(
        "Leader stops before the stop line; the follower does not stop and strikes it from behind.", 14,
        (car(1, "leader: stops, struck from behind, shoved forward", leader, leader_keys),
         car(2, "follower: no visible stop, strikes the leader", follower, strike(impact, contact_y + 30.0, follower_rate, 1.2, end))),
        (IncidentSpec("collision_candidate", (1, 2), impact, "rear_end", contribution("ONE_CONTRIBUTOR", (
            GroundTruthAction(track_id=2, rule="did_not_stop_behind_stopped_road_user", action_time=None,
                              note=f"leader at rest from {rest:.3f} s; follower closes without stopping"),), "follower did not stop behind a stopped road user"),
            "contact separation is the scripted gap between contact points, not a body gap"),),
    )


def _t_bone(rng: np.random.Generator, end: float) -> ScenarioScript:
    impact = rng.uniform(4.5, 5.5)
    striker_rate, struck_rate = rng.uniform(6.0, 7.0), rng.uniform(5.5, 6.5)
    gap, struck_x = rng.uniform(2.6, 3.2), 27.0 + rng.uniform(-0.8, 0.8)
    striker = deflect(PathBuilder((27.0, -12.0), 0.05).straight((27.0, 27.0 - gap)), 90.0, -15.0, 1.0).build()
    struck = deflect(PathBuilder((-15.0, 27.0), 0.05).straight((struck_x, 27.0)), 0.0, 30.0, 1.8).build()
    impact_progress = 27.0 - gap + 12.0
    stop_line_time = impact - (impact_progress - (STOP_LINE_Y + 12.0)) / striker_rate
    return ScenarioScript(
        "Northbound road user passes the stop line without stopping and strikes the side of an eastbound road user.", 12,
        (car(1, "northbound: no visible stop at the stop line, strikes the crossing road user", striker, strike(impact, impact_progress, striker_rate, 1.0, end)),
         car(2, "eastbound: struck on its side, pushed north", struck, strike(impact, struck_x + 15.0, struck_rate, 1.8, end))),
        (IncidentSpec("collision_candidate", (1, 2), impact, "t_bone", contribution("ONE_CONTRIBUTOR", (
            GroundTruthAction(track_id=1, rule="no_visible_stop_at_stop_line", action_time=stop_line_time, note="crosses stop-1 at cruise"),),
            "observed action only; no approved rule source, so not a violation finding"), "side impact at about 90 degrees"),),
    )


def _lane_change_sideswipe(rng: np.random.Generator, end: float) -> ScenarioScript:
    impact = rng.uniform(4.0, 4.6)
    straight_rate, changer_rate = rng.uniform(5.0, 5.6), rng.uniform(5.2, 5.8)
    builder = PathBuilder((33.0, -10.0), 0.05).straight((33.0, 26.0))
    heading = lane_change_until(builder, 27.0, 30.0, 29.0 + rng.uniform(-0.3, 0.3))
    contact = builder.points[-1]
    impact_progress = builder.length()
    changer = deflect(builder, heading, -20.0, 1.5).build()
    boundary_time = impact - (impact_progress - first_progress(changer, lambda x, _: x <= 30.0)) / changer_rate
    straight_y = contact[1] - rng.uniform(0.0, 1.0)
    straight = deflect(PathBuilder((27.0, -10.0), 0.05).straight((27.0, straight_y)), 90.0, 20.0, 1.5).build()
    return ScenarioScript(
        "A road user changes from lane 2 into lane 1 beside a road user already in lane 1, and they sideswipe.", 12,
        (car(1, "lane 1: continues straight, struck on its right side", straight, strike(impact, straight_y + 10.0, straight_rate, 1.5, end)),
         car(2, "lane 2 to lane 1: lane change into the occupied lane", changer, strike(impact, impact_progress, changer_rate, 1.5, end))),
        (IncidentSpec("collision_candidate", (1, 2), impact, "sideswipe", contribution("ONE_CONTRIBUTOR", (
            GroundTruthAction(track_id=2, rule="lane_change_into_occupied_lane", action_time=boundary_time, note="contact point crosses the lane-1/lane-2 boundary"),),
            "lane change into an occupied lane shortly before contact"), "same-direction contact with lateral offset"),),
    )


def _both_lane_change(rng: np.random.Generator, end: float) -> ScenarioScript:
    impact = rng.uniform(4.0, 4.6)
    rate = rng.uniform(5.0, 5.6)
    users: list[RoadUser] = []
    actions: list[GroundTruthAction] = []
    for track_id, lane_x, stop_x, boundary_x, turn in ((1, 21.0, 26.0, 24.0, 20.0), (2, 33.0, 28.0, 30.0, -20.0)):
        builder = PathBuilder((lane_x, -10.0), 0.05).straight((lane_x, 25.0))
        heading = lane_change_until(builder, 27.0, 30.0, stop_x)
        impact_progress = builder.length()
        path = deflect(builder, heading, turn, 1.5).build()
        moving_east = lane_x < 27.0
        crossing = first_progress(path, lambda x, _, b=boundary_x, east=moving_east: x >= b if east else x <= b)
        users.append(car(track_id, f"lane change from x={lane_x:.0f} into lane 1", path, strike(impact, impact_progress, rate * (1.0 + 0.01 * track_id), 1.5, end)))
        actions.append(GroundTruthAction(track_id=track_id, rule="lane_change_into_occupied_lane", action_time=impact - (impact_progress - crossing) / (rate * (1.0 + 0.01 * track_id)),
                                         note=f"contact point crosses the lane boundary at x={boundary_x:.0f}"))
    return ScenarioScript(
        "Road users in lane 0 and lane 2 both change into lane 1 at the same time and sideswipe there.", 12, tuple(users),
        (IncidentSpec("collision_candidate", (1, 2), impact, "sideswipe", contribution("MULTIPLE_CONTRIBUTORS", tuple(actions),
                      "both lane changes are contributing actions; each is also counterevidence against the other being the sole contributor"),
                      "converging lane changes; relative heading is about twice one lane-change angle"),),
    )


def _stop_line_then_cross(rng: np.random.Generator, end: float, pet: float, description: str) -> ScenarioScript:
    """Northbound road user stops at the stop line, then crosses behind an eastbound road user `pet` seconds later."""
    north_rate, east_rate = rng.uniform(5.5, 6.5), rng.uniform(5.5, 6.5)
    north = PathBuilder((27.0, -12.0), 0.05).straight((27.0, 70.0)).build()
    north_keys = schedule(rng.uniform(0.0, 0.5), float(north.lengths[-1]), north_rate, ((27.0, 6.0, rng.uniform(2.0, 3.0)),))
    north_cross = time_at_progress(north_keys, 27.0 + 12.0)
    east = PathBuilder((-15.0, 27.0), 0.05).straight((75.0, 27.0)).build()
    east_keys = schedule(north_cross - pet - (27.0 + 15.0) / east_rate, float(east.lengths[-1]), east_rate, ())
    return ScenarioScript(description, 16, (
        car(1, "northbound: stops at the stop line, then proceeds straight", north, north_keys),
        car(2, "eastbound: straight through without stopping", east, east_keys),
    ), ())


def _near_miss_crossing(rng: np.random.Generator, end: float) -> ScenarioScript:
    pet = rng.uniform(0.8, 1.2)
    script = _stop_line_then_cross(rng, end, pet, "Northbound road user stops, then crosses about 1 s behind an eastbound road user: a near miss.")
    return replace(script, incidents=(IncidentSpec("near_miss_candidate", (1, 2), None, None, contribution(
        "NO_SUPPORTED_CONTRIBUTOR", (), "both road users' visible actions are complete and no rule applies"), f"scripted point PET {pet:.3f} s"),))


def _normal_crossing(rng: np.random.Generator, end: float) -> ScenarioScript:
    return _stop_line_then_cross(rng, end, rng.uniform(3.5, 4.5), "Ordinary crossing with a large PET; nothing should trigger.")


def _occluded_impact(rng: np.random.Generator, end: float) -> ScenarioScript:
    north_rate, east_rate = rng.uniform(5.5, 6.5), rng.uniform(5.5, 6.5)
    gap, struck_x = rng.uniform(2.6, 3.2), 27.0 + rng.uniform(-0.8, 0.8)
    striker = deflect(PathBuilder((27.0, -12.0), 0.05).straight((27.0, 27.0 - gap)), 90.0, -15.0, 1.0).build()
    keys = schedule(rng.uniform(0.0, 0.5), 27.0 - gap + 12.0, north_rate, ((27.0, 5.0, rng.uniform(1.5, 2.5)),))
    impact = keys[-1].time
    struck = deflect(PathBuilder((-15.0, 27.0), 0.05).straight((struck_x, 27.0)), 0.0, 30.0, 1.8).build()
    lost = (impact - rng.uniform(0.8, 1.0), impact + rng.uniform(0.5, 0.7))
    return ScenarioScript(
        "Crossing impact while the northbound road user is occluded around the moment of contact.", 14,
        (car(1, "northbound: stops at the stop line, proceeds, occluded around impact", striker, brake_after(keys, 1.0, end), lost=lost),
         car(2, "eastbound: struck on its side", struck, strike(impact, struck_x + 15.0, east_rate, 1.8, end))),
        (IncidentSpec("collision_candidate", (1, 2), impact, "t_bone", contribution(
            "UNDETERMINED", (), f"track 1 is unavailable from {lost[0]:.3f} s to {lost[1]:.3f} s, covering the decisive window"),
            "the impact itself is not visible"),),
    )


def _single_vehicle(rng: np.random.Generator, end: float) -> ScenarioScript:
    swerve_time, rate = rng.uniform(4.0, 5.0), rng.uniform(5.5, 6.5)
    swerve_x = rng.uniform(40.0, 46.0)
    swerver = deflect(PathBuilder((-10.0, 27.0), 0.05).straight((swerve_x, 27.0)), 0.0, rng.uniform(30.0, 40.0), 2.0).build()
    waiting = PathBuilder((27.0, -12.0), 0.05).straight((27.0, 70.0)).build()
    waiting_keys = schedule(rng.uniform(0.0, 0.5), float(waiting.lengths[-1]), rng.uniform(5.5, 6.5), ((27.0, 6.0, swerve_time + rng.uniform(1.0, 2.0)),))
    return ScenarioScript(
        "An eastbound road user swerves left and stops abruptly in the road with no other road user nearby.", 12,
        (car(1, "eastbound: abrupt swerve and stop", swerver, strike(swerve_time, swerve_x + 10.0, rate, 2.0, end)),
         car(2, "northbound: waits at the stop line well away from the swerve", waiting, waiting_keys)),
        (IncidentSpec("single_vehicle_candidate", (1,), swerve_time, "single", None, "peak is the start of the swerve"),),
    )


def _box_overlap_only(rng: np.random.Generator, end: float) -> ScenarioScript:
    rate, start = rng.uniform(4.8, 5.4), rng.uniform(0.0, 0.5)
    lead = rng.uniform(0.5, 1.5)
    car_path = PathBuilder((27.0, -5.0), 0.05).straight((27.0, 70.0)).build()
    bus_path = PathBuilder((33.0, -5.0 + lead), 0.05).straight((33.0, 70.0)).build()
    return ScenarioScript(
        "A car and a bus travel side by side in adjacent lanes; their image boxes overlap far from the camera but they never come close.", 16,
        (car(1, "lane 1: straight", car_path, schedule(start, float(car_path.lengths[-1]), rate, ())),
         car(2, "lane 2: straight, tall wide box", bus_path, schedule(start, float(bus_path.lengths[-1]), rate, ()), class_name="bus", box_scale=(2.8, 2.6))),
        (),
    )


def _pedestrian_near_miss(rng: np.random.Generator, end: float) -> ScenarioScript:
    pet, walk_rate, car_rate = rng.uniform(0.8, 1.2), rng.uniform(1.3, 1.5), rng.uniform(5.5, 6.5)
    walk = PathBuilder((12.0, 19.0), 0.05).straight((48.0, 19.0)).build()
    walk_keys = schedule(rng.uniform(0.0, 0.5), float(walk.lengths[-1]), walk_rate, ())
    pedestrian_cross = time_at_progress(walk_keys, 27.0 - 12.0)
    north = PathBuilder((27.0, -12.0), 0.05).straight((27.0, 70.0)).build()
    north_keys = schedule(pedestrian_cross + pet - (19.0 + 12.0) / car_rate, float(north.lengths[-1]), car_rate, ())
    stop_line_time = time_at_progress(north_keys, STOP_LINE_Y + 12.0)
    return ScenarioScript(
        "A car passes the stop line without stopping and crosses the pedestrian crossing about 1 s behind a pedestrian.", 16,
        (car(1, "pedestrian: walks east on the crossing", walk, walk_keys, class_name="person"),
         car(2, "northbound: no visible stop at the stop line, passes behind the pedestrian", north, north_keys)),
        (IncidentSpec("near_miss_candidate", (1, 2), None, None, contribution("ONE_CONTRIBUTOR", (
            GroundTruthAction(track_id=2, rule="no_visible_stop_at_stop_line", action_time=stop_line_time, note="crosses stop-1 at cruise"),),
            "observed action only; no approved rule source, so not a violation finding"), f"scripted point PET {pet:.3f} s"),),
    )


BUILDERS: dict[str, Callable[[np.random.Generator, float], ScenarioScript]] = {
    "rear_end": _rear_end, "t_bone": _t_bone, "lane_change_sideswipe": _lane_change_sideswipe, "both_lane_change": _both_lane_change,
    "near_miss_crossing": _near_miss_crossing, "normal_crossing": _normal_crossing, "occluded_impact": _occluded_impact,
    "single_vehicle": _single_vehicle, "box_overlap_only": _box_overlap_only, "pedestrian_near_miss": _pedestrian_near_miss,
}


def scenario_script(scenario: str, seed: int) -> ScenarioScript:
    """Script one scenario; timing, rates, and offsets vary with the seed."""
    if scenario not in BUILDERS:
        raise IncidentError(f"Unknown incident scenario '{scenario}'; choose from {', '.join(SCENARIOS)}.")
    rng = np.random.default_rng([seed, SCENARIOS.index(scenario)])
    script = BUILDERS[scenario](rng, SCHEDULE_END)
    if not 10 <= script.duration_seconds < SCHEDULE_END:
        raise IncidentError(f"Scenario '{scenario}' duration must be between 10 s and {SCHEDULE_END:.0f} s.")
    return script


# --- Ground truth from scripts ---


def _heading(user: RoadUser, time: float) -> float:
    progress = progress_at(user.keyframes, time)
    if progress is None:
        raise IncidentError(f"Road user {user.track_id} is not scheduled at {time:.3f} s.")
    (x0, y0), (x1, y1) = position_at(user.path, max(progress - 0.3, 0.0)), position_at(user.path, progress)
    return math.degrees(math.atan2(y1 - y0, x1 - x0))


def _position(user: RoadUser, time: float) -> tuple[float, float]:
    progress = progress_at(user.keyframes, time)
    if progress is None:
        raise IncidentError(f"Road user {user.track_id} is not scheduled at {time:.3f} s.")
    return position_at(user.path, progress)


def _occluded(user: RoadUser, time: float) -> bool:
    return user.lost is not None and user.lost[0] <= time < user.lost[1]


def incident_ground_truth(case_id: str, scenario: str, seed: int, pixel_noise_sigma: float) -> IncidentGroundTruth:
    """Exact labels for one scenario case, computed from the scripts alone."""
    script = scenario_script(scenario, seed)
    frames = range(script.duration_seconds * FPS)
    users = {user.track_id: user for user in script.users}
    positions = {track: [(frame, *world) for frame in frames if (world := visible_world(user, frame / FPS)) is not None] for track, user in users.items()}
    times = np.arange(0.0, script.duration_seconds, TRACE_STEP)
    traces = {track: [(float(time), world) for time in times if (world := visible_world(user, float(time))) is not None] for track, user in users.items()}
    homography = fixture_homography()

    pairs: list[GroundTruthPair] = []
    conflicts: list[GroundTruthConflict] = []
    for a, b in combinations(sorted(users), 2):
        lookup = {time: world for time, world in traces[b]}
        shared = [(time, math.dist(world, lookup[time])) for time, world in traces[a] if time in lookup]
        if not shared:
            continue
        minimum_time, minimum = min(shared, key=lambda item: item[1])
        overlap = []
        for frame in frames:
            time = frame / FPS
            worlds = [visible_world(users[track], time) for track in (a, b)]
            if any(world is None for world in worlds) or any(_occluded(users[track], time) for track in (a, b)):
                continue
            boxes = [box_geometry(homography, users[track], world) for track, world in zip((a, b), worlds, strict=True)]
            (pa, qa, wa, ha), (pb, qb, wb, hb) = boxes
            if abs(pa - pb) * 2 < wa + wb and qa - ha < qb and qb - hb < qa:
                overlap.append(frame)
        pairs.append(GroundTruthPair(track_a=a, track_b=b, minimum_separation=minimum, minimum_separation_time=minimum_time, box_overlap_frames=tuple(overlap)))
        conflicts.extend(_true_conflicts(a, traces[a], b, traces[b]))

    incidents: list[GroundTruthIncident] = []
    for number, spec in enumerate(script.incidents, 1):
        pair = next((item for item in pairs if (item.track_a, item.track_b) == tuple(sorted(spec.track_ids))), None)
        peak = spec.peak_time if spec.peak_time is not None else pair.minimum_separation_time if pair else None
        if peak is None:
            raise IncidentError(f"Scenario '{scenario}' incident {number} has no peak time.")
        points = [_position(users[track], peak) for track in spec.track_ids]
        location = (sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points))
        relative = None
        if len(spec.track_ids) == 2:
            difference = abs(_heading(users[spec.track_ids[0]], peak) - _heading(users[spec.track_ids[1]], peak)) % 360.0
            relative = min(difference, 360.0 - difference)
        incidents.append(GroundTruthIncident(
            incident_id=f"gt-incident-{number}", event_type=spec.event_type, track_ids=spec.track_ids, peak_time=peak, peak_frame=round(peak * FPS),
            location=location, contact_separation=math.dist(*points) if len(points) == 2 else None, relative_heading_degrees=relative,
            collision_type=spec.collision_type, contribution=spec.contribution, note=spec.note,
        ))
    return IncidentGroundTruth(
        schema_version="1.0", case_id=case_id, scenario=scenario, description=script.description, source="controlled_synthetic",
        annotator="scripted generator (exact kinematics)", unit="m", frame_rate=float(FPS), duration_seconds=float(script.duration_seconds),
        road_users=tuple(GroundTruthRoadUser(track_id=user.track_id, class_name=user.class_name, role=user.scenario,
                                             positions=tuple(positions[user.track_id]), unavailable_window=user.lost) for user in script.users),
        incidents=tuple(incidents), conflicts=tuple(sorted(conflicts, key=lambda item: item.second_pass_time)), pairs=tuple(pairs),
        notes=(f"seed={seed}", f"pixel_noise_sigma={pixel_noise_sigma}", "Synthetic rectangles on a drawn road; not real CCTV and not a detector benchmark.",
               "Positions are road-contact points; separations are contact-point distances, not vehicle-body gaps.",
               "Conflicts are true path crossings only; road users following on the same path are not annotated as conflicts.",
               "Point PET uses a conflict area of radius zero; evaluation must compare at the estimator's conflict radius."),
    )


def _true_conflicts(a: int, trace_a: list[tuple[float, tuple[float, float]]], b: int, trace_b: list[tuple[float, tuple[float, float]]]) -> list[GroundTruthConflict]:
    """Point crossings of two true visible paths, with the first time each contact point reaches the crossing."""
    if len(trace_a) < 2 or len(trace_b) < 2:
        return []
    paths = {a: LineString([world for _, world in trace_a]), b: LineString([world for _, world in trace_b])}
    shared = paths[a].intersection(paths[b])
    found = [shared] if shared.geom_type == "Point" else [part for part in getattr(shared, "geoms", ()) if part.geom_type == "Point"]
    result = []
    for point in found:
        passes: dict[int, float] = {}
        for track, trace in ((a, trace_a), (b, trace_b)):
            xy = np.asarray([world for _, world in trace], dtype=np.float64)
            arc = np.concatenate(([0.0], np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))))
            index = min(int(np.searchsorted(arc, paths[track].project(ShapelyPoint(point)) - 1e-9)), len(trace) - 1)
            passes[track] = trace[index][0]
        first, second = sorted(passes, key=lambda track: (passes[track], track))
        result.append(GroundTruthConflict(track_first=first, track_second=second, location=(float(point.x), float(point.y)),
                                          first_pass_time=passes[first], second_pass_time=passes[second], point_pet_seconds=passes[second] - passes[first]))
    return result


# --- Rendering ---


@dataclass(frozen=True)
class IncidentCase:
    case_id: str
    scenario: str
    reconstruction_directory: Path
    ground_truth_path: Path


def incident_case_id(prefix: str, seed: int, scenario: str) -> str:
    return require_identifier(f"{prefix}-{seed:03d}-{SCENARIOS.index(scenario) + 1:02d}-{scenario.replace('_', '-')}", "incident case ID")


def create_incident_case(cases_root: Path, case_id: str, scenario: str, seed: int, pixel_noise_sigma: float, ffmpeg: str, ffprobe: str) -> IncidentCase:
    """Render, ingest, track, calibrate, reconstruct, and annotate one incident scenario case."""
    require_identifier(case_id, "incident case ID")
    if not 0 <= pixel_noise_sigma <= 5:
        raise IncidentError("Incident fixture pixel noise must be between 0 and 5 px.")
    script = scenario_script(scenario, seed)
    truth = incident_ground_truth(case_id, scenario, seed, pixel_noise_sigma)
    rendered = render_staged_case(cases_root, case_id, script.users, seed, pixel_noise_sigma, script.duration_seconds * FPS,
                                  RECONSTRUCTION_RUN, ffmpeg, ffprobe, INCIDENT_LANES)
    if {track: tuple(items) for track, items in rendered.positions.items()} != {user.track_id: user.positions for user in truth.road_users}:
        raise IncidentError(f"Rendered positions for '{case_id}' do not match the scripted ground truth.")
    destination = incident_ground_truth_path(cases_root, case_id)
    destination.parent.mkdir(parents=True)
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(truth.model_dump_json(indent=2) + "\n")
    return IncidentCase(case_id, scenario, rendered.reconstruction_directory, destination)


def create_incident_fixture(
    cases_root: Path, prefix: str, seed: int, pixel_noise_sigma: float, ffmpeg: str, ffprobe: str, scenarios: tuple[str, ...],
) -> tuple[IncidentCase, ...]:
    """One case per requested scenario for one seed."""
    return tuple(create_incident_case(cases_root, incident_case_id(prefix, seed, scenario), scenario, seed, pixel_noise_sigma, ffmpeg, ffprobe)
                 for scenario in scenarios)
