"""Controlled synthetic staged-motion cases with exact movement ground truth.

Each road user follows a scripted road-plane path (straight lines, constant-radius arcs, and
lane-change S-curves) with progress keyframes, so stop/start, turn, lane-change, stop-line, and
zone timings are known exactly. Boxes carry seeded pixel noise to emulate detector jitter.
The rendered video is visibly labelled as synthetic and must never be presented as real CCTV.
"""

from dataclasses import asdict, dataclass
from itertools import combinations
import json
import math
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray
from shapely.geometry import LineString, Point as ShapelyPoint, Polygon

from minivision_reconstruct.calibration import build_calibration, parse_correspondence_rows, parse_road_feature, save_calibration_revision
from minivision_reconstruct.domain.models import TrackRecord
from minivision_reconstruct.evidence.case_store import ingest_case
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.movement.models import MovementError
from minivision_reconstruct.movement.validation import GroundTruthEvent, GroundTruthTrack, MovementGroundTruth, ground_truth_path
from minivision_reconstruct.reconstruction.models import require_identifier
from minivision_reconstruct.reconstruction.pipeline import automatic_track_input, prepare_reconstruction


FPS = 25
WIDTH, HEIGHT = 640, 480
TRACKING_RUN = "staged-tracks"
RECONSTRUCTION_RUN = "week7-staged"
LANES: dict[str, tuple[tuple[float, float], ...]] = {
    "lane-1": ((24.0, 0.0), (30.0, 0.0), (30.0, 60.0), (24.0, 60.0)),
    "lane-2": ((30.0, 0.0), (36.0, 0.0), (36.0, 60.0), (30.0, 60.0)),
    "lane-3": ((0.0, 24.0), (60.0, 24.0), (60.0, 30.0), (0.0, 30.0)),
    "lane-4": ((0.0, 30.0), (60.0, 30.0), (60.0, 36.0), (0.0, 36.0)),
}
STOP_LINE: tuple[tuple[float, float], tuple[float, float]] = ((24.0, 16.0), (30.0, 16.0))
CROSSWALK: tuple[tuple[float, float], ...] = ((23.0, 17.0), (37.0, 17.0), (37.0, 21.0), (23.0, 21.0))
VEHICLES = frozenset({"car", "truck", "bus", "motorcycle", "bicycle"})
CLASS_IDS: dict[str, int] = {"person": 0, "bicycle": 1, "car": 2, "motorcycle": 3, "bus": 4, "truck": 5}
CONTROL_ROWS = "80,450,0,0\n560,450,60,0\n430,70,60,60\n210,70,0,60"


@dataclass(frozen=True)
class Path2D:
    """Densely sampled road-plane path with cumulative arc length and labelled maneuver spans."""

    points: NDArray[np.float64]
    lengths: NDArray[np.float64]
    turns: tuple[tuple[float, float, str], ...]
    lane_changes: tuple[tuple[float, float, float], ...]


@dataclass(frozen=True)
class Keyframe:
    """Simulation input only: path progress (m) and its rate at a time; never exported as evidence."""

    time: float
    progress: float
    rate: float


@dataclass(frozen=True)
class RoadUser:
    track_id: int
    class_name: str
    scenario: str
    path: Path2D
    keyframes: tuple[Keyframe, ...]
    lost: tuple[float, float] | None
    interpolated: tuple[float, float] | None
    box_scale: tuple[float, float] = (1.0, 1.0)


@dataclass(frozen=True)
class StagedCase:
    case_id: str
    reconstruction_directory: Path
    ground_truth_path: Path


@dataclass(frozen=True)
class RenderedCase:
    """A rendered, ingested, tracked, calibrated, and reconstructed case plus its true visible positions."""

    case_directory: Path
    reconstruction_directory: Path
    positions: dict[int, list[tuple[int, float, float]]]


