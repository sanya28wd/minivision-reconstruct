"""Deterministic Canvas exports streamed through one Chromium and FFmpeg process."""

import base64
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import TypedDict

import cv2
import numpy as np
from numpy import uint8
from numpy.typing import NDArray
from playwright.sync_api import Browser, Page, Playwright, Error as PlaywrightError, sync_playwright

from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.reconstruction.media import ASSET_DIRECTORY, ReplayMediaConnector
from minivision_reconstruct.reconstruction.models import ArtifactHash, ExportManifest, ReplayExportError, require_identifier
from minivision_reconstruct.reconstruction.pipeline import FFmpegEncoder
from minivision_reconstruct.reconstruction.storage import write_record


class RenderImages(TypedDict):
    road: str
    comparison: str


BOOTSTRAP = """async ({base, bundle}) => {
  const renderer = await import(base + '/assets/renderer.js');
  const index = renderer.indexBundle(bundle);
  const map = document.createElement('canvas'); map.width = 1060; map.height = 790;
  const camera = document.createElement('canvas'); camera.width = 720; camera.height = 790;
  const comparison = document.createElement('canvas'); comparison.width = 1920; comparison.height = 1080;
  const road = document.createElement('canvas'); road.width = 1920; road.height = 1080;
  document.body.append(comparison);
  window.renderEvidence = async ({image, sourceFrame}) => {
    const bitmap = await createImageBitmap(await (await fetch('data:image/png;base64,' + image)).blob());
    renderer.renderMap(map.getContext('2d'), bundle, index, sourceFrame, bundle.scene, renderer.DEFAULT_VIEW, renderer.DEFAULT_OPTIONS, null);
    renderer.renderCamera(camera.getContext('2d'), bitmap, bundle, index, sourceFrame, null);
    renderer.renderComparison(comparison.getContext('2d'), map, camera, bundle, sourceFrame);
    renderer.renderMap(road.getContext('2d'), bundle, index, sourceFrame, bundle.scene, renderer.DEFAULT_VIEW, renderer.DEFAULT_OPTIONS, null);
    bitmap.close();
    return {road: road.toDataURL('image/png').split(',')[1], comparison: comparison.toDataURL('image/png').split(',')[1]};
  };
}"""


class CanvasExportConnector:
    """A persistent Chromium page invoking the exact interactive scene renderer."""

    def __init__(self, media: ReplayMediaConnector) -> None:
        self.playwright: Playwright = sync_playwright().start()
        try:
            self.browser: Browser = self.playwright.chromium.launch(headless=True)
            self.page: Page = self.browser.new_page(viewport={"width": 1920, "height": 1080})
            self.page.goto(media.base_url + "/health")
            self.page.evaluate(BOOTSTRAP, {"base": media.base_url, "bundle": media.bundle.model_dump(mode="json")})
        except PlaywrightError as error:
            self.playwright.stop()
            raise ReplayExportError(f"Cannot start Canvas export: {error}. Install the project Playwright Chromium runtime with 'python -m playwright install chromium'.") from error

    def render(self, image: NDArray[uint8], source_frame: int) -> RenderImages:
        success, encoded = cv2.imencode(".png", image)
        if not success:
            raise ReplayExportError(f"Cannot encode camera frame {source_frame} for Canvas export.")
        try:
            return self.page.evaluate("args => window.renderEvidence(args)", {"image": base64.b64encode(encoded).decode("ascii"), "sourceFrame": source_frame})
        except PlaywrightError as error:
            raise ReplayExportError(f"Canvas renderer failed at source frame {source_frame}: {error}") from error

    def close(self) -> None:
        self.browser.close()
        self.playwright.stop()


