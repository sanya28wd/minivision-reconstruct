"""Controlled synthetic road evidence with known geometry for smoke verification."""

from collections.abc import Callable
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray
from numpy import uint8

from minivision_reconstruct.calibration import build_calibration, parse_correspondence_rows, parse_road_feature, save_calibration_revision
from minivision_reconstruct.domain.models import TrackRecord
from minivision_reconstruct.evidence.case_store import ingest_case
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.perception.review_workflow import append_review_event, export_reviewed_revision, load_track_records, open_review_context
from minivision_reconstruct.reconstruction.models import ReconstructionError, SceneShape, require_identifier
from minivision_reconstruct.reconstruction.storage import load_bundle, save_scene_revision
from minivision_reconstruct.reconstruction.pipeline import prepare_reconstruction, reviewed_track_input


@dataclass(frozen=True)
class DemoCase:
    case_id: str
    tracking_run: str
    review_revision: str
    calibration_revision: str
    reconstruction_directory: Path


WorldPoint = tuple[float, float]
Polygon = tuple[WorldPoint, ...]


@dataclass(frozen=True)
class DemoLayout:
    """Known road geometry and scripted positions for a synthetic presentation case."""

    title: str
    bounds: tuple[float, float, float, float]
    lanes: tuple[Polygon, ...]
    crossings: tuple[Polygon, ...]
    markings: tuple[Polygon, ...]
    position: Callable[[float, int], WorldPoint]


def create_controlled_demo(cases_root: Path, case_id: str, duration_seconds: int, road_users: int, ffmpeg: str, ffprobe: str) -> DemoCase:
    """Create visibly synthetic evidence and reviewed tracks; never label them real CCTV."""
    return _create_demo(cases_root, case_id, duration_seconds, road_users, ffmpeg, ffprobe, intersection_layout())