class PathBuilder:
    """Accumulate straight, arc, and lane-change pieces at fixed sampling resolution."""

    def __init__(self, start: tuple[float, float], resolution: float) -> None:
        self.points: list[tuple[float, float]] = [start]
        self.resolution = resolution
        self.turns: list[tuple[float, float, str]] = []
        self.lane_changes: list[tuple[float, float, float]] = []

    def length(self) -> float:
        return float(np.sum(np.hypot(*np.diff(np.asarray(self.points), axis=0).T))) if len(self.points) > 1 else 0.0

    def straight(self, end: tuple[float, float]) -> "PathBuilder":
        start = np.asarray(self.points[-1])
        steps = max(1, math.ceil(float(np.hypot(*(np.asarray(end) - start))) / self.resolution))
        self.points.extend((float(start[0] + (end[0] - start[0]) * k / steps), float(start[1] + (end[1] - start[1]) * k / steps)) for k in range(1, steps + 1))
        return self

    def arc(self, centre: tuple[float, float], radius: float, start_degrees: float, end_degrees: float) -> "PathBuilder":
        begin = self.length()
        steps = max(2, math.ceil(abs(math.radians(end_degrees - start_degrees)) * radius / self.resolution))
        for k in range(1, steps + 1):
            angle = math.radians(start_degrees + (end_degrees - start_degrees) * k / steps)
            self.points.append((centre[0] + radius * math.cos(angle), centre[1] + radius * math.sin(angle)))
        self.turns.append((begin, self.length(), "turn_left" if end_degrees > start_degrees else "turn_right"))
        return self

    def lane_change_north(self, lateral_end: float, distance: float, crossing_x: float) -> "PathBuilder":
        """Smoothstep lateral shift while travelling north; records the boundary-crossing progress."""
        start = self.points[-1]
        begin = self.length()
        steps = max(2, math.ceil(distance / self.resolution))
        crossing: float | None = None
        for k in range(1, steps + 1):
            u = k / steps
            x = start[0] + (lateral_end - start[0]) * (3 * u ** 2 - 2 * u ** 3)
            previous = self.points[-1]
            self.points.append((x, start[1] + distance * u))
            if crossing is None and (previous[0] - crossing_x) * (x - crossing_x) <= 0 and previous[0] != x:
                crossing = self.length()
        if crossing is None:
            raise MovementError("Lane-change path does not cross the declared lane boundary.")
        self.lane_changes.append((begin, self.length(), crossing))
        return self

    def build(self) -> Path2D:
        points = np.asarray(self.points, dtype=np.float64)
        lengths = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(points, axis=0).T))])
        return Path2D(points, lengths, tuple(self.turns), tuple(self.lane_changes))


def position_at(path: Path2D, progress: float) -> tuple[float, float]:
    """Linear interpolation along the dense path at a given arc length."""
    clipped = min(max(progress, 0.0), float(path.lengths[-1]))
    return float(np.interp(clipped, path.lengths, path.points[:, 0])), float(np.interp(clipped, path.lengths, path.points[:, 1]))


def progress_at(keyframes: tuple[Keyframe, ...], time: float) -> float | None:
    """Cubic Hermite progress between keyframes (exact for uniform braking); None outside the schedule."""
    if time < keyframes[0].time or time > keyframes[-1].time:
        return None
    for first, second in zip(keyframes, keyframes[1:]):
        if first.time <= time <= second.time:
            duration = second.time - first.time
            u = (time - first.time) / duration
            h00, h10, h01, h11 = 2 * u ** 3 - 3 * u ** 2 + 1, u ** 3 - 2 * u ** 2 + u, -2 * u ** 3 + 3 * u ** 2, u ** 3 - u ** 2
            return h00 * first.progress + h10 * duration * first.rate + h01 * second.progress + h11 * duration * second.rate
    return keyframes[-1].progress


def time_at_progress(keyframes: tuple[Keyframe, ...], progress: float) -> float:
    """First time a monotonic schedule reaches the given progress, to 1 ms."""
    times = np.minimum(np.arange(keyframes[0].time, keyframes[-1].time + 0.0005, 0.001), keyframes[-1].time)
    values = np.asarray([progress_at(keyframes, float(time)) for time in times])
    index = int(np.argmax(values >= progress - 1e-9))
    if values[index] < progress - 1e-9:
        raise MovementError(f"Schedule never reaches progress {progress}.")
    return float(times[index])


