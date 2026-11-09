"""Share-link exchange and separate admin HTTP endpoints (ADR 0010).

To access /admin in a browser today, the operator provides the owner secret via an
Authorization: Bearer <secret> request header (e.g., using a browser header-injection
extension, reverse proxy, or curl). No browser login form is provided by design per
ADR 0010 / ADR 0011.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from incident_report.access import AccessStore, RateLimiter, secret_hash
from incident_report.config import AccessSettings
from incident_report.feedback import FeedbackCategory, FeedbackStatus, FeedbackStore

COOKIE = "__Host-incident_session"


def _contains_disallowed_control_chars(s: str, *, allow_newlines: bool = False) -> bool:
    """Return True if string contains NUL bytes or disallowed control characters.

    PostgreSQL text fields reject NUL bytes (0x00), which would trigger unhandled 500s.
    If allow_newlines is True, CR, LF, and HT (tab) are accepted.
    """
    for ch in s:
        code = ord(ch)
        if code == 0:
            return True
        if code < 32 or code == 127:
            if allow_newlines and ch in ("\n", "\r", "\t"):
                continue
            return True
    return False


def _csrf_token(request: Request) -> str:
    cookie = request.cookies.get(COOKIE) or ""
    session_id = str(request.state.attribution.session_id) if getattr(request.state, "attribution", None) else ""
    auth = request.headers.get("authorization", "")
    seed = f"{cookie}:{session_id}:{auth}"
    return hashlib.sha256(f"feedback_csrf_salt:{seed}".encode()).hexdigest()


def _check_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    expected_base = str(request.base_url).rstrip("/")
    expected_origin = f"{request.url.scheme}://{request.url.netloc}".rstrip("/")
    origin_clean = origin.rstrip("/")
    return origin_clean in (expected_base, expected_origin)


async def _is_form_submission(request: Request) -> bool:
    content_type = request.headers.get("content-type", "").lower()
    return content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data"))


async def _verify_csrf(request: Request) -> bool:
    form = await request.form()
    token = form.get("csrf_token")
    if not token or not isinstance(token, str):
        return False
    return secrets.compare_digest(token, _csrf_token(request))


class LinkOpening(BaseModel):
    model_config = ConfigDict(extra="forbid")
    link_id: uuid.UUID
    # Length follows token_urlsafe(32); input may be invalid without echoing it back.
    secret: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


def install_access(
    app: FastAPI,
    store: AccessStore,
    settings: AccessSettings,
    templates: Jinja2Templates | None = None,
    feedback_store: FeedbackStore | None = None,
) -> None:
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        if request.url.path == "/access/open":
            return JSONResponse({"detail": "Invalid share link."}, status_code=422)
        # Keep the existing error contract for report forms.
        from fastapi.exception_handlers import request_validation_exception_handler

        return await request_validation_exception_handler(request, exc)

    limiter = RateLimiter(settings.window_seconds, settings.limiter_keys)

    @app.middleware("http")
    async def gate(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request.state.request_id = uuid.uuid4()
        session = await run_in_threadpool(store.session, request.cookies.get(COOKIE))
        request.state.attribution = session.attribution if session else None
        actor = "visitor" if session else "anonymous"
        response: Response | None = None
        path = request.url.path
        admin_path = path == "/admin" or path.startswith("/admin/")
        link_path = path in ("/share", "/access/open")
        group = "admin" if admin_path else "link"
        limit = settings.admin_attempts if admin_path else settings.link_attempts
        peer = request.client.host if request.client else "unknown"
        if (admin_path or link_path) and not limiter.allow(group, peer, limit):
            response = JSONResponse(
                {"detail": "Too many access attempts. Try again later."},
                status_code=429,
                headers={"Retry-After": str(settings.window_seconds)},
            )
        elif admin_path:
            supplied = request.headers.get("authorization", "")
            if (
                not settings.admin_secret
                or not supplied.startswith("Bearer ")
                or not secrets.compare_digest(secret_hash(supplied[7:]), secret_hash(settings.admin_secret))
            ):
                response = JSONResponse({"detail": "Admin access refused."}, status_code=403)
            elif request.method == "POST" and not _check_origin(request):
                response = JSONResponse({"detail": "Cross-origin submission refused."}, status_code=403)
            else:
                actor = "admin"
                request.state.attribution = None
        elif path not in ("/share", "/access/open") and not path.startswith("/static/"):
            if not session or not session.valid:
                response = JSONResponse({"detail": "Open a valid share link to use the tool."}, status_code=403)
        try:
            if response is None:
                response = await call_next(request)
        except Exception:
            await run_in_threadpool(
                store.record_event,
                request_id=request.state.request_id,
                actor=actor,
                attribution=request.state.attribution,
                action="request.failed",
                status_code=500,
            )
            raise
        attribution = request.state.attribution
        if actor != "admin" and attribution is not None:
            actor = "visitor"
        route = request.scope.get("route")
        action = request.method + " " + (route.path if route else "unmatched")
        target_type: str | None = None
        target_id: str | None = None
        if "link_id" in request.path_params:
            target_type = "link"
            target_id = str(request.path_params["link_id"])
        elif "session_id" in request.path_params:
            target_type = "session"
            target_id = str(request.path_params["session_id"])
        elif "feedback_id" in request.path_params:
            target_type = "feedback"
            target_id = str(request.path_params["feedback_id"])

        should_record = True
        if actor == "anonymous" and response.status_code in (403, 429):
            should_record = limiter.allow("refusal_audit", peer, settings.refusal_audit_attempts)

        if should_record:
            await run_in_threadpool(
                store.record_event,
                request_id=request.state.request_id,
                actor=actor,
                attribution=attribution,
                action=action,
                status_code=response.status_code,
                run_id=getattr(request.state, "run_id", None),
                target_type=target_type,
                target_id=target_id,
            )
        response.headers["Cache-Control"] = "no-store"
        # no-referrer makes native form POSTs send Origin: null in Chromium,
        # which our same-origin guard correctly refuses. Keep referrers local
        # while preserving the real Origin on officer decision forms (RPT-42).
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if getattr(request.state, "embedded_print", False):
            # Only the script-free print renderer embeds fixed local styles and fonts.
            response.headers["Content-Security-Policy"] += "; style-src 'self' data:; font-src 'self' data:"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = f"max-age={settings.hsts_seconds}"
        return response

    def _render_admin_dashboard(
        request: Request,
        *,
        new_link_url: str | None = None,
        status_code: int = 200,
    ) -> Response:
        if templates is None:
            return JSONResponse(jsonable_encoder(store.list_links()))
        category_raw = request.query_params.get("category")
        selected_category: FeedbackCategory | None = None
        if category_raw is not None:
            if len(category_raw) > 50 or _contains_disallowed_control_chars(category_raw, allow_newlines=False):
                return JSONResponse({"detail": "Invalid category."}, status_code=422)
            try:
                selected_category = FeedbackCategory(category_raw)
            except ValueError:
                return JSONResponse({"detail": "Invalid category."}, status_code=422)

        feedback_list = []
        new_feedback_count = 0
        if feedback_store is not None:
            feedback_list = feedback_store.list_feedback(category=selected_category)
            new_feedback_count = feedback_store.count_new_feedback()

        links_list = store.list_links()
        active_links_count = sum(1 for link in links_list if link["enabled"])
        sessions_list = store.list_sessions()
        active_sessions_count = sum(1 for s in sessions_list if not s["blocked"])

        context = {
            "csrf_token": _csrf_token(request),
            "new_link_url": new_link_url,
            "new_feedback_count": new_feedback_count,
            "active_links_count": active_links_count,
            "active_sessions_count": active_sessions_count,
            "selected_category": selected_category.value if selected_category else None,
            "categories": list(FeedbackCategory),
            "statuses": list(FeedbackStatus),
            "feedback_list": feedback_list,
            "links": links_list,
            "sessions": sessions_list,
        }
        return templates.TemplateResponse(request, "admin.html", context, status_code=status_code)

    @app.post("/admin/links", status_code=201)
    async def create_link(request: Request) -> Response:
        is_form = await _is_form_submission(request)
        if is_form:
            if not await _verify_csrf(request):
                return JSONResponse({"detail": "Invalid or missing CSRF token."}, status_code=403)
            res = store.create_link()
            if templates is not None:
                link_url = f"{str(request.base_url).rstrip('/')}/share#{res['link_id']}.{res['secret']}"
                return _render_admin_dashboard(request, new_link_url=link_url, status_code=200)
            return JSONResponse(res, status_code=201)
        return JSONResponse(store.create_link(), status_code=201)

    @app.get("/admin")
    def admin_dashboard(request: Request) -> Response:
        if templates is None:
            return JSONResponse(jsonable_encoder(store.list_links()))
        return _render_admin_dashboard(request)

    @app.get("/admin/links")
    def links() -> Response:
        return JSONResponse(jsonable_encoder(store.list_links()))

    @app.get("/admin/events")
    def events() -> Response:
        return JSONResponse(jsonable_encoder(store.list_events()))

    @app.post("/admin/links/{link_id}/disable")
    async def disable(request: Request, link_id: uuid.UUID) -> Response:
        is_form = await _is_form_submission(request)
        if is_form and not await _verify_csrf(request):
            return JSONResponse({"detail": "Invalid or missing CSRF token."}, status_code=403)
        result = store.change_link(link_id, rotate=False)
        if not result:
            return JSONResponse({"detail": "No such link."}, status_code=404)
        if is_form:
            return RedirectResponse("/admin", status_code=303)
        return JSONResponse(result, status_code=200)

    @app.post("/admin/links/{link_id}/rotate")
    async def rotate(request: Request, link_id: uuid.UUID) -> Response:
        is_form = await _is_form_submission(request)
        if is_form and not await _verify_csrf(request):
            return JSONResponse({"detail": "Invalid or missing CSRF token."}, status_code=403)
        result = store.change_link(link_id, rotate=True)
        if not result:
            return JSONResponse({"detail": "No such link."}, status_code=404)
        if is_form:
            if templates is not None:
                link_url = f"{str(request.base_url).rstrip('/')}/share#{result['link_id']}.{result['secret']}"
                return _render_admin_dashboard(request, new_link_url=link_url, status_code=200)
            return JSONResponse(result, status_code=200)
        return JSONResponse(result, status_code=200)

    @app.post("/admin/sessions/{session_id}/block")
    async def block(request: Request, session_id: uuid.UUID) -> Response:
        is_form = await _is_form_submission(request)
        if is_form and not await _verify_csrf(request):
            return JSONResponse({"detail": "Invalid or missing CSRF token."}, status_code=403)
        found = store.block_session(session_id)
        if not found:
            return JSONResponse({"detail": "No such session."}, status_code=404)
        if is_form:
            return RedirectResponse("/admin", status_code=303)
        return JSONResponse({"blocked": found}, status_code=200)

    @app.post("/admin/feedback/{feedback_id}/status")
    async def update_feedback_status(request: Request, feedback_id: uuid.UUID) -> Response:
        is_form = await _is_form_submission(request)
        status_raw: Any = None
        if is_form:
            form = await request.form()
            token = form.get("csrf_token")
            if not token or not isinstance(token, str) or not secrets.compare_digest(token, _csrf_token(request)):
                return JSONResponse({"detail": "Invalid or missing CSRF token."}, status_code=403)
            status_raw = form.get("status")
        else:
            try:
                body = await request.json()
                status_raw = body.get("status")
            except Exception:
                status_raw = None

        if not status_raw or not isinstance(status_raw, str):
            return JSONResponse({"detail": "Status is required."}, status_code=422)

        status_str = status_raw.strip()
        if len(status_str) > 20 or _contains_disallowed_control_chars(status_str, allow_newlines=False):
            return JSONResponse({"detail": "Invalid status value."}, status_code=422)

        try:
            new_status = FeedbackStatus(status_str)
        except ValueError:
            return JSONResponse({"detail": f"Invalid status: {status_str}"}, status_code=422)

        if feedback_store is None:
            return JSONResponse({"detail": "Feedback store is not configured."}, status_code=503)

        updated = await run_in_threadpool(
            feedback_store.update_status,
            feedback_id,
            new_status,
            request_id=request.state.request_id,
        )
        if updated is None:
            return JSONResponse({"detail": "No such feedback submission."}, status_code=404)

        if is_form:
            return RedirectResponse("/admin", status_code=303)
        return JSONResponse({"status": updated.status.value}, status_code=200)

    @app.get("/admin/sessions")
    def sessions() -> Response:
        return JSONResponse(jsonable_encoder(store.list_sessions()))

    @app.get("/admin/feedback.json")
    def feedback_json(
        request: Request,
        category: str | None = None,
        status: str | None = None,
    ) -> Response:
        if feedback_store is None:
            return JSONResponse([])
        selected_category: FeedbackCategory | None = None
        if category is not None:
            if len(category) > 50 or _contains_disallowed_control_chars(category, allow_newlines=False):
                return JSONResponse({"detail": "Invalid category."}, status_code=422)
            try:
                selected_category = FeedbackCategory(category)
            except ValueError:
                return JSONResponse({"detail": "Invalid category."}, status_code=422)

        selected_status: FeedbackStatus | None = None
        if status is not None:
            if len(status) > 20 or _contains_disallowed_control_chars(status, allow_newlines=False):
                return JSONResponse({"detail": "Invalid status."}, status_code=422)
            try:
                selected_status = FeedbackStatus(status)
            except ValueError:
                return JSONResponse({"detail": "Invalid status."}, status_code=422)

        items = feedback_store.list_feedback(category=selected_category, status=selected_status)
        return JSONResponse(jsonable_encoder([item.model_dump(mode="json") for item in items]))

    @app.post("/access/open")
    def open_link(request: Request, opening: LinkOpening) -> Response:
        result = store.open_link(opening.link_id, opening.secret, settings.session_seconds, request.cookies.get(COOKIE))
        if result is None:
            return JSONResponse({"detail": "Share link access refused."}, status_code=403)
        attribution, cookie = result
        request.state.attribution = attribution
        response = JSONResponse({"session_id": str(attribution.session_id), "link_id": str(attribution.link_id)})
        # Lax permits forwarded-link navigation while withholding cookies on cross-site POSTs.
        # API: https://starlette.dev/responses/#set-cookie
        response.set_cookie(
            COOKIE, cookie, max_age=settings.session_seconds, secure=True, httponly=True, samesite="lax"
        )
        return response
