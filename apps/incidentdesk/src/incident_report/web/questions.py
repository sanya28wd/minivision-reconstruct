"""Officer question form: same Report access gate, CSRF, bounded input and isolated persistence."""

import secrets
import uuid
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from incident_report.ai import AIConnectionError
from incident_report.questions import (
    MAX_QA_BODY_BYTES,
    MAX_QUESTION_CHARS,
    QAConnection,
    QuestionRateLimited,
    QuestionStore,
)
from incident_report.runlog import RunLog
from incident_report.storage import EvidenceStore
from incident_report.web.access import _contains_disallowed_control_chars, _csrf_token
from incident_report.web.body_limit import BodyTooLargeError, read_limited_body
from incident_report.web.review import accessible_report


def install_questions(
    app: FastAPI,
    log: RunLog,
    store: QuestionStore,
    qa: QAConnection,
    evidence: EvidenceStore | None,
    error_page: Callable[[Request, int, str], Response],
) -> None:
    @app.post("/reports/{run_id}/questions")
    async def ask_question(request: Request, run_id: uuid.UUID) -> Response:
        try:
            run = await run_in_threadpool(accessible_report, log, request, run_id)
        except HTTPException as exc:
            return error_page(request, exc.status_code, str(exc.detail))
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != f"{request.url.scheme}://{request.url.netloc}":
            return error_page(request, 403, "Cross-origin submission refused.")
        try:
            await read_limited_body(request, max_bytes=MAX_QA_BODY_BYTES)
        except BodyTooLargeError:
            return error_page(request, 413, "Question request exceeds the size limit.")
        form = await request.form(max_part_size=MAX_QA_BODY_BYTES)
        csrf = form.get("csrf_token")
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, _csrf_token(request)):
            return error_page(request, 403, "Invalid or missing CSRF token.")
        question = form.get("question")
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > MAX_QUESTION_CHARS
            or _contains_disallowed_control_chars(question, allow_newlines=True)
        ):
            return error_page(request, 422, "Enter a question of at most 2000 characters without control characters.")
        if evidence is None:
            return error_page(request, 503, "Q&A needs the original videos. Ask the owner to configure storage.")
        if run.report is None:
            return error_page(request, 404, "No report with that id.")
        try:
            await run_in_threadpool(store.ask, run.report, question, qa=qa, evidence=evidence)
        except QuestionRateLimited:
            return error_page(request, 429, "Too many questions for this Report. Try again in one minute.")
        except AIConnectionError as exc:
            return error_page(request, 503, str(exc))
        return RedirectResponse(f"/reports/{run_id}#officer-question", status_code=303)
