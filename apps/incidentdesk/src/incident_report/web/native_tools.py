"""Native producer tools: existing report access, immutable attached IDs, CSRF writes."""

import hashlib
import json
import math
import re
import secrets
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from incident_report.domain import Role
from incident_report.reconstruction.config import ReconstructionSettings
from incident_report.reconstruction.config import native_tools_enabled as native_enabled
from incident_report.reconstruction.database import local_database
from incident_report.reconstruction.native_client import ToolsRefused, ToolsUnavailable, ToolsWorker
from incident_report.reconstruction.store import Workspace
from incident_report.runlog import RunLog
from incident_report.web.access import _check_origin, _csrf_token
from incident_report.web.body_limit import BodyTooLargeError, read_limited_body
from incident_report.web.review import accessible_report
from incident_report.web.templating import build_environment


class InvalidBoundingBox(ValueError):
    """A box contains a non-finite coordinate."""


def track_action(fields: dict[str, str]) -> dict[str, Any]:
    action = fields.get("action", "")
    parameters = {
        "correct": ("track_id", "frame_number"),
        "add": ("track_id", "frame_number"),
        "create_track": ("new_track_id", "frame_number"),
        "delete_observation": ("track_id", "frame_number"),
        "delete_track": ("track_id",),
        "merge": ("primary_track_id", "secondary_track_id"),
        "split": ("track_id", "frame_number", "new_track_id"),
        "repair_gap": ("track_id", "start_frame", "end_frame"),
        "undo": (),
        "export": (),
    }
    if action not in parameters:
        raise ValueError("Choose a track review action.")
    payload: dict[str, Any] = {key: int(fields.get(key, "")) for key in parameters[action]}
    if action == "create_track":
        payload["track_id"] = payload.pop("new_track_id")
    if action in {"correct", "add", "create_track"}:
        if action != "correct" or fields.get("class_name"):
            payload["class_name"] = fields.get("class_name", "")
        if fields.get("bbox", "").strip():
            payload["bbox"] = [float(value) for value in fields["bbox"].split(",")]
            if not all(math.isfinite(value) for value in payload["bbox"]):
                raise InvalidBoundingBox("Bounding box coordinates must be finite numbers.")
    return {
        "action_id": str(uuid.UUID(fields["action_id"])),
        **(
            {
                "expected_event_id": fields.get("expected_event_id", ""),
                "expected_sequence": int(fields.get("expected_sequence", "0")),
            }
            if action == "undo"
            else {}
        ),
        "action": action,
        "payload": payload,
        "reviewer": fields.get("reviewer", "").strip(),
        "reason": fields.get("reason", "").strip(),
    }


