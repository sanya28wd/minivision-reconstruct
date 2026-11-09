"""Same-origin replay transport and public producer save adapters."""

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
import json
from pathlib import PurePosixPath
import socket
from uuid import uuid4

import cv2
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.incident import review
from minivision_reconstruct.incident.models import ReviewDecision
from minivision_reconstruct.movement.ui import load_movement_run
from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.reconstruction.media import (
    ASSET_DIRECTORY,
    ReplayMediaConnector,
)
from minivision_reconstruct.reconstruction.models import MarkerDocument, SceneDocument
from minivision_reconstruct.reconstruction.storage import (
    revision_ids,
    save_marker_revision,
    save_scene_revision,
)
from minivision_reconstruct.reconstruction.export import (
    export_snapshot,
    export_reconstruction,
)

from integration.tools_worker.evidence import (
    VerificationCache,
    confined_file,
    load_evidence,
    source_frame,
)
from integration.tools_worker.writes import IdempotentWrites, WriteConflict


def jsonable(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    return value


def safe_file(base, relative):
    path = PurePosixPath(relative)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError("Unsafe producer path")
    result = base / relative
    if not result.resolve(strict=True).is_relative_to(base.resolve(strict=True)) or not result.is_file():
        raise ValueError("Unsafe producer path")
    return result


def manifest_paths(run):
    value = json.loads((run / "manifest.json").read_bytes())
    for item in value.get("artifacts", ()):
        safe_file(run, item["filename"])
    return value


def routes(root, select_case, exports_root=None):
    writes = IdempotentWrites()
    verification = VerificationCache()

    def media_context(request):
        from integration.tools_worker.app import confined_child

        case_id, run_id = request.path_params["case_id"], request.path_params["run_id"]

        # Hot reads check the actual ancestry/references instead of enumerating a case.
        def selected(base, identifier):
            case = confined_child(base, identifier)
            confined_file(case, "metadata/evidence_manifest.json")
            return case

        case, evidence, video = load_evidence(root, case_id, selected, verification)
        run = confined_child(case / "analysis/reconstruction", run_id)
        manifest_path = confined_file(case, f"analysis/reconstruction/{run_id}/manifest.json")
        manifest = json.loads(manifest_path.read_bytes())
        if manifest["case_id"] != case_id or manifest["run_id"] != run_id:
            raise ValueError("Wrong reconstruction")
        for name in ("video", "tracks", "calibration"):
            path = confined_file(root, manifest[f"source_{name}_path"])
            if not path.resolve().is_relative_to(case):
                raise ValueError("Wrong case reference")
            verification.verify(path, manifest[f"source_{name}_sha256"])
        return evidence, video, run, manifest

    def context(request, values):
        from integration.tools_worker.app import confined_child

        case_id, run_id = request.path_params["case_id"], request.path_params["run_id"]
        case, evidence, video = load_evidence(root, case_id, select_case)
        run = confined_child(case / "analysis/reconstruction", run_id)
        manifest = manifest_paths(run)
        if manifest["case_id"] != case_id or manifest["run_id"] != run_id:
            raise ValueError("Wrong reconstruction")
        for name in (
            "source_video_path",
            "source_tracks_path",
            "source_calibration_path",
        ):
            path = safe_file(root, manifest[name])
            if not path.resolve().is_relative_to(case):
                raise ValueError("Wrong case reference")
        scene = values.get("scene_revision", "initial")
        marker = values.get("marker_revision", "initial")
        media = ReplayMediaConnector(root, run, scene, marker, 0)
        return case, evidence, video, run, media

    def incident_run(case, run, values):
        from integration.tools_worker.app import confined_child

        incident = confined_child(case / "analysis/incident", values["incident_run"])
        manifest = manifest_paths(incident)
        movement = confined_child(case / "analysis/movement", manifest["movement_run"])
        movement_manifest = manifest_paths(movement)
        if (
            manifest["case_id"] != case.name
            or movement_manifest["reconstruction_run"] != run.name
            or movement_manifest["case_id"] != case.name
        ):
            raise ValueError("Unrelated incident or movement run")
        return incident, movement

    def read(request):
        values = dict(request.query_params)
        resource = request.path_params.get("resource", "")
        if resource == "video" or resource.startswith(("assets/", "frame/")):
            evidence, video, run, manifest = media_context(request)
            if resource == "video":
                proxy = confined_file(run, "replay.mp4")
                artifact = next((item for item in manifest["artifacts"] if item["filename"] == "replay.mp4"), None)
                if artifact is None:
                    raise ValueError("Missing replay artifact")
                verification.verify(proxy, artifact["sha256"])
                return FileResponse(proxy, media_type="video/mp4")
            if resource.startswith("frame/"):
                frame = source_frame(evidence, video, int(resource.removeprefix("frame/")))
                success, image = cv2.imencode(".png", frame.image)
                if not success:
                    raise ValueError("Cannot render frame")
                return Response(image.tobytes(), media_type="image/png")
            filename = resource.removeprefix("assets/")
            if filename not in {"workspace.js", "workspace.css", "renderer.js"}:
                raise ValueError("Unknown asset")
            return FileResponse(
                ASSET_DIRECTORY / filename, media_type="text/css" if filename.endswith(".css") else "text/javascript"
            )
        case, evidence, video, run, media = context(request, values)
        if resource == "bundle":
            return Response(media.bundle.model_dump_json(), media_type="application/json")
        if resource.startswith("snapshot/"):
            return FileResponse(
                safe_file(run / "snapshots", resource.removeprefix("snapshot/")),
                media_type="image/png",
            )
        if resource:
            raise ValueError("Unknown resource")
        data = {
            "case_id": case.name,
            "run_id": run.name,
            "scenes": revision_ids(run, "scenes"),
            "markers": revision_ids(run, "markers"),
            "scene_revision": values.get("scene_revision", "initial"),
            "marker_revision": values.get("marker_revision", "initial"),
        }
        if values.get("incident_run"):
            incident, movement = incident_run(case, run, values)
            motion = load_movement_run(str(movement), sha256_file(movement / "manifest.json"))
            frame_number = int(values.get("frame", 0))
            data.update(
                incident_runs=[incident.name],
                review_revisions=review.review_revisions(incident),
                incidents=jsonable(review.effective_view(incident, values.get("review_revision", "initial"))),
                movement={
                    "manifest": jsonable(motion.manifest),
                    "events": jsonable(motion.events),
                    "sequence": jsonable(motion.sequence),
                    "lanes": motion.lanes,
                    "source_frame": frame_number,
                    "movement": jsonable(tuple(item for item in motion.movement if item.source_frame == frame_number)),
                    "smoothed": jsonable(tuple(item for item in motion.smoothed if item.source_frame == frame_number)),
                    "separation": jsonable(
                        tuple(item for item in motion.separation if item.source_frame == frame_number)
                    ),
                },
            )
        return JSONResponse(data)

    def perform(request, body):
        if not body["reviewer"].strip() or not body["reason"].strip():
            raise ValueError("Attribution required")
        case, evidence, video, run, media = context(request, body)
        action = body["action"]
        if action == "scene_save":
            saved = save_scene_revision(
                run,
                SceneDocument.model_validate(body["document"]),
                body["reviewer"],
            )
            result = {"revision": saved.name, "category": "scene"}
        elif action == "marker_save":
            saved = save_marker_revision(
                run,
                MarkerDocument.model_validate(body["document"]),
                body["reviewer"],
                media.bundle,
            )
            result = {"revision": saved.name, "category": "marker"}
        elif action in {"incident_review", "incident_export", "export_attach"}:
            incident, movement = incident_run(case, run, body)
            revision = body["review_revision"]
            if action == "incident_review":
                if revision != review.review_revisions(incident)[-1]:
                    raise ValueError("Select the latest review revision to record a decision")
                decision = ReviewDecision.model_validate(
                    {
                        **body["decision"],
                        "reviewer": body["reviewer"],
                        "decided_at": datetime.now(UTC).isoformat(),
                    }
                )
                saved = review.save_review_revision(
                    incident,
                    review.with_decision(review.load_review(incident, revision).decisions, decision),
                    body["reviewer"],
                )
            elif action == "export_attach":
                from integration.tools_worker.bundle_export import export_bundle

                result = export_bundle(exports_root, case, run, incident, evidence, body)
                return JSONResponse(result)
            else:
                saved = review.export_reviewed_view(incident, revision)
            result = {"revision": saved.name, "category": "review"}
        elif action in {"snapshot_request", "reconstruction_export"}:
            with socket.socket() as temporary:
                temporary.bind(("127.0.0.1", 0))
                port = temporary.getsockname()[1]
            media = ReplayMediaConnector(root, run, body["scene_revision"], body["marker_revision"], port)
            media.start()
            try:
                if action == "snapshot_request":
                    saved = run / "snapshots" / f"frame-{int(body['source_frame'])}-{uuid4().hex}.png"
                    export_snapshot(media, saved, int(body["source_frame"]))
                    result = {"snapshot": saved.name}
                else:
                    saved = run / "exports" / ("export-" + uuid4().hex)
                    import os

                    export_reconstruction(
                        media,
                        saved,
                        body["scene_revision"],
                        body["marker_revision"],
                        int(body.get("source_frame", 0)),
                        os.environ.get("RECONSTRUCTION_FFMPEG", "ffmpeg"),
                        lambda current, total: None,
                    )
                    result = {"export": saved.name}
            finally:
                media.close()
        else:
            raise ValueError("Unknown replay action")
        return JSONResponse(result)

    def write(request, body):
        scope = (request.path_params["case_id"], request.path_params["run_id"])
        return writes.execute(scope, body, lambda: perform(request, body))

    async def endpoint(request):
        body = None
        try:
            if request.method == "GET":
                return await run_in_threadpool(read, request)
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Object required")
            return await run_in_threadpool(write, request, body)
        except WriteConflict as error:
            return JSONResponse({"error": str(error), "write_state": "refused"}, status_code=409)
        except (ValueError, KeyError, TypeError, AttributeError):
            return JSONResponse(
                {
                    "error": "Replay input or save refused; verify selected revisions and attribution",
                    "write_state": "refused",
                },
                status_code=422,
            )
        except (EvidenceError, OSError, cv2.error) as error:
            refused = (
                isinstance(error, EvidenceError)
                and isinstance(body, dict)
                and body.get("action") in {"scene_save", "marker_save", "incident_review"}
            )
            return JSONResponse(
                {
                    "error": "Replay validation refused" if refused else "Replay completion was not confirmed",
                    **({"write_state": "refused"} if refused else {}),
                },
                status_code=422 if refused else 503,
            )

    async def completed(request):
        try:
            body = await request.json()
            result = writes.lookup((request.path_params["case_id"], request.path_params["run_id"]), body)
            if result is None:
                raise WriteConflict("Completed action unavailable; inspect revision and attachment history")
            return result
        except (ValueError, KeyError, TypeError) as error:
            return JSONResponse(
                {
                    "error": str(error) if isinstance(error, WriteConflict) else "Invalid completed-action query",
                    "write_state": "refused",
                },
                status_code=409,
            )

    return [
        Route("/cases/{case_id}/replay/{run_id}", endpoint, methods=["GET", "POST"]),
        Route("/cases/{case_id}/replay/{run_id}/completed", completed, methods=["POST"]),
        Route("/cases/{case_id}/replay/{run_id}/{resource:path}", endpoint),
    ]