def schedule(start: float, total: float, rate: float, stops: tuple[tuple[float, float, float], ...]) -> tuple[Keyframe, ...]:
    """Cruise at a constant rate with uniform braking/launch into stops at (progress, braking distance, dwell)."""
    frames = [Keyframe(start, 0.0, rate)]
    for at, braking, dwell in stops:
        cruise_end = at - braking
        time = frames[-1].time + (cruise_end - frames[-1].progress) / rate
        frames.append(Keyframe(time, cruise_end, rate))
        stop_time = time + 2 * braking / rate
        frames.append(Keyframe(stop_time, at, 0.0))
        frames.append(Keyframe(stop_time + dwell, at, 0.0))
        frames.append(Keyframe(stop_time + dwell + 2 * braking / rate, at + braking, rate))
    frames.append(Keyframe(frames[-1].time + (total - frames[-1].progress) / rate, total, rate))
    return tuple(frames)


def scripted_road_users(seed: int) -> tuple[RoadUser, ...]:
    """Eight scripted participants whose timing and dwell vary with the seed."""
    rng = np.random.default_rng(seed)
    scale = lambda base: float(base * rng.uniform(0.85, 1.15))  # noqa: E731
    jitter = lambda base: float(base + rng.uniform(-0.5, 0.5))  # noqa: E731
    dwell = lambda: float(rng.uniform(2.0, 4.0))  # noqa: E731
    north_stop = PathBuilder((27.0, 1.0), 0.05).straight((27.0, 59.0)).build()
    rate_1 = scale(6.0)
    keys_1 = schedule(jitter(0.5), float(north_stop.lengths[-1]), rate_1, ((14.0, 8.0, dwell()),))
    east = PathBuilder((1.0, 27.0), 0.05).straight((59.0, 27.0)).build()
    rate_2 = scale(6.0)
    passing = time_at_progress(keys_1, 26.0) + float(rng.uniform(0.8, 1.4))
    keys_2 = schedule(passing - 26.0 / rate_2, float(east.lengths[-1]), rate_2, ())
    left = PathBuilder((27.0, 1.0), 0.05).straight((27.0, 27.0)).arc((21.0, 27.0), 6.0, 0.0, 90.0).straight((1.0, 33.0)).build()
    right = PathBuilder((33.0, 59.0), 0.05).straight((33.0, 39.0)).arc((27.0, 39.0), 6.0, 0.0, -90.0).straight((1.0, 33.0)).build()
    change = PathBuilder((27.0, 1.0), 0.05).straight((27.0, 5.0)).lane_change_north(33.0, 12.0, 30.0).straight((33.0, 59.0)).build()
    west = PathBuilder((59.0, 33.0), 0.05).straight((1.0, 33.0)).build()
    walk = PathBuilder((12.0, 19.0), 0.05).straight((48.0, 19.0)).build()
    bus = PathBuilder((59.0, 33.0), 0.05).straight((1.0, 33.0)).build()
    left_start, motorcycle_start = jitter(16.0), jitter(20.0)
    rate_6 = scale(7.0)
    users = (
        RoadUser(1, "car", "north approach, stop at stop line, continue straight", north_stop, keys_1, None, None),
        RoadUser(2, "car", "eastbound straight, crosses behind road user 1", east, keys_2, None, None),
        RoadUser(3, "car", "north approach, left turn into westbound lane", left, schedule(left_start, float(left.lengths[-1]), scale(5.0), ()), None, (left_start + 1.0, left_start + 1.12)),
        RoadUser(4, "truck", "southbound, right turn into westbound lane", right, schedule(jitter(3.0), float(right.lengths[-1]), scale(5.0), ()), None, None),
        RoadUser(5, "car", "north approach, lane change from lane 1 to lane 2", change, schedule(jitter(23.0), float(change.lengths[-1]), scale(6.0), ()), None, None),
        RoadUser(6, "motorcycle", "westbound straight with a 1 s occlusion", west, schedule(motorcycle_start, float(west.lengths[-1]), rate_6, ()), (motorcycle_start + 4.0, motorcycle_start + 5.0), None),
        RoadUser(7, "person", "pedestrian waits at the kerb, then uses the crossing", walk, schedule(jitter(1.0), float(walk.lengths[-1]), scale(1.4), ((10.5, 0.6, dwell()),)), None, None),
        RoadUser(8, "bus", "westbound, abrupt visible stop before the junction, then continues", bus, schedule(jitter(27.0), float(bus.lengths[-1]), scale(5.0), ((14.0, 4.0, dwell()),)), None, None),
    )
    return users


