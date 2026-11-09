"""Public calibration workflow adapter, with source-derived dimensions and hashes."""

import base64
from dataclasses import asdict

import cv2
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minivision_reconstruct.calibration import (
    build_calibration,
    parse_correspondence_rows,
    parse_road_feature,
    render_road_overlay,
    save_calibration_revision,
)
from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.reconstruction.pipeline import load_calibration

from integration.tools_worker.evidence import load_evidence, source_frame
from integration.tools_worker.writes import IdempotentWrites, WriteConflict


def point_text(points):
    return "\n".join(f"{p.image_x},{p.image_y},{p.world_x},{p.world_y}" for p in points)


def feature_inputs(features):
    return [
        {
            "feature_id": f.feature_id,
            "name": f.name,
            "kind": f.kind,
            "points": "\n".join(f"{x},{y}" for x, y in f.image_points),
        }
        for f in features
    ]


def routes(root, select_case):
    writes = IdempotentWrites()

    def execute(request, body=None):
        from integration.tools_worker.app import confined_child

        try:
            case, manifest, video = load_evidence(root, request.path_params["case_id"], select_case)
            revision = request.path_params["revision"]
            saved = confined_child(case / "analysis/calibration", revision)
            original = load_calibration(saved / "calibration.json", manifest, case)
            frame_number = int(request.query_params.get("frame", "0")) if body is None else int(body["frame"])
            frame = source_frame(manifest, video, frame_number)
            if body is None:
                if request.path_params.get("image"):
                    success, png = cv2.imencode(".png", frame.image)
                    if not success:
                        raise ValueError("Image unavailable")
                    return Response(png.tobytes(), media_type="image/png")
                return JSONResponse(
                    {
                        "case_id": manifest.case_id,
                        "frame": frame_number,
                        "mode": original.mode,
                        "world_unit": original.world_unit,
                        "control_text": point_text(original.control_points),
                        "validation_text": point_text(original.validation_points),
                        "validation_tolerance": original.validation_tolerance,
                        "features": feature_inputs(original.features),
                        "calibration": asdict(original),
                    }
                )
            if not body["reviewer"].strip() or not body["reason"].strip():
                raise ValueError("Attribution required")
            if body["mode"] not in {"metric", "non_metric"}:
                raise ValueError("Invalid calibration mode")
            if any(row["kind"] not in {"lane", "stop_line", "crossing", "monitored_zone"} for row in body["features"]):
                raise ValueError("Invalid feature kind")
            features = tuple(
                parse_road_feature(row["feature_id"], row["name"], row["kind"], row["points"])
                for row in body["features"]
            )
            document = build_calibration(
                manifest.case_id,
                manifest.preserved_sha256,
                frame.image.shape[1],
                frame.image.shape[0],
                body["mode"],
                body["world_unit"],
                parse_correspondence_rows(body["control_text"], "control"),
                parse_correspondence_rows(body["validation_text"], "validation"),
                float(body["validation_tolerance"]),
                features,
            )
            if body["action"] == "save":
                if not document.validation_passed:
                    raise ValueError("Held-out validation failed")
                return JSONResponse({"revision": save_calibration_revision(case, document, frame.image).name})
            if body["action"] not in {"preview", "download"}:
                raise ValueError("Unknown action")
            success, png = cv2.imencode(".png", render_road_overlay(frame.image, document))
            if not success:
                raise ValueError("Overlay unavailable")
            return JSONResponse(
                {
                    "calibration": asdict(document),
                    "overlay_png": base64.b64encode(png.tobytes()).decode("ascii"),
                }
            )
        except OSError:
            return JSONResponse(
                {
                    "error": "Calibration completion was not confirmed. Inspect saved revisions before reloading to start another action."
                },
                status_code=503,
            )
        except (
            ValueError,
            EvidenceError,
            KeyError,
            TypeError,
            AttributeError,
            cv2.error,
        ):
            return JSONResponse(
                {
                    "error": "Calibration input refused; verify points, features and held-out validation",
                    "write_state": "refused",
                },
                status_code=422,
            )

    async def endpoint(request):
        body = None
        if request.method == "POST":
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("Object required")
            except (ValueError, TypeError):
                return JSONResponse({"error": "Invalid calibration request"}, status_code=422)
        if body is not None and body.get("action") == "save":
            try:
                # Every selected base revision writes into the same case revision namespace.
                return await run_in_threadpool(
                    writes.execute,
                    (request.path_params["case_id"],),
                    {**body, "base_revision": request.path_params["revision"]},
                    lambda: execute(request, body),
                )
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                return JSONResponse(
                    {
                        "error": str(error) if isinstance(error, WriteConflict) else "Valid action ID required",
                        "write_state": "refused",
                    },
                    status_code=409 if isinstance(error, WriteConflict) else 422,
                )
        return await run_in_threadpool(execute, request, body)

    return [
        Route("/cases/{case_id}/calibration/{revision}", endpoint, methods=["GET", "POST"]),
        Route("/cases/{case_id}/calibration/{revision}/{image}", endpoint),
    ]
