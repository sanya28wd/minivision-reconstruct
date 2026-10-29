"""Case-level planar calibration and road-geometry persistence."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np
from numpy import uint8
from numpy.typing import NDArray

from minivision_reconstruct.domain.errors import CalibrationError
from minivision_reconstruct.domain.models import (
    CalibrationDocument,
    CalibrationMode,
    CalibrationPoint,
    CalibrationPointRole,
    RoadFeature,
    RoadFeatureKind,
    TrackRecord,
)


CALIBRATION_SCHEMA_VERSION = "1.0"
ROAD_FEATURE_COLORS: dict[str, tuple[int, int, int]] = {
    "lane": (40, 200, 40),
    "stop_line": (0, 80, 255),
    "crossing": (255, 180, 0),
    "monitored_zone": (220, 40, 220),
}


def parse_correspondence_rows(text: str, role: CalibrationPointRole) -> tuple[CalibrationPoint, ...]:
    """Parse one image-x,image-y,world-x,world-y correspondence per line."""
    points: list[CalibrationPoint] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        values = [part.strip() for part in line.split(",")]
        if len(values) != 4:
            raise CalibrationError(
                f"{role.title()} point line {line_number} must contain image_x,image_y,world_x,world_y."
            )
        try:
            coordinates = tuple(float(value) for value in values)
        except ValueError as error:
            raise CalibrationError(f"{role.title()} point line {line_number} contains a non-numeric coordinate.") from error
        if not all(math.isfinite(value) for value in coordinates):
            raise CalibrationError(f"{role.title()} point line {line_number} contains a non-finite coordinate.")
        points.append(CalibrationPoint(
            point_id=f"{role}-{len(points) + 1}",
            image_x=coordinates[0], image_y=coordinates[1],
            world_x=coordinates[2], world_y=coordinates[3], role=role,
        ))
    return tuple(points)


def parse_road_feature(
    feature_id: str, name: str, kind: RoadFeatureKind, points_text: str
) -> RoadFeature:
    """Parse image pixel vertices for one lane, marking, crossing, or zone."""
    normalized_id = feature_id.strip()
    normalized_name = name.strip()
    if not normalized_id or not normalized_name:
        raise CalibrationError("Road feature ID and name are required.")
    points: list[tuple[float, float]] = []
    for line_number, raw_line in enumerate(points_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        values = [part.strip() for part in line.split(",")]
        if len(values) != 2:
            raise CalibrationError(f"Road feature point line {line_number} must contain image_x,image_y.")
        try:
            x_value, y_value = (float(value) for value in values)
        except ValueError as error:
            raise CalibrationError(f"Road feature point line {line_number} contains a non-numeric coordinate.") from error
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            raise CalibrationError(f"Road feature point line {line_number} contains a non-finite coordinate.")
        points.append((x_value, y_value))
    minimum_points = 2 if kind == "stop_line" else 3
    if len(points) < minimum_points:
        raise CalibrationError(f"A {kind.replace('_', ' ')} needs at least {minimum_points} image points.")
    if kind == "stop_line" and len(points) != 2:
        raise CalibrationError("A stop line needs exactly two image points.")
    return RoadFeature(normalized_id, normalized_name, kind, tuple(points), ())


def build_calibration(
    case_id: str,
    source_video_sha256: str,
    image_width: int,
    image_height: int,
    mode: CalibrationMode,
    world_unit: str,
    control_points: tuple[CalibrationPoint, ...],
    validation_points: tuple[CalibrationPoint, ...],
    validation_tolerance: float,
    features: tuple[RoadFeature, ...],
) -> CalibrationDocument:
    """Fit a planar image-to-world transform and score held-out points."""
    if image_width <= 0 or image_height <= 0:
        raise CalibrationError("Calibration image width and height must be positive.")
    if len(source_video_sha256) != 64 or any(character not in "0123456789abcdef" for character in source_video_sha256.lower()):
        raise CalibrationError("Calibration requires a 64-character source video SHA-256 hash.")
    if mode == "metric" and world_unit not in {"m", "ft"}:
        raise CalibrationError("Metric calibration world units must be metres ('m') or feet ('ft').")
    if mode == "non_metric" and world_unit != "relative_units":
        raise CalibrationError("Non-metric calibration must use 'relative_units'.")
    if not math.isfinite(validation_tolerance) or validation_tolerance <= 0:
        raise CalibrationError("Validation tolerance must be a positive finite number.")
    if len(control_points) < 4:
        raise CalibrationError("At least four control point pairs are required to calculate a homography.")
    if not validation_points:
        raise CalibrationError("Add at least one held-out validation point to check calibration accuracy.")
    _validate_calibration_points((*control_points, *validation_points), image_width, image_height)
    if any(point.role != "control" for point in control_points):
        raise CalibrationError("Every fitting point must be marked as a control point.")
    if any(point.role != "validation" for point in validation_points):
        raise CalibrationError("Every held-out point must be marked as a validation point.")
    if len({(point.image_x, point.image_y) for point in control_points}) != len(control_points):
        raise CalibrationError("Control image coordinates must be unique.")
    if len({(point.world_x, point.world_y) for point in control_points}) != len(control_points):
        raise CalibrationError("Control world coordinates must be unique.")
    control_image = np.asarray([(point.image_x, point.image_y) for point in control_points], dtype=np.float64)
    control_world = np.asarray([(point.world_x, point.world_y) for point in control_points], dtype=np.float64)
    if np.linalg.matrix_rank(control_image - control_image.mean(axis=0)) < 2:
        raise CalibrationError("Control image points are collinear; choose points spread across the road plane.")
    if np.linalg.matrix_rank(control_world - control_world.mean(axis=0)) < 2:
        raise CalibrationError("Control world points are collinear; choose points spread across the road plane.")
    try:
        homography, _ = cv2.findHomography(control_image, control_world, method=0)
    except cv2.error as error:
        raise CalibrationError(f"Homography calculation failed: {error}") from error
    if homography is None or homography.shape != (3, 3) or not np.isfinite(homography).all():
        raise CalibrationError("Homography calculation was degenerate; review the control point pairs.")
    control_errors = _reprojection_errors(homography, control_points)
    validation_errors = _reprojection_errors(homography, validation_points)
    control_rmse = _root_mean_square(control_errors)
    validation_rmse = _root_mean_square(validation_errors)
    validation_max_error = max(validation_errors)
    if len({feature.feature_id for feature in features}) != len(features):
        raise CalibrationError("Road feature IDs must be unique.")
    projected_features = tuple(
        _project_feature(feature, homography, image_width, image_height)
        for feature in features
    )
    matrix = tuple(tuple(float(value) for value in row) for row in homography)
    validation_passed = validation_max_error <= validation_tolerance
    return CalibrationDocument(
        schema_version=CALIBRATION_SCHEMA_VERSION,
        calibration_id="pending",
        case_id=case_id,
        source_video_sha256=source_video_sha256,
        image_width=image_width,
        image_height=image_height,
        mode=mode,
        world_unit=world_unit,
        metric_claims_permitted=mode == "metric" and validation_passed,
        control_points=control_points,
        validation_points=validation_points,
        homography=matrix,
        control_rmse=control_rmse,
        validation_rmse=validation_rmse,
        validation_max_error=validation_max_error,
        validation_tolerance=validation_tolerance,
        validation_passed=validation_passed,
        features=projected_features,
    )


def render_road_overlay(image: NDArray[uint8], calibration: CalibrationDocument) -> NDArray[uint8]:
    """Return a source-frame copy with calibration points and road features drawn."""
    if image.shape[1] != calibration.image_width or image.shape[0] != calibration.image_height:
        raise CalibrationError("Overlay frame dimensions do not match the calibration image dimensions.")
    output = image.copy()
    for feature in calibration.features:
        color = ROAD_FEATURE_COLORS[feature.kind]
        vertices = np.asarray([[(round(x), round(y)) for x, y in feature.image_points]], dtype=np.int32)
        if feature.kind == "stop_line":
            cv2.polylines(output, vertices, False, color, 3, cv2.LINE_AA)
        else:
            overlay = output.copy()
            cv2.fillPoly(overlay, vertices, color)
            cv2.addWeighted(overlay, 0.18, output, 0.82, 0, output)
            cv2.polylines(output, vertices, True, color, 3, cv2.LINE_AA)
        first_x, first_y = feature.image_points[0]
        cv2.putText(output, feature.name, (round(first_x), max(18, round(first_y) - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
    for point in calibration.control_points:
        _draw_calibration_point(output, point, (0, 230, 80))
    for point in calibration.validation_points:
        _draw_calibration_point(output, point, (0, 160, 255))
    return output


def project_image_points(
    calibration: CalibrationDocument, image_points: tuple[tuple[float, float], ...]
) -> tuple[tuple[float, float], ...]:
    """Project source-image coordinates into the calibration's world coordinate system."""
    _validate_image_coordinates(image_points, calibration.image_width, calibration.image_height)
    homography = np.asarray(calibration.homography, dtype=np.float64)
    return _apply_homography(homography, image_points)


