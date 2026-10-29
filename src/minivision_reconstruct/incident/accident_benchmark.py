"""ACCIDENT synthetic (CARLA) subset for Week 8: a stratified, scene-disjoint selection and its registry rows.

Selection is deterministic for a seed. Each camera view (map + camera_position) belongs to exactly one split and
contributes at most one clip, so dev and held-out never share a viewpoint. Within each collision type, clips are
spread across weather conditions. Clips are downloaded by hand from Kaggle; registration hashes what is on disk.

Calibration uses only the camera pose stored with each clip (no labels): CARLA annotations carry the sensor's world
location and rotation but no intrinsics, and a 90-degree field of view reproduces the parked cars of dev clip
Town03_head-on_rain_30 within 6.4 px. Moving vehicles carry no world positions, so the §11.2 table scores impact
time, image-space location, collision type, and the colliding pair, never metres.
"""

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from minivision_reconstruct.calibration import build_calibration
from minivision_reconstruct.domain.models import CalibrationDocument, CalibrationPoint
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.incident.models import IncidentConfig, IncidentError, IncidentEvent
from minivision_reconstruct.movement.validation import _summary, _write_csv
from minivision_reconstruct.reconstruction.storage import read_records


SOURCE_URL = "https://www.kaggle.com/datasets/picekl/accident"
SELECTION_COLUMNS = (
    "local_video_id", "split", "type", "weather", "map", "camera_position", "rgb_path", "annotations_path",
    "accident_time", "accident_frame", "center_x", "center_y", "x1", "y1", "x2", "y2", "no_frames", "duration", "width", "height",
)
REGISTRY_COLUMNS = (
    "dataset_name", "dataset_version", "source_url", "licence", "retrieved_at", "local_video_id", "source_video_id", "split",
    "development_allowed", "labels_used_for_tuning", "local_relative_path", "sha256", "file_size_bytes", "duration_seconds",
    "frames_per_second", "width", "height", "annotation_path", "purpose", "privacy_notes",
)


REAL_PRIVACY = "Public CCTV from YouTube via ACCIDENT; may show plates; research use only; Git-ignored"
# split -> (held out, purpose, privacy note); held-out rows are registered as not allowed for development.
SPLIT_POLICY: dict[str, tuple[bool, str, str]] = {
    "week8_dev": (False, "Week 8 dev sanity check only; never used to tune thresholds", "Synthetic CARLA rendering; Git-ignored"),
    "week8_heldout": (True, "Week 8 held-out impact time, location, and type evaluation (frozen config only)", "Synthetic CARLA rendering; Git-ignored"),
    "real_dev": (False, "Real-footage development: baseline measurement and tuning", REAL_PRIVACY),
    "real_test": (True, "Locked real-footage test set: run once with a frozen configuration; never used for tuning", REAL_PRIVACY),
}
REAL_SELECTION_COLUMNS = (
    "local_video_id", "split", "type", "rollover", "day_time", "weather", "region", "scene_layout", "quality", "rgb_path", "annotations_path",
    "accident_time", "accident_frame", "center_x", "center_y", "x1", "y1", "x2", "y2", "no_frames", "duration", "width", "height", "official_split",
)


@dataclass(frozen=True)
class SubsetPlan:
    per_type_dev: int
    per_type_heldout: int
    seed: int


def _scene_split(scene: str, seed: int, heldout_fraction: float) -> str:
    """Stable split for one camera view, independent of which clips are later drawn from it."""
    digest = hashlib.sha256(f"{seed}:{scene}".encode()).digest()
    return "week8_heldout" if int.from_bytes(digest[:8], "big") / 2 ** 64 < heldout_fraction else "week8_dev"


