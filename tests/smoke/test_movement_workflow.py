"""Week 7 movement sequence, uncertainty, validation, and browser coverage on a staged fixture."""

from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic, sleep

import cv2
from playwright.sync_api import sync_playwright
from pydantic import TypeAdapter
import pytest

from minivision_reconstruct.calibration import build_calibration, save_calibration_revision
from minivision_reconstruct.domain.models import CalibrationDocument
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.movement.models import (
    MovementConfig, MovementError, MovementEvent, MovementManifest, MovementObservation, PairSeparation, SmoothedPoint,
)
from minivision_reconstruct.movement.pipeline import analyze_movement
from minivision_reconstruct.movement.precision import display_time, display_value, round_to_uncertainty
from minivision_reconstruct.movement.staged_fixture import RECONSTRUCTION_RUN, TRACKING_RUN, StagedCase, create_staged_case
from minivision_reconstruct.movement.uncertainty import observation_uncertainty
from minivision_reconstruct.movement.validation import evaluate_movement
from minivision_reconstruct.reconstruction.models import Observation
from minivision_reconstruct.reconstruction.pipeline import automatic_track_input, prepare_reconstruction
from minivision_reconstruct.reconstruction.storage import load_bundle, read_record, read_records


CONFIG = Path("configs/movement.json").resolve()
FORBIDDEN = ("speed", "velocity", "accel")


@pytest.fixture(scope="module")
def staged(tmp_path_factory: pytest.TempPathFactory) -> StagedCase:
    return create_staged_case(tmp_path_factory.mktemp("week7") / "cases", "staged-smoke", 7, 0.7, 45, "ffmpeg", "ffprobe")


@pytest.fixture(scope="module")
def movement_run(staged: StagedCase) -> Path:
    return analyze_movement(cases_root(staged), staged.case_id, RECONSTRUCTION_RUN, CONFIG, "movement-smoke")


def cases_root(staged: StagedCase) -> Path:
    return staged.reconstruction_directory.parents[3]


def test_raw_observations_preserved_and_gaps_not_bridged(staged: StagedCase, movement_run: Path) -> None:
    before = sha256_file(staged.reconstruction_directory / "observations.jsonl")
    bundle = load_bundle(staged.reconstruction_directory, "initial", "initial")
    smoothed = read_records(movement_run / "smoothed.jsonl", SmoothedPoint)
    raw = {(item.track_id, item.source_frame): item.position for item in bundle.observations}
    assert all(point.raw_position == raw[(point.track_id, point.source_frame)] for point in smoothed)
    assert sha256_file(staged.reconstruction_directory / "observations.jsonl") == before
    occluded = [point for point in smoothed if point.track_id == 6]
    lost = [point for point in occluded if point.state == "lost"]
    assert len(lost) == 25 and all(point.smoothed_position is None and point.segment_id is None for point in lost)
    assert len({point.segment_id for point in occluded if point.segment_id is not None}) == 2
    manifest = read_record(movement_run / "manifest.json", MovementManifest)
    assert manifest.source_reconstruction_manifest_sha256 == sha256_file(staged.reconstruction_directory / "manifest.json")


def test_outputs_never_contain_speed(movement_run: Path) -> None:
    for model in (SmoothedPoint, MovementObservation, MovementEvent, PairSeparation, MovementManifest):
        assert not [name for name in model.model_fields if any(word in name.lower() for word in FORBIDDEN)]
    for path in movement_run.iterdir():
        if path.suffix == ".jsonl":
            keys = {key for line in path.read_text().splitlines() for key in json.loads(line)}
            assert not [key for key in keys if any(word in key.lower() for word in FORBIDDEN)], path
    header = (movement_run / "trajectories.csv").read_text().splitlines()[0].lower()
    assert not any(word in header for word in FORBIDDEN)


def test_stop_turn_lane_and_crossing_events(movement_run: Path) -> None:
    events = read_records(movement_run / "events.jsonl", MovementEvent)
    kinds = {(event.event_type, event.track_ids) for event in events}
    assert {("stop", (1,)), ("start", (1,)), ("turn_left", (3,)), ("turn_right", (4,)), ("lane_change", (5,)), ("stop_line_crossing", (1,)), ("stop", (7,)), ("stop", (8,))} <= kinds
    assert all(event.time_window[0] <= event.peak_time <= event.time_window[1] and event.status == "PROPOSED_BY_SYSTEM" for event in events)
    stop = next(event for event in events if event.event_type == "stop" and event.track_ids == (1,))
    states = {item.source_frame: item for item in read_records(movement_run / "movement_observations.jsonl", MovementObservation) if item.track_id == 1}
    assert states[stop.peak_frame + 10].movement_state == "stopped" and states[stop.peak_frame + 10].heading_degrees is None
    lanes = json.loads((movement_run / "lane_sequences.json").read_text())
    assert lanes["5"] == ["lane-1", "lane-2"] and lanes["3"] == ["lane-1", "lane-4"] and "7" not in lanes


def test_uncertainty_follows_tracking_and_calibration_quality(staged: StagedCase) -> None:
    root = cases_root(staged) / staged.case_id
    calibration = TypeAdapter(CalibrationDocument).validate_json((root / "analysis" / "calibration" / "revision-001" / "calibration.json").read_bytes())
    config = read_record(CONFIG, MovementConfig).uncertainty
    observed = Observation(source_frame=0, source_time=0, track_id=1, class_name="car", position=(27, 10), bbox=(300, 380, 320, 400),
                           state="observed", provenance="automatic", detection_confidence=0.9)
    interpolated = observed.model_copy(update={"state": "interpolated"})
    far = observed.model_copy(update={"bbox": (300, 100, 320, 120)})
    base_relative, base_absolute = observation_uncertainty(observed, calibration, config)
    assert observation_uncertainty(interpolated, calibration, config)[0] > base_relative
    assert observation_uncertainty(far, calibration, config)[0] > base_relative
    assert observation_uncertainty(observed, replace(calibration, validation_rmse=0.5), config)[1] > base_absolute