def install_native_tools(
    app: FastAPI, log: RunLog, error_page: Callable[[Request, int, str], Response], settings: ReconstructionSettings
) -> None:
    if settings.enabled:
        native_enabled()
    templates = Jinja2Templates(env=build_environment())

    def audit_action(
        request: Request, run_id: uuid.UUID, link_id: uuid.UUID, body: dict[str, Any], stage: str, tool: str = "tracks"
    ) -> None:
        attribution = request.state.attribution
        log.audit.append(
            event_type="reconstruction",
            action=f"native.{tool}.{stage}",
            actor="visitor",
            session_id=attribution.session_id,
            link_id=attribution.link_id,
            request_id=request.state.request_id,
            run_id=run_id,
            target_type="reconstruction_attachment",
            target_id=str(link_id),
            details={
                "operation": body["action"],
                "reviewer_sha256": hashlib.sha256(body["reviewer"].encode()).hexdigest(),
                "reason_sha256": hashlib.sha256(body["reason"].encode()).hexdigest(),
            },
        )

    def selection(
        request: Request, run_id: uuid.UUID, link_id: uuid.UUID, completed_only: bool = False
    ) -> tuple[Any, Any]:
        if not settings.enabled or not native_enabled():
            raise HTTPException(404, "Native tools are disabled.")
        if getattr(request.state, "attribution", None) is None:
            raise HTTPException(403, "Open a valid share link to use the tool.")
        run = accessible_report(log, request, run_id)
        if run.report is None:
            raise HTTPException(404, "Report unavailable.")
        with psycopg.connect(local_database(log.database_url)) as connection:
            workspace = Workspace(connection, settings)
            candidates = workspace.history(run_id) if completed_only else workspace.current(run_id)
            links = [item for item in candidates if item.link_id == link_id]
            if not links or not any(
                view.sha256 == links[0].view_sha256 and view.role != Role.INTERIOR for view in run.report.views
            ):
                raise HTTPException(404, "Attached road-view selection unavailable.")
            link = links[0]
            decision = workspace.decision(link.import_id)
            if decision.envelope is None:
                raise HTTPException(404, "Re-import this evidence before opening tools.")
            source = next((item for item in decision.envelope.sources if item.view_id == link.source_view_id), None)
            if source is None:
                raise HTTPException(404, "Attached source is unavailable; re-import this evidence.")
        return link, source

    async def submitted(
        request: Request, run_id: uuid.UUID, link_id: uuid.UUID, completed_only: bool = False
    ) -> tuple[Any, Any, dict[str, str]]:
        link, source = await run_in_threadpool(selection, request, run_id, link_id, completed_only)
        if not _check_origin(request):
            raise HTTPException(403, "Cross-origin submission refused.")
        try:
            await read_limited_body(request, max_bytes=2 * 1024 * 1024)
        except BodyTooLargeError:
            raise HTTPException(413, "Request too large.") from None
        form = await request.form(max_part_size=2 * 1024 * 1024)
        fields = {key: value if isinstance(value, str) else "" for key, value in form.items()}
        if not secrets.compare_digest(fields.get("csrf_token", ""), _csrf_token(request)):
            raise HTTPException(403, "Invalid or missing CSRF token.")
        if not fields.get("reviewer", "").strip() or not fields.get("reason", "").strip():
            raise HTTPException(422, {"error": "Reviewer and reason are required.", "write_state": "refused"})
        return link, source, fields

    async def audited_write(
        request: Request, run_id: uuid.UUID, link_id: uuid.UUID, body: dict[str, Any], path: str, tool: str
    ) -> dict[str, Any]:
        await run_in_threadpool(audit_action, request, run_id, link_id, body, "requested", tool)
        result = await run_in_threadpool(ToolsWorker().json, path, body)
        try:
            await run_in_threadpool(audit_action, request, run_id, link_id, body, "saved", tool)
        except psycopg.Error:
            raise HTTPException(
                503,
                "The action completed, but audit confirmation failed. Reload saved revisions; do not repeat the action.",
            ) from None
        return result

    def replay_values(source: Any, values: Any) -> dict[str, str]:
        return {
            "scene_revision": values.get("scene_revision", "initial"),
            "marker_revision": values.get("marker_revision", "initial"),
            "review_revision": values.get("review_revision") or source.review_revision,
            "incident_run": source.incident_run,
            "frame": values.get("frame", "0"),
        }

    @app.get("/reports/{run_id}/tools/{link_id}/replay")
    @app.get("/reports/{run_id}/tools/{link_id}/replay/{resource:path}")
    def replay(request: Request, run_id: uuid.UUID, link_id: uuid.UUID, resource: str = "") -> Response:
        link, source = selection(request, run_id, link_id)
        values = replay_values(source, request.query_params)
        path = f"/cases/{quote(link.producer_case_id, safe='')}/replay/{quote(source.reconstruction_run, safe='')}"
        query = "?" + urlencode(values)
        try:
            if resource:
                if not (
                    resource in {"bundle", "video", "assets/workspace.js", "assets/workspace.css", "assets/renderer.js"}
                    or re.fullmatch(r"frame/[0-9]+", resource)
                    or re.fullmatch(r"snapshot/frame-[0-9]+-[a-f0-9]+\.png", resource)
                ):
                    raise HTTPException(404, "Unknown replay resource.")
                if resource == "video":
                    blocks, content_type, status, headers = ToolsWorker().stream(
                        path + "/video" + query, request.headers.get("range")
                    )
                    return StreamingResponse(blocks, status_code=status, media_type=content_type, headers=headers)
                resource_bytes, content_type, status, headers = ToolsWorker().exchange(path + "/" + resource + query)
                return Response(resource_bytes, status_code=status, media_type=content_type, headers=headers)
            data = ToolsWorker().json(path + query)
            return templates.TemplateResponse(
                request,
                "native_replay.html",
                {
                    "data": data,
                    "values": values,
                    "base": request.url.path,
                    "run_id": run_id,
                    "csrf_token": _csrf_token(request),
                },
            )
        except (ToolsUnavailable, OSError, psycopg.Error):
            return error_page(request, 503, "Local replay tools are unavailable.")

    @app.post("/reports/{run_id}/tools/{link_id}/replay")
    async def save_replay(request: Request, run_id: uuid.UUID, link_id: uuid.UUID) -> Response:
        link, source, fields = await submitted(request, run_id, link_id, request.headers.get("x-native-retry") == "1")
        try:
            action = fields.get("action", "")
            if action not in {
                "scene_save",
                "marker_save",
                "snapshot_request",
                "incident_review",
                "incident_export",
                "reconstruction_export",
                "export_attach",
            }:
                raise ValueError("Unknown action")
            body: dict[str, Any] = {
                **replay_values(source, fields),
                "action": action,
                "reviewer": fields["reviewer"].strip(),
                "reason": fields["reason"].strip(),
                "action_id": fields.get("action_id") or str(uuid.uuid4()),
            }
            if action in {"scene_save", "marker_save"}:
                body["document"] = json.loads(fields["document"])
            elif action in {"snapshot_request", "reconstruction_export"}:
                body["source_frame"] = int(fields.get("source_frame", "0"))
            elif action == "incident_review":
                body["decision"] = {
                    "target_kind": fields["target_kind"],
                    "target_id": fields["target_id"],
                    "action": fields["decision_action"],
                    "note": fields.get("note", ""),
                    "modified_peak_time": float(fields["modified_peak_time"])
                    if fields.get("modified_peak_time")
                    else None,
                    "modified_location": [float(value) for value in fields["modified_location"].split(",")]
                    if fields.get("modified_location")
                    else None,
                    "modified_outcome": fields.get("modified_outcome") or None,
                    "modified_contributing_track_ids": [
                        int(value) for value in fields["modified_contributing_track_ids"].split(",")
                    ]
                    if fields.get("modified_contributing_track_ids")
                    else ([] if fields.get("change_contributors") == "yes" else None),
                }
            elif action == "export_attach":
                with psycopg.connect(local_database(log.database_url)) as connection:
                    decision = Workspace(connection, settings).decision(link.import_id)
                    if decision.envelope is None:
                        raise ValueError("Re-import the evidence first")
                    body["export_template"] = decision.envelope.model_dump(mode="json")
                    body["source_view_id"] = link.source_view_id
            path = f"/cases/{quote(link.producer_case_id, safe='')}/replay/{quote(source.reconstruction_run, safe='')}"
            if link.superseded_by is not None:
                # Historical selections may read completed results, never start writes.
                result = await run_in_threadpool(ToolsWorker().json, path + "/completed", body)
                if action == "export_attach":
                    from incident_report.evidence_import.reader import inspect_bundle
                    from incident_report.reconstruction.paths import bundle_path

                    if settings.evidence_exports_root is None:
                        raise ToolsUnavailable("Exports root unavailable")
                    bundle_name = str(result.get("bundle", ""))
                    if not re.fullmatch(r"native-[a-f0-9]{32}", bundle_name):
                        raise ToolsUnavailable("Invalid completed export identifier")
                    inspected = await run_in_threadpool(
                        inspect_bundle, bundle_path(settings.evidence_exports_root, bundle_name)
                    )
                    with psycopg.connect(local_database(log.database_url)) as connection:
                        matched = next(
                            (
                                item
                                for item in Workspace(connection, settings).history(run_id)
                                if item.import_id == inspected.import_id and item.view_sha256 == link.view_sha256
                            ),
                            None,
                        )
                    if matched is None:
                        return JSONResponse(
                            {
                                "error": "Export completed but this retired selection cannot attach it. Inspect import history and attach from the incident workspace.",
                                "bundle": bundle_name,
                                "review_url": f"/reports/{run_id}/review",
                                "write_state": "refused",
                            },
                            status_code=409,
                        )
                    result.update(
                        new_link_id=str(matched.link_id), review_url=f"/reports/{run_id}/review", recovered=True
                    )
                return JSONResponse(result)
            result = await audited_write(request, run_id, link_id, body, path, "replay")
            if action == "export_attach":
                bundle_name = str(result.get("bundle", ""))
                if not re.fullmatch(r"native-[a-f0-9]{32}", bundle_name):
                    raise ToolsUnavailable("Invalid exported bundle identifier")

                def attach_export() -> str:
                    from incident_report.reconstruction.paths import list_bundles

                    if settings.evidence_exports_root is None or bundle_name not in list_bundles(
                        settings.evidence_exports_root
                    ):
                        raise ValueError("Export bundle is unavailable")
                    with psycopg.connect(local_database(log.database_url)) as connection:
                        attachments = Workspace(connection, settings).attach(run_id, bundle_name, body["reviewer"])
                        return str(attachments[0].link_id)

                try:
                    new_link_id = await run_in_threadpool(attach_export)
                except (ValueError, OSError, psycopg.Error):
                    return JSONResponse(
                        {
                            "error": "Export completed; attachment was not confirmed. Check import history and select this exported bundle in the incident workspace before repeating.",
                            "bundle": bundle_name,
                            "review_url": f"/reports/{run_id}/review",
                        },
                        status_code=503,
                    )
                result.update(new_link_id=new_link_id, review_url=f"/reports/{run_id}/review")
            return JSONResponse(result)
        except ToolsRefused as error:
            return JSONResponse({"error": str(error), "write_state": "refused"}, status_code=error.status_code)
        except ToolsUnavailable:
            return error_page(
                request, 503, "The worker did not confirm completion. Inspect saved revisions before submitting again."
            )
        except (ValueError, KeyError, TypeError):
            return JSONResponse(
                {"error": "Check replay fields, attribution and selected revisions.", "write_state": "refused"},
                status_code=422,
            )
        except (OSError, psycopg.Error):
            return error_page(request, 503, "Local audit or tools are unavailable.")

    @app.get("/reports/{run_id}/tools/{link_id}/calibration")
    @app.get("/reports/{run_id}/tools/{link_id}/calibration/frame")
    def calibration(
        request: Request, run_id: uuid.UUID, link_id: uuid.UUID, frame: int = 0, revision: str | None = None
    ) -> Response:
        link, source = selection(request, run_id, link_id)
        current_revision = revision or source.calibration_revision
        path = f"/cases/{quote(link.producer_case_id, safe='')}/calibration/{quote(current_revision, safe='')}"
        try:
            if request.url.path.endswith("/frame"):
                body, content_type, status, headers = ToolsWorker().exchange(f"{path}/image?frame={frame}")
                return Response(
                    body, status_code=status, media_type=content_type, headers={**headers, "Cache-Control": "no-store"}
                )
            data = ToolsWorker().json(f"{path}?frame={frame}")
            return templates.TemplateResponse(
                request,
                "native_calibration.html",
                {
                    "data": data,
                    "features_json": json.dumps(data["features"]),
                    "current_revision": current_revision,
                    "action_id": str(uuid.uuid4()),
                    "base": request.url.path,
                    "frame": frame,
                    "run_id": run_id,
                    "csrf_token": _csrf_token(request),
                },
            )
        except (ToolsUnavailable, OSError, psycopg.Error):
            return error_page(request, 503, "Local calibration tools are unavailable.")

    @app.post("/reports/{run_id}/tools/{link_id}/calibration")
    async def save_calibration(request: Request, run_id: uuid.UUID, link_id: uuid.UUID) -> Response:
        link, source, fields = await submitted(request, run_id, link_id)
        try:
            body: dict[str, Any] = {
                key: fields.get(key, "")
                for key in ("reviewer", "reason", "action", "mode", "world_unit", "control_text", "validation_text")
            }
            body.update(
                frame=int(fields.get("frame", "")),
                validation_tolerance=float(fields.get("validation_tolerance", "")),
                features=json.loads(fields.get("features", "[]")),
            )
            path = f"/cases/{quote(link.producer_case_id, safe='')}/calibration/{quote(fields.get('calibration_revision') or source.calibration_revision, safe='')}"
            if body["action"] == "save":
                body["action_id"] = str(uuid.UUID(fields["action_id"]))
                result = await audited_write(request, run_id, link_id, body, path, "calibration")
                saved_revision = quote(str(result["revision"]), safe="")
                redirect = request.url.path + f"?saved={saved_revision}&revision={saved_revision}&frame={body['frame']}"
                if request.headers.get("accept") == "application/json":
                    return JSONResponse({"redirect": redirect})
                return RedirectResponse(
                    redirect,
                    status_code=303,
                )
            if body["action"] not in {"preview", "download"}:
                raise ValueError("Unknown action")
            result = await run_in_threadpool(ToolsWorker().json, path, body)
            if body["action"] == "download":
                return Response(
                    json.dumps(result["calibration"], sort_keys=True, indent=2) + "\n",
                    media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="calibration.json"'},
                )
            return JSONResponse(result)
        except ToolsRefused as error:
            return JSONResponse({"error": str(error), "write_state": "refused"}, status_code=error.status_code)
        except ToolsUnavailable:
            return error_page(
                request, 503, "The worker did not confirm completion. Inspect saved revisions before submitting again."
            )
        except (ValueError, KeyError, TypeError):
            return JSONResponse(
                {"error": "Check calibration points, features and validation fields.", "write_state": "refused"},
                status_code=422,
            )
        except (OSError, psycopg.Error):
            return error_page(request, 503, "Local audit or tools are unavailable.")

    @app.get("/reports/{run_id}/tools/{link_id}/tracks")
    @app.get("/reports/{run_id}/tools/{link_id}/tracks/frame")
    def tracks(request: Request, run_id: uuid.UUID, link_id: uuid.UUID, frame: int = 0) -> Response:
        link, source = selection(request, run_id, link_id)
        if frame < 0:
            raise HTTPException(422, "Choose a nonnegative source frame.")
        worker_path = f"/cases/{quote(link.producer_case_id, safe='')}/tracks/{quote(source.tracking_run, safe='')}"
        try:
            worker = ToolsWorker()
            if request.url.path.endswith("/frame"):
                body, content_type, status, headers = worker.exchange(f"{worker_path}/image?frame={frame}")
                return Response(
                    body, status_code=status, media_type=content_type, headers={**headers, "Cache-Control": "no-store"}
                )
            data = worker.json(f"{worker_path}?frame={frame}")
            return templates.TemplateResponse(
                request,
                "native_tracks.html",
                {
                    "data": data,
                    "frame": frame,
                    "base": str(request.url.path),
                    "action_id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "csrf_token": _csrf_token(request),
                },
            )
        except (ToolsUnavailable, OSError, psycopg.Error):
            return error_page(request, 503, "Local tools are unavailable. Ask the owner to start the tools worker.")

    @app.post("/reports/{run_id}/tools/{link_id}/tracks")
    async def save_tracks(request: Request, run_id: uuid.UUID, link_id: uuid.UUID) -> Response:
        link, source = await run_in_threadpool(selection, request, run_id, link_id)
        if not _check_origin(request):
            return error_page(request, 403, "Cross-origin submission refused.")
        try:
            await read_limited_body(request, max_bytes=16 * 1024)
            form = await request.form(max_part_size=16 * 1024)
            fields = {key: value if isinstance(value, str) else "" for key, value in form.items()}
            if not secrets.compare_digest(fields.get("csrf_token", ""), _csrf_token(request)):
                return error_page(request, 403, "Invalid or missing CSRF token.")
            body = track_action(fields)
            if not body["reviewer"] or not body["reason"]:
                raise ValueError("Reviewer and reason are required.")
            path = f"/cases/{quote(link.producer_case_id, safe='')}/tracks/{quote(source.tracking_run, safe='')}"
            await run_in_threadpool(audit_action, request, run_id, link_id, body, "requested")
            await run_in_threadpool(ToolsWorker().json, path, body)
            try:
                await run_in_threadpool(audit_action, request, run_id, link_id, body, "saved")
            except psycopg.Error:
                return error_page(
                    request,
                    503,
                    "The action completed, but audit confirmation failed. Reload saved revisions; do not repeat the action.",
                )
        except BodyTooLargeError:
            return error_page(request, 413, "Request body exceeds maximum size limit.")
        except InvalidBoundingBox as error:
            return JSONResponse({"error": str(error), "write_state": "refused"}, status_code=400)
        except ToolsRefused as error:
            return JSONResponse({"error": str(error), "write_state": "refused"}, status_code=error.status_code)
        except ToolsUnavailable:
            return error_page(
                request, 503, "The worker did not confirm completion. Inspect saved revisions before submitting again."
            )
        except (ValueError, KeyError, TypeError):
            return JSONResponse(
                {"error": "Check the action fields, reviewer and reason.", "write_state": "refused"}, status_code=422
            )
        except (OSError, psycopg.Error):
            return error_page(request, 503, "No save was started: local audit or evidence storage is unavailable.")
        if request.headers.get("accept") == "application/json":
            return JSONResponse({"redirect": request.url.path})
        return RedirectResponse(request.url.path, status_code=303)