def _apply_homography(
    homography: NDArray[np.float64], image_points: tuple[tuple[float, float], ...]
) -> tuple[tuple[float, float], ...]:
    values = np.asarray([image_points], dtype=np.float64)
    try:
        projected = cv2.perspectiveTransform(values, homography)[0]
    except cv2.error as error:
        raise CalibrationError(f"Could not project image coordinates: {error}") from error
    result = tuple((float(point[0]), float(point[1])) for point in projected)
    if not all(math.isfinite(value) for point in result for value in point):
        raise CalibrationError("Homography produced a non-finite world coordinate.")
    return result


def project_track_footpoint(calibration: CalibrationDocument, record: TrackRecord) -> tuple[float, float]:
    """Project a boxed track record's bottom-center point into calibrated coordinates."""
    if record.case_id != calibration.case_id:
        raise CalibrationError(
            f"Track record case '{record.case_id}' does not match calibration case '{calibration.case_id}'."
        )
    if None in (record.bbox_x1, record.bbox_y2, record.bbox_x2):
        raise CalibrationError(f"Track {record.track_id} at frame {record.frame_number} has no box to project.")
    footpoint = ((float((record.bbox_x1 + record.bbox_x2) / 2), float(record.bbox_y2)),)
    return project_image_points(calibration, footpoint)[0]


