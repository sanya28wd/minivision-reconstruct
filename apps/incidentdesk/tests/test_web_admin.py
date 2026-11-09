"""Tests for Admin page: share links, sessions, Feedback inbox (RPT-44, spec 001)."""

from __future__ import annotations

import io
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from audit_test_helpers import unsafe_test_only_truncate
from incident_report.ai import ReplayAIConnection
from incident_report.audit import AuditLog
from incident_report.feedback import (
    FeedbackCategory,
    FeedbackStatus,
    FeedbackStore,
    FeedbackSubmission,
)
from incident_report.hashing import sha256_hex
from incident_report.runlog import RunLog
from incident_report.storage import create_evidence_store
from incident_report.web import create_app


def _extract_csrf(page_text: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page_text)
    assert match is not None, "csrf_token not found in page"
    return match.group(1)


@pytest.fixture
def admin_app_and_clients(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[TestClient, TestClient, str]:
    unsafe_test_only_truncate(run_log.database_url, "audit_log", "anonymous_sessions", "share_links")
    with psycopg.connect(run_log.database_url) as conn:
        conn.execute("DELETE FROM feedback")

    admin_secret = "synthetic-test-admin-secret"  # noqa: S105
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", admin_secret)

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("INCIDENT_EVIDENCE_DIR", str(evidence_dir))

    from incident_report.config import Settings

    settings = Settings.from_env({"DATABASE_URL": run_log.database_url})
    store = create_evidence_store(settings)

    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=store)
    admin = TestClient(
        app,
        base_url="https://testserver",
        headers={"Authorization": f"Bearer {admin_secret}"},
    )
    visitor = TestClient(app, base_url="https://testserver")

    # Open a share link for visitor
    created_link = admin.post("/admin/links").json()
    opened = visitor.post(
        "/access/open",
        json={"link_id": created_link["link_id"], "secret": created_link["secret"]},
    )
    assert opened.status_code == 200

    return admin, visitor, created_link["link_id"]


def test_admin_pages_unreachable_from_share_link_sessions(
    admin_app_and_clients: tuple[TestClient, TestClient, str],
) -> None:
    _, visitor, link_id = admin_app_and_clients
    fake_id = str(uuid.uuid4())

    # All admin routes must return 403 for share-link visitors
    assert visitor.get("/admin").status_code == 403
    assert visitor.get("/admin/links").status_code == 403
    assert visitor.get("/admin/sessions").status_code == 403
    assert visitor.post("/admin/links").status_code == 403
    assert visitor.post(f"/admin/links/{link_id}/disable").status_code == 403
    assert visitor.post(f"/admin/links/{link_id}/rotate").status_code == 403
    assert visitor.post(f"/admin/sessions/{fake_id}/block").status_code == 403
    assert visitor.post(f"/admin/feedback/{fake_id}/status").status_code == 403
    assert visitor.get("/admin/incidents").status_code == 403


def test_admin_page_renders_html_with_links_sessions_and_feedback(
    admin_app_and_clients: tuple[TestClient, TestClient, str], run_log: RunLog
) -> None:
    admin, visitor, link_id = admin_app_and_clients

    # Populate a feedback entry
    fb_store = FeedbackStore(run_log.database_url)
    fb_id = uuid.uuid4()
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb_id,
            created_at=datetime.now(UTC),
            text="Initial test feedback text.",
            category=FeedbackCategory.BUG,
            name="Officer Test",
            status=FeedbackStatus.NEW,
        )
    )

    resp = admin.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Admin" in resp.text
    # CSRF token present
    csrf_token = _extract_csrf(resp.text)
    assert len(csrf_token) == 64
    # Sections present (exact headings)
    assert '<h2 id="heading-links">Share Links</h2>' in resp.text
    assert '<h2 id="heading-sessions">Visitor Sessions</h2>' in resp.text
    assert '<h2 id="heading-feedback">Feedback Inbox</h2>' in resp.text
    # Feedback entry text present
    assert "Initial test feedback text." in resp.text
    # Exact new feedback count is 1
    assert '<span class="admin-stat__number">1</span>' in resp.text
    assert '<span class="admin-stat__label">New Feedback</span>' in resp.text

    # Updating status to seen causes the new-feedback count to drop from 1 to 0
    fb_store.update_status(fb_id, FeedbackStatus.SEEN)
    resp2 = admin.get("/admin")
    assert resp2.status_code == 200
    assert '<span class="admin-stat__number">0</span>' in resp2.text


