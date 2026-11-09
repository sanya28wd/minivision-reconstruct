"""Feedback subsystem: models, validation, and storage (RPT-43, spec 001).

Feedback allows users/officers to report issues, confusion, bugs, or feature requests.
Feedback text is stored safely in PostgreSQL and strictly isolated from AI analysis inputs.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

import psycopg
from pydantic import BaseModel, ConfigDict

from incident_report.audit import AuditLog

MAX_FEEDBACK_TEXT_CHARS = 5000
MAX_NAME_CHARS = 200
MAX_SCREENSHOT_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB size cap (spec 001, RPT-43)
MAX_USER_AGENT_CHARS = 500  # Project assumption, to be validated: keeps UA rows bounded (ADR 0006)

# Fixed allowance for multipart text fields and MIME overhead (RPT-52 carry-over).
# Derived from:
# - MAX_FEEDBACK_TEXT_CHARS (5,000) * 4 bytes/char worst-case UTF-8 = 20,000 bytes
# - MAX_NAME_CHARS (200) * 4 bytes/char worst-case UTF-8 = 800 bytes
# - MAX_USER_AGENT_CHARS (500) * 4 bytes/char worst-case UTF-8 = 2,000 bytes
# - 64 KiB (65,536 bytes) for multipart boundaries, headers, CSRF token, category, etc.
FEEDBACK_TEXT_ALLOWANCE_BYTES = (
    (MAX_FEEDBACK_TEXT_CHARS * 4) + (MAX_NAME_CHARS * 4) + (MAX_USER_AGENT_CHARS * 4) + (64 * 1024)
)
# Overall maximum request body size permitted for feedback submissions.
MAX_FEEDBACK_BODY_SIZE_BYTES = MAX_SCREENSHOT_SIZE_BYTES + FEEDBACK_TEXT_ALLOWANCE_BYTES


class FeedbackCategory(enum.StrEnum):
    """Allowed feedback categories (spec 001, RPT-43)."""

    WRONG_RESULT = "wrong result"
    CONFUSING = "confusing"
    BUG = "bug"
    TOO_SLOW = "too slow"
    MISSING_FEATURE = "missing feature"
    OTHER = "other"


class FeedbackStatus(enum.StrEnum):
    """Lifecycle status of a feedback submission in the admin inbox (RPT-44)."""

    NEW = "new"
    SEEN = "seen"
    FIXED = "fixed"
    WONT_FIX = "won't fix"


def detect_image_type(data: bytes) -> str | None:
    """Detect image MIME type from magic bytes. Returns None if not an allowed image format."""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 6 and (data[:6] == b"GIF87a" or data[:6] == b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class FeedbackSubmission(BaseModel):
    """A feedback submission with auto-attached context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    feedback_id: uuid.UUID
    created_at: datetime
    text: str
    category: FeedbackCategory | None = None
    name: str | None = None
    screenshot_sha256: str | None = None
    session_id: uuid.UUID | None = None
    link_id: uuid.UUID | None = None
    screen: str | None = None
    browser: str | None = None
    incident_id: str | None = None
    run_id: uuid.UUID | None = None
    report_version: str | None = None
    request_id: uuid.UUID | None = None
    status: FeedbackStatus = FeedbackStatus.NEW


def sanitize_screen_path(screen: str | None) -> str:
    """Validate screen redirect path.

    Only same-site relative paths starting with a single '/' are permitted.
    Protocol-relative paths ('//'), backslashes, URI schemes (':'), control
    characters, and paths exceeding 200 chars fall back to '/'.
    """
    if not screen or not isinstance(screen, str):
        return "/"
    s = screen.strip()
    if not s.startswith("/") or s.startswith("//") or "\\" in s or ":" in s:
        return "/"
    if len(s) > 200:
        return "/"
    for ch in s:
        code = ord(ch)
        if code == 0 or code < 32 or code == 127:
            return "/"
    return s