def select_subset(manifest_path: Path, plan: SubsetPlan) -> list[dict[str, str]]:
    """Per collision type: dev and held-out clips from distinct scenes, round-robin across weather."""
    with manifest_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise IncidentError(f"ACCIDENT manifest '{manifest_path}' is empty.")
    fraction = plan.per_type_heldout / (plan.per_type_dev + plan.per_type_heldout)
    rng = np.random.default_rng(plan.seed)
    used_scenes: set[str] = set()
    selected: list[dict[str, str]] = []
    for type_index, collision_type in enumerate(sorted({row["type"] for row in rows})):
        for split, wanted in (("week8_dev", plan.per_type_dev), ("week8_heldout", plan.per_type_heldout)):
            pool = [row for row in rows if row["type"] == collision_type and _scene_split(f"{row['map']}/{row['camera_position']}", plan.seed, fraction) == split]
            by_weather: dict[str, list[dict[str, str]]] = {}
            for row in (pool[index] for index in rng.permutation(len(pool))):
                by_weather.setdefault(row["weather"], []).append(row)
            picked: list[dict[str, str]] = []
            weathers = sorted(by_weather)
            # Rotate the starting weather per type so small dev quotas still cover every condition across types.
            weathers = weathers[type_index % len(weathers):] + weathers[:type_index % len(weathers)] if weathers else weathers
            while len(picked) < wanted and any(by_weather.values()):
                for weather in weathers:
                    while by_weather[weather] and f"{by_weather[weather][0]['map']}/{by_weather[weather][0]['camera_position']}" in used_scenes:
                        by_weather[weather].pop(0)
                    if by_weather[weather] and len(picked) < wanted:
                        row = by_weather[weather].pop(0)
                        used_scenes.add(f"{row['map']}/{row['camera_position']}")
                        picked.append(row)
            if len(picked) < wanted:
                raise IncidentError(f"Only {len(picked)} scene-disjoint '{collision_type}' clips available for {split}; {wanted} requested.")
            selected.extend({"local_video_id": Path(row["rgb_path"]).stem, "split": split, **{key: row[key] for key in SELECTION_COLUMNS[2:]}} for row in picked)
    return selected


@dataclass(frozen=True)
class RealSubsetPlan:
    per_type_dev: int
    per_type_test: int
    min_height: int
    seed: int
    exclude: frozenset[str]


def _source(path: str) -> str:
    """ACCIDENT real clips are named <YouTube ID>[_<segment>]_<clip>.mp4; the YouTube video (11 characters) stands for the camera."""
    return Path(path).stem.rsplit("_", 1)[0][:11]


def select_real_subset(manifest_path: Path, plan: RealSubsetPlan) -> list[dict[str, str]]:
    """Real clips per collision type: development from the official geo-aware train split, test from its test split.

    The geo-aware split puts disjoint regions on each side, so development and test cameras differ. Sources found in
    both official splits, already-inspected clips (and their sources), and clips below the minimum height are
    dropped. Each source contributes at most one clip, spread across day/night and weather.
    """
    with manifest_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise IncidentError(f"ACCIDENT manifest '{manifest_path}' is empty.")
    sides: dict[str, set[str]] = {}
    for row in rows:
        sides.setdefault(_source(row["path"]), set()).add(row["split_geo_aware"])
    excluded_sources = {_source(clip) for clip in plan.exclude}
    eligible = [row for row in rows if len(sides[_source(row["path"])]) == 1 and _source(row["path"]) not in excluded_sources
                and Path(row["path"]).stem not in plan.exclude and int(row["height"]) >= plan.min_height]
    rng = np.random.default_rng(plan.seed)
    used: set[str] = set()
    selected: list[dict[str, str]] = []
    for type_index, collision_type in enumerate(sorted({row["type"] for row in rows})):
        for split, official, wanted in (("real_dev", "train", plan.per_type_dev), ("real_test", "test", plan.per_type_test)):
            pool = [row for row in eligible if row["type"] == collision_type and row["split_geo_aware"] == official]
            strata: dict[tuple[str, str], list[dict[str, str]]] = {}
            for row in (pool[index] for index in rng.permutation(len(pool))):
                strata.setdefault((row["day_time"], row["weather"]), []).append(row)
            keys = sorted(strata)
            keys = keys[type_index % len(keys):] + keys[:type_index % len(keys)] if keys else keys
            picked: list[dict[str, str]] = []
            while len(picked) < wanted and any(strata.values()):
                for key in keys:
                    while strata[key] and _source(strata[key][0]["path"]) in used:
                        strata[key].pop(0)
                    if strata[key] and len(picked) < wanted:
                        row = strata[key].pop(0)
                        used.add(_source(row["path"]))
                        picked.append(row)
            if len(picked) < wanted:
                raise IncidentError(f"Only {len(picked)} eligible '{collision_type}' clips available for {split}; {wanted} requested.")
            selected.extend({
                "local_video_id": Path(row["path"]).stem, "split": split, "rgb_path": row["path"],
                # Real clips carry their labels in the manifest row itself, relative to the data root.
                "annotations_path": f"manifests/{manifest_path.name}",
                "official_split": row["split_geo_aware"],
                **{key: row[key] for key in REAL_SELECTION_COLUMNS if key in row},
            } for row in picked)
    return selected