def test_feedback_store_filters_by_status(run_log: RunLog) -> None:
    fb_store = FeedbackStore(run_log.database_url)
    with psycopg.connect(run_log.database_url) as conn:
        conn.execute("DELETE FROM feedback")

    now = datetime.now(UTC)
    id1, id2, id3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=id1,
            created_at=now - timedelta(minutes=2),
            text="New fb",
            status=FeedbackStatus.NEW,
        )
    )
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=id2,
            created_at=now - timedelta(minutes=1),
            text="Seen fb",
            status=FeedbackStatus.SEEN,
        )
    )
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=id3,
            created_at=now,
            text="Fixed fb",
            status=FeedbackStatus.FIXED,
        )
    )

    new_list = fb_store.list_feedback(status=FeedbackStatus.NEW)
    assert len(new_list) == 1 and new_list[0].feedback_id == id1

    seen_list = fb_store.list_feedback(status=FeedbackStatus.SEEN)
    assert len(seen_list) == 1 and seen_list[0].feedback_id == id2

    fixed_list = fb_store.list_feedback(status=FeedbackStatus.FIXED)
    assert len(fixed_list) == 1 and fixed_list[0].feedback_id == id3


def test_share_link_secret_never_in_url_or_redirect_location(
    admin_app_and_clients: tuple[TestClient, TestClient, str],
) -> None:
    admin, _, link_id = admin_app_and_clients
    page = admin.get("/admin")
    csrf = _extract_csrf(page.text)

    # 1. Form POST to create link: must render 200 directly, no Location header, no secret in URL
    resp_create = admin.post("/admin/links", data={"csrf_token": csrf})
    assert resp_create.status_code == 200
    assert resp_create.headers.get("Location") is None
    match_url = re.search(r'class="share-url">[^<]*#([0-9a-f-]{36})\.([A-Za-z0-9_-]{43})<', resp_create.text)
    assert match_url is not None, "Generated share URL not found in 200 response HTML"
    created_id = match_url.group(1)
    secret = match_url.group(2)
    assert len(secret) == 43
    assert secret not in (resp_create.headers.get("Location") or "")
    assert secret not in str(resp_create.url)

    # 2. Form POST to rotate link: must render 200 directly, no Location header, no secret in URL
    resp_rotate = admin.post(f"/admin/links/{created_id}/rotate", data={"csrf_token": csrf})
    assert resp_rotate.status_code == 200
    assert resp_rotate.headers.get("Location") is None
    match_rot = re.search(r'class="share-url">[^<]*#([0-9a-f-]{36})\.([A-Za-z0-9_-]{43})<', resp_rotate.text)
    assert match_rot is not None, "Rotated share URL not found in 200 response HTML"
    rot_secret = match_rot.group(2)
    assert rot_secret not in (resp_rotate.headers.get("Location") or "")
    assert rot_secret not in str(resp_rotate.url)

    # 3. Regular GET /admin must never contain secret in URL or HTML
    get_page = admin.get("/admin")
    assert secret not in get_page.text
    assert rot_secret not in get_page.text


