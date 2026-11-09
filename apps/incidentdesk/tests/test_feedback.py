"""Unit and integration tests for feedback storage and audit logging (RPT-43, spec 001)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from incident_report.audit import AuditLog
from incident_report.feedback import (
    FeedbackCategory,
    FeedbackStore,
    FeedbackSubmission,
    detect_image_type,
)
from incident_report.runlog import RunLog


def test_feedback_category_enum() -> None:
    assert FeedbackCategory.WRONG_RESULT.value == "wrong result"
    assert FeedbackCategory.CONFUSING.value == "confusing"
    assert FeedbackCategory.BUG.value == "bug"
    assert FeedbackCategory.TOO_SLOW.value == "too slow"
    assert FeedbackCategory.MISSING_FEATURE.value == "missing feature"
    assert FeedbackCategory.OTHER.value == "other"


def test_detect_image_type() -> None:
    # PNG magic bytes
    assert detect_image_type(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR...") == "image/png"
    # JPEG magic bytes
    assert detect_image_type(b"\xff\xd8\xff\xe0\x00\x10JFIF...") == "image/jpeg"
    # GIF magic bytes
    assert detect_image_type(b"GIF89a\x01\x00\x01\x00...") == "image/gif"
    assert detect_image_type(b"GIF87a\x01\x00\x01\x00...") == "image/gif"
    # WebP magic bytes
    assert detect_image_type(b"RIFF\x1a\x00\x00\x00WEBPVP8 ...") == "image/webp"
    # Non-images
    assert detect_image_type(b"plain text content") is None
    assert detect_image_type(b"\x00\x00\x00\x18ftypmp42") is None
    assert detect_image_type(b"") is None


def test_feedback_store_records_and_retrieves_feedback(run_log: RunLog) -> None:
    store = FeedbackStore(run_log.database_url)
    feedback_id = uuid.uuid4()
    session_id = uuid.uuid4()
    link_id = uuid.uuid4()
    run_id = uuid.uuid4()
    request_id = uuid.uuid4()
    created_at = datetime.now(UTC)

    submission = FeedbackSubmission(
        feedback_id=feedback_id,
        created_at=created_at,
        text="The speed calculation appears too high for the blue vehicle.",
        category=FeedbackCategory.WRONG_RESULT,
        name="Officer Ahmed",
        screenshot_sha256="aa" * 32,
        session_id=session_id,
        link_id=link_id,
        screen="/reports/" + str(run_id),
        browser="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        incident_id="bb" * 32,
        run_id=run_id,
        report_version="overview-1",
        request_id=request_id,
    )

    store.record_feedback(submission)

    retrieved = store.get(feedback_id)
    assert retrieved is not None
    assert retrieved.feedback_id == feedback_id
    assert retrieved.text == submission.text
    assert retrieved.category == FeedbackCategory.WRONG_RESULT
    assert retrieved.name == "Officer Ahmed"
    assert retrieved.screenshot_sha256 == "aa" * 32
    assert retrieved.session_id == session_id
    assert retrieved.link_id == link_id
    assert retrieved.screen == "/reports/" + str(run_id)
    assert retrieved.browser == submission.browser
    assert retrieved.incident_id == "bb" * 32
    assert retrieved.run_id == run_id
    assert retrieved.report_version == "overview-1"
    assert retrieved.request_id == request_id

    # Verify audit log entry was written
    audit = AuditLog(run_log.database_url)
    verification = audit.verify()
    assert verification.valid is True
    assert verification.total_entries >= 1

    entries = audit.list_entries()
    fb_entries = [e for e in entries if e.event_type == "feedback" and e.target_id == str(feedback_id)]
    assert len(fb_entries) == 1
    fb_entry = fb_entries[0]
    assert fb_entry.action == "feedback.submit"
    assert fb_entry.actor == "visitor"
    assert fb_entry.session_id == session_id
    assert fb_entry.link_id == link_id
    assert fb_entry.target_type == "feedback"
    assert fb_entry.target_id == str(feedback_id)
    assert fb_entry.details.get("feedback_id") == str(feedback_id)
    assert fb_entry.details.get("category") == "wrong result"
    assert fb_entry.details.get("has_screenshot") is True
    assert fb_entry.details.get("screenshot_sha256") == "aa" * 32
