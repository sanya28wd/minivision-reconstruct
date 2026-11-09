"""ADR 0010 through HTTP, plus Run and access-event read interfaces. No live AI."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audit_test_helpers import unsafe_test_only_truncate
from conftest import write_recording
from incident_report.ai import ReplayAIConnection
from incident_report.domain import Role
from incident_report.entry import PROMPT_VERSION
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.runlog import RunLog
from incident_report.web import create_app
from video_fixtures import synthetic_video

AccessClients = tuple[TestClient, TestClient, dict[str, str], dict[str, str]]


def test_requests_without_a_share_session_are_refused(recordings_dir: Path, run_log: RunLog) -> None:
    client = TestClient(
        create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log), base_url="https://testserver"
    )
    assert client.get("/").status_code == 403
    assert client.post("/reports", data={"role": "exterior"}).status_code == 403


def test_admin_creates_a_link_that_opens_an_anonymous_session(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "synthetic-test-admin")
    unsafe_test_only_truncate(run_log.database_url, "audit_log", "anonymous_sessions", "share_links")
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log)
    admin = TestClient(app, base_url="https://testserver", headers={"Authorization": "Bearer synthetic-test-admin"})
    created = admin.post("/admin/links")
    assert created.status_code == 201
    link = created.json()
    visitor = TestClient(app, base_url="https://testserver")
    opened = visitor.post("/access/open", json={"link_id": link["link_id"], "secret": link["secret"]})
    assert opened.status_code == 200
    cookie = opened.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert visitor.get("/").status_code == 200
    sessions = admin.get("/admin/sessions").json()
    assert sessions[-1]["link_id"] == link["link_id"]
    assert sessions[-1]["session_id"] not in visitor.cookies.get("__Host-incident_session").split(".")[1:]


@pytest.fixture
def access_clients(recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch) -> AccessClients:
    unsafe_test_only_truncate(run_log.database_url, "audit_log", "anonymous_sessions", "share_links")
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "synthetic-test-admin")
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log)
    admin = TestClient(app, base_url="https://testserver", headers={"Authorization": "Bearer synthetic-test-admin"})
    visitor = TestClient(app, base_url="https://testserver")
    link = admin.post("/admin/links").json()
    opened = visitor.post("/access/open", json={"link_id": link["link_id"], "secret": link["secret"]})
    assert opened.status_code == 200
    return admin, visitor, link, opened.json()


@pytest.mark.parametrize("action", ["disable", "rotate", "block"])
def test_revocation_refuses_the_next_request_and_old_link(access_clients: AccessClients, action: str) -> None:
    admin, visitor, link, session = access_clients
    if action == "block":
        assert admin.post(f"/admin/sessions/{session['session_id']}/block").status_code == 200
    else:
        assert admin.post(f"/admin/links/{link['link_id']}/{action}").status_code == 200
    assert visitor.get("/").status_code == 403
    assert visitor.post("/reports", data={"role": "exterior"}).status_code == 403
    assert visitor.post("/access/open", json={"link_id": link["link_id"], "secret": link["secret"]}).status_code == 403


@pytest.mark.parametrize("secret", [None, "wrong-synthetic-secret"])
def test_missing_or_wrong_admin_secret_and_share_sessions_are_refused(
    access_clients: AccessClients, secret: str | None
) -> None:
    _, visitor, _, _ = access_clients
    headers = {"Authorization": "Bearer " + secret} if secret else {}
    assert visitor.get("/admin", headers=headers).status_code == 403
    assert visitor.get("/admin/sessions", headers=headers).status_code == 403
    assert visitor.post("/admin/links", headers=headers).status_code == 403


def test_unset_admin_secret_refuses_access_even_with_a_header(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INCIDENT_ADMIN_SECRET", raising=False)
    client = TestClient(
        create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log), base_url="https://testserver"
    )
    assert client.get("/admin").status_code == 403
    assert client.post("/admin/links", headers={"Authorization": "Bearer synthetic-test-admin"}).status_code == 403


def test_invalid_link_never_sets_a_cookie(access_clients: AccessClients) -> None:
    _, visitor, link, _ = access_clients
    import uuid

    for link_id, secret in [(link["link_id"], "x" * 43), (str(uuid.uuid4()), link["secret"])]:
        response = visitor.post("/access/open", json={"link_id": link_id, "secret": secret})
        assert response.status_code == 403
        assert "set-cookie" not in response.headers


def test_rotate_issues_a_new_secret_and_revokes_sessions_on_another_instance(
    access_clients: AccessClients, recordings_dir: Path, run_log: RunLog
) -> None:
    admin, visitor, link, _ = access_clients
    other = TestClient(
        create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log), base_url="https://testserver"
    )
    other.cookies.update(visitor.cookies)
    rotated = admin.post(f"/admin/links/{link['link_id']}/rotate").json()
    assert rotated["secret"] != link["secret"]
    assert other.get("/").status_code == 403
    other.cookies.clear()
    assert (
        other.post("/access/open", json={"link_id": rotated["link_id"], "secret": rotated["secret"]}).status_code == 200
    )
    assert other.get("/").status_code == 200


def test_runs_and_request_events_keep_the_same_anonymous_attribution(
    access_clients: AccessClients, recordings_dir: Path, run_log: RunLog
) -> None:
    admin, visitor, _, session = access_clients
    content = synthetic_video(b"attributed-view")
    write_recording(
        recordings_dir,
        incident_input_hash([(Role.EXTERIOR, sha256_hex(content))]),
        PROMPT_VERSION,
        "Attributed summary.",
    )
    response = visitor.post("/reports", data={"role": "exterior"}, files={"video": ("front.mp4", content, "video/mp4")})
    assert response.status_code == 200
    (run,) = run_log.list_runs()
    assert run.attribution is not None
    assert str(run.attribution.session_id) == session["session_id"]
    assert str(run.attribution.link_id) == session["link_id"]
    events = admin.get("/admin/events").json()
    (event,) = [e for e in events if e["request_id"] == str(run.request_id)]
    assert event["session_id"] == session["session_id"] and event["link_id"] == session["link_id"]
    assert event["run_id"] == str(run.run_id) and event["status_code"] == 303
    assert all(e["session_id"] == session["session_id"] for e in events if e["actor"] == "visitor")


@pytest.mark.parametrize("endpoint", ["/admin", "/access/open"])
def test_rate_limits_count_failed_attempts(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setenv("INCIDENT_ACCESS_ADMIN_ATTEMPTS", "2")
    monkeypatch.setenv("INCIDENT_ACCESS_LINK_ATTEMPTS", "2")
    client = TestClient(
        create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log), base_url="https://testserver"
    )
    for _ in range(2):
        response = client.get(endpoint) if endpoint == "/admin" else client.post(endpoint, json={})
        assert response.status_code != 429
    response = client.get(endpoint) if endpoint == "/admin" else client.post(endpoint, json={})
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


def test_no_secrets_in_logs_events_or_admin_listings(
    access_clients: AccessClients, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    admin, visitor, link, _ = access_clients
    cookie = visitor.cookies.get("__Host-incident_session")
    with caplog.at_level(logging.DEBUG):
        assert visitor.get("/").status_code == 200
        assert (
            visitor.post("/access/open", json={"link_id": link["link_id"], "secret": link["secret"]}).status_code == 200
        )
        listings = admin.get("/admin/links").text + admin.get("/admin/sessions").text + admin.get("/admin/events").text
        assert admin.get("/admin", headers={"Authorization": "Bearer wrong-synthetic-admin"}).status_code == 403
    for secret in [link["secret"], cookie, cookie.split(".", 1)[1], "synthetic-test-admin", "wrong-synthetic-admin"]:
        assert secret not in caplog.text
        assert secret not in listings


def test_opening_the_same_link_keeps_the_browser_identity(access_clients: AccessClients) -> None:
    admin, visitor, link, session = access_clients
    response = visitor.post("/access/open", json={"link_id": link["link_id"], "secret": link["secret"]})
    assert response.json() == session
    assert len(admin.get("/admin/sessions").json()) == 1


def test_failed_runs_have_request_attribution_too(access_clients: AccessClients, run_log: RunLog) -> None:
    admin, visitor, _, session = access_clients
    response = visitor.post(
        "/reports",
        data={"role": "exterior"},
        files={"video": ("missing.mp4", synthetic_video(b"no-recording"), "video/mp4")},
    )
    assert response.status_code == 422
    (run,) = run_log.list_runs()
    assert run.attribution is not None
    assert str(run.attribution.session_id) == session["session_id"]
    events = admin.get("/admin/events").json()
    (event,) = [e for e in events if e["request_id"] == str(run.request_id)]
    assert event["run_id"] == str(run.run_id)


def test_share_bootstrap_keeps_secret_out_of_the_request_url(access_clients: AccessClients) -> None:
    _, visitor, link, _ = access_clients
    assert link["url"].startswith("/share#")
    page = visitor.get("/share")
    assert page.status_code == 200
    assert link["secret"] not in page.text
    assert "/static/js/share.js" in page.text


def test_report_view_events_identify_the_run(
    access_clients: AccessClients, recordings_dir: Path, run_log: RunLog
) -> None:
    admin, visitor, _, _ = access_clients
    content = synthetic_video(b"view-audit")
    write_recording(
        recordings_dir, incident_input_hash([(Role.EXTERIOR, sha256_hex(content))]), PROMPT_VERSION, "Viewed summary."
    )
    response = visitor.post("/reports", data={"role": "exterior"}, files={"video": ("front.mp4", content, "video/mp4")})
    assert response.status_code == 200
    (run,) = run_log.list_runs()
    events = admin.get("/admin/events").json()
    (viewed,) = [e for e in events if e["action"] == "GET /reports/{run_id}"]
    assert viewed["run_id"] == str(run.run_id)


def test_anonymous_refusals_are_rate_limited_in_audit_log(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "synthetic-test-admin")
    monkeypatch.setenv("INCIDENT_ACCESS_REFUSAL_AUDIT_ATTEMPTS", "3")
    unsafe_test_only_truncate(run_log.database_url, "audit_log", "anonymous_sessions", "share_links")
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log)
    client = TestClient(app, base_url="https://testserver")

    for _ in range(10):
        res = client.get("/")
        assert res.status_code == 403

    admin = TestClient(app, base_url="https://testserver", headers={"Authorization": "Bearer synthetic-test-admin"})
    events = admin.get("/admin/events").json()
    refusal_events = [e for e in events if e["actor"] == "anonymous" and e["status_code"] == 403]
    assert len(refusal_events) == 3


def test_admin_management_targets_recorded_in_audit_log(access_clients: AccessClients, run_log: RunLog) -> None:
    admin, _, link, session = access_clients
    link_id = link["link_id"]
    session_id = session["session_id"]

    res_disable = admin.post(f"/admin/links/{link_id}/disable")
    assert res_disable.status_code == 200

    res_block = admin.post(f"/admin/sessions/{session_id}/block")
    assert res_block.status_code == 200

    from incident_report.audit import AuditLog

    audit = AuditLog(run_log.database_url)
    entries = audit.list_entries()

    disable_entries = [e for e in entries if e.action == "POST /admin/links/{link_id}/disable"]
    assert len(disable_entries) == 1
    assert disable_entries[0].target_type == "link"
    assert disable_entries[0].target_id == link_id

    block_entries = [e for e in entries if e.action == "POST /admin/sessions/{session_id}/block"]
    assert len(block_entries) == 1
    assert block_entries[0].target_type == "session"
    assert block_entries[0].target_id == session_id