def _create_demo(cases_root: Path, case_id: str, duration_seconds: int, road_users: int, ffmpeg: str, ffprobe: str, layout: DemoLayout) -> DemoCase:
    """Build calibrated evidence and a replay from one explicit synthetic layout."""
    require_identifier(case_id, "demo case ID")
    if not 2 <= road_users <= 20 or not 2 <= duration_seconds <= 40:
        raise ReconstructionError("Controlled demo requires 2–20 road users and 2–40 seconds.")
    cases_root.mkdir(parents=True, exist_ok=True)
    fixture_path = cases_root / f"{case_id}-synthetic.avi"
    if fixture_path.exists() or (cases_root / case_id).exists():
        raise ReconstructionError(f"Demo case '{case_id}' already exists; choose a new ID.")
    controls = parse_correspondence_rows("80,450,0,0\n560,450,60,0\n430,70,60,60\n210,70,0,60", "control")
    image_points = np.asarray([(point.image_x, point.image_y) for point in controls], dtype=np.float64)
    world_points = np.asarray([(point.world_x, point.world_y) for point in controls], dtype=np.float64)
    inverse, _ = cv2.findHomography(world_points, image_points)
    if inverse is None:
        raise ReconstructionError("Controlled fixture homography could not be constructed.")
    validation_pixel = _image_point(inverse, (18.0, 32.0))
    validation = parse_correspondence_rows(f"{validation_pixel[0]},{validation_pixel[1]},18,32", "validation")
    polygons = layout.lanes
    feature_rows = tuple("\n".join(f"{p[0]},{p[1]}" for p in (_image_point(inverse, point) for point in polygon)) for polygon in polygons)
    features = tuple(parse_road_feature(f"lane-{index + 1}", f"Controlled lane {index + 1}", "lane", rows) for index, rows in enumerate(feature_rows))
    crossings = tuple(parse_road_feature(f"crossing-{index + 1}", "Synthetic pedestrian crossing", "crossing", "\n".join(f"{p[0]},{p[1]}" for p in (_image_point(inverse, point) for point in polygon))) for index, polygon in enumerate(layout.crossings))
    features = (*features, *crossings)
    fps = 25
    writer = cv2.VideoWriter(str(fixture_path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (640, 480))
    if not writer.isOpened():
        raise ReconstructionError(f"Cannot create synthetic fixture video '{fixture_path}'.")
    records: list[TrackRecord] = []
    classes: tuple[str, ...] = ("car", "car", "truck", "bus", "motorcycle", "bicycle", "person")
    class_ids: dict[str, int] = {"person": 0, "bicycle": 1, "car": 2, "motorcycle": 3, "bus": 4, "truck": 5}
    try:
        for frame in range(duration_seconds * fps):
            time = frame / fps
            image: NDArray[uint8] = np.full((480, 640, 3), (178, 192, 171), dtype=np.uint8)
            for feature in features:
                vertices = np.asarray(feature.image_points, dtype=np.int32)
                cv2.fillPoly(image, [vertices], (235, 235, 230) if feature.kind == "crossing" else (73, 78, 80))
                cv2.polylines(image, [vertices], True, (230, 227, 211), 1, cv2.LINE_AA)
            for track_id in range(1, road_users + 1):
                point = layout.position(time, track_id)
                px, py = _image_point(inverse, point)
                size = 7 + (60 - point[1]) / 10
                bbox = (max(0.0, px - size / 2), max(0.0, py - size * 1.5), min(639.0, px + size / 2), min(479.0, py))
                color = (int((track_id * 41 + 100) % 180), int((track_id * 73 + 90) % 180), int((track_id * 53 + 160) % 180))
                cv2.rectangle(image, (round(bbox[0]), round(bbox[1])), (round(bbox[2]), round(bbox[3])), color, -1)
                cv2.putText(image, str(track_id), (round(bbox[0]), round(bbox[1]) - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (250, 250, 250), 1, cv2.LINE_AA)
                state = "lost" if track_id == 1 and 35 <= frame <= 39 else "interpolated" if track_id == 2 and 20 <= frame <= 22 else "recovered" if track_id == 1 and frame == 40 else "observed"
                class_name = classes[(track_id - 1) % len(classes)]
                records.append(TrackRecord(
                    case_id=case_id, run_id="controlled-tracks", frame_number=frame, timestamp_seconds=time,
                    track_id=track_id, class_id=class_ids[class_name], class_name=class_name, confidence=None if state == "lost" else 0.95,
                    bbox_x1=None if state == "lost" else bbox[0], bbox_y1=None if state == "lost" else bbox[1],
                    bbox_x2=None if state == "lost" else bbox[2], bbox_y2=None if state == "lost" else bbox[3],
                    state=state, track_age_frames=frame + 1, gap_length_frames=frame - 34 if state == "lost" else 0, schema_version="1.0",
                ))
            cv2.rectangle(image, (0, 0), (640, 40), (33, 44, 49), -1)
            cv2.putText(image, "CONTROLLED SYNTHETIC FIXTURE / NOT REAL CCTV", (15, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (245, 245, 235), 1, cv2.LINE_AA)
            writer.write(image)
    finally:
        writer.release()
    ingested = ingest_case(fixture_path, cases_root, case_id, ffprobe)
    run_directory = ingested.case_directory / "analysis" / "tracks" / "controlled-tracks"
    run_directory.mkdir(parents=True)
    tracks_path = run_directory / "tracks.jsonl"
    with tracks_path.open("x") as stream:
        for record in records:
            stream.write(json.dumps(asdict(record)) + "\n")
    (run_directory / "run_manifest.json").write_text(json.dumps({"tracks_sha256": sha256_file(tracks_path), "fixture_kind": "controlled synthetic ground-truth observations"}) + "\n")
    context = open_review_context(cases_root, case_id, "controlled-tracks")
    record = next(item for item in records if item.frame_number == 10 and item.track_id == 1)
    append_review_event(context, "Controlled fixture reviewer", "correct", {
        "track_id": 1, "frame_number": 10, "bbox": [record.bbox_x1, record.bbox_y1, record.bbox_x2, record.bbox_y2],
        "frame_width": 640, "frame_height": 480, "class_name": record.class_name,
    })
    review_directory = export_reviewed_revision(context, load_track_records(tracks_path, case_id, "controlled-tracks"))
    calibration = build_calibration(case_id, ingested.manifest.preserved_sha256, 640, 480, "metric", "m", controls, validation, 0.01, features)
    capture = cv2.VideoCapture(str(ingested.preserved_video_path))
    try:
        success, image = capture.read()
        if not success:
            raise ReconstructionError("Cannot read the controlled calibration reference frame.")
    finally:
        capture.release()
    calibration_directory = save_calibration_revision(ingested.case_directory, calibration, image)
    tracks = reviewed_track_input(cases_root, case_id, "controlled-tracks", review_directory.name)
    reconstruction = prepare_reconstruction(cases_root.resolve(), case_id, tracks, calibration_directory / "calibration.json", "week6-demo", ffmpeg)
    return DemoCase(case_id, "controlled-tracks", review_directory.name, calibration_directory.name, reconstruction)


def _image_point(homography: NDArray[np.float64], point: tuple[float, float]) -> tuple[float, float]:
    projected = cv2.perspectiveTransform(np.asarray([[point]], dtype=np.float64), homography)[0, 0]
    return float(projected[0]), float(projected[1])


def intersection_position(time: float, track_id: int) -> WorldPoint:
    progress = ((time * 1.5 + track_id * 3.2) % 54) + 3
    return (progress, 27.0 if track_id % 4 == 0 else 33.0) if track_id % 2 == 0 else (27.0 if track_id % 4 == 1 else 33.0, progress)


def boulevard_position(time: float, track_id: int) -> WorldPoint:
    """Opposing vehicle flows with a pedestrian crossing on a separate scripted path."""
    if track_id == 7:
        return (43.0, 17.0 + time * 1.4)
    progress = ((time * 2.1 + track_id * 7.0) % 54) + 3
    return (progress, 26.0) if track_id % 2 == 0 else (60.0 - progress, 34.0)


def roundabout_position(time: float, track_id: int) -> WorldPoint:
    angle = time * 0.14 + track_id * math.tau / 6
    radius = 16.0 if track_id % 2 == 0 else 20.0
    return (30.0 + radius * math.cos(angle), 30.0 + radius * math.sin(angle))


def intersection_layout() -> DemoLayout:
    return DemoLayout("Controlled intersection", (-6.0, -6.0, 66.0, 66.0), (
        ((24.0, 0.0), (30.0, 0.0), (30.0, 60.0), (24.0, 60.0)),
        ((30.0, 0.0), (36.0, 0.0), (36.0, 60.0), (30.0, 60.0)),
        ((0.0, 24.0), (60.0, 24.0), (60.0, 30.0), (0.0, 30.0)),
        ((0.0, 30.0), (60.0, 30.0), (60.0, 36.0), (0.0, 36.0)),
    ), (), (), intersection_position)


def boulevard_layout() -> DemoLayout:
    return DemoLayout("Boulevard crossing", (-3.0, 12.0, 63.0, 48.0), (
        ((0.0, 22.0), (60.0, 22.0), (60.0, 38.0), (0.0, 38.0)),
    ), (((41.0, 22.0), (45.0, 22.0), (45.0, 38.0), (41.0, 38.0)),), (
        ((0.0, 30.0), (39.0, 30.0)), ((47.0, 30.0), (60.0, 30.0)),
    ), boulevard_position)


def roundabout_layout() -> DemoLayout:
    """Non-overlapping ring sectors preserve the grassy central island."""
    sectors: tuple[Polygon, ...] = tuple(
        ((30 + 24 * math.cos(index * math.tau / 48), 30 + 24 * math.sin(index * math.tau / 48)),
         (30 + 24 * math.cos((index + 1) * math.tau / 48), 30 + 24 * math.sin((index + 1) * math.tau / 48)),
         (30 + 12 * math.cos((index + 1) * math.tau / 48), 30 + 12 * math.sin((index + 1) * math.tau / 48)),
         (30 + 12 * math.cos(index * math.tau / 48), 30 + 12 * math.sin(index * math.tau / 48))) for index in range(48)
    )
    approaches: tuple[Polygon, ...] = (
        ((0., 24.), (9., 24.), (9., 36.), (0., 36.)),
        ((51., 24.), (60., 24.), (60., 36.), (51., 36.)),
        ((24., 0.), (36., 0.), (36., 9.), (24., 9.)),
        ((24., 51.), (36., 51.), (36., 60.), (24., 60.)),
    )
    ring: Polygon = tuple((30 + 18 * math.cos(index * math.tau / 72), 30 + 18 * math.sin(index * math.tau / 72)) for index in range(73))
    return DemoLayout("Roundabout circulation", (-6.0, -6.0, 66.0, 66.0), (*sectors, *approaches), (
        ((2., 24.), (5., 24.), (5., 36.), (2., 36.)),
        ((55., 24.), (58., 24.), (58., 36.), (55., 36.)),
    ), (ring,), roundabout_position)


def create_demo_gallery(cases_root: Path, ffmpeg: str, ffprobe: str) -> tuple[DemoCase, ...]:
    """Create three distinct, visibly synthetic cases without replacing existing evidence."""
    definitions: tuple[tuple[str, int, DemoLayout], ...] = (
        ("demo-boulevard-crossing", 7, boulevard_layout()),
        ("demo-roundabout-circulation", 6, roundabout_layout()),
        ("demo-intersection-mixed-traffic", 8, intersection_layout()),
    )
    for case_id, _, _ in definitions:
        if (cases_root / case_id).exists() or (cases_root / f"{case_id}-synthetic.avi").exists():
            raise ReconstructionError(f"Gallery case '{case_id}' already exists; choose a separate cases root.")
    cases: list[DemoCase] = []
    for case_id, users, layout in definitions:
        case = _create_demo(cases_root, case_id, 12, users, ffmpeg, ffprobe, layout)
        _save_gallery_scene(case, layout)
        cases.append(case)
    return tuple(cases)


def _save_gallery_scene(case: DemoCase, layout: DemoLayout) -> None:
    """Persist illustration styling as a separate immutable scene revision."""
    bundle = load_bundle(case.reconstruction_directory, "initial", "initial")
    shapes = tuple(SceneShape(shape_id=f"demo-surface-{index}", kind="surface", points=polygon, label="") for index, polygon in enumerate(layout.lanes))
    markings = tuple(SceneShape(shape_id=f"demo-marking-{index}", kind="marking", points=points, label="") for index, points in enumerate(layout.markings))
    scene = bundle.scene.model_copy(update={"shapes": (*shapes, *markings), "bounds": layout.bounds})
    save_scene_revision(case.reconstruction_directory, scene, "Synthetic scene generator")