def test_non_metric_calibration_reports_relative_units_only(staged: StagedCase) -> None:
    root = cases_root(staged)
    case_root = root / staged.case_id
    metric = TypeAdapter(CalibrationDocument).validate_json((case_root / "analysis" / "calibration" / "revision-001" / "calibration.json").read_bytes())
    relative = build_calibration(staged.case_id, metric.source_video_sha256, metric.image_width, metric.image_height, "non_metric", "relative_units",
                                 metric.control_points, metric.validation_points, metric.validation_tolerance, metric.features)
    capture = cv2.VideoCapture(str(next((case_root / "original").iterdir())))
    image = capture.read()[1]
    capture.release()
    revision = save_calibration_revision(case_root, relative, image)
    prepare_reconstruction(root, staged.case_id, automatic_track_input(root, staged.case_id, TRACKING_RUN), revision / "calibration.json", "relative-replay", "ffmpeg")
    run = analyze_movement(root, staged.case_id, "relative-replay", CONFIG, "movement-relative")
    manifest = read_record(run / "manifest.json", MovementManifest)
    assert manifest.unit == "relative_units" and any("not physical" in item for item in manifest.limitations)
    assert {item.unit for item in read_records(run / "smoothed.jsonl", SmoothedPoint)} == {"relative_units"}
    assert not any(item.physical_distance for item in read_records(run / "separation.jsonl", PairSeparation))
    assert all(event.unit == "relative_units" for event in read_records(run / "events.jsonl", MovementEvent))


def test_timing_error_reported_against_ground_truth(staged: StagedCase, movement_run: Path, tmp_path: Path) -> None:
    destination = evaluate_movement(cases_root(staged), (staged.case_id,), movement_run.name, tmp_path, "smoke-eval")
    metrics = json.loads((destination / "metrics.json").read_text())
    assert metrics["gate"]["timing_error_reported_against_ground_truth"]
    assert metrics["config_sha256"] == sha256_file(CONFIG)
    for kind in ("stop", "start", "turn_left", "turn_right", "lane_change", "stop_line_crossing"):
        assert metrics["per_event_type"][kind]["recall"] == 1.0, kind
        assert metrics["per_event_type"][kind]["peak_error_s"]["max"] <= metrics["per_event_type"][kind]["tolerance_s"], kind
    assert metrics["position_rmse_smoothed"] < metrics["position_rmse_raw"]
    assert metrics["true_position_inside_reported_uncertainty"] > 0.95
    assert {"matches.csv", "failures.csv", "plots.html", "config_snapshot.json"} <= {path.name for path in destination.iterdir()}
    with pytest.raises(MovementError):
        evaluate_movement(cases_root(staged), (staged.case_id,), movement_run.name, tmp_path, "smoke-eval")


def test_display_precision_and_run_immutability(staged: StagedCase, movement_run: Path) -> None:
    assert display_value(12.3456, 0.7, "m") == "12.3 ± 0.7 m"
    assert display_value(1234.5, 23, "m") == "1230 ± 20 m"
    assert round_to_uncertainty(6.27, 0.3) == (6.3, 0.3)
    assert display_time(4.2, (4.2, 4.2), 0.04) == "4.20 ± 0.04 s"
    with pytest.raises(MovementError):
        display_value(1.0, 0.0, "m")
    with pytest.raises(MovementError):
        analyze_movement(cases_root(staged), staged.case_id, RECONSTRUCTION_RUN, CONFIG, movement_run.name)
    row = (movement_run / "trajectories.csv").read_text().splitlines()[1].split(",")
    assert all(len(value.split(".")[-1]) <= 2 for value in row[6:10] if value)


def test_browser_movement_panel_seeks_replay(staged: StagedCase, movement_run: Path, tmp_path: Path) -> None:
    port, media_port = free_port(), free_port()
    command = [sys.executable, "-m", "streamlit", "run", "scripts/reconstruct_case.py", "--server.headless", "true", "--server.address", "127.0.0.1",
               "--server.port", str(port), "--browser.gatherUsageStats", "false", "--", "--cases-root", str(cases_root(staged)),
               "--media-port", str(media_port), "--ffmpeg", "ffmpeg"]
    environment = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}
    with (tmp_path / "streamlit.log").open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, env=environment)
        try:
            deadline = monotonic() + 20
            while True:
                with socket.socket() as connection:
                    if connection.connect_ex(("127.0.0.1", port)) == 0:
                        break
                if process.poll() is not None or monotonic() > deadline:
                    raise RuntimeError((tmp_path / "streamlit.log").read_text())
                sleep(0.1)
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch()
                page = browser.new_page(viewport={"width": 1500, "height": 5200})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                page.locator(".replay-root[data-source-frame='0']").wait_for(timeout=30000)
                page.get_by_text("Movement sequence and uncertainty", exact=True).wait_for(timeout=30000)
                page.get_by_label("Inspect event in replay").click()
                page.keyboard.type("turn_left")
                page.keyboard.press("Enter")
                page.get_by_role("button", name="Go to source frame").click()
                turn = next(event for event in read_records(movement_run / "events.jsonl", MovementEvent) if event.event_type == "turn_left")
                page.locator(f".replay-root[data-source-frame='{turn.peak_frame}'][data-selected-track='3']").wait_for(timeout=30000)
                for name in ("Trajectory", "Heading", "Movement states", "Lane use", "Separation"):
                    page.get_by_role("tab", name=name).click()
                assert page.locator("[data-testid='stException']").count() == 0
                assert errors == []
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
