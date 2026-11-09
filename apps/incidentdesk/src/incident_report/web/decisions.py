"""Override, Approve and new-version routes (RPT-38).

Same gate as /reports/{id}: the share-session middleware refuses a request with no valid session before any of
this runs. These POSTs add the two form defences the feedback form uses: a same-origin check on the Origin header
and a per-session CSRF token. The bearer secret is never read here (it only works under /admin, ADR 0010).
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from incident_report.decisions import (
    MAX_REASON_TEXT_CHARS,
    OVERRIDE_REASON_LABELS,
    DecisionRefused,
    findings_of,
)
from incident_report.domain import Run
from incident_report.runlog import RunLog
from incident_report.web.access import _check_origin, _csrf_token
from incident_report.web.body_limit import BodyTooLargeError, read_limited_body
from incident_report.web.review import accessible_report

# project assumption, to be validated: the form holds a few short fields; reject anything much larger.
MAX_DECISION_BODY_BYTES = 16 * 1024

ErrorPage = Callable[[Request, int, str], Response]


def decision_context(request: Request, log: RunLog, run: Run, version: int | None) -> dict[str, Any]:
    """Template context for the decision panel. Raises DecisionRefused (404) for an unknown version."""
    if run.report is None:  # the caller got `run` from accessible_report, which never returns one without a report
        raise DecisionRefused(404, "No report with that id.")
    state = log.decisions.state(run.run_id, version)
    return {
        "decision": state,
        "findings": findings_of(run.report),
        "override_reasons": [(reason.value, label) for reason, label in OVERRIDE_REASON_LABELS.items()],
        "reason_max_chars": MAX_REASON_TEXT_CHARS,
        "violation_override_count": sum(1 for key in state.overrides if key.startswith("violation:")),
        "csrf_token": _csrf_token(request),
    }


def _text(value: UploadFile | str | None) -> str:
    return value if isinstance(value, str) else ""


def install_decisions(app: FastAPI, log: RunLog, error_page: ErrorPage) -> None:
    async def _checked_form(request: Request) -> dict[str, str] | Response:
        """Same-origin, size, and CSRF checks. Returns the form fields, or the refusal to send."""
        if not _check_origin(request):
            return error_page(request, 403, "Cross-origin submission refused.")
        try:
            await read_limited_body(request, max_bytes=MAX_DECISION_BODY_BYTES)
        except BodyTooLargeError:
            return error_page(request, 413, "Request body exceeds maximum size limit.")
        form = await request.form(max_part_size=MAX_DECISION_BODY_BYTES)
        fields = {key: _text(value) for key, value in form.items()}
        supplied = fields.get("csrf_token", "")
        if not supplied or not secrets.compare_digest(supplied, _csrf_token(request)):
            return error_page(request, 403, "Invalid or missing CSRF token.")
        return fields

    async def _decide(
        request: Request,
        run_id: uuid.UUID,
        action: Callable[[Run, dict[str, str]], object],
    ) -> Response:
        fields = await _checked_form(request)
        if isinstance(fields, Response):
            return fields
        attribution = request.state.attribution
        if attribution is None:
            return error_page(request, 403, "Open a valid share link to use the tool.")
        try:
            run = await run_in_threadpool(accessible_report, log, request, run_id)
            await run_in_threadpool(action, run, fields)
        except HTTPException as exc:
            return error_page(request, exc.status_code, str(exc.detail))
        except DecisionRefused as exc:
            return error_page(request, exc.status_code, exc.message)
        return RedirectResponse(f"/reports/{run_id}/review", status_code=303)

    @app.post("/reports/{run_id}/overrides")
    async def add_override(request: Request, run_id: uuid.UUID) -> Response:
        def action(run: Run, fields: dict[str, str]) -> object:
            return log.decisions.add_override(
                run,
                finding_key=fields.get("finding_key", ""),
                new_value=fields.get("new_value", ""),
                reason_code=fields.get("reason_code", ""),
                reason_text=fields.get("reason_text", ""),
                attribution=request.state.attribution,
                request_id=request.state.request_id,
                new_version=fields.get("new_version") == "1",
            )

        return await _decide(request, run_id, action)

    @app.post("/reports/{run_id}/approve")
    async def approve(request: Request, run_id: uuid.UUID) -> Response:
        def action(run: Run, fields: dict[str, str]) -> object:
            return log.decisions.approve(
                run, attribution=request.state.attribution, request_id=request.state.request_id
            )

        return await _decide(request, run_id, action)

    @app.post("/reports/{run_id}/versions")
    async def new_version(request: Request, run_id: uuid.UUID) -> Response:
        def action(run: Run, fields: dict[str, str]) -> object:
            return log.decisions.create_version(
                run, attribution=request.state.attribution, request_id=request.state.request_id
            )

        return await _decide(request, run_id, action)