class FeedbackStore:
    """PostgreSQL storage and audit trail integration for user feedback."""

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._audit = AuditLog(database_url)

    @property
    def audit(self) -> AuditLog:
        return self._audit

    def record_feedback(self, submission: FeedbackSubmission) -> None:
        """Insert feedback into database and append an audit event."""
        clean_screen = sanitize_screen_path(submission.screen) if submission.screen else None
        with psycopg.connect(self._url) as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO feedback ("
                    "feedback_id, created_at, text, category, name, screenshot_sha256, "
                    "session_id, link_id, screen, browser, incident_id, run_id, "
                    "report_version, request_id, status"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        submission.feedback_id,
                        submission.created_at,
                        submission.text,
                        submission.category.value if submission.category else None,
                        submission.name,
                        submission.screenshot_sha256,
                        submission.session_id,
                        submission.link_id,
                        clean_screen,
                        submission.browser,
                        submission.incident_id,
                        submission.run_id,
                        submission.report_version,
                        submission.request_id,
                        submission.status.value,
                    ),
                )
                actor = "visitor" if submission.session_id else "anonymous"
                self._audit.append(
                    conn=conn,
                    event_type="feedback",
                    action="feedback.submit",
                    actor=actor,
                    session_id=submission.session_id,
                    link_id=submission.link_id,
                    request_id=submission.request_id,
                    run_id=submission.run_id,
                    status_code=None,
                    target_type="feedback",
                    target_id=str(submission.feedback_id),
                    details={
                        "feedback_id": str(submission.feedback_id),
                        "category": submission.category.value if submission.category else None,
                        "screen": submission.screen[:200] if submission.screen else None,
                        "has_screenshot": submission.screenshot_sha256 is not None,
                        "screenshot_sha256": submission.screenshot_sha256,
                    },
                )

    def has_screenshot(self, sha256: str) -> bool:
        """Return True if sha256 is recorded as an attached screenshot in feedback."""
        with psycopg.connect(self._url) as conn:
            row = conn.execute(
                "SELECT 1 FROM feedback WHERE screenshot_sha256 = %s LIMIT 1",
                (sha256,),
            ).fetchone()
        return row is not None

    def get(self, feedback_id: uuid.UUID) -> FeedbackSubmission | None:
        with psycopg.connect(self._url) as conn:
            row = conn.execute(
                "SELECT feedback_id, created_at, text, category, name, screenshot_sha256, "
                "session_id, link_id, screen, browser, incident_id, run_id, "
                "report_version, request_id, status FROM feedback WHERE feedback_id = %s",
                (feedback_id,),
            ).fetchone()
        if row is None:
            return None
        return _to_submission(row)

    def list_feedback(
        self,
        *,
        category: FeedbackCategory | str | None = None,
        status: FeedbackStatus | str | None = None,
    ) -> list[FeedbackSubmission]:
        query = (
            "SELECT feedback_id, created_at, text, category, name, screenshot_sha256, "
            "session_id, link_id, screen, browser, incident_id, run_id, "
            "report_version, request_id, status FROM feedback"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if category:
            clauses.append("category = %s")
            cat_str = category.value if isinstance(category, FeedbackCategory) else str(category)
            params.append(cat_str)
        if status:
            clauses.append("status = %s")
            stat_str = status.value if isinstance(status, FeedbackStatus) else str(status)
            params.append(stat_str)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        with psycopg.connect(self._url) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_to_submission(r) for r in rows]

    def count_new_feedback(self) -> int:
        with psycopg.connect(self._url) as conn:
            row = conn.execute("SELECT count(*) FROM feedback WHERE status = 'new'").fetchone()
        return int(row[0]) if row else 0

    def update_status(
        self,
        feedback_id: uuid.UUID,
        new_status: FeedbackStatus,
        *,
        request_id: uuid.UUID | None = None,
    ) -> FeedbackSubmission | None:
        """Update feedback status and append an audit event (RPT-44)."""
        with psycopg.connect(self._url) as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT feedback_id, created_at, text, category, name, screenshot_sha256, "
                    "session_id, link_id, screen, browser, incident_id, run_id, "
                    "report_version, request_id, status FROM feedback WHERE feedback_id = %s FOR UPDATE",
                    (feedback_id,),
                ).fetchone()
                if row is None:
                    return None
                old_submission = _to_submission(row)
                old_status = old_submission.status
                conn.execute(
                    "UPDATE feedback SET status = %s WHERE feedback_id = %s",
                    (new_status.value, feedback_id),
                )
                self._audit.append(
                    conn=conn,
                    event_type="feedback",
                    action="feedback.status_change",
                    actor="admin",
                    session_id=old_submission.session_id,
                    link_id=old_submission.link_id,
                    request_id=request_id,
                    run_id=old_submission.run_id,
                    status_code=None,
                    target_type="feedback",
                    target_id=str(feedback_id),
                    details={
                        "feedback_id": str(feedback_id),
                        "status": new_status.value,
                        "old_status": old_status.value,
                    },
                )
        return self.get(feedback_id)


def _to_submission(row: tuple[Any, ...]) -> FeedbackSubmission:
    (
        feedback_id,
        created_at,
        text,
        category,
        name,
        screenshot_sha256,
        session_id,
        link_id,
        screen,
        browser,
        incident_id,
        run_id,
        report_version,
        request_id,
        status_val,
    ) = row
    cat_val = FeedbackCategory(category) if category else None
    stat_val = FeedbackStatus(status_val) if status_val else FeedbackStatus.NEW
    return FeedbackSubmission(
        feedback_id=feedback_id,
        created_at=created_at,
        text=text,
        category=cat_val,
        name=name,
        screenshot_sha256=screenshot_sha256,
        session_id=session_id,
        link_id=link_id,
        screen=screen,
        browser=browser,
        incident_id=incident_id,
        run_id=run_id,
        report_version=report_version,
        request_id=request_id,
        status=stat_val,
    )