def write_selection(rows: list[dict[str, str]], destination: Path) -> None:
    if destination.exists():
        raise IncidentError(f"Selection '{destination}' already exists; it records what was chosen and is not overwritten.")
    with destination.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REAL_SELECTION_COLUMNS if "official_split" in rows[0] else SELECTION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def register_subset(selection_path: Path, data_root: Path, registry_path: Path, project_root: Path, retrieved_at: str) -> tuple[list[str], list[str]]:
    """Append registry rows for downloaded clips; returns (registered IDs, still missing IDs). Existing rows are kept."""
    with selection_path.open(encoding="utf-8", newline="") as stream:
        selection = list(csv.DictReader(stream))
    with registry_path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != REGISTRY_COLUMNS:
            raise IncidentError(f"Registry '{registry_path}' has unexpected columns {reader.fieldnames}.")
        known = {row["local_video_id"] for row in reader}
    registered, missing, new_rows = [], [], []
    for row in selection:
        video, annotation = data_root / row["rgb_path"], data_root / row["annotations_path"]
        if not annotation.is_file() and annotation.name.endswith(".json.gz"):
            # Kaggle serves the annotation uncompressed; the registry records the file actually on disk.
            annotation = annotation.with_suffix("")
        if row["local_video_id"] in known:
            continue
        if not video.is_file() or not annotation.is_file():
            missing.append(row["local_video_id"])
            continue
        if row["split"] not in SPLIT_POLICY:
            raise IncidentError(f"Unknown split '{row['split']}' for '{row['local_video_id']}'.")
        heldout, purpose, privacy = SPLIT_POLICY[row["split"]]
        new_rows.append({
            "dataset_name": "ACCIDENT", "dataset_version": f"Kaggle-retrieved-{retrieved_at}", "source_url": SOURCE_URL, "licence": "CC-BY-NC-SA-4.0",
            "retrieved_at": retrieved_at, "local_video_id": row["local_video_id"], "source_video_id": row["local_video_id"], "split": row["split"],
            "development_allowed": str(not heldout).lower(), "labels_used_for_tuning": "false",
            "local_relative_path": str(video.resolve().relative_to(project_root.resolve())), "sha256": sha256_file(video),
            "file_size_bytes": str(video.stat().st_size), "duration_seconds": row["duration"],
            "frames_per_second": f"{float(row['no_frames']) / float(row['duration']):.6f}", "width": row["width"], "height": row["height"],
            "annotation_path": str(annotation.resolve().relative_to(project_root.resolve())),
            "purpose": purpose, "privacy_notes": privacy,
        })
        registered.append(row["local_video_id"])
    with registry_path.open("a", encoding="utf-8", newline="") as stream:
        csv.DictWriter(stream, fieldnames=REGISTRY_COLUMNS).writerows(new_rows)
    return registered, missing


# --- Calibration from the CARLA camera pose ---

@dataclass(frozen=True)
class CameraModel:
    """Assumptions shared by every clip; recorded in each calibration's ASSUMPTIONS note."""

    fov_degrees: float
    ground_z: float
    max_range: float


def _carla_rotation(pitch: float, yaw: float, roll: float) -> np.ndarray:
    """Camera-local (x forward, y right, z up) to CARLA world, as carla.Transform.get_matrix."""
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cr, sr = math.cos(math.radians(roll)), math.sin(math.radians(roll))
    return np.array([[cp * cy, cy * sp * sr - sy * cr, -cy * sp * cr - sy * sr],
                     [cp * sy, sy * sp * sr + cy * cr, -sy * sp * cr + cy * sr],
                     [sp, -cp * sr, cp * cr]])