def save_calibration_revision(
    case_directory: Path,
    calibration: CalibrationDocument,
    source_frame: NDArray[uint8],
) -> Path:
    """Write an immutable numbered JSON and road-overlay revision for one case."""
    calibration_root = case_directory / "analysis" / "calibration"
    calibration_root.mkdir(parents=True, exist_ok=True)
    revision_number = max(
        (int(path.name.removeprefix("revision-")) for path in calibration_root.glob("revision-[0-9][0-9][0-9]") if path.is_dir()),
        default=0,
    ) + 1
    calibration_id = f"revision-{revision_number:03d}"
    destination = calibration_root / calibration_id
    stored_document = replace(calibration, calibration_id=calibration_id)
    with TemporaryDirectory(prefix=f".{calibration_id}-", dir=calibration_root) as temporary_name:
        temporary_directory = Path(temporary_name)
        json_path = temporary_directory / "calibration.json"
        json_path.write_text(json.dumps(asdict(stored_document), sort_keys=True, indent=2) + "\n", encoding="utf-8")
        overlay_path = temporary_directory / "road_overlay.png"
        if not cv2.imwrite(str(overlay_path), render_road_overlay(source_frame, stored_document)):
            raise CalibrationError(f"OpenCV could not write calibration overlay '{overlay_path}'.")
        temporary_directory.rename(destination)
    return destination


def _validate_calibration_points(
    points: tuple[CalibrationPoint, ...], image_width: int, image_height: int
) -> None:
    for point in points:
        values = (point.image_x, point.image_y, point.world_x, point.world_y)
        if not all(math.isfinite(value) for value in values):
            raise CalibrationError(f"Point '{point.point_id}' contains a non-finite coordinate.")
        if not 0 <= point.image_x < image_width or not 0 <= point.image_y < image_height:
            raise CalibrationError(f"Image point '{point.point_id}' lies outside the {image_width}x{image_height} frame.")


def _reprojection_errors(
    homography: NDArray[np.float64], points: tuple[CalibrationPoint, ...]
) -> tuple[float, ...]:
    image_points = np.asarray([[(point.image_x, point.image_y) for point in points]], dtype=np.float64)
    projected = cv2.perspectiveTransform(image_points, homography)[0]
    errors = tuple(
        float(np.hypot(projected[index][0] - point.world_x, projected[index][1] - point.world_y))
        for index, point in enumerate(points)
    )
    if not all(math.isfinite(error) for error in errors):
        raise CalibrationError("Homography produced non-finite reprojection errors; check point geometry.")
    return errors


def _root_mean_square(values: tuple[float, ...]) -> float:
    return float(math.sqrt(sum(value * value for value in values) / len(values)))


def _project_feature(
    feature: RoadFeature, homography: NDArray[np.float64], image_width: int, image_height: int
) -> RoadFeature:
    if not feature.feature_id.strip() or not feature.name.strip():
        raise CalibrationError("Every road feature needs an ID and a name.")
    minimum_points = 2 if feature.kind == "stop_line" else 3
    if len(feature.image_points) < minimum_points or (feature.kind == "stop_line" and len(feature.image_points) != 2):
        expected = "exactly two" if feature.kind == "stop_line" else "at least three"
        raise CalibrationError(f"Feature '{feature.name}' needs {expected} image points.")
    for x_value, y_value in feature.image_points:
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            raise CalibrationError(f"Feature '{feature.name}' has a non-finite image coordinate.")
        if not 0 <= x_value < image_width or not 0 <= y_value < image_height:
            raise CalibrationError(f"Feature '{feature.name}' has a point outside the {image_width}x{image_height} frame.")
    try:
        world_points = _apply_homography(homography, feature.image_points)
    except CalibrationError as error:
        raise CalibrationError(f"Feature '{feature.name}' projects to an invalid world coordinate: {error}") from error
    return RoadFeature(feature.feature_id, feature.name, feature.kind, feature.image_points, world_points)


def _draw_calibration_point(
    image: NDArray[uint8], point: CalibrationPoint, color: tuple[int, int, int]
) -> None:
    center = (round(point.image_x), round(point.image_y))
    cv2.circle(image, center, 7, color, -1, cv2.LINE_AA)
    cv2.putText(image, point.point_id, (center[0] + 8, center[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2, cv2.LINE_AA)


def _validate_image_coordinates(
    image_points: tuple[tuple[float, float], ...], image_width: int, image_height: int
) -> None:
    if not image_points:
        raise CalibrationError("At least one image coordinate is required for projection.")
    for x_value, y_value in image_points:
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            raise CalibrationError("Image coordinates must be finite numbers.")
        # Box edges clipped to the frame lie exactly on its boundary, which is a valid image location.
        if not 0 <= x_value <= image_width or not 0 <= y_value <= image_height:
            raise CalibrationError(f"Image point ({x_value:g}, {y_value:g}) lies outside the calibrated image.")