def test_all_form_routes_enforce_csrf_and_origin(
    admin_app_and_clients: tuple[TestClient, TestClient, str], run_log: RunLog
) -> None:
    admin, _, link_id = admin_app_and_clients
    fb_store = FeedbackStore(run_log.database_url)
    fb_id = uuid.uuid4()
    fb_store.record_feedback(FeedbackSubmission(feedback_id=fb_id, created_at=datetime.now(UTC), text="CSRF test."))
    sessions = admin.get("/admin/sessions").json()
    assert len(sessions) >= 1
    session_id = sessions[0]["session_id"]

    page = admin.get("/admin")
    csrf = _extract_csrf(page.text)

    # 5 form endpoints
    endpoints = [
        ("POST", "/admin/links", {"csrf_token": csrf}),
        ("POST", f"/admin/links/{link_id}/disable", {"csrf_token": csrf}),
        ("POST", f"/admin/links/{link_id}/rotate", {"csrf_token": csrf}),
        ("POST", f"/admin/sessions/{session_id}/block", {"csrf_token": csrf}),
        ("POST", f"/admin/feedback/{fb_id}/status", {"csrf_token": csrf, "status": "seen"}),
    ]

    for _method, path, valid_data in endpoints:
        # A. Foreign Origin -> 403
        resp_foreign = admin.post(path, data=valid_data, headers={"Origin": "https://evil.example"})
        assert resp_foreign.status_code == 403, f"{path} accepted foreign Origin"

        # B. Missing CSRF token -> 403
        no_csrf_data = {k: v for k, v in valid_data.items() if k != "csrf_token"}
        resp_no_csrf = admin.post(
            path,
            data=no_csrf_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp_no_csrf.status_code == 403, f"{path} accepted missing CSRF"

        # C. Bad CSRF token -> 403
        bad_csrf_data = dict(valid_data)
        bad_csrf_data["csrf_token"] = "invalid-csrf-token"  # noqa: S105
        resp_bad_csrf = admin.post(
            path,
            data=bad_csrf_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp_bad_csrf.status_code == 403, f"{path} accepted bad CSRF"


def test_form_actions_stop_existing_visitor_session_on_next_request(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unsafe_test_only_truncate(run_log.database_url, "audit_log", "anonymous_sessions", "share_links")
    admin_secret = "synthetic-test-admin-secret"  # noqa: S105
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", admin_secret)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("INCIDENT_EVIDENCE_DIR", str(evidence_dir))

    from incident_report.config import Settings

    settings = Settings.from_env({"DATABASE_URL": run_log.database_url})
    store = create_evidence_store(settings)
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=store)
    admin = TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {admin_secret}"})

    page = admin.get("/admin")
    csrf = _extract_csrf(page.text)

    # --- Part A: Form block session stops visitor session on next request ---
    link1 = admin.post("/admin/links").json()
    visitor1 = TestClient(app, base_url="https://testserver")
    opened1 = visitor1.post("/access/open", json={"link_id": link1["link_id"], "secret": link1["secret"]})
    assert opened1.status_code == 200
    v1_session_id = opened1.json()["session_id"]
    assert visitor1.get("/").status_code == 200

    # Look up session by exact session_id
    sessions1 = admin.get("/admin/sessions").json()
    matched_session = next((s for s in sessions1 if s["session_id"] == v1_session_id), None)
    assert matched_session is not None and matched_session["blocked"] is False

    # Block via form
    resp_block = admin.post(
        f"/admin/sessions/{v1_session_id}/block",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp_block.status_code == 303
    # Visitor 1 next request is refused
    assert visitor1.get("/").status_code == 403

    # --- Part B: Form disable link stops visitor session on next request ---
    link2 = admin.post("/admin/links").json()
    visitor2 = TestClient(app, base_url="https://testserver")
    opened2 = visitor2.post("/access/open", json={"link_id": link2["link_id"], "secret": link2["secret"]})
    assert opened2.status_code == 200
    assert visitor2.get("/").status_code == 200

    # Disable link2 via form
    resp_disable = admin.post(
        f"/admin/links/{link2['link_id']}/disable",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp_disable.status_code == 303
    # Visitor 2 next request is refused
    assert visitor2.get("/").status_code == 403

    # --- Part C: Form rotate link stops visitor session on next request ---
    link3 = admin.post("/admin/links").json()
    visitor3 = TestClient(app, base_url="https://testserver")
    opened3 = visitor3.post("/access/open", json={"link_id": link3["link_id"], "secret": link3["secret"]})
    assert opened3.status_code == 200
    assert visitor3.get("/").status_code == 200

    # Rotate link3 via form
    resp_rotate = admin.post(f"/admin/links/{link3['link_id']}/rotate", data={"csrf_token": csrf})
    assert resp_rotate.status_code == 200  # Renders page directly with new link
    # Visitor 3 next request is refused
    assert visitor3.get("/").status_code == 403


def test_feedback_inbox_displays_newest_first_and_filters_by_category(
    admin_app_and_clients: tuple[TestClient, TestClient, str], run_log: RunLog
) -> None:
    admin, _, _ = admin_app_and_clients
    fb_store = FeedbackStore(run_log.database_url)

    now = datetime.now(UTC)
    fb1 = uuid.uuid4()
    fb2 = uuid.uuid4()
    fb3 = uuid.uuid4()

    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb1,
            created_at=now - timedelta(minutes=10),
            text="Older bug report.",
            category=FeedbackCategory.BUG,
        )
    )
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb2,
            created_at=now,
            text="Newer confusing report.",
            category=FeedbackCategory.CONFUSING,
        )
    )
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb3,
            created_at=now - timedelta(minutes=5),
            text="Mid bug report.",
            category=FeedbackCategory.BUG,
        )
    )

    # All feedback: newest first
    resp_all = admin.get("/admin")
    assert resp_all.status_code == 200
    pos_fb2 = resp_all.text.find("Newer confusing report.")
    pos_fb3 = resp_all.text.find("Mid bug report.")
    pos_fb1 = resp_all.text.find("Older bug report.")
    assert pos_fb2 < pos_fb3 < pos_fb1

    # Filter by category=bug
    resp_bug = admin.get("/admin?category=bug")
    assert resp_bug.status_code == 200
    assert "Older bug report." in resp_bug.text
    assert "Mid bug report." in resp_bug.text
    assert "Newer confusing report." not in resp_bug.text

    # Filter by category=confusing
    resp_conf = admin.get("/admin?category=confusing")
    assert resp_conf.status_code == 200
    assert "Newer confusing report." in resp_conf.text
    assert "Older bug report." not in resp_conf.text

    # Invalid category returns 422
    resp_inv = admin.get("/admin?category=invalid_category")
    assert resp_inv.status_code == 422


