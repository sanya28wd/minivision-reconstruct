"""Loopback-only, range-capable replay media connector and exact-frame delivery."""

import logging
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

import cv2
from pydantic import TypeAdapter, ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
import uvicorn

from minivision_reconstruct.domain.models import EvidenceManifest, FrameRate
from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.reconstruction.models import ReconstructionError, ReplayBundle, require_child
from minivision_reconstruct.reconstruction.storage import load_bundle, verify_hash
from minivision_reconstruct.video.reader import read_frame_at


LOGGER = logging.getLogger(__name__)
ASSET_DIRECTORY = Path(__file__).parent / "component" / "frontend" / "dist"


class ReplayMediaConnector:
    """Own one verified case's media service; expose neither arbitrary paths nor originals."""

    def __init__(self, cases_root: Path, run_directory: Path, scene_revision: str, marker_revision: str, port: int) -> None:
        self.bundle: ReplayBundle = load_bundle(run_directory, scene_revision, marker_revision)
        self.run_directory = run_directory.resolve()
        manifest = self.bundle.manifest
        self.video_path = require_child(cases_root / manifest.source_video_path, cases_root / manifest.case_id)
        verify_hash(self.video_path, manifest.source_video_sha256)
        verify_hash(require_child(cases_root / manifest.source_tracks_path, cases_root / manifest.case_id), manifest.source_tracks_sha256)
        verify_hash(require_child(cases_root / manifest.source_calibration_path, cases_root / manifest.case_id), manifest.source_calibration_sha256)
        self.token = uuid4().hex
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}/{self.token}"
        self.registry: ReplayMediaRegistryConnector | None = None

    def start(self) -> None:
        self.registry = ReplayMediaRegistryConnector(self.port)
        self.registry.register(self)
        self.registry.start()

    def close(self) -> None:
        if self.registry is not None:
            self.registry.close()

    async def _health(self, request: Request) -> JSONResponse:
        return JSONResponse({"case_id": self.bundle.manifest.case_id, "run_id": self.bundle.manifest.run_id})

    async def _bundle(self, request: Request) -> Response:
        return Response(self.bundle.model_dump_json(), media_type="application/json", headers={"Cache-Control": "no-store"})

    async def _video(self, request: Request) -> FileResponse:
        return FileResponse(self.run_directory / "replay.mp4", media_type="video/mp4")

    async def _frame(self, request: Request) -> Response:
        frame_number = int(request.path_params["frame_number"])
        if frame_number >= len(self.bundle.source_frames):
            return JSONResponse({"error": "Source frame is outside this case", "requested_frame": frame_number}, status_code=404)
        try:
            image = await run_in_threadpool(self._decode_frame, frame_number)
        except (EvidenceError, ValidationError, OSError, cv2.error) as error:
            LOGGER.warning("Exact-frame delivery failed", extra={"frame_number": frame_number, "case_id": self.bundle.manifest.case_id, "error_detail": str(error)})
            return JSONResponse({"error": str(error), "requested_frame": frame_number}, status_code=422)
        return Response(image, media_type="image/png", headers={"Cache-Control": "no-store", "X-Source-Frame": str(frame_number)})

    def _decode_frame(self, frame_number: int) -> bytes:
        manifest = self.bundle.manifest
        # The prepared frame index is authoritative even for frame-rate timestamp sources.
        metadata_path = self.video_path.parent.parent / "metadata" / "evidence_manifest.json"
        evidence = TypeAdapter(EvidenceManifest).validate_json(metadata_path.read_bytes())
        if evidence.frame_rate_numerator is None or evidence.frame_rate_denominator is None:
            raise ReconstructionError(f"Source frame rate is unavailable for '{self.video_path}'.")
        frame = read_frame_at(self.video_path, FrameRate(evidence.frame_rate_numerator, evidence.frame_rate_denominator), frame_number)
        factor = min(1280 / manifest.source_width, 720 / manifest.source_height, 1)
        resized = cv2.resize(frame.image, (max(1, round(manifest.source_width * factor)), max(1, round(manifest.source_height * factor))), interpolation=cv2.INTER_AREA)
        success, encoded = cv2.imencode(".png", resized)
        if not success:
            raise ReconstructionError(f"PNG encoding failed for source frame {frame_number}.")
        return encoded.tobytes()

    async def _asset(self, request: Request) -> Response:
        filename = str(request.path_params["filename"])
        if filename not in {"renderer.js", "workspace.js", "workspace.css"}:
            return Response("Unknown renderer asset", status_code=404)
        path = ASSET_DIRECTORY / filename
        if not path.is_file():
            return Response(f"Build frontend assets first; missing {filename}", status_code=503)
        return FileResponse(path, media_type="text/css" if filename.endswith(".css") else "text/javascript")


class ReplayMediaRegistryConnector:
    """Share one loopback port across browser sessions, each with its own case token."""

    def __init__(self, port: int) -> None:
        if not 1 <= port <= 65535:
            raise ReconstructionError(f"Media port must be between 1 and 65535; received {port}.")
        self.port = port
        self.connectors: dict[str, ReplayMediaConnector] = {}
        self.server: uvicorn.Server | None = None
        self.thread: Thread | None = None

    def register(self, connector: ReplayMediaConnector) -> None:
        if len(self.connectors) >= 32:
            raise ReconstructionError("Replay media service has 32 active sessions; restart the local application to release unused sessions.")
        self.connectors = {**self.connectors, connector.token: connector}

    def unregister(self, token: str) -> None:
        self.connectors = {key: value for key, value in self.connectors.items() if key != token}

    def start(self) -> None:
        app = Starlette(routes=[
            Route("/{token}/bundle", self._bundle), Route("/{token}/video", self._video),
            Route("/{token}/frame/{frame_number:int}", self._frame),
            Route("/{token}/assets/{filename}", self._asset), Route("/{token}/health", self._health),
        ], middleware=[Middleware(CORSMiddleware, allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+", allow_methods=["GET", "HEAD"], allow_headers=["Range"])])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = Thread(target=self.server.run, daemon=True, name=f"minivision-media-registry-{self.port}")
        self.thread.start()
        deadline = monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or monotonic() >= deadline:
                raise ReconstructionError(f"Cannot start replay registry on 127.0.0.1:{self.port}; choose an unused --media-port.")
            sleep(0.05)

    def close(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise ReconstructionError(f"Replay media service on port {self.port} did not stop within five seconds.")

    def _connector(self, request: Request) -> ReplayMediaConnector:
        token = str(request.path_params["token"])
        connector = self.connectors.get(token)
        if connector is None:
            raise HTTPException(status_code=404, detail=f"Unknown replay session '{token}'; reload the reconstruction page.")
        return connector

    async def _bundle(self, request: Request) -> Response:
        return await self._connector(request)._bundle(request)

    async def _video(self, request: Request) -> Response:
        return await self._connector(request)._video(request)

    async def _frame(self, request: Request) -> Response:
        return await self._connector(request)._frame(request)

    async def _asset(self, request: Request) -> Response:
        return await self._connector(request)._asset(request)

    async def _health(self, request: Request) -> Response:
        return await self._connector(request)._health(request)