def carla_ground_points(annotation_path: Path, width: int, height: int, model: CameraModel) -> list[tuple[float, float, float, float]]:
    """Image grid points whose camera ray meets the road plane within range, as (u, v, x, y) in a right-handed local frame.

    CARLA's world is left-handed, so y is negated; the origin is the camera's ground nadir.
    """
    sensor = json.loads(annotation_path.read_text(encoding="utf-8"))["sensor"]
    rotation = _carla_rotation(**sensor["rotation"])
    camera = np.array([sensor["location"][axis] for axis in "xyz"])
    if camera[2] <= model.ground_z:
        raise IncidentError(f"Camera in '{annotation_path.name}' is not above the assumed road plane z = {model.ground_z}.")
    focal = width / (2 * math.tan(math.radians(model.fov_degrees) / 2))
    points = []
    for v in np.linspace(0.05, 0.95, 10) * height:
        for u in np.linspace(0.05, 0.95, 10) * width:
            ray = rotation @ np.array([1.0, (u - width / 2) / focal, -(v - height / 2) / focal])
            if ray[2] >= 0:
                continue
            ground = camera + (model.ground_z - camera[2]) / ray[2] * ray
            if math.hypot(*(ground[:2] - camera[:2])) <= model.max_range:
                points.append((float(u), float(v), float(ground[0] - camera[0]), float(camera[1] - ground[1])))
    return points


def carla_calibration(annotation_path: Path, case_id: str, video_sha256: str, width: int, height: int, model: CameraModel) -> CalibrationDocument:
    """Metric homography from the stored camera pose: the four extreme in-range grid points fit, the rest validate.

    The validation points check only the planar fit of this camera model; the model itself (FOV, flat road) was
    checked once on a dev clip. No road features are defined: the annotations carry no lane geometry.
    """
    points = carla_ground_points(annotation_path, width, height, model)
    if len(points) < 6:
        raise IncidentError(f"Only {len(points)} image grid points of '{annotation_path.name}' reach the road within {model.max_range} m.")
    corners = {max(points, key=key) for key in (lambda p: p[0] + p[1], lambda p: p[0] - p[1], lambda p: -p[0] + p[1], lambda p: -p[0] - p[1])}
    if len(corners) < 4:
        raise IncidentError(f"The in-range road region of '{annotation_path.name}' is degenerate.")
    control = tuple(CalibrationPoint(f"control-{index}", u, v, x, y, "control") for index, (u, v, x, y) in enumerate(sorted(corners), 1))
    validation = tuple(CalibrationPoint(f"validation-{index}", u, v, x, y, "validation")
                       for index, (u, v, x, y) in enumerate(sorted(set(points) - corners), 1))
    return build_calibration(case_id, video_sha256, width, height, "metric", "m", control, validation, 0.5, ())


def calibration_assumptions(annotation_path: Path, model: CameraModel, calibration: CalibrationDocument) -> str:
    sensor = json.loads(annotation_path.read_text(encoding="utf-8"))["sensor"]
    location, rotation = sensor["location"], sensor["rotation"]
    return (
        f"# Calibration {calibration.calibration_id} assumptions (metric, from the CARLA camera pose)\n\n"
        f"- Source: `sensor` block of `{annotation_path.name}` (camera metadata only; no object or collision labels used).\n"
        f"- Camera at world ({location['x']:.2f}, {location['y']:.2f}, {location['z']:.2f}) m, pitch {rotation['pitch']:.2f}, "
        f"yaw {rotation['yaw']:.2f}, roll {rotation['roll']:.2f} degrees.\n"
        f"- Field of view assumed {model.fov_degrees:g} degrees (CARLA default; not stored in the annotation). Checked once on dev clip "
        "Town03_head-on_rain_30: parked cars project within 6.4 px of their labelled boxes; 80 or 100 degrees give 40-160 px.\n"
        f"- Road assumed flat at world z = {model.ground_z:g} m. Hills or bridges change the scale by about (road height / camera height).\n"
        f"- Only image points whose ray meets the road within {model.max_range:g} m are used. World frame: metres, origin at the camera's "
        "ground nadir, y negated so the frame is right-handed.\n"
        f"- Validation RMSE {calibration.validation_rmse:.4f} m over {len(calibration.validation_points)} grid points checks only the planar fit "
        "of this camera model, not the model itself.\n"
        "- No lanes, stop lines, or crossings are defined (the annotations carry no road geometry), so lane-based signals and rules cannot fire.\n"
        f"- Created {datetime.now(UTC).date().isoformat()} by the Week 8 ACCIDENT benchmark preparation.\n"
    )


# --- §11.2 result table ---