def test_feedback_status_transition_persists_and_audits(
    admin_app_and_clients: tuple[TestClient, TestClient, str], run_log: RunLog
) -> None:
    admin, _, _ = admin_app_and_clients
    fb_store = FeedbackStore(run_log.database_url)
    fb_id = uuid.uuid4()

    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb_id,
            created_at=datetime.now(UTC),
            text="Test status update.",
            category=FeedbackCategory.OTHER,
            status=FeedbackStatus.NEW,
        )
    )

    page = admin.get("/admin")
    csrf = _extract_csrf(page.text)

    # 1. Update new -> seen
    resp_seen = admin.post(
        f"/admin/feedback/{fb_id}/status",
        data={"csrf_token": csrf, "status": "seen"},
    )
    assert resp_seen.status_code in (200, 303)

    updated = fb_store.get(fb_id)
    assert updated is not None
    assert updated.status == FeedbackStatus.SEEN

    # Audit log check
    audit = AuditLog(run_log.database_url)
    assert audit.verify().valid is True
    entries = audit.list_entries()
    status_entries = [
        e
        for e in entries
        if e.event_type == "feedback" and e.action == "feedback.status_change" and e.target_id == str(fb_id)
    ]
    assert len(status_entries) == 1
    assert status_entries[0].actor == "admin"
    assert status_entries[0].details.get("status") == "seen"
    assert status_entries[0].details.get("old_status") == "new"

    # 2. Update seen -> fixed
    resp_fixed = admin.post(
        f"/admin/feedback/{fb_id}/status",
        data={"csrf_token": csrf, "status": "fixed"},
    )
    assert resp_fixed.status_code in (200, 303)
    fixed_sub = fb_store.get(fb_id)
    assert fixed_sub is not None
    assert fixed_sub.status == FeedbackStatus.FIXED

    # 3. Update fixed -> won't fix
    resp_wont = admin.post(
        f"/admin/feedback/{fb_id}/status",
        data={"csrf_token": csrf, "status": "won't fix"},
    )
    assert resp_wont.status_code in (200, 303)
    wont_sub = fb_store.get(fb_id)
    assert wont_sub is not None
    assert wont_sub.status == FeedbackStatus.WONT_FIX

    # 4. Unknown feedback ID -> 404
    resp_unknown = admin.post(
        f"/admin/feedback/{uuid.uuid4()}/status",
        data={"csrf_token": csrf, "status": "seen"},
    )
    assert resp_unknown.status_code == 404

    # 5. Invalid status -> 422
    resp_invalid = admin.post(
        f"/admin/feedback/{fb_id}/status",
        data={"csrf_token": csrf, "status": "bogus_status"},
    )
    assert resp_invalid.status_code == 422