def export_reconstruction(media: ReplayMediaConnector, destination: Path, scene_revision: str, marker_revision: str, snapshot_frame: int, ffmpeg_command: str, progress: Callable[[int, int], None]) -> Path:
    """Create verified 1080p videos and a source-exact PNG in an immutable directory."""
    require_identifier(scene_revision, "scene revision")
    require_identifier(marker_revision, "marker revision")
    if destination.exists():
        raise ReplayExportError(f"Export '{destination}' already exists; choose a new export ID.")
    if snapshot_frame < 0 or snapshot_frame >= len(media.bundle.source_frames):
        raise ReplayExportError(f"Snapshot frame {snapshot_frame} is outside this case.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".export-", dir=destination.parent) as temporary, ExitStack() as stack:
        staged = Path(temporary)
        renderer = CanvasExportConnector(media)
        stack.callback(renderer.close)
        road_encoder = FFmpegEncoder(staged / "birds_eye.mp4", 1920, 1080, 30, ffmpeg_command)
        stack.callback(road_encoder.close)
        comparison_encoder = FFmpegEncoder(staged / "comparison.mp4", 1920, 1080, 30, ffmpeg_command)
        stack.callback(comparison_encoder.close)
        capture = cv2.VideoCapture(str(media.run_directory / "replay.mp4"))
        stack.callback(capture.release)
        for entry in media.bundle.replay_frames:
            decoded, image = capture.read()
            if not decoded:
                raise ReplayExportError(f"Proxy decode failed at replay frame {entry.replay_frame}.")
            rendered = renderer.render(image, entry.source_frame)
            road_encoder.write(_decode_png(rendered["road"]))
            comparison_encoder.write(_decode_png(rendered["comparison"]))
            progress(entry.replay_frame + 1, len(media.bundle.replay_frames))
        road_encoder.finish()
        comparison_encoder.finish()
        snapshot = _snapshot_images(media, renderer, snapshot_frame)
        (staged / "snapshot.png").write_bytes(base64.b64decode(snapshot["comparison"]))
        (staged / "birds_eye_snapshot.png").write_bytes(base64.b64decode(snapshot["road"]))
        for name in ("birds_eye.mp4", "comparison.mp4"):
            verify_export_video(staged / name, len(media.bundle.replay_frames))
        scene_path = _revision_path(media.run_directory, "scenes", scene_revision)
        marker_path = _revision_path(media.run_directory, "markers", marker_revision)
        for filename in ("observations.jsonl", "source_frames.jsonl", "replay_frames.jsonl", "manifest.json"):
            output_name = "reconstruction_manifest.json" if filename == "manifest.json" else filename
            shutil.copyfile(media.run_directory / filename, staged / output_name)
        write_record(staged / "scene.json", media.bundle.scene)
        write_record(staged / "markers.json", media.bundle.markers)
        manifest = ExportManifest(
            schema_version="1.0", source_manifest_sha256=sha256_file(media.run_directory / "manifest.json"),
            scene_revision=scene_revision, scene_sha256=sha256_file(scene_path),
            marker_revision=marker_revision, markers_sha256=sha256_file(marker_path),
            renderer_sha256=sha256_file(ASSET_DIRECTORY / "renderer.js"),
            frame_count=len(media.bundle.replay_frames), frame_rate=30, width=1920, height=1080,
            snapshot_source_frame=snapshot_frame, created_at=datetime.now(UTC).isoformat(),
            artifacts=tuple(ArtifactHash(filename=path.name, sha256=sha256_file(path)) for path in sorted(staged.iterdir())),
        )
        write_record(staged / "manifest.json", manifest)
        staged.rename(destination)
    return destination


def export_snapshot(media: ReplayMediaConnector, destination: Path, source_frame: int) -> Path:
    """Export the exact currently selected source frame through the shared renderer."""
    if destination.exists():
        raise ReplayExportError(f"Snapshot '{destination}' already exists.")
    if source_frame < 0 or source_frame >= len(media.bundle.source_frames):
        raise ReplayExportError(f"Snapshot frame {source_frame} is outside this case.")
    renderer = CanvasExportConnector(media)
    try:
        result = _snapshot_images(media, renderer, source_frame)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(base64.b64decode(result["comparison"]))
    finally:
        renderer.close()
    return destination


def _snapshot_images(media: ReplayMediaConnector, renderer: CanvasExportConnector, source_frame: int) -> RenderImages:
    image = cv2.imdecode(np.frombuffer(media._decode_frame(source_frame), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ReplayExportError(f"Cannot decode exact source frame {source_frame} for snapshot.")
    return renderer.render(image, source_frame)


def _decode_png(value: str) -> NDArray[uint8]:
    image = cv2.imdecode(np.frombuffer(base64.b64decode(value), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape != (1080, 1920, 3):
        raise ReplayExportError("Canvas export returned an invalid 1920×1080 PNG.")
    return image


def _revision_path(run_directory: Path, category: str, revision: str) -> Path:
    if revision == "initial":
        return run_directory / ("scene.json" if category == "scenes" else "markers.json")
    return run_directory / category / revision / "revision.json"


def verify_export_video(path: Path, expected_frames: int) -> None:
    """Decode every frame and validate resolution, rate, and total duration."""
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ReplayExportError(f"Export is not decodable: '{path}'.")
        if (round(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))) != (1920, 1080):
            raise ReplayExportError(f"Export '{path}' does not have 1920×1080 dimensions.")
        if abs(capture.get(cv2.CAP_PROP_FPS) - 30) > 0.001:
            raise ReplayExportError(f"Export '{path}' does not have a 30 FPS time base.")
        count = 0
        while capture.read()[0]:
            count += 1
        if count != expected_frames:
            raise ReplayExportError(f"Export '{path}' has {count} frames; expected {expected_frames}.")
    finally:
        capture.release()
