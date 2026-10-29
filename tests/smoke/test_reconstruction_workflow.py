"""Real file, decoder, review, browser, and encoder coverage for Week 6."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from time import monotonic, sleep

import cv2
from playwright.sync_api import sync_playwright
from pydantic import TypeAdapter
import pytest

from minivision_reconstruct.evidence.case_store import ingest_case
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.domain.models import EvidenceManifest
from minivision_reconstruct.reconstruction.demo import DemoCase, create_controlled_demo
from minivision_reconstruct.reconstruction.export import export_reconstruction
from minivision_reconstruct.reconstruction.media import ReplayMediaConnector
from minivision_reconstruct.reconstruction.models import Marker, MarkerDocument, ReconstructionError
from minivision_reconstruct.reconstruction.pipeline import _build_proxy, automatic_track_input, load_calibration, prepare_reconstruction
from minivision_reconstruct.reconstruction.storage import load_bundle, save_marker_revision, save_scene_revision


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> DemoCase:
    return create_controlled_demo(tmp_path_factory.mktemp("week6") / "cases", "controlled-evidence", 2, 4, "ffmpeg", "ffprobe")


def cases_root(demo: DemoCase) -> Path:
    return demo.reconstruction_directory.parents[3]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_projection_provenance_revisions_and_integrity(demo: DemoCase, tmp_path: Path) -> None:
    run = demo.reconstruction_directory
    bundle = load_bundle(run, "initial", "initial")
    first = next(item for item in bundle.observations if item.track_id == 1 and item.source_frame == 0)
    assert first.position == pytest.approx((27, 6.2), abs=0.0001)
    assert next(item for item in bundle.observations if item.track_id == 1 and item.source_frame == 10).provenance == "reviewer"
    assert next(item for item in bundle.observations if item.track_id == 2 and item.source_frame == 21).state == "interpolated"
    assert all(item.position is None and item.bbox is None for item in bundle.observations if item.state == "lost")
    assert bundle.manifest.replay_frame_count == 60
    assert bundle.manifest.repeated_frames == 10
    assert bundle.manifest.omitted_frames == 0
    scene_hash = sha256_file(run / "scene.json")
    changed = bundle.scene.model_copy(update={"rotation_degrees": 25.0})
    saved_scene = save_scene_revision(run, changed, "Smoke reviewer")
    marker = Marker(marker_id="evidence-1", source_frame=10, source_time=0.4, position=(30, 30), track_ids=(1, 2), label="Reviewer observation", note="Controlled fixture annotation", reviewer="Smoke reviewer", created_at="2026-10-04T00:00:00Z")
    saved_markers = save_marker_revision(run, MarkerDocument(schema_version="1.0", markers=(marker,)), "Smoke reviewer", bundle)
    reopened = load_bundle(run, saved_scene.name, saved_markers.name)
    assert reopened.scene.rotation_degrees == 25
    assert reopened.markers.markers[0] == marker
    assert sha256_file(run / "scene.json") == scene_hash
    with pytest.raises(ReconstructionError, match="locked"):
        save_scene_revision(run, changed.model_copy(update={"references": ()}), "Smoke reviewer")
    with pytest.raises(ReconstructionError, match="timestamp"):
        save_marker_revision(run, MarkerDocument(schema_version="1.0", markers=(marker.model_copy(update={"source_time": 0.5}),)), "Smoke reviewer", bundle)
    copied = tmp_path / "run"
    shutil.copytree(run, copied)
    (copied / "scene.json").write_text("{}")
    with pytest.raises(ReconstructionError, match="Integrity mismatch"):
        load_bundle(copied, "initial", "initial")
    assert not any("speed" in name for name in type(first).model_fields)


def test_automatic_selection_calibration_errors_and_units(demo: DemoCase, tmp_path: Path) -> None:
    root = cases_root(demo)
    tracks = automatic_track_input(root, demo.case_id, demo.tracking_run)
    original = root / demo.case_id / "analysis" / "calibration" / demo.calibration_revision / "calibration.json"
    evidence_path = root / demo.case_id / "metadata" / "evidence_manifest.json"
    evidence = TypeAdapter(EvidenceManifest).validate_json(evidence_path.read_bytes())
    invalid = original.parent / "invalid.json"
    value: dict[str, object] = json.loads(original.read_bytes())
    invalid.write_text(json.dumps({**value, "validation_passed": False}))
    with pytest.raises(ReconstructionError, match="held-out"):
        load_calibration(invalid, evidence, root / demo.case_id)
    invalid.write_text(json.dumps({**value, "case_id": "another-case"}))
    with pytest.raises(ReconstructionError, match="different case"):
        load_calibration(invalid, evidence, root / demo.case_id)
    invalid.write_text(json.dumps({**value, "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}))
    with pytest.raises(ReconstructionError, match="control points"):
        load_calibration(invalid, evidence, root / demo.case_id)
    relative = original.parent / "relative.json"
    relative.write_text(json.dumps({**value, "mode": "non_metric", "world_unit": "relative_units", "metric_claims_permitted": False}))
    automatic_run = prepare_reconstruction(root, demo.case_id, tracks, relative, "automatic-relative", "ffmpeg")
    bundle = load_bundle(automatic_run, "initial", "initial")
    assert bundle.scene.mode == "non_metric"
    assert bundle.scene.display_unit == "relative_units"
    assert all(item.provenance == "automatic" for item in bundle.observations)
    with pytest.raises(ReconstructionError, match="already exists"):
        prepare_reconstruction(root, demo.case_id, tracks, relative, "automatic-relative", "ffmpeg")


def test_nonuniform_source_timestamps(demo: DemoCase, tmp_path: Path) -> None:
    source = cases_root(demo) / demo.case_id / "original" / f"{demo.case_id}-synthetic.avi"
    variable = tmp_path / "variable.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source), "-vf", "setpts=if(lt(N\\,25)\\,N/(25*TB)\\,(1+(N-25)/10)/TB)", "-fps_mode", "vfr", "-c:v", "libx264", str(variable)], check=True, capture_output=True)
    case = ingest_case(variable, tmp_path / "vfr-cases", "vfr", "ffprobe")
    frames, replay, _, _ = _build_proxy(case.preserved_video_path, case.manifest, tmp_path / "proxy.mp4", "ffmpeg")
    gaps = {round(frames[offset].source_time - frames[offset - 1].source_time, 3) for offset in range(1, len(frames))}
    assert len(gaps) > 1
    for entry in replay:
        target = frames[0].source_time + entry.replay_time
        assert entry.source_time <= target + 1e-6
        if entry.source_frame + 1 < len(frames):
            assert frames[entry.source_frame + 1].source_time > target - 1e-6


def test_shared_renderer_video_export(demo: DemoCase, tmp_path: Path) -> None:
    media = ReplayMediaConnector(cases_root(demo), demo.reconstruction_directory, "initial", "initial", free_port())
    media.start()
    try:
        destination = export_reconstruction(media, tmp_path / "presentation", "initial", "initial", 21, "ffmpeg", lambda done, total: None)
    finally:
        media.close()
    manifest: dict[str, object] = json.loads((destination / "manifest.json").read_bytes())
    assert manifest["frame_count"] == 60
    assert manifest["snapshot_source_frame"] == 21
    image = cv2.imread(str(destination / "snapshot.png"))
    assert image.shape == (1080, 1920, 3)
    assert (destination / "birds_eye.mp4").stat().st_size > 0
    assert (destination / "comparison.mp4").stat().st_size > 0


def test_browser_selection_seek_editor_and_markers(demo: DemoCase, tmp_path: Path) -> None:
    port, media_port = free_port(), free_port()
    command = [sys.executable, "-m", "streamlit", "run", "scripts/reconstruct_case.py", "--server.headless", "true", "--server.address", "127.0.0.1", "--server.port", str(port), "--browser.gatherUsageStats", "false", "--", "--cases-root", str(cases_root(demo)), "--media-port", str(media_port), "--ffmpeg", "ffmpeg"]
    with (tmp_path / "streamlit.log").open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, env={**os.environ, "PYTHONPATH": str(Path("src").resolve())})
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
                page = browser.new_page(viewport={"width": 1500, "height": 1200})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                page.locator(".replay-root[data-source-frame='0']").wait_for(timeout=20000)
                page.get_by_label("Reviewer name", exact=True).fill("Browser reviewer")
                page.get_by_label("Reviewer name", exact=True).press("Enter")
                page.locator(".replay-root[data-source-frame='0'][data-reviewer='Browser reviewer']").wait_for()
                page.get_by_label("Selected road user", exact=True).select_option("1")
                assert page.locator(".replay-root").get_attribute("data-selected-track") == "1"
                page.locator(".scrub").evaluate("el => { el.value = '35'; el.dispatchEvent(new Event('input', {bubbles:true})); }")
                page.locator(".replay-root[data-source-frame='35']").wait_for()
                assert "Unavailable" in page.locator(".inspector").inner_text()
                page.get_by_label("Next source frame", exact=True).click()
                page.locator(".replay-root[data-source-frame='36']").wait_for()
                page.locator(".scrub").evaluate("el => { el.value = '21'; el.dispatchEvent(new Event('input', {bubbles:true})); }")
                page.locator(".replay-root[data-source-frame='21']").wait_for()
                page.get_by_label("Selected road user", exact=True).select_option("2")
                assert "interpolated" in page.locator(".inspector").inner_text()
                page.get_by_text("Edit visual road scene", exact=True).click()
                page.get_by_label("Display rotation (degrees)").fill("30")
                page.get_by_label("Display rotation (degrees)").press("Tab")
                page.get_by_role("button", name="Undo", exact=True).click()
                assert page.get_by_label("Display rotation (degrees)").input_value() == "0"
                page.get_by_text("Reviewer event markers", exact=True).click()
                page.get_by_label("Marker label", exact=True).fill("Browser annotation")
                page.get_by_role("button", name="Add marker at current frame", exact=True).click()
                assert "Browser annotation" in page.locator(".markers").inner_text()
                page.get_by_role("button", name="Save marker revision", exact=True).click()
                page.get_by_text("Saved revision-", exact=False).wait_for()
                page.screenshot(path=str(tmp_path / "browser.png"), full_page=True)
                assert errors == []
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
