"""Thin input/rendering adapter; saved review semantics belong to the producer."""

from dataclasses import asdict
import json
from pathlib import PurePosixPath

import cv2
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minivision_reconstruct.domain.models import FrameRate
from minivision_reconstruct.domain.errors import EvidenceError
from minivision_reconstruct.perception import review_workflow as workflow
from minivision_reconstruct.video.reader import read_frame_at
from minivision_reconstruct.video.renderer import render_tracking_frame
from integration.tools_worker.writes import IdempotentWrites, WriteConflict


ACTIONS = {
    "correct",
    "add",
    "create_track",
    "delete_observation",
    "delete_track",
    "merge",
    "split",
    "repair_gap",
}


def routes(root, select_case):
    writes = IdempotentWrites()

    def context(request):
        case_id, run_id = request.path_params["case_id"], request.path_params["run_id"]
        case = select_case(root, case_id)
        from integration.tools_worker.app import confined_child

        confined_child(case / "analysis/tracks", run_id)
        manifest = json.loads((case / "metadata/evidence_manifest.json").read_bytes())
        declared = manifest["preserved_relative_path"]
        path = PurePosixPath(declared)
        if path.is_absolute() or ".." in path.parts or "\\" in declared or not path.parts or path.parts[0] != case_id:
            raise ValueError("Invalid preserved input")
        if not (root / declared).resolve(strict=True).is_relative_to(case):
            raise ValueError("Invalid preserved input")
        return workflow.open_review_context(root, case_id, run_id)

    def load(request):
        ctx = context(request)
        automatic = workflow.load_track_records(ctx.source_records_path, ctx.case_id, ctx.source_run_id)
        events = workflow.read_review_events(ctx.event_log_path, ctx.case_id, ctx.source_run_id)
        return ctx, automatic, workflow.rebuild_reviewed_tracks(automatic, events), events

    def read(request):
        try:
            ctx, automatic, reviewed, events = load(request)
            frame_number = int(request.query_params.get("frame", "0"))
            if frame_number < 0:
                raise ValueError("Invalid frame")
            if request.path_params.get("image"):
                manifest = ctx.evidence_manifest
                rate = (
                    None
                    if manifest.frame_rate_numerator is None or manifest.frame_rate_denominator is None
                    else FrameRate(manifest.frame_rate_numerator, manifest.frame_rate_denominator)
                )
                frame = read_frame_at(ctx.source_video_path, rate, frame_number)
                records = tuple(item.record for item in reviewed if item.record.frame_number == frame_number)
                image = render_tracking_frame(frame.image, records, frame.number, frame.timestamp_seconds, {}, {})
                success, encoded = cv2.imencode(".png", image)
                if not success:
                    raise ValueError("Image unavailable")
                return Response(encoded.tobytes(), media_type="image/png")
            return JSONResponse(
                {
                    "latest_event_id": events[-1].event_id if events else None,
                    "latest_sequence": events[-1].sequence if events else 0,
                    "case_id": ctx.case_id,
                    "run_id": ctx.source_run_id,
                    "records": [asdict(item) for item in reviewed if item.record.frame_number == frame_number],
                    "track_ids": sorted({item.record.track_id for item in reviewed}),
                    "last_frame": max((item.record.frame_number for item in reviewed), default=None),
                }
            )
        except (ValueError, OSError, KeyError, TypeError, cv2.error, EvidenceError):
            return JSONResponse({"error": "Tracking input unavailable or invalid"}, status_code=422)

    def write(request, body):
        try:
            reviewer = body["reviewer"].strip()
            reason = body["reason"].strip()
            if not reviewer or not reason or len(reviewer) > 200 or len(reason) > 2000:
                raise ValueError("Attribution required")
            ctx, automatic, reviewed, events = load(request)
            action = body["action"]
            if action == "undo":
                if (
                    not events
                    or body.get("expected_event_id") != events[-1].event_id
                    or body.get("expected_sequence") != events[-1].sequence
                ):
                    raise WriteConflict("The latest review action changed. Reload before undoing.")
                workflow.undo_latest_review_event(ctx, reviewer, automatic)
            elif action == "export":
                output = workflow.export_reviewed_revision(ctx, automatic)
                return JSONResponse({"revision": output.name})
            elif action in ACTIONS:
                payload = body["payload"]
                if not isinstance(payload, dict):
                    raise ValueError("Invalid payload")
                payload = dict(payload)
                if action in {"add", "create_track"} or (action == "correct" and "bbox" in payload):
                    manifest = ctx.evidence_manifest
                    rate = (
                        None
                        if manifest.frame_rate_numerator is None or manifest.frame_rate_denominator is None
                        else FrameRate(
                            manifest.frame_rate_numerator,
                            manifest.frame_rate_denominator,
                        )
                    )
                    frame = read_frame_at(ctx.source_video_path, rate, int(payload["frame_number"]))
                    payload.update(
                        frame_width=frame.image.shape[1],
                        frame_height=frame.image.shape[0],
                    )
                    if action in {"add", "create_track"}:
                        payload["timestamp_seconds"] = frame.timestamp_seconds
                workflow.append_review_event(ctx, reviewer, action, {**payload, "reason": reason})
            else:
                raise ValueError("Unknown action")
            return JSONResponse({"saved": True})
        except WriteConflict as error:
            return JSONResponse({"error": str(error), "write_state": "refused"}, status_code=409)
        except OSError:
            return JSONResponse(
                {
                    "error": "Review completion was not confirmed. Inspect saved review history before reloading to start another action."
                },
                status_code=503,
            )
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            EvidenceError,
        ):
            return JSONResponse(
                {"error": "Review action refused; check input and attribution", "write_state": "refused"},
                status_code=422,
            )

    async def endpoint(request):
        if request.method == "GET":
            return await run_in_threadpool(read, request)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Object required")
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid review request"}, status_code=422)
        try:
            return await run_in_threadpool(
                writes.execute,
                (request.path_params["case_id"], request.path_params["run_id"]),
                body,
                lambda: write(request, body),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            return JSONResponse(
                {
                    "error": str(error) if isinstance(error, WriteConflict) else "Valid action ID required",
                    "write_state": "refused",
                },
                status_code=409 if isinstance(error, WriteConflict) else 422,
            )

    return [
        Route("/cases/{case_id}/tracks/{run_id}", endpoint, methods=["GET", "POST"]),
        Route("/cases/{case_id}/tracks/{run_id}/{image}", endpoint),
    ]