FAILURE_SUFFIX = "-FAILED.txt"
LABEL_TYPES = {"rear-end": "rear_end", "head-on": "head_on", "sideswipe": "sideswipe", "t-bone": "t_bone", "single": "single"}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
RESULT_COLUMNS = (
    "local_video_id", "split", "label_type", "weather", "map", "label_time", "label_frame",
    "proposed", "event_id", "event_type", "confidence", "predicted_time", "impact_time_error_s", "within_time_tolerance",
    "predicted_center_x", "predicted_center_y", "spatial_error_normalised", "inside_label_box",
    "predicted_type", "type_correct", "pair_correct", "collision_candidates", "false_alarms",
    "near_misses", "near_miss_at_impact", "right_pair_near_miss_at_impact", "actors_tracked_at_impact", "actors_same_id_after_window", "pipeline_failure", "config_id", "pipeline_version",
)


def primary_candidate(events: list[IncidentEvent]) -> IncidentEvent | None:
    """Label-blind choice: the most confident collision or single-vehicle candidate, earliest peak first on ties."""
    candidates = [event for event in events if event.event_type != "near_miss_candidate"]
    return min(candidates, key=lambda event: (CONFIDENCE_RANK[event.confidence], event.peak_time), default=None)


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    width, height = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def _track_box(tracks: dict[int, dict[int, tuple[float, float, float, float]]], track_id: int, frame: int, reach: int) -> tuple[float, float, float, float] | None:
    """The track's box at the frame, or the nearest boxed frame within reach (tracks are often lost at impact)."""
    boxes = tracks.get(track_id, {})
    for offset in sorted(range(-reach, reach + 1), key=abs):
        if frame + offset in boxes:
            return boxes[frame + offset]
    return None


def pair_correct(event: IncidentEvent, annotation: dict, tracks: dict[int, dict[int, tuple[float, float, float, float]]], minimum_iou: float) -> bool | None:
    """Do the proposed road users match the labelled colliding actors' boxes at the proposed peak? None if unmeasurable."""
    first = annotation["base"][0]["iteration"]
    frames = {record["iteration"] - first: {obj["id"]: obj["2d_bbox"] for obj in record["objects"]} for record in annotation["base"]}
    labelled = annotation["collision"][0]["ids"] if annotation["collision"] else []
    objects = frames.get(event.peak_frame, {})
    truth = [tuple(float(value) for corner in objects[actor] for value in corner) for actor in labelled if actor in objects]
    proposed = [_track_box(tracks, track_id, event.peak_frame, 5) for track_id in event.track_ids]
    if not truth or any(box is None for box in proposed):
        return None
    if len(proposed) == 1:
        return any(_iou(proposed[0], box) >= minimum_iou for box in truth)
    if len(truth) < 2:
        return False
    return max(min(_iou(proposed[0], truth[i]), _iou(proposed[1], truth[1 - i])) for i in (0, 1)) >= minimum_iou


def tracking_at_impact(annotation: dict, tracks: dict[int, dict[int, tuple[float, float, float, float]]], impact_frame: int,
                       after_frames: int, minimum_iou: float) -> tuple[int, int]:
    """Diagnostic: (labelled colliding actors matched by a track at impact, of those, how many keep the same track ID after_frames later)."""
    first = annotation["base"][0]["iteration"]
    frames = {record["iteration"] - first: {obj["id"]: obj["2d_bbox"] for obj in record["objects"]} for record in annotation["base"]}
    labelled = annotation["collision"][0]["ids"] if annotation["collision"] else []
    objects = frames.get(impact_frame, {})
    matched, kept = 0, 0
    for actor in labelled:
        if actor not in objects:
            continue
        truth = tuple(float(value) for corner in objects[actor] for value in corner)
        best = max(((track_id, _iou(boxes[impact_frame], truth)) for track_id, boxes in tracks.items() if impact_frame in boxes), key=lambda item: item[1], default=None)
        if best is None or best[1] < minimum_iou:
            continue
        matched += 1
        kept += impact_frame + after_frames <= max(tracks[best[0]])
    return matched, kept


def _to_image(homography: tuple[tuple[float, ...], ...], point: tuple[float, float]) -> tuple[float, float]:
    projected = np.linalg.inv(np.asarray(homography)) @ np.array([point[0], point[1], 1.0])
    return float(projected[0] / projected[2]), float(projected[1] / projected[2])