def fixture_homography() -> NDArray[np.float64]:
    """World-to-image homography shared by every staged fixture."""
    controls = parse_correspondence_rows(CONTROL_ROWS, "control")
    inverse, _ = cv2.findHomography(
        np.asarray([(point.world_x, point.world_y) for point in controls], dtype=np.float64),
        np.asarray([(point.image_x, point.image_y) for point in controls], dtype=np.float64),
    )
    if inverse is None:
        raise MovementError("Staged fixture homography could not be constructed.")
    return inverse


def visible_world(user: RoadUser, time: float) -> tuple[float, float] | None:
    """True road-plane contact point when the road user is scheduled and inside the drawn scene."""
    progress = progress_at(user.keyframes, time)
    if progress is None:
        return None
    world = position_at(user.path, progress)
    return world if 0.5 <= world[0] <= 59.5 and 0.5 <= world[1] <= 59.5 else None


def box_geometry(homography: NDArray[np.float64], user: RoadUser, world: tuple[float, float]) -> tuple[float, float, float, float]:
    """Projected contact point (bottom-centre) and the noise-free box width and height, in pixels."""
    px, py = _image_point(homography, world)
    size = 9 + (60 - world[1]) / 8
    width, height = (size * 0.45, size * 1.6) if user.class_name == "person" else (size * 1.4, size)
    return px, py, width * user.box_scale[0], height * user.box_scale[1]


def true_box(homography: NDArray[np.float64], user: RoadUser, world: tuple[float, float]) -> tuple[float, float, float, float]:
    """Noise-free image box whose bottom-centre is the projected contact point."""
    px, py, width, height = box_geometry(homography, user, world)
    return px - width / 2, py - height, px + width / 2, py


def create_staged_case(cases_root: Path, case_id: str, seed: int, pixel_noise_sigma: float, duration_seconds: int, ffmpeg: str, ffprobe: str) -> StagedCase:
    """Render, ingest, track, calibrate, reconstruct, and annotate one staged case."""
    require_identifier(case_id, "staged case ID")
    if not 20 <= duration_seconds <= 60 or not 0 <= pixel_noise_sigma <= 5:
        raise MovementError("Staged case requires 20–60 seconds and pixel noise between 0 and 5 px.")
    users = scripted_road_users(seed)
    rendered = render_staged_case(cases_root, case_id, users, seed, pixel_noise_sigma, duration_seconds * FPS, RECONSTRUCTION_RUN, ffmpeg, ffprobe, LANES)
    truth = _ground_truth(case_id, seed, pixel_noise_sigma, users, rendered.positions)
    destination = ground_truth_path(cases_root, case_id)
    destination.parent.mkdir(parents=True)
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(truth.model_dump_json(indent=2) + "\n")
    return StagedCase(case_id, rendered.reconstruction_directory, destination)


