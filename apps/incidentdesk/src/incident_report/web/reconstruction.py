"""Flagged local workspace routes, using the existing share session and CSRF gates."""

import secrets
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from incident_report.domain import Report
from incident_report.reconstruction.config import ReconstructionSettings
from incident_report.reconstruction.database import local_database
from incident_report.reconstruction.presentation import context
from incident_report.reconstruction.store import Workspace
from incident_report.runlog import RunLog
from incident_report.web.access import _check_origin, _csrf_token
from incident_report.web.body_limit import BodyTooLargeError, read_limited_body
from incident_report.web.review import accessible_report

# Project assumption, to be validated: these forms contain only short review fields.
MAX_WORKSPACE_BODY_BYTES = 16 * 1024


def reconstruction_context(
    log: RunLog, report: Report, settings: ReconstructionSettings | None = None
) -> dict[str, Any]:
    try:
        settings = settings if settings is not None else ReconstructionSettings.from_env()
        if not settings.enabled:
            return {}
        with psycopg.connect(local_database(log.database_url)) as connection:
            return context(Workspace(connection, settings), report, settings)
    except (ValueError, OSError, psycopg.Error):
        return {
            "reconstruction_unavailable": "Local reconstruction workspace is unavailable. Ask the owner to check its configuration."
        }


def install_reconstruction(
    app: FastAPI,
    log: RunLog,
    error_page: Callable[[Request, int, str], Response],
    settings: ReconstructionSettings,
) -> None:
    @app.get("/reconstruct/{tool}/")
    def tool_fallback(tool: str) -> Response:
        from incident_report.reconstruction.tools import unavailable

        if not settings.enabled:
            return PlainTextResponse("Not found", status_code=404)
        try:
            return PlainTextResponse(unavailable(tool), status_code=503)
        except ValueError:
            return PlainTextResponse("Not found", status_code=404)

    @app.post("/reports/{run_id}/reconstruction/{action}")
    async def change(request: Request, run_id: uuid.UUID, action: str) -> Response:
        if not settings.enabled:
            return error_page(request, 404, "Reconstruction workspace is disabled.")
        if not _check_origin(request):
            return error_page(request, 403, "Cross-origin submission refused.")
        try:
            await read_limited_body(request, max_bytes=MAX_WORKSPACE_BODY_BYTES)
        except BodyTooLargeError:
            return error_page(request, 413, "Request body exceeds maximum size limit.")
        form = await request.form(max_part_size=MAX_WORKSPACE_BODY_BYTES)
        fields = {key: value if isinstance(value, str) else "" for key, value in form.items()}
        if not secrets.compare_digest(fields.get("csrf_token", ""), _csrf_token(request)):
            return error_page(request, 403, "Invalid or missing CSRF token.")
        if request.state.attribution is None:
            return error_page(request, 403, "Open a valid share link to use the tool.")

        def execute() -> None:
            accessible_report(log, request, run_id)
            with psycopg.connect(local_database(log.database_url)) as connection:
                workspace = Workspace(connection, settings)
                if action == "attach":
                    # The UI offers a select, and the same boundary is enforced for forged submissions.
                    from incident_report.reconstruction.paths import list_bundles

                    if settings.evidence_exports_root is None or fields.get("bundle") not in list_bundles(
                        settings.evidence_exports_root
                    ):
                        raise ValueError("Choose an available local export bundle")
                    workspace.attach(run_id, fields["bundle"], fields.get("reviewer", "")[:200])
                elif action == "mapping":
                    from incident_report.reconstruction.mapping import map_track

                    map_track(
                        workspace,
                        run_id,
                        fields.get("import_id", ""),
                        fields.get("view", ""),
                        fields.get("track_ref", ""),
                        fields.get("mapping_action", ""),
                        fields.get("party_code") or None,
                        fields.get("reviewer", ""),
                        fields.get("reason", ""),
                        reconfirm_id=int(fields["reconfirm_id"]) if fields.get("reconfirm_id") else None,
                    )
                elif action == "alignment":
                    from incident_report.reconstruction.alignment import Alignment, record_alignment

                    record_alignment(
                        workspace,
                        run_id,
                        Alignment(
                            fields.get("view", ""),
                            fields.get("reference", ""),
                            float(fields.get("offset", "")),
                            float(fields.get("uncertainty", "")),
                            fields.get("clock_reliability", "unknown"),
                            fields.get("reviewer", ""),
                            fields.get("reason", ""),
                        ),
                    )
                else:
                    raise ValueError("Unknown reconstruction action")

        try:
            await run_in_threadpool(execute)
        except HTTPException as exc:
            return error_page(request, exc.status_code, str(exc.detail))
        except ValueError as exc:
            return error_page(request, 422, str(exc))
        except (OSError, psycopg.Error):
            return error_page(request, 503, "Local reconstruction workspace is unavailable.")
        return RedirectResponse(f"/reports/{run_id}/review", status_code=303)