def test_admin_form_csrf_and_origin_protection(
    admin_app_and_clients: tuple[TestClient, TestClient, str], run_log: RunLog
) -> None:
    admin, _, _ = admin_app_and_clients
    fb_store = FeedbackStore(run_log.database_url)
    fb_id = uuid.uuid4()
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb_id,
            created_at=datetime.now(UTC),
            text="CSRF test feedback.",
        )
    )

    page = admin.get("/admin")
    csrf = _extract_csrf(page.text)

    # 1. Foreign Origin -> 403
    resp_foreign = admin.post(
        f"/admin/feedback/{fb_id}/status",
        data={"csrf_token": csrf, "status": "seen"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp_foreign.status_code == 403

    # 2. Foreign Origin on link create -> 403
    resp_link_foreign = admin.post(
        "/admin/links",
        data={"csrf_token": csrf},
        headers={"Origin": "https://evil.example"},
    )
    assert resp_link_foreign.status_code == 403

    # 3. Missing CSRF token on form post -> 403
    resp_no_csrf = admin.post(
        f"/admin/feedback/{fb_id}/status",
        data={"status": "seen"},
    )
    assert resp_no_csrf.status_code == 403

    # 4. Bad CSRF token -> 403
    resp_bad_csrf = admin.post(
        f"/admin/feedback/{fb_id}/status",
        data={"csrf_token": "bad-csrf-token", "status": "seen"},
    )
    assert resp_bad_csrf.status_code == 403


def test_blocking_session_takes_effect_on_next_request(
    admin_app_and_clients: tuple[TestClient, TestClient, str],
) -> None:
    admin, visitor, _ = admin_app_and_clients

    # Visitor's current session works
    assert visitor.get("/").status_code == 200

    # Find visitor's session id
    sessions = admin.get("/admin/sessions").json()
    assert len(sessions) >= 1
    visitor_session_id = sessions[-1]["session_id"]

    # Admin blocks session
    block_resp = admin.post(f"/admin/sessions/{visitor_session_id}/block")
    assert block_resp.status_code == 200

    # On next request, visitor is refused (403)
    next_req = visitor.get("/")
    assert next_req.status_code == 403


def test_feedback_content_escaped_and_screenshots_scoped_in_admin(
    admin_app_and_clients: tuple[TestClient, TestClient, str], run_log: RunLog
) -> None:
    admin, _, _ = admin_app_and_clients
    fb_store = FeedbackStore(run_log.database_url)

    evidence = admin.app.state.evidence  # type: ignore[attr-defined]
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    shot_sha = sha256_hex(png_bytes)
    evidence.put(io.BytesIO(png_bytes), sha256=shot_sha)

    fb_id = uuid.uuid4()
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb_id,
            created_at=datetime.now(UTC),
            text="<script>alert('xss')</script>",
            name="<img src=x onerror=alert(1)>",
            screenshot_sha256=shot_sha,
            screen="/reports/test",
            run_id=uuid.uuid4(),
        )
    )

    page = admin.get("/admin")
    assert page.status_code == 200
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;alert" in page.text
    assert "<img src=x" not in page.text
    assert "&lt;img" in page.text

    # Screenshot links only to scoped route
    assert f"/feedback/screenshots/{shot_sha}" in page.text


def test_admin_inputs_length_capped_and_control_chars_rejected(
    admin_app_and_clients: tuple[TestClient, TestClient, str], run_log: RunLog
) -> None:
    admin, _, _ = admin_app_and_clients
    fb_store = FeedbackStore(run_log.database_url)
    fb_id = uuid.uuid4()
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb_id,
            created_at=datetime.now(UTC),
            text="Control chars test.",
        )
    )

    page = admin.get("/admin")
    csrf = _extract_csrf(page.text)

    # 1. NUL byte in status -> 422
    resp_nul = admin.post(
        f"/admin/feedback/{fb_id}/status",
        data={"csrf_token": csrf, "status": "seen\x00"},
    )
    assert resp_nul.status_code == 422

    # 2. Control character in status -> 422
    resp_ctrl = admin.post(
        f"/admin/feedback/{fb_id}/status",
        data={"csrf_token": csrf, "status": "seen\x1b"},
    )
    assert resp_ctrl.status_code == 422

    # 3. Overlong status -> 422
    resp_long = admin.post(
        f"/admin/feedback/{fb_id}/status",
        data={"csrf_token": csrf, "status": "a" * 100},
    )
    assert resp_long.status_code == 422

    # 4. Control char in category query param -> 422
    resp_cat_ctrl = admin.get("/admin?category=bug%00")
    assert resp_cat_ctrl.status_code == 422
