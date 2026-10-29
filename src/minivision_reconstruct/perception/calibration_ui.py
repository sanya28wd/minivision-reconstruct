"""Local Streamlit interface for image-to-road calibration."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

import cv2
from minivision_reconstruct.calibration import (
    build_calibration,
    parse_correspondence_rows,
    parse_road_feature,
    render_road_overlay,
    save_calibration_revision,
)
from minivision_reconstruct.domain.errors import CalibrationError, EvidenceError
from minivision_reconstruct.domain.models import EvidenceManifest, FrameRate, RoadFeature
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.video.reader import read_frame_at


def run_calibration_app() -> None:
    """Launch a local browser UI for calibration and road-feature entry."""
    import streamlit as st

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cases-root", type=Path, default=Path("cases"))
    parsed, _ = parser.parse_known_args(_streamlit_arguments())
    cases_root = parsed.cases_root
    st.set_page_config(page_title="MiniVision Calibration", layout="wide")
    st.title("Road calibration")
    st.caption("Map source-image pixels to a road plane using control points, then check held-out validation points.")
    case_ids = _available_case_ids(cases_root)
    if not case_ids:
        st.error(f"No ingested cases were found under '{cases_root}'.")
        return

    case_id = st.selectbox("Case", case_ids)
    try:
        case_directory, manifest, video_path = _load_calibration_case(cases_root, case_id)
    except CalibrationError as error:
        st.error(str(error))
        return
    frame_rate = _manifest_frame_rate(manifest)
    frame_key = f"calibration-frame-{case_id}"
    frame_count = manifest.decoded_frame_count
    frame_column, mode_column, unit_column = st.columns((1, 1, 1))
    with frame_column:
        frame_number = int(st.number_input(
            "Reference frame", min_value=0, max_value=max(frame_count - 1, 0), step=1, key=frame_key,
        ))
    with mode_column:
        mode_label = st.selectbox("Coordinate mode", ("Metric", "Non-metric"), key=f"calibration-mode-{case_id}")
    mode = "metric" if mode_label == "Metric" else "non_metric"
    if mode == "metric":
        with unit_column:
            world_unit = st.selectbox("World unit", ("m", "ft"), key=f"calibration-unit-{case_id}")
        default_tolerance = 0.2
    else:
        world_unit = "relative_units"
        with unit_column:
            st.text_input("World unit", value="relative units", disabled=True, key=f"calibration-relative-unit-{case_id}")
        st.info("Non-metric mode reports relative coordinates and errors only; it cannot make metre, distance, or speed claims.")
        default_tolerance = 0.05

    try:
        frame = read_frame_at(video_path, frame_rate, frame_number)
    except EvidenceError as error:
        st.error(str(error))
        return
    st.write(f"Source frame: {manifest.width} × {manifest.height} pixels · origin (0, 0) is top-left.")

    with st.expander("Enter image/world point pairs", expanded=True):
        st.caption("Use one row per point: image_x,image_y,world_x,world_y. Enter at least four control points and one separate validation point.")
        control_text = st.text_area(
            "Control points used to fit the homography",
            placeholder="200,700,0,0\n1200,700,20,0\n250,300,0,40\n1100,300,20,40",
            height=120,
            key=f"control-points-{case_id}",
        )
        validation_text = st.text_area(
            "Held-out validation points",
            placeholder="700,500,10,20",
            height=80,
            key=f"validation-points-{case_id}",
        )
        validation_tolerance = float(st.number_input(
            f"Maximum allowed validation error ({world_unit})",
            min_value=0.000001,
            value=default_tolerance,
            step=default_tolerance,
            format="%.6f",
            key=f"validation-tolerance-{case_id}-{mode}",
        ))

    features_key = f"calibration-features-{case_id}"
    if features_key not in st.session_state:
        st.session_state[features_key] = ()
    _render_feature_entry(st, features_key, frame.image.shape[1], frame.image.shape[0], case_id)
    features = st.session_state[features_key]

    calibration = None
    try:
        control_points = parse_correspondence_rows(control_text, "control")
        validation_points = parse_correspondence_rows(validation_text, "validation")
        calibration = build_calibration(
            case_id=case_id,
            source_video_sha256=manifest.preserved_sha256,
            image_width=frame.image.shape[1],
            image_height=frame.image.shape[0],
            mode=mode,
            world_unit=world_unit,
            control_points=control_points,
            validation_points=validation_points,
            validation_tolerance=validation_tolerance,
            features=features,
        )
        error_suffix = world_unit if mode == "metric" else "relative units"
        st.metric("Control point RMSE", f"{calibration.control_rmse:.4f} {error_suffix}")
        st.metric("Validation point RMSE", f"{calibration.validation_rmse:.4f} {error_suffix}")
        st.metric("Worst validation point", f"{calibration.validation_max_error:.4f} {error_suffix}")
        if calibration.validation_passed:
            st.success(f"Validation passes: every held-out point is within {validation_tolerance:g} {error_suffix}.")
        else:
            st.warning(f"Validation fails: at least one point exceeds the {validation_tolerance:g} {error_suffix} tolerance.")
        preview = render_road_overlay(frame.image, calibration)
        st.image(cv2.cvtColor(preview, cv2.COLOR_BGR2RGB), caption=f"Calibration overlay · frame {frame_number}", use_container_width=True)
        st.download_button(
            "Download calibration JSON",
            data=json.dumps(asdict(calibration), sort_keys=True, indent=2) + "\n",
            file_name=f"{case_id}-calibration.json",
            mime="application/json",
        )
    except CalibrationError as error:
        st.info(str(error))

    if st.button("Save calibration JSON and road overlay", disabled=calibration is None or not calibration.validation_passed):
        if calibration is None:
            st.error("Enter valid control and validation point pairs before saving.")
            return
        try:
            output_directory = save_calibration_revision(case_directory, calibration, frame.image)
            st.success(f"Calibration saved to {output_directory}")
        except CalibrationError as error:
            st.error(str(error))


def _render_feature_entry(
    st: object, features_key: str, image_width: int, image_height: int, case_id: str
) -> None:
    st.subheader("Road features")
    st.caption("Enter source-image pixel vertices, one x,y pair per line. Lanes, crossings, and monitored zones are polygons; stop lines use two endpoints.")
    with st.form(f"road-feature-form-{case_id}"):
        feature_columns = st.columns((1, 1, 2))
        with feature_columns[0]:
            kind = st.selectbox("Feature type", ("lane", "stop_line", "crossing", "monitored_zone"))
        with feature_columns[1]:
            name = st.text_input("Feature name", placeholder="northbound lane")
        with feature_columns[2]:
            points_text = st.text_area("Image vertices", placeholder="100,700\n500,700\n450,300\n150,300", height=110)
        add_feature = st.form_submit_button("Add road feature")
    if add_feature:
        try:
            existing_features = st.session_state[features_key]
            feature_id = f"{kind}-{len(existing_features) + 1}"
            feature = parse_road_feature(feature_id, name, kind, points_text)
            _validate_feature_bounds(feature, image_width, image_height)
            st.session_state[features_key] = (*existing_features, feature)
        except CalibrationError as error:
            st.error(str(error))
    for feature_index, feature in enumerate(st.session_state[features_key]):
        feature_column, remove_column = st.columns((5, 1))
        feature_column.write(f"{feature.kind.replace('_', ' ').title()} · {feature.name} · {len(feature.image_points)} vertices")
        if remove_column.button("Remove", key=f"remove-feature-{case_id}-{feature.feature_id}-{feature_index}"):
            st.session_state[features_key] = tuple(
                candidate for candidate in st.session_state[features_key] if candidate.feature_id != feature.feature_id
            )
            st.rerun()


def _validate_feature_bounds(feature: RoadFeature, image_width: int, image_height: int) -> None:
    for x_value, y_value in feature.image_points:
        if not 0 <= x_value < image_width or not 0 <= y_value < image_height:
            raise CalibrationError(f"Feature '{feature.name}' has a point outside the {image_width}x{image_height} image.")


def _load_calibration_case(cases_root: Path, case_id: str) -> tuple[Path, EvidenceManifest, Path]:
    case_directory = cases_root / case_id
    manifest_path = case_directory / "metadata" / "evidence_manifest.json"
    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest_value, dict):
            raise CalibrationError(f"Evidence manifest must be a JSON object: '{manifest_path}'.")
        manifest = EvidenceManifest(**manifest_value)
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise CalibrationError(f"Could not read evidence manifest '{manifest_path}': {error}") from error
    video_path = case_directory / "original" / manifest.original_source_filename
    if not video_path.is_file():
        raise CalibrationError(f"Preserved source video is missing: '{video_path}'.")
    actual_hash = sha256_file(video_path)
    if actual_hash != manifest.preserved_sha256:
        raise CalibrationError(
            f"Preserved video hash mismatch for case '{case_id}': expected {manifest.preserved_sha256}, got {actual_hash}."
        )
    return case_directory, manifest, video_path


def _manifest_frame_rate(manifest: EvidenceManifest) -> FrameRate | None:
    if manifest.frame_rate_numerator is None or manifest.frame_rate_denominator is None:
        return None
    if manifest.frame_rate_numerator <= 0 or manifest.frame_rate_denominator <= 0:
        return None
    return FrameRate(manifest.frame_rate_numerator, manifest.frame_rate_denominator)


def _available_case_ids(cases_root: Path) -> list[str]:
    if not cases_root.is_dir():
        return []
    return sorted(
        case_directory.name
        for case_directory in cases_root.iterdir()
        if case_directory.is_dir() and (case_directory / "metadata" / "evidence_manifest.json").is_file()
    )


def _streamlit_arguments() -> list[str]:
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1 :]
    return sys.argv[1:]