def evaluate_clip(row: dict[str, str], case_root: Path, data_root: Path, incident_run: str, config: IncidentConfig) -> dict[str, object]:
    """One §11.2 row: the label-blind primary candidate against the clip's manifest label.

    A clip whose chain failed (for example, no detections at all) stays in the table as a miss with its reason.
    """
    label = {"local_video_id": row["local_video_id"], "split": row["split"], "label_type": LABEL_TYPES[row["type"]], "weather": row["weather"],
             "map": row["map"], "label_time": float(row["accident_time"]), "label_frame": int(row["accident_frame"]), "config_id": config.config_id}
    failure = case_root / "analysis" / "incident" / f"{incident_run}{FAILURE_SUFFIX}"
    if failure.is_file():
        return {**label, "proposed": False, "collision_candidates": 0, "false_alarms": 0, "near_misses": 0, "near_miss_at_impact": False,
                "right_pair_near_miss_at_impact": False,
                "pipeline_failure": failure.read_text(encoding="utf-8").strip()}
    incident = case_root / "analysis" / "incident" / incident_run
    events = list(read_records(incident / "events.jsonl", IncidentEvent))
    manifest = json.loads((incident / "manifest.json").read_text(encoding="utf-8"))
    movement = json.loads((case_root / "analysis" / "movement" / manifest["movement_run"] / "manifest.json").read_text(encoding="utf-8"))
    reconstruction = case_root / "analysis" / "reconstruction" / movement["reconstruction_run"]
    reconstruction_manifest = json.loads((reconstruction / "manifest.json").read_text(encoding="utf-8"))
    calibration = json.loads((case_root.parent / reconstruction_manifest["source_calibration_path"]).read_text(encoding="utf-8"))
    homography, tracking = calibration["homography"], reconstruction_manifest["tracking_run"]
    tracks: dict[int, dict[int, tuple[float, float, float, float]]] = {}
    with (case_root / "analysis" / "tracks" / tracking / "tracks.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record["bbox_x1"] is not None:
                tracks.setdefault(record["track_id"], {})[record["frame_number"]] = (record["bbox_x1"], record["bbox_y1"], record["bbox_x2"], record["bbox_y2"])
    annotation_path = data_root / row["annotations_path"]
    annotation_path = annotation_path if annotation_path.is_file() else annotation_path.with_suffix("")
    annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
    tolerance = config.validation.impact_time_tolerance_seconds
    label_time, width, height = float(row["accident_time"]), float(row["width"]), float(row["height"])
    collisions = [event for event in events if event.event_type != "near_miss_candidate"]
    near = [event for event in events if event.event_type == "near_miss_candidate"]
    primary = primary_candidate(events)
    fps = float(row["no_frames"]) / float(row["duration"])
    tracked, kept = tracking_at_impact(annotation, tracks, int(row["accident_frame"]), round(config.collision.post_window_seconds * fps), 0.3)
    result: dict[str, object] = {
        **label, "proposed": primary is not None,
        "collision_candidates": len(collisions),
        "false_alarms": sum(abs(event.peak_time - label_time) > tolerance for event in collisions),
        "actors_tracked_at_impact": tracked, "actors_same_id_after_window": kept,
        "near_misses": len(near), "near_miss_at_impact": any(abs(event.peak_time - label_time) <= tolerance for event in near),
        "right_pair_near_miss_at_impact": any(abs(event.peak_time - label_time) <= tolerance and pair_correct(event, annotation, tracks, 0.3) for event in near),
        "pipeline_version": manifest["pipeline_version"],
    }
    if primary is None:
        return result
    u, v = _to_image(homography, primary.location)
    x, y = u / width, v / height
    error = primary.peak_time - label_time
    result.update({
        "event_id": primary.event_id, "event_type": primary.event_type, "confidence": primary.confidence, "predicted_time": primary.peak_time,
        "impact_time_error_s": error, "within_time_tolerance": abs(error) <= tolerance, "predicted_center_x": x, "predicted_center_y": y,
        "spatial_error_normalised": math.hypot(x - float(row["center_x"]), y - float(row["center_y"])),
        "inside_label_box": float(row["x1"]) <= x <= float(row["x2"]) and float(row["y1"]) <= y <= float(row["y2"]),
        "predicted_type": primary.predicted_collision_type, "type_correct": primary.predicted_collision_type == LABEL_TYPES[row["type"]],
        "pair_correct": pair_correct(primary, annotation, tracks, 0.3),
    })
    return result


def _share(rows: list[dict[str, object]], key: str) -> str:
    known = [row[key] for row in rows if row.get(key) is not None]
    return f"{sum(bool(value) for value in known)}/{len(known)}" if known else "–"


def summarise(rows: list[dict[str, object]]) -> dict[str, object]:
    proposed = [row for row in rows if row["proposed"]]
    timely = [row for row in proposed if row["within_time_tolerance"]]
    return {
        "clips": len(rows), "proposed": len(proposed), "detected_within_tolerance": len(timely),
        "detection_rate": len(timely) / len(rows) if rows else None,
        "impact_time_error_s": _summary([row["impact_time_error_s"] for row in proposed]),
        "spatial_error_normalised": _summary([row["spatial_error_normalised"] for row in timely]),
        "inside_label_box": _share(timely, "inside_label_box"), "type_correct": _share(timely, "type_correct"),
        "pair_correct": _share(timely, "pair_correct"),
        "false_alarms_per_clip": sum(row["false_alarms"] for row in rows) / len(rows) if rows else None,
        "near_miss_at_impact_without_detection": sum(row["near_miss_at_impact"] and not row.get("within_time_tolerance") for row in rows),
        "right_pair_near_miss_without_detection": sum(row["right_pair_near_miss_at_impact"] and not row.get("within_time_tolerance") for row in rows),
        "pipeline_failures": sum(bool(row.get("pipeline_failure")) for row in rows),
        "both_actors_tracked_at_impact": sum(row.get("actors_tracked_at_impact") == 2 for row in rows),
        "both_actors_same_id_after_window": sum(row.get("actors_same_id_after_window") == 2 for row in rows),
    }


def evaluate_accident(selection_path: Path, split: str, cases_root: Path, data_root: Path, incident_run: str, config: IncidentConfig,
                      case_prefix: str, output: Path) -> Path:
    """Write results.csv, summary.json and summary.md for every clip of one split; refuses to overwrite."""
    with selection_path.open(encoding="utf-8", newline="") as stream:
        selection = [row for row in csv.DictReader(stream) if row["split"] == split]
    if not selection:
        raise IncidentError(f"No '{split}' clips in '{selection_path}'.")
    if output.exists():
        raise IncidentError(f"Evaluation '{output}' already exists; results are not overwritten.")
    rows = [evaluate_clip(row, cases_root / f"{case_prefix}{row['local_video_id']}", data_root, incident_run, config) for row in selection]
    output.mkdir(parents=True)
    _write_csv(output / "results.csv", rows, RESULT_COLUMNS)
    overall = summarise(rows)
    by_type = {kind: summarise([row for row in rows if row["label_type"] == kind]) for kind in sorted({row["label_type"] for row in rows})}
    (output / "summary.json").write_text(json.dumps({"split": split, "config_id": config.config_id, "incident_run": incident_run,
                                                     "overall": overall, "by_type": by_type}, indent=2) + "\n", encoding="utf-8")
    lines = [f"# ACCIDENT synthetic §11.2 results: {split} ({config.config_id}, run {incident_run})", "",
             "Primary candidate = most confident collision or single-vehicle proposal (label-blind). Detected = its peak within "
             f"±{config.validation.impact_time_tolerance_seconds} s of the labelled impact. Location, type and pair are scored on detected clips only. "
             "A near miss counts at impact when its peak is within the same tolerance. The tracking columns are diagnostics from the labelled boxes (IoU >= 0.3); the single-vehicle type has one labelled actor.", "",
             "| Group | Clips | Proposed | Detected | Median abs. time error (s) | Median location error (norm.) | Inside label box | Type correct | Pair correct | False alarms / clip | Near miss at impact, missed clips (right pair) | Both actors tracked at impact | Both keep their track ID through the post-window | Chain failures |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, summary in (("all", overall), *by_type.items()):
        time, place = summary["impact_time_error_s"]["median"], summary["spatial_error_normalised"]["median"]
        lines.append(f"| {name} | {summary['clips']} | {summary['proposed']} | {summary['detected_within_tolerance']} | "
                     f"{'–' if time is None else f'{time:.2f}'} | {'–' if place is None else f'{place:.3f}'} | {summary['inside_label_box']} | "
                     f"{summary['type_correct']} | {summary['pair_correct']} | {summary['false_alarms_per_clip']:.2f} | {summary['near_miss_at_impact_without_detection']} ({summary['right_pair_near_miss_without_detection']}) | "
                     f"{summary['both_actors_tracked_at_impact']} | {summary['both_actors_same_id_after_window']} | {summary['pipeline_failures']} |")
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output
