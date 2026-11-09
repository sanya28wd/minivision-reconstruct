"""The web app. Routes call only the incident entry point (seam 1) and the run log.

Framework facts relied on (checked against the installed package source, since the official
docs site was unreachable from the build sandbox):
- Starlette 1.7.0 Jinja2Templates: TemplateResponse(request, name, context, status_code);
  an Environment passed as `env=` is used as given (starlette/templating.py).
- Starlette's default Environment uses select_autoescape(), which escapes only .html/.htm/.xml
  templates; we build our own Environment with autoescape=True so nothing depends on filenames.
- Uploads: Starlette's multipart parser spools each file to a SpooledTemporaryFile (memory up to
  1 MB, then an OS temp file removed when the request closes it); form text fields are capped at
  1 MB per part (starlette/formparsers.py).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import os
import secrets
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from incident_report.access import AccessStore
from incident_report.agreement import AgreementSettings
from incident_report.ai import AIConnection, AIConnectionError, ReplayAIConnection
from incident_report.ai.gemini import GeminiAIConnection
from incident_report.ai.google_video import GoogleVideoTransport, PreparedVideo
from incident_report.config import AccessSettings, ProcessingSettings, Settings
from incident_report.decisions import DecisionRefused
from incident_report.domain import InvalidIncident, Origin, Role, Run, RunStatus, View
from incident_report.entry import PROMPT_VERSION, analyse_incident
from incident_report.feedback import (
    MAX_FEEDBACK_BODY_SIZE_BYTES,
    MAX_FEEDBACK_TEXT_CHARS,
    MAX_NAME_CHARS,
    MAX_SCREENSHOT_SIZE_BYTES,
    MAX_USER_AGENT_CHARS,
    FeedbackCategory,
    FeedbackStore,
    FeedbackSubmission,
    detect_image_type,
    sanitize_screen_path,
)
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.intake import configured_store, receive_view
from incident_report.intake_limits import IntakeLimits
from incident_report.package import PackageRefused
from incident_report.package import build as build_report_package
from incident_report.questions import QAConnection, QuestionStore, ReplayQAConnection
from incident_report.reconstruction.config import ReconstructionSettings
from incident_report.runlog import RunLog
from incident_report.sample_reports import BANNER, SampleReport, load_samples
from incident_report.storage import EvidenceStore, StorageError, create_evidence_store
from incident_report.storage.errors import InvalidEvidenceKey
from incident_report.storage.interface import validate_sha256_key
from incident_report.web.access import (
    _check_origin,
    _contains_disallowed_control_chars,
    _csrf_token,
    install_access,
)
from incident_report.web.body_limit import BodyTooLargeError, read_limited_body
from incident_report.web.decisions import decision_context, install_decisions
from incident_report.web.native_tools import install_native_tools
from incident_report.web.printing import render_print
from incident_report.web.questions import install_questions
from incident_report.web.reconstruction import install_reconstruction, reconstruction_context
from incident_report.web.review import accessible_report, install_review
from incident_report.web.staging import StagedUpload, StagingUnavailable, UploadStages
from incident_report.web.templating import build_environment
from incident_report.web.uploads import staged_form

logger = logging.getLogger(__name__)

# Longest accepted label, acquisition-method note or filename, in characters.
# project assumption, to be validated: a generous bound that keeps pages and rows small.
MAX_TEXT_FIELD_CHARS = 200


_STATIC_DIR = Path(__file__).parent / "static"


class EventBroadcaster:
    """Thread-safe SSE event broadcaster for active analysis runs."""

    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]]] = {}

    def subscribe(
        self, run_id: uuid.UUID, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        self._subscribers.setdefault(run_id, []).append((loop, queue))

    def unsubscribe(self, run_id: uuid.UUID, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if run_id in self._subscribers:
            self._subscribers[run_id] = [p for p in self._subscribers[run_id] if p[1] is not queue]
            if not self._subscribers[run_id]:
                del self._subscribers[run_id]

    def emit(self, run_id: uuid.UUID, event: str, data: dict[str, Any]) -> None:
        msg = {"event": event, "data": data}
        for loop, q in list(self._subscribers.get(run_id, [])):
            try:
                loop.call_soon_threadsafe(q.put_nowait, msg)
            except RuntimeError:
                pass


def create_app(
    *,
    ai: AIConnection | None = None,
    run_log: RunLog | None = None,
    evidence: EvidenceStore | None = None,
    samples_dir: Path | None = None,
    prompt_version: str = PROMPT_VERSION,
    qa: QAConnection | None = None,
) -> FastAPI:
    """Build the app. With no arguments, settings come from the environment (see README).

    app.state.evidence holds the configured storage adapter. An unconfigured deployment
    can display pages, but refuses uploads rather than losing evidence.
    """
    reconstruction_settings = ReconstructionSettings.from_env()
    # Override OS MIME mappings (including the Windows registry) for consistent static responses.
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("font/woff2", ".woff2")

    if ai is None or run_log is None:
        settings = Settings.from_env()
        ai = ai or ReplayAIConnection(settings.recordings_dir)
        run_log = run_log or RunLog(settings.database_url)
        evidence = evidence or create_evidence_store(settings)
    if evidence is None and os.environ.get("INCIDENT_EVIDENCE_DIR", "").strip():
        evidence = configured_store()
    limits = IntakeLimits.from_env()
    ai_connection, log = ai, run_log

    # autoescape is on for every template (CLAUDE.md guardrail); see web/templating.py.
    templates = Jinja2Templates(env=build_environment())
    preupload_slots = threading.BoundedSemaphore(limits.stage_workers)

    def prepare_public(sha256: str) -> PreparedVideo | None:
        # Existing connection declaration is the only authority for public footage.
        # Never infer public status from a filename, request field or selected role.
        if not isinstance(ai_connection, GeminiAIConnection) or not isinstance(
            ai_connection.transport, GoogleVideoTransport
        ):
            return None
        if not preupload_slots.acquire(blocking=False):
            return None
        try:
            return ai_connection.transport.preupload(
                ai_connection.video, sha256, ai_connection.settings, on_done=preupload_slots.release
            )
        except Exception as exc:
            logger.warning("Optional video pre-upload unavailable: %s", type(exc).__name__)
            preupload_slots.release()
            return None

    stages = UploadStages(evidence, limits, prepare_public) if evidence is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if stages:
                stages.close()

    app = FastAPI(title="Incident Video Report (prototype)", lifespan=lifespan)
    # RPT-04: the stylesheets and self-hosted fonts of the shared shell. StaticFiles refuses paths that
    # leave the directory (starlette/staticfiles.py, get_path / lookup_path).
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.state.evidence = evidence
    broadcaster = EventBroadcaster()
    app.state.broadcaster = broadcaster
    proc_settings = ProcessingSettings.from_env()
    officer_agreement = AgreementSettings(n_runs=1)
    research_runs = os.environ.get("INCIDENT_AGREEMENT_RUNS")
    if research_runs is not None and research_runs.strip() != "1":
        logger.warning("INCIDENT_AGREEMENT_RUNS is ignored for officer Runs; using one analysis.")

    question_store = QuestionStore(log.database_url)
    qa_connection = qa or ReplayQAConnection(Path(__file__).parents[1] / "ai" / "recordings")
    feedback_store = FeedbackStore(log.database_url)
    install_access(
        app,
        AccessStore(log.database_url),
        AccessSettings.from_env(),
        templates=templates,
        feedback_store=feedback_store,
    )

    def error_page(request: Request, status: int, message: str) -> Response:
        return templates.TemplateResponse(request, "error.html", {"message": message}, status_code=status)

    @app.get("/share", response_class=HTMLResponse)
    def share(request: Request) -> Response:
        return templates.TemplateResponse(request, "share.html", {})

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "upload.html",
            {"roles": list(Role), "max_chars": MAX_TEXT_FIELD_CHARS, "staging_csrf": _csrf_token(request)},
        )

    @app.post("/uploads/stage")
    async def stage_upload(request: Request) -> Response:
        if not _check_origin(request) or not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), _csrf_token(request)
        ):
            return JSONResponse({"detail": "Invalid or missing CSRF token."}, status_code=403)
        if evidence is None or stages is None:
            return error_page(request, 503, "Evidence storage is not configured.")
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            return error_page(request, 400, "Choose a video file first.")
        try:
            async with staged_form(request, evidence, limits) as form:
                files = [v for _, v in form.multi_items() if isinstance(v, UploadFile)]
                if len(files) != 1 or not _basename(files[0].filename or "") or not files[0].size:
                    raise InvalidIncident("Stage exactly one video at a time.")
                filename = _basename(files[0].filename or "")
                if len(filename) > MAX_TEXT_FIELD_CHARS or _contains_disallowed_control_chars(filename):
                    raise InvalidIncident("The file name is invalid or too long.")
                token = await run_in_threadpool(
                    stages.add, files[0].file, owner=str(request.state.attribution.session_id), filename=filename
                )
                return JSONResponse({"staging_id": token}, status_code=201)
        except (InvalidIncident, ValueError, TimeoutError):
            return error_page(request, 422, "The temporary upload is invalid or exceeds the configured limits.")
        except (StorageError, OSError):
            return error_page(request, 503, "Temporary upload storage is unavailable.")

    @app.post("/reports")
    async def create_report(request: Request, background_tasks: BackgroundTasks) -> Response:
        if evidence is None:
            return error_page(request, 503, "Evidence storage is not configured. Ask the owner to configure it.")
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            return error_page(request, 400, "Choose a video file first.")
        try:
            async with (
                staged_form(request, evidence, limits) as form,
                _consumed_stages(form, stages, str(request.state.attribution.session_id)) as staged_uploads,
            ):
                interior_uploads = [f for f in form.getlist("interior_files") if isinstance(f, UploadFile)]
                exterior_uploads = [f for f in form.getlist("exterior_files") if isinstance(f, UploadFile)]
                legacy_uploads = [f for f in form.getlist("video") if isinstance(f, UploadFile)]

                valid_interior = [f for f in interior_uploads if _basename(f.filename or "") and f.size != 0]
                valid_exterior = [f for f in exterior_uploads if _basename(f.filename or "") and f.size != 0]
                valid_legacy = [f for f in legacy_uploads if _basename(f.filename or "") and f.size != 0]

                if not valid_interior and not valid_exterior and not valid_legacy and not staged_uploads:
                    return error_page(request, 400, "Choose a video file first.")

                # Separate text fields from file uploads
                text_data = {
                    k: v
                    for k, v in form.items()
                    if k
                    not in ("video", "interior_files", "exterior_files", "interior_staging_ids", "exterior_staging_ids")
                    and not hasattr(v, "file")
                }
                try:
                    fields = _FormFields.model_validate(text_data)
                except ValidationError:
                    return error_page(request, 422, "Choose an Interior or Exterior role and use shorter text fields.")

                all_valid = valid_interior + valid_exterior + valid_legacy
                for f in all_valid:
                    fname = _basename(f.filename or "")
                    if len(fname) > MAX_TEXT_FIELD_CHARS:
                        return error_page(request, 422, "The file name is too long.")

                views: list[View] = []
                for f in valid_interior:
                    views.append(
                        View(
                            stream=f.file,
                            filename=_basename(f.filename or ""),
                            role=Role.INTERIOR,
                            label=_clean(fields.interior_label) or _clean(fields.label),
                            acquisition_method=_clean(fields.interior_acquisition_method)
                            or _clean(fields.acquisition_method),
                        )
                    )
                for f in valid_exterior:
                    views.append(
                        View(
                            stream=f.file,
                            filename=_basename(f.filename or ""),
                            role=Role.EXTERIOR,
                            label=_clean(fields.exterior_label) or _clean(fields.label),
                            acquisition_method=_clean(fields.exterior_acquisition_method)
                            or _clean(fields.acquisition_method),
                        )
                    )
                for f in valid_legacy:
                    if fields.role is None:
                        return error_page(
                            request, 422, "Choose an Interior or Exterior role and use shorter text fields."
                        )
                    views.append(
                        View(
                            stream=f.file,
                            filename=_basename(f.filename or ""),
                            role=fields.role,
                            label=_clean(fields.label),
                            acquisition_method=_clean(fields.acquisition_method),
                        )
                    )

                for role, staged in staged_uploads:
                    if staged.handle is None:
                        raise InvalidIncident("Temporary upload is closed.")
                    views.append(
                        View(
                            stream=staged.handle,
                            filename=staged.filename,
                            role=role,
                            label=_clean(fields.interior_label if role is Role.INTERIOR else fields.exterior_label)
                            or _clean(fields.label),
                            acquisition_method=_clean(
                                fields.interior_acquisition_method
                                if role is Role.INTERIOR
                                else fields.exterior_acquisition_method
                            )
                            or _clean(fields.acquisition_method),
                        )
                    )
                if (
                    len(views) > limits.max_files
                    or sum(v.stream.seek(0, 2) for v in views) > limits.max_total_upload_bytes
                ):
                    raise InvalidIncident("The upload exceeds the configured count or total size limit.")
                for v in views:
                    v.stream.seek(0)
                if not views:
                    return error_page(request, 400, "Choose a video file first.")

                run_id = uuid.uuid4()
                attribution = request.state.attribution
                session = str(attribution.session_id) if attribution else str(uuid.uuid4())
                accepted = []
                for number, v in enumerate(views):
                    accepted_view = receive_view(
                        v,
                        evidence,
                        session_id=session,
                        limits=limits,
                        record=partial(log.record_intake_view, run_id, number),
                    )
                    accepted.append(accepted_view)
                    log.record_upload_audit(
                        run_id=run_id,
                        view_number=number,
                        view=accepted_view,
                        attribution=attribution,
                        request_id=request.state.request_id,
                    )
                report_views = tuple(accepted)
                input_hash = incident_input_hash((v.role, v.sha256) for v in report_views)

                officer_q = _clean(fields.officer_question)
                if officer_q:
                    log.record_officer_question(run_id, officer_q)

                log.record(
                    Run(
                        run_id=run_id,
                        origin=Origin.APP,
                        created_at=datetime.now(UTC),
                        input_hash=input_hash,
                        report=None,
                        status=RunStatus.PROCESSING,
                        total_latency_ms=0.0,
                        current_step="overview",
                        attribution=attribution,
                        request_id=request.state.request_id,
                    )
                )
                request.state.run_id = run_id

                def run_analysis() -> None:
                    started_at = datetime.now(UTC)

                    def step_cb(step: str) -> None:
                        now = datetime.now(UTC)
                        elapsed = round((now - started_at).total_seconds(), 1)
                        broadcaster.emit(
                            run_id,
                            "step",
                            {
                                "step": step,
                                "status": "processing",
                                "started_at": started_at.isoformat(),
                                "elapsed_seconds": elapsed,
                            },
                        )

                    try:
                        analyse_incident(
                            report_views,
                            Origin.APP,
                            prompt_version=prompt_version,
                            ai=ai_connection,
                            run_log=log,
                            evidence=evidence,
                            limits=limits,
                            attribution=attribution,
                            request_id=request.state.request_id,
                            officer_question=officer_q,
                            run_id=run_id,
                            on_step=step_cb,
                            processing_settings=proc_settings,
                            agreement_settings=officer_agreement,
                        )
                        broadcaster.emit(
                            run_id,
                            "done",
                            {
                                "step": "done",
                                "status": "succeeded",
                                "redirect": f"/reports/{run_id}",
                                "started_at": started_at.isoformat(),
                                "elapsed_seconds": round((datetime.now(UTC) - started_at).total_seconds(), 1),
                            },
                        )
                    except Exception as exc:
                        logger.error("Analysis failed for run %s: %s", run_id, type(exc).__name__)
                        stored_error = "Analysis failed."
                        try:
                            fresh_run = log.get(run_id)
                            if fresh_run.status is RunStatus.PROCESSING:
                                log.record(
                                    fresh_run.model_copy(
                                        update={
                                            "status": RunStatus.FAILED,
                                            "error": "Analysis failed.",
                                            "current_step": None,
                                            "total_latency_ms": (datetime.now(UTC) - started_at).total_seconds() * 1000,
                                        }
                                    )
                                )
                            if fresh_run.error:
                                stored_error = fresh_run.error
                        except Exception as store_exc:
                            logger.error("Failed to finalize run %s: %s", run_id, type(store_exc).__name__)
                        now = datetime.now(UTC)
                        elapsed = round((now - started_at).total_seconds(), 1)
                        broadcaster.emit(
                            run_id,
                            "error",
                            {
                                "step": "failed",
                                "status": "failed",
                                "error": stored_error,
                                "started_at": started_at.isoformat(),
                                "elapsed_seconds": elapsed,
                            },
                        )

                prepared = []
                for _, staged in staged_uploads:
                    lease = staged.detach_ready_preparation()
                    if lease is not None:
                        prepared.append(lease)

                def run_prepared_analysis() -> None:
                    try:
                        with ExitStack() as stack:
                            if isinstance(ai_connection, GeminiAIConnection) and isinstance(
                                ai_connection.transport, GoogleVideoTransport
                            ):
                                stack.enter_context(ai_connection.transport.preuploaded_scope(prepared))
                            run_analysis()
                    finally:
                        for lease in prepared:
                            lease.close()

                background_tasks.add_task(run_prepared_analysis)
                response: Response = RedirectResponse(f"/reports/{run_id}", status_code=303)
        except StagingUnavailable as exc:
            response = error_page(request, 422, str(exc))
            response.headers["X-Staging-Unavailable"] = "1"
        except (AIConnectionError, InvalidIncident) as exc:
            response = error_page(request, 422, str(exc))
        except (StorageError, OSError):
            response = error_page(request, 503, "Evidence storage is unavailable. Ask the owner to check it.")
        return response

    @app.get("/reports/{run_id}", response_class=HTMLResponse)
    def show_report(request: Request, run_id: uuid.UUID, version: int | None = Query(default=None, ge=1)) -> Response:
        try:
            run = log.get(run_id)
        except KeyError:
            return error_page(request, 404, "No report with that id.")

        request.state.run_id = run.run_id

        if run.status is RunStatus.PROCESSING:
            elapsed_seconds = round((datetime.now(UTC) - run.created_at).total_seconds(), 1)
            return templates.TemplateResponse(
                request,
                "processing.html",
                {
                    "run": run,
                    "current_step": run.current_step or "uploading",
                    "elapsed_seconds": elapsed_seconds,
                },
            )

        if run.status is RunStatus.FAILED:
            return templates.TemplateResponse(
                request,
                "processing.html",
                {
                    "run": run,
                    "error": run.error or "Analysis failed.",
                },
                status_code=422,
            )

        if run.status is RunStatus.SUCCEEDED and run.report is not None:
            try:
                context = decision_context(request, log, run, version)
                context.update(reconstruction_context(log, run.report, reconstruction_settings))
            except DecisionRefused as exc:
                return error_page(request, exc.status_code, exc.message)
            return templates.TemplateResponse(
                request,
                "report.html",
                {"report": run.report, "questions": question_store.list_for_report(run_id), **context},
            )

        return error_page(request, 404, "No report with that id.")

    # RPT-40: ready-made Reports from recorded model output. Built once, on first use, by the replay
    # path; no AI connection is touched. The same access gate as every other page applies.
    sample_lock = threading.Lock()
    sample_cache: list[dict[str, SampleReport]] = []

    def built_samples() -> dict[str, SampleReport]:
        with sample_lock:
            if not sample_cache:
                sample_cache.append({s.sample_id: s for s in load_samples(samples_dir)})
            return sample_cache[0]

    @app.get("/samples", response_class=HTMLResponse)
    def list_samples(request: Request) -> Response:
        samples = list(built_samples().values())
        return templates.TemplateResponse(request, "samples.html", {"samples": samples, "banner_text": BANNER})

    @app.get("/samples/{sample_id}", response_class=HTMLResponse)
    def show_sample(request: Request, sample_id: str) -> Response:
        sample = built_samples().get(sample_id)
        if sample is None:
            return error_page(request, 404, "No sample with that id.")
        return templates.TemplateResponse(
            request, "report.html", {"report": sample.report, "sample": sample, "banner_text": BANNER}
        )

    incidents_page_size = 50

    @app.get("/incidents", response_class=HTMLResponse)
    def list_incidents(
        request: Request,
        page: int = Query(default=1, ge=1),
    ) -> Response:
        attribution = request.state.attribution  # set by the access middleware; no session means 403 before here
        offset = (page - 1) * incidents_page_size
        rows = log.list_runs_for_link(attribution.link_id, limit=incidents_page_size + 1, offset=offset)
        has_next = len(rows) > incidents_page_size
        paged_rows = rows[:incidents_page_size]
        has_prev = page > 1

        return templates.TemplateResponse(
            request,
            "incidents.html",
            {
                "incidents": paged_rows,
                "page": page,
                "has_prev": has_prev,
                "has_next": has_next,
                "base_path": "/incidents",
                "is_admin": False,
            },
        )

    @app.get("/admin/incidents")
    def admin_list_incidents(
        request: Request,
        page: int = Query(default=1, ge=1),
    ) -> Response:
        offset = (page - 1) * incidents_page_size
        rows = log.list_all_runs(limit=incidents_page_size + 1, offset=offset)
        has_next = len(rows) > incidents_page_size
        paged_rows = rows[:incidents_page_size]
        has_prev = page > 1

        accept = request.headers.get("accept", "")
        if templates is None or ("application/json" in accept and "text/html" not in accept):
            return JSONResponse(
                {
                    "incidents": [
                        {
                            "run_id": str(r.run_id),
                            "created_at": r.created_at.isoformat(),
                            "status": r.status_label,
                            "view_count": r.view_count,
                            "is_ready": r.is_ready,
                            "link_id": str(r.run.attribution.link_id) if r.run.attribution else None,
                        }
                        for r in paged_rows
                    ],
                    "page": page,
                    "has_prev": has_prev,
                    "has_next": has_next,
                }
            )

        return templates.TemplateResponse(
            request,
            "incidents.html",
            {
                "incidents": paged_rows,
                "page": page,
                "has_prev": has_prev,
                "has_next": has_next,
                "base_path": "/admin/incidents",
                "page_title": "All Incidents",
                "page_description": "All logged incidents across all visitor sessions.",
                "is_admin": True,
            },
        )

    @app.get("/reports/{run_id}/review", response_class=HTMLResponse)
    def review_workspace(
        request: Request, run_id: uuid.UUID, version: int | None = Query(default=None, ge=1)
    ) -> Response:
        try:
            run = accessible_report(log, request, run_id)
            context = decision_context(request, log, run, version)
            if run.report is not None:
                context.update(reconstruction_context(log, run.report, reconstruction_settings))
        except HTTPException as exc:
            return error_page(request, exc.status_code, str(exc.detail))
        except DecisionRefused as exc:
            return error_page(request, exc.status_code, exc.message)
        return templates.TemplateResponse(
            request,
            "report.html",
            {
                "report": run.report,
                "review_workspace": True,
                "questions": question_store.list_for_report(run_id),
                **context,
            },
        )

    @app.get("/reports/{run_id}/print", response_class=HTMLResponse)
    def print_report(request: Request, run_id: uuid.UUID, version: int | None = Query(default=None, ge=1)) -> Response:
        try:
            run = accessible_report(log, request, run_id)
            context = decision_context(request, log, run, version)
        except HTTPException as exc:
            return error_page(request, exc.status_code, str(exc.detail))
        except DecisionRefused as exc:
            return error_page(request, exc.status_code, exc.message)
        if run.report is None:
            return error_page(request, 404, "No report with that id.")
        request.state.embedded_print = True
        return HTMLResponse(render_print(run.report, context["decision"]))

    @app.get("/reports/{run_id}/versions/{n}/package")
    def download_package(request: Request, run_id: uuid.UUID, n: int) -> Response:
        try:
            accessible_report(log, request, run_id)
            content = build_report_package(
                run_id,
                n,
                log=log,
                evidence=evidence,
                attribution=request.state.attribution,
                request_id=request.state.request_id,
            )
        except HTTPException as exc:
            return error_page(request, exc.status_code, str(exc.detail))
        except DecisionRefused as exc:
            return error_page(request, exc.status_code, exc.message)
        except PackageRefused as exc:
            return error_page(request, 409, str(exc))
        return Response(
            content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="incident-report-{run_id}-v{n}.zip"',
                "Cache-Control": "no-store",
            },
        )

    install_questions(app, log, question_store, qa_connection, evidence, error_page)
    install_review(app, log, evidence)
    install_decisions(app, log, error_page)
    install_reconstruction(app, log, error_page, reconstruction_settings)
    install_native_tools(app, log, error_page, reconstruction_settings)

    @app.post("/reports/{run_id}/retry")
    async def retry_report(request: Request, run_id: uuid.UUID, background_tasks: BackgroundTasks) -> Response:
        try:
            old_run = log.get(run_id)
        except KeyError:
            return error_page(request, 404, "No report with that id.")

        if old_run.status is not RunStatus.FAILED:
            return error_page(request, 409, "Only a failed report can be retried.")

        in_flight = log.find_in_flight_run(old_run.input_hash)
        if in_flight is not None:
            return RedirectResponse(f"/reports/{in_flight.run_id}", status_code=303)

        intake_views = log.intake_views(run_id)
        if not intake_views:
            return error_page(request, 400, "Cannot retry: no evidence views found for this incident.")

        if evidence is None:
            return error_page(request, 503, "Evidence storage is not configured. Ask the owner to configure it.")

        new_run_id = uuid.uuid4()
        attribution = request.state.attribution
        officer_q = log.get_officer_question(run_id)
        if officer_q:
            log.record_officer_question(new_run_id, officer_q)

        for number, iv in enumerate(intake_views):
            log.record_intake_view(new_run_id, number, iv)

        log.record(
            Run(
                run_id=new_run_id,
                origin=old_run.origin,
                created_at=datetime.now(UTC),
                input_hash=old_run.input_hash,
                report=None,
                status=RunStatus.PROCESSING,
                total_latency_ms=0.0,
                current_step="overview",
                attribution=attribution,
                request_id=request.state.request_id,
            )
        )

        def run_retry_analysis() -> None:
            started_at = datetime.now(UTC)

            def step_cb(step: str) -> None:
                now = datetime.now(UTC)
                elapsed = round((now - started_at).total_seconds(), 1)
                broadcaster.emit(
                    new_run_id,
                    "step",
                    {
                        "step": step,
                        "status": "processing",
                        "started_at": started_at.isoformat(),
                        "elapsed_seconds": elapsed,
                    },
                )

            try:
                analyse_incident(
                    intake_views,
                    old_run.origin,
                    prompt_version=prompt_version,
                    ai=ai_connection,
                    run_log=log,
                    evidence=evidence,
                    limits=limits,
                    attribution=attribution,
                    request_id=request.state.request_id,
                    officer_question=officer_q,
                    run_id=new_run_id,
                    on_step=step_cb,
                    processing_settings=proc_settings,
                    agreement_settings=officer_agreement,
                )
                broadcaster.emit(
                    new_run_id,
                    "done",
                    {
                        "step": "done",
                        "status": "succeeded",
                        "redirect": f"/reports/{new_run_id}",
                        "started_at": started_at.isoformat(),
                        "elapsed_seconds": round((datetime.now(UTC) - started_at).total_seconds(), 1),
                    },
                )
            except Exception as exc:
                logger.error("Analysis failed for retry run %s: %s", new_run_id, type(exc).__name__)
                stored_error = "Analysis failed."
                try:
                    fresh_run = log.get(new_run_id)
                    if fresh_run.status is RunStatus.PROCESSING:
                        log.record(
                            fresh_run.model_copy(
                                update={
                                    "status": RunStatus.FAILED,
                                    "error": "Analysis failed.",
                                    "current_step": None,
                                    "total_latency_ms": (datetime.now(UTC) - started_at).total_seconds() * 1000,
                                }
                            )
                        )
                    if fresh_run.error:
                        stored_error = fresh_run.error
                except Exception as store_exc:
                    logger.error("Failed to finalize retry run %s: %s", new_run_id, type(store_exc).__name__)
                now = datetime.now(UTC)
                elapsed = round((now - started_at).total_seconds(), 1)
                broadcaster.emit(
                    new_run_id,
                    "error",
                    {
                        "step": "failed",
                        "status": "failed",
                        "error": stored_error,
                        "started_at": started_at.isoformat(),
                        "elapsed_seconds": elapsed,
                    },
                )

        background_tasks.add_task(run_retry_analysis)
        return RedirectResponse(f"/reports/{new_run_id}", status_code=303)

    @app.get("/reports/{run_id}/events")
    async def report_events(request: Request, run_id: uuid.UUID) -> Response:
        try:
            initial_run = log.get(run_id)
        except KeyError:
            return error_page(request, 404, "No report with that id.")

        async def event_generator() -> AsyncIterator[str]:
            started_iso = initial_run.created_at.isoformat()
            if initial_run.status is RunStatus.SUCCEEDED:
                data = json.dumps(
                    {
                        "step": "done",
                        "status": "succeeded",
                        "redirect": f"/reports/{run_id}",
                        "started_at": started_iso,
                        "elapsed_seconds": round((datetime.now(UTC) - initial_run.created_at).total_seconds(), 1),
                    }
                )
                yield f"event: done\ndata: {data}\n\n"
                return
            if initial_run.status is RunStatus.FAILED:
                data = json.dumps(
                    {
                        "step": "failed",
                        "status": "failed",
                        "error": initial_run.error or "Analysis failed.",
                        "started_at": started_iso,
                        "elapsed_seconds": round((datetime.now(UTC) - initial_run.created_at).total_seconds(), 1),
                    }
                )
                yield f"event: error\ndata: {data}\n\n"
                return

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            broadcaster.subscribe(run_id, loop, queue)
            try:
                run = log.get(run_id)
                started_iso = run.created_at.isoformat()
                if run.status is RunStatus.SUCCEEDED:
                    d = json.dumps(
                        {
                            "step": "done",
                            "status": "succeeded",
                            "redirect": f"/reports/{run_id}",
                            "started_at": started_iso,
                            "elapsed_seconds": round((datetime.now(UTC) - run.created_at).total_seconds(), 1),
                        }
                    )
                    yield f"event: done\ndata: {d}\n\n"
                    return
                if run.status is RunStatus.FAILED:
                    d = json.dumps(
                        {
                            "step": "failed",
                            "status": "failed",
                            "error": run.error or "Analysis failed.",
                            "started_at": started_iso,
                            "elapsed_seconds": round((datetime.now(UTC) - run.created_at).total_seconds(), 1),
                        }
                    )
                    yield f"event: error\ndata: {d}\n\n"
                    return

                curr_step = run.current_step or "uploading"
                init_data = json.dumps(
                    {
                        "step": curr_step,
                        "status": "processing",
                        "started_at": started_iso,
                        "elapsed_seconds": round((datetime.now(UTC) - run.created_at).total_seconds(), 1),
                    }
                )
                yield f"event: step\ndata: {init_data}\n\n"

                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=2.0)
                        ev_name = item.get("event", "step")
                        payload = dict(item.get("data", {}))
                        if "started_at" not in payload:
                            payload["started_at"] = started_iso
                        if "elapsed_seconds" not in payload:
                            payload["elapsed_seconds"] = round((datetime.now(UTC) - run.created_at).total_seconds(), 1)
                        ev_data = json.dumps(payload)
                        yield f"event: {ev_name}\ndata: {ev_data}\n\n"
                        if ev_name in ("done", "error"):
                            break
                    except TimeoutError:
                        try:
                            fresh_run = log.get(run_id)
                            fresh_started_iso = fresh_run.created_at.isoformat()
                            if fresh_run.status is RunStatus.SUCCEEDED:
                                d = json.dumps(
                                    {
                                        "step": "done",
                                        "status": "succeeded",
                                        "redirect": f"/reports/{run_id}",
                                        "started_at": fresh_started_iso,
                                        "elapsed_seconds": round(
                                            (datetime.now(UTC) - fresh_run.created_at).total_seconds(), 1
                                        ),
                                    }
                                )
                                yield f"event: done\ndata: {d}\n\n"
                                break
                            if fresh_run.status is RunStatus.FAILED:
                                d = json.dumps(
                                    {
                                        "step": "failed",
                                        "status": "failed",
                                        "error": fresh_run.error or "Analysis failed.",
                                        "started_at": fresh_started_iso,
                                        "elapsed_seconds": round(
                                            (datetime.now(UTC) - fresh_run.created_at).total_seconds(), 1
                                        ),
                                    }
                                )
                                yield f"event: error\ndata: {d}\n\n"
                                break
                        except KeyError:
                            pass
                        yield ": ping\n\n"
            finally:
                broadcaster.unsubscribe(run_id, queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/feedback", response_class=HTMLResponse)
    def feedback_form(request: Request) -> Response:
        screen_raw = request.query_params.get("screen", "")
        screen = sanitize_screen_path(screen_raw)
        run_id = request.query_params.get("run_id", "")
        incident_id = request.query_params.get("incident_id", "")
        report_version = request.query_params.get("report_version", "")
        token = _csrf_token(request)
        return templates.TemplateResponse(
            request,
            "feedback.html",
            {
                "csrf_token": token,
                "screen": screen,
                "run_id": run_id,
                "incident_id": incident_id,
                "report_version": report_version,
                "categories": list(FeedbackCategory),
                "max_chars": MAX_FEEDBACK_TEXT_CHARS,
            },
        )

    @app.post("/feedback")
    async def submit_feedback(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin:
            expected_base = str(request.base_url).rstrip("/")
            expected_origin = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
            origin_clean = origin.rstrip("/")
            if origin_clean != expected_base and origin_clean != expected_origin:
                return error_page(request, 403, "Cross-origin submission refused.")

        content_length_raw = request.headers.get("content-length")
        if content_length_raw is not None:
            try:
                content_length = int(content_length_raw)
                if content_length < 0:
                    raise ValueError
            except ValueError:
                return error_page(request, 400, "Invalid Content-Length header.")
            if content_length > MAX_FEEDBACK_BODY_SIZE_BYTES:
                return error_page(request, 413, "Request body exceeds maximum size limit.")

        try:
            await read_limited_body(request, max_bytes=MAX_FEEDBACK_BODY_SIZE_BYTES)
        except BodyTooLargeError:
            return error_page(request, 413, "Request body exceeds maximum size limit.")

        form = await request.form(max_part_size=MAX_FEEDBACK_BODY_SIZE_BYTES)

        csrf_token = form.get("csrf_token")
        expected_csrf = _csrf_token(request)
        if not csrf_token or not secrets.compare_digest(str(csrf_token), expected_csrf):
            return error_page(request, 403, "Invalid or missing CSRF token.")

        text = _form_str(form.get("text"))
        if not text:
            return error_page(request, 422, "Feedback text is required.")
        if len(text) > MAX_FEEDBACK_TEXT_CHARS:
            return error_page(request, 422, f"Feedback text must be at most {MAX_FEEDBACK_TEXT_CHARS} characters.")
        if _contains_disallowed_control_chars(text, allow_newlines=True):
            return error_page(request, 422, "Feedback text contains invalid control characters.")

        category_raw = _form_str(form.get("category"))
        category: FeedbackCategory | None = None
        if category_raw:
            try:
                category = FeedbackCategory(category_raw)
            except ValueError:
                return error_page(request, 422, f"Invalid feedback category: {category_raw}")

        name_raw = _form_str(form.get("name")) or None
        if name_raw:
            if len(name_raw) > MAX_NAME_CHARS:
                return error_page(request, 422, f"Name must be at most {MAX_NAME_CHARS} characters.")
            if _contains_disallowed_control_chars(name_raw, allow_newlines=False):
                return error_page(request, 422, "Name contains invalid control characters.")

        screen_raw = _form_str(form.get("screen")) or None
        screen: str | None = None
        if screen_raw:
            if len(screen_raw) > MAX_TEXT_FIELD_CHARS:
                return error_page(
                    request, 422, f"Screen path is too long: must be at most {MAX_TEXT_FIELD_CHARS} characters."
                )
            if _contains_disallowed_control_chars(screen_raw, allow_newlines=False):
                return error_page(request, 422, "Screen path contains invalid control characters.")
            screen = sanitize_screen_path(screen_raw)

        run_id_raw = _form_str(form.get("run_id"))
        run_id: uuid.UUID | None = None
        incident_id: str | None = None
        report_version: str | None = None
        if run_id_raw:
            try:
                parsed_run_id = uuid.UUID(run_id_raw)
            except ValueError:
                return error_page(request, 422, "Invalid run identifier.")
            try:
                run = log.get(parsed_run_id)
            except KeyError:
                return error_page(request, 422, "No run found with that identifier.")
            if run.report is None:
                return error_page(request, 422, "No report exists for this run.")
            if (
                run.attribution is not None
                and getattr(request.state, "attribution", None) is not None
                and run.attribution.link_id != request.state.attribution.link_id
            ):
                return error_page(request, 403, "Run not accessible.")
            run_id = run.run_id
            incident_id = run.input_hash
            report_version = run.report.prompt_version
            request.state.run_id = run.run_id

        screenshot_sha256: str | None = None
        screenshot_val = form.get("screenshot")
        if isinstance(screenshot_val, UploadFile) and screenshot_val.filename:
            if evidence is None:
                return error_page(request, 503, "Evidence storage is not configured. Screenshots cannot be saved.")
            chunks: list[bytes] = []
            total_bytes = 0
            while True:
                chunk = await screenshot_val.read(64 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_SCREENSHOT_SIZE_BYTES:
                    return error_page(request, 422, "Screenshot exceeds maximum size limit of 5 MB.")
                chunks.append(chunk)
            content = b"".join(chunks)
            if len(content) == 0:
                return error_page(request, 422, "Screenshot file is empty.")
            detected_type = detect_image_type(content[:16])
            if detected_type is None:
                return error_page(request, 422, "Only image files (PNG, JPEG, WebP, GIF) are accepted.")
            screenshot_sha256 = sha256_hex(content)
            evidence.put(io.BytesIO(content), sha256=screenshot_sha256)

        session_id = request.state.attribution.session_id if getattr(request.state, "attribution", None) else None
        link_id = request.state.attribution.link_id if getattr(request.state, "attribution", None) else None
        browser_raw = request.headers.get("user-agent")
        browser = browser_raw[:MAX_USER_AGENT_CHARS] if browser_raw else None

        submission = FeedbackSubmission(
            feedback_id=uuid.uuid4(),
            created_at=datetime.now(UTC),
            text=text,
            category=category,
            name=name_raw,
            screenshot_sha256=screenshot_sha256,
            session_id=session_id,
            link_id=link_id,
            screen=screen,
            browser=browser,
            incident_id=incident_id,
            run_id=run_id,
            report_version=report_version,
            request_id=getattr(request.state, "request_id", None),
        )

        await run_in_threadpool(feedback_store.record_feedback, submission)

        return templates.TemplateResponse(
            request,
            "feedback_success.html",
            {"submission": submission},
            status_code=200,
        )

    @app.get("/feedback/screenshots/{sha256}")
    def view_screenshot(request: Request, sha256: str) -> Response:
        try:
            clean_key = validate_sha256_key(sha256)
        except InvalidEvidenceKey:
            return error_page(request, 404, "Invalid screenshot key.")
        if not feedback_store.has_screenshot(clean_key):
            return error_page(request, 404, "Screenshot not found.")
        if evidence is None or not evidence.exists(clean_key):
            return error_page(request, 404, "Screenshot not found.")
        try:
            stream = evidence.get(clean_key)
            header = stream.read(16)
            mime = detect_image_type(header)
            if mime is None:
                stream.close()
                return error_page(request, 404, "Screenshot not found.")
            rest = stream.read()
            stream.close()
            data = header + rest
        except Exception:
            return error_page(request, 404, "Screenshot not found.")
        headers = {
            "Content-Type": mime,
            "Content-Disposition": f'inline; filename="{clean_key[:16]}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        }
        return Response(content=data, media_type=mime, headers=headers)

    @app.get("/admin/feedback/screenshots/{sha256}")
    def admin_view_screenshot(request: Request, sha256: str) -> Response:
        return view_screenshot(request, sha256)

    return app


def _basename(filename: str) -> str:
    """Drop any directory part a client sent; the name is display-only and never a path."""
    return PurePosixPath(PureWindowsPath(filename).name).name


def _clean(text: str | None) -> str | None:
    text = (text or "").strip()
    return text or None


def _form_str(value: UploadFile | str | None) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


class _FormFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role | None = None
    label: str | None = Field(default=None, max_length=MAX_TEXT_FIELD_CHARS)
    acquisition_method: str | None = Field(default=None, max_length=MAX_TEXT_FIELD_CHARS)
    interior_label: str | None = Field(default=None, max_length=MAX_TEXT_FIELD_CHARS)
    interior_acquisition_method: str | None = Field(default=None, max_length=MAX_TEXT_FIELD_CHARS)
    exterior_label: str | None = Field(default=None, max_length=MAX_TEXT_FIELD_CHARS)
    exterior_acquisition_method: str | None = Field(default=None, max_length=MAX_TEXT_FIELD_CHARS)
    officer_question: str | None = Field(default=None, max_length=MAX_TEXT_FIELD_CHARS)


@asynccontextmanager
async def _consumed_stages(
    form: Any, stages: UploadStages | None, owner: str
) -> AsyncIterator[list[tuple[Role, StagedUpload]]]:
    with ExitStack() as stack:
        ids: list[str] = []
        roles: list[Role] = []
        for role in (Role.INTERIOR, Role.EXTERIOR):
            raw = form.get(role.value + "_staging_ids", "")
            if not isinstance(raw, str):
                raise InvalidIncident("Invalid temporary upload ids.")
            tokens = raw.split(",") if raw else []
            ids.extend(tokens)
            roles.extend([role] * len(tokens))
        if ids and stages is None:
            raise InvalidIncident("Temporary uploads are unavailable.")
        accepted = stack.enter_context(stages.consume(ids, owner)) if stages else []
        yield list(zip(roles, accepted, strict=True))