def render_staged_case(
    cases_root: Path, case_id: str, users: tuple[RoadUser, ...], seed: int, pixel_noise_sigma: float,
    frame_count: int, reconstruction_run: str, ffmpeg: str, ffprobe: str, lanes: dict[str, tuple[tuple[float, float], ...]],
) -> RenderedCase:
    """Render scripted road users on the staged intersection, then ingest, track, calibrate, and reconstruct."""
    require_identifier(case_id, "staged case ID")
    cases_root.mkdir(parents=True, exist_ok=True)
    fixture_path = cases_root / f"{case_id}-staged-synthetic.avi"
    if fixture_path.exists() or (cases_root / case_id).exists():
        raise MovementError(f"Staged case '{case_id}' already exists; choose a new ID.")
    controls = parse_correspondence_rows(CONTROL_ROWS, "control")
    inverse = fixture_homography()
    to_image = lambda point: _image_point(inverse, point)  # noqa: E731
    validation_pixel = to_image((18.0, 32.0))
    validation = parse_correspondence_rows(f"{validation_pixel[0]},{validation_pixel[1]},18,32", "validation")
    features = (
        *(parse_road_feature(lane_id, f"Staged {lane_id}", "lane", "\n".join(f"{x},{y}" for x, y in map(to_image, polygon))) for lane_id, polygon in lanes.items()),
        parse_road_feature("stop-1", "Northbound stop line", "stop_line", "\n".join(f"{x},{y}" for x, y in map(to_image, STOP_LINE))),
        parse_road_feature("crossing-1", "Pedestrian crossing", "crossing", "\n".join(f"{x},{y}" for x, y in map(to_image, CROSSWALK))),
    )
    rng = np.random.default_rng(seed + 10_000)
    records: list[TrackRecord] = []
    truth_positions: dict[int, list[tuple[int, float, float]]] = {user.track_id: [] for user in users}
    writer = cv2.VideoWriter(str(fixture_path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (WIDTH, HEIGHT))
    if not writer.isOpened():
        raise MovementError(f"Cannot create staged fixture video '{fixture_path}'.")
    try:
        pending: dict[int, list[tuple[int, float, tuple[float, float, float, float] | None]]] = {user.track_id: [] for user in users}
        for frame in range(frame_count):
            time = frame / FPS
            image = np.full((HEIGHT, WIDTH, 3), (178, 192, 171), dtype=np.uint8)
            for polygon in lanes.values():
                vertices = np.asarray([to_image(point) for point in polygon], dtype=np.int32)
                cv2.fillPoly(image, [vertices], (73, 78, 80))
                cv2.polylines(image, [vertices], True, (230, 227, 211), 1, cv2.LINE_AA)
            cv2.polylines(image, [np.asarray([to_image(point) for point in CROSSWALK], dtype=np.int32)], True, (240, 240, 240), 1, cv2.LINE_AA)
            cv2.line(image, tuple(round(v) for v in to_image(STOP_LINE[0])), tuple(round(v) for v in to_image(STOP_LINE[1])), (250, 250, 250), 2, cv2.LINE_AA)
            for user in users:
                world = visible_world(user, time)
                if world is None:
                    continue
                truth_positions[user.track_id].append((frame, world[0], world[1]))
                px, py, width, height = box_geometry(inverse, user, world)
                box_true = (px - width / 2, py - height, px + width / 2, py)
                color = (int((user.track_id * 41 + 100) % 180), int((user.track_id * 73 + 90) % 180), int((user.track_id * 53 + 160) % 180))
                occluded = user.lost is not None and user.lost[0] <= time < user.lost[1]
                if not occluded:
                    cv2.rectangle(image, (round(box_true[0]), round(box_true[1])), (round(box_true[2]), round(box_true[3])), color, -1)
                dx, dy, dh = rng.normal(0.0, pixel_noise_sigma, 3) if pixel_noise_sigma > 0 else (0.0, 0.0, 0.0)
                noisy_height = max(3.0, height + dh * 0.5)
                box = (max(0.0, px + dx - width / 2), max(0.0, py + dy - noisy_height), min(WIDTH - 1.0, px + dx + width / 2), min(HEIGHT - 1.0, py + dy))
                pending[user.track_id].append((frame, time, None if occluded else box))
            cv2.rectangle(image, (0, 0), (WIDTH, 40), (33, 44, 49), -1)
            cv2.putText(image, "CONTROLLED SYNTHETIC STAGED MOTION / NOT REAL CCTV", (12, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (245, 245, 235), 1, cv2.LINE_AA)
            writer.write(image)
    finally:
        writer.release()
    for user in users:
        records.extend(_track_records(case_id, user, pending[user.track_id]))
    records.sort(key=lambda record: (record.frame_number, record.track_id))
    ingested = ingest_case(fixture_path, cases_root, case_id, ffprobe)
    run_directory = ingested.case_directory / "analysis" / "tracks" / TRACKING_RUN
    run_directory.mkdir(parents=True)
    tracks_path = run_directory / "tracks.jsonl"
    with tracks_path.open("x") as stream:
        for record in records:
            stream.write(json.dumps(asdict(record)) + "\n")
    (run_directory / "run_manifest.json").write_text(json.dumps({
        "tracks_sha256": sha256_file(tracks_path), "fixture_kind": "controlled synthetic staged motion with seeded pixel noise",
        "seed": seed, "pixel_noise_sigma": pixel_noise_sigma,
    }) + "\n")
    calibration = build_calibration(case_id, ingested.manifest.preserved_sha256, WIDTH, HEIGHT, "metric", "m", controls, validation, 0.01, features)
    capture = cv2.VideoCapture(str(ingested.preserved_video_path))
    try:
        success, image = capture.read()
        if not success:
            raise MovementError("Cannot read the staged calibration reference frame.")
    finally:
        capture.release()
    calibration_directory = save_calibration_revision(ingested.case_directory, calibration, image)
    tracks = automatic_track_input(cases_root, case_id, TRACKING_RUN)
    reconstruction = prepare_reconstruction(cases_root.resolve(), case_id, tracks, calibration_directory / "calibration.json", reconstruction_run, ffmpeg)
    return RenderedCase(ingested.case_directory, reconstruction, truth_positions)


def _track_records(case_id: str, user: RoadUser, samples: list[tuple[int, float, tuple[float, float, float, float] | None]]) -> list[TrackRecord]:
    """Tracker-style states: occlusion becomes 'lost'; a short span becomes linearly 'interpolated'."""
    records: list[TrackRecord] = []
    boxes = {frame: box for frame, _, box in samples}
    for age, (frame, time, box) in enumerate(samples, 1):
        state = "observed"
        if box is None:
            state = "lost"
        elif user.interpolated is not None and user.interpolated[0] <= time < user.interpolated[1]:
            before = max(candidate for candidate in boxes if candidate < frame and not (user.interpolated[0] <= candidate / FPS < user.interpolated[1]))
            after = min(candidate for candidate in boxes if candidate > frame and not (user.interpolated[0] <= candidate / FPS < user.interpolated[1]))
            weight = (frame - before) / (after - before)
            box = tuple(a + (b - a) * weight for a, b in zip(boxes[before], boxes[after], strict=True))
            state = "interpolated"
        elif records and records[-1].state == "lost":
            state = "recovered"
        gap = records[-1].gap_length_frames + 1 if state == "lost" and records and records[-1].state == "lost" else 1 if state == "lost" else 0
        records.append(TrackRecord(
            case_id=case_id, run_id=TRACKING_RUN, frame_number=frame, timestamp_seconds=frame / FPS, track_id=user.track_id,
            class_id=CLASS_IDS[user.class_name], class_name=user.class_name, confidence=None if box is None else 0.9,
            bbox_x1=None if box is None else box[0], bbox_y1=None if box is None else box[1],
            bbox_x2=None if box is None else box[2], bbox_y2=None if box is None else box[3],
            state=state, track_age_frames=age, gap_length_frames=gap, schema_version="1.0",
        ))
    return records


def _ground_truth(case_id: str, seed: int, noise: float, users: tuple[RoadUser, ...], positions: dict[int, list[tuple[int, float, float]]]) -> MovementGroundTruth:
    events: list[GroundTruthEvent] = []
    stop_line, crosswalk = LineString(STOP_LINE), Polygon(CROSSWALK)
    lanes = {lane_id: Polygon(polygon) for lane_id, polygon in LANES.items()}
    sequences: dict[str, tuple[str, ...]] = {}
    for user in users:
        visible = positions[user.track_id]
        first_time, last_time = visible[0][0] / FPS, visible[-1][0] / FPS
        def add(event_type: str, peak: float, start: float | None, end: float | None, note: str, from_feature: str | None, to_feature: str | None) -> None:
            if first_time <= peak <= last_time:
                events.append(GroundTruthEvent(event_id=f"gt-{event_type}-t{user.track_id}-{len(events) + 1}", event_type=event_type, track_ids=(user.track_id,),
                                               start_time=start, peak_time=peak, end_time=end, from_feature=from_feature, to_feature=to_feature, note=note))
        for first, second in zip(user.keyframes, user.keyframes[1:]):
            if first.rate == 0 and second.rate == 0 and first.progress == second.progress:
                add("stop", first.time, first.time, second.time, f"comes to rest at progress {first.progress:.1f} m", None, None)
                add("start", second.time, first.time, second.time, "visible movement resumes", None, None)
        for begin, end, direction in user.path.turns:
            start, finish = time_at_progress(user.keyframes, begin), time_at_progress(user.keyframes, end)
            add(direction, time_at_progress(user.keyframes, (begin + end) / 2), start, finish, "constant-radius arc; peak at half the heading change", None, None)
        for begin, end, crossing in user.path.lane_changes:
            add("lane_change", time_at_progress(user.keyframes, crossing), time_at_progress(user.keyframes, begin), time_at_progress(user.keyframes, end),
                "contact point crosses the shared lane boundary", "lane-1", "lane-2")
        samples = np.arange(user.keyframes[0].time, user.keyframes[-1].time, 0.002)
        trace = [(float(time), position_at(user.path, progress_at(user.keyframes, float(time)))) for time in samples]
        for (t0, p0), (_, p1) in zip(trace, trace[1:]):
            if p0 != p1 and LineString((p0, p1)).intersects(stop_line):
                add("stop_line_crossing", t0, None, None, "contact point crosses stop-1", "stop-1", None)
        inside = [crosswalk.contains(ShapelyPoint(point)) for _, point in trace]
        for index in range(1, len(trace)):
            if inside[index] != inside[index - 1]:
                kind = "zone_entry" if inside[index] else "zone_exit"
                add(kind, trace[index][0], None, None, f"contact point {'enters' if inside[index] else 'leaves'} crossing-1",
                    None if inside[index] else "crossing-1", "crossing-1" if inside[index] else None)
        if user.class_name in VEHICLES:
            labels: list[str | None] = []
            for _, x, y in visible:
                containing = [lane_id for lane_id, polygon in lanes.items() if polygon.contains(ShapelyPoint(x, y))]
                labels.append(containing[0] if len(containing) == 1 else None)
            sequence: list[str] = []
            for index, label in enumerate(labels):
                if label is not None and labels[index:index + 5] == [label] * 5 and (not sequence or sequence[-1] != label):
                    sequence.append(label)
            sequences[str(user.track_id)] = tuple(sequence)
    for first, second in combinations(users, 2):
        common = sorted({frame for frame, _, _ in positions[first.track_id]} & {frame for frame, _, _ in positions[second.track_id]})
        lost = lambda user, frame: user.lost is not None and user.lost[0] <= frame / FPS < user.lost[1]  # noqa: E731
        common = [frame for frame in common if not lost(first, frame) and not lost(second, frame)]
        if len(common) < 10:
            continue
        lookup_a = {frame: (x, y) for frame, x, y in positions[first.track_id]}
        lookup_b = {frame: (x, y) for frame, x, y in positions[second.track_id]}
        distances = [math.dist(lookup_a[frame], lookup_b[frame]) for frame in common]
        index = int(np.argmin(distances))
        if 0 < index < len(common) - 1 and distances[0] > distances[index] + 1.0 and distances[-1] > distances[index] + 1.0:
            events.append(GroundTruthEvent(event_id=f"gt-closest_approach-t{first.track_id}-{second.track_id}", event_type="closest_approach",
                                           track_ids=(first.track_id, second.track_id), start_time=None, peak_time=common[index] / FPS, end_time=None,
                                           from_feature=None, to_feature=None, note=f"true minimum contact-point separation {distances[index]:.1f} m"))
    return MovementGroundTruth(
        schema_version="1.0", case_id=case_id, source="controlled_synthetic", annotator="scripted generator (exact kinematics)", unit="m",
        frame_rate=float(FPS),
        annotated_event_types=("stop", "start", "turn_left", "turn_right", "lane_change", "stop_line_crossing", "zone_entry", "zone_exit", "closest_approach"),
        tracks=tuple(GroundTruthTrack(track_id=user.track_id, class_name=user.class_name, scenario=user.scenario,
                                      positions=tuple(item for item in positions[user.track_id] if not (user.lost is not None and user.lost[0] <= item[0] / FPS < user.lost[1])))
                     for user in users),
        events=tuple(sorted(events, key=lambda item: item.peak_time)), lane_sequences=sequences,
        notes=(f"seed={seed}", f"pixel_noise_sigma={noise}", "Synthetic rectangles on a drawn road; not real CCTV and not a detector benchmark.",
               "Closest approaches are annotated only when the true separation falls and rises by more than 1 m around an interior minimum."),
    )


def _image_point(homography: NDArray[np.float64], point: tuple[float, float]) -> tuple[float, float]:
    projected = cv2.perspectiveTransform(np.asarray([[point]], dtype=np.float64), homography)[0, 0]
    return float(projected[0]), float(projected[1])
