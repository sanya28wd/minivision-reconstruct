"""Tests for Incidents list: status, share-link scoping, admin view, paging, query counts (RPT-41)."""

from __future__ import annotations

import unittest.mock as mock
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from audit_test_helpers import unsafe_test_only_truncate
from incident_report.ai import ReplayAIConnection
from incident_report.config import Settings
from incident_report.domain import (
    Attribution,
    IncidentRow,
    Origin,
    Report,
    ReportView,
    Role,
    Run,
    RunStatus,
)
from incident_report.runlog import RunLog
from incident_report.storage import create_evidence_store
from incident_report.web import create_app


def _make_run(
    run_id: uuid.UUID,
    *,
    status: RunStatus = RunStatus.SUCCEEDED,
    is_valid: bool = True,
    created_at: datetime | None = None,
    link_id: uuid.UUID | None = None,
    error: str | None = None,
) -> Run:
    session_id = uuid.uuid4() if link_id else None
    report: Report | None = None
    if status is RunStatus.SUCCEEDED:
        report = Report(
            run_id=run_id,
            input_hash="a" * 64,
            prompt_version="test-1",
            provenance="synthetic",
            summary="Test report summary.",
            views=(),
            is_valid=is_valid,
        )
    return Run(
        run_id=run_id,
        origin=Origin.APP,
        created_at=created_at or datetime.now(UTC),
        input_hash="a" * 64,
        report=report,
        status=status,
        total_latency_ms=100.0,
        error=error if status is RunStatus.FAILED else None,
        attribution=Attribution(session_id=session_id, link_id=link_id) if session_id and link_id else None,
    )


def test_incident_row_status_labels() -> None:
    run_id = uuid.uuid4()

    proc = IncidentRow(run=_make_run(run_id, status=RunStatus.PROCESSING), view_count=1)
    assert proc.status_label == "Processing"
    assert proc.is_ready is False

    ready = IncidentRow(run=_make_run(run_id, status=RunStatus.SUCCEEDED, is_valid=True), view_count=2)
    assert ready.status_label == "Ready"
    assert ready.is_ready is True

    invalid = IncidentRow(run=_make_run(run_id, status=RunStatus.SUCCEEDED, is_valid=False), view_count=1)
    assert invalid.status_label == "Invalid"
    assert invalid.is_ready is False

    failed = IncidentRow(run=_make_run(run_id, status=RunStatus.FAILED, error="Error"), view_count=0)
    assert failed.status_label == "Failed"
    assert failed.is_ready is False


def test_list_runs_for_link_scoped_and_ordered(run_log: RunLog) -> None:
    link_a = uuid.uuid4()
    link_b = uuid.uuid4()
    now = datetime.now(UTC)

    run_a1 = _make_run(uuid.uuid4(), link_id=link_a, created_at=now - timedelta(minutes=5))
    run_a2 = _make_run(uuid.uuid4(), link_id=link_a, created_at=now - timedelta(minutes=1))
    run_b1 = _make_run(uuid.uuid4(), link_id=link_b, created_at=now - timedelta(minutes=2))

    run_log.record(run_a1)
    run_log.record(run_a2)
    run_log.record(run_b1)

    # Scoped to link_a, newest first
    rows_a = run_log.list_runs_for_link(link_a)
    assert len(rows_a) == 2
    assert rows_a[0].run_id == run_a2.run_id
    assert rows_a[1].run_id == run_a1.run_id

    # Scoped to link_b
    rows_b = run_log.list_runs_for_link(link_b)
    assert len(rows_b) == 1
    assert rows_b[0].run_id == run_b1.run_id

    # Unscoped admin sees all 3, newest first
    admin_rows = run_log.list_all_runs()
    admin_ids = [r.run_id for r in admin_rows]
    assert run_a2.run_id in admin_ids
    assert run_b1.run_id in admin_ids
    assert run_a1.run_id in admin_ids
    assert admin_ids.index(run_a2.run_id) < admin_ids.index(run_b1.run_id) < admin_ids.index(run_a1.run_id)


def test_list_runs_includes_intake_view_count(run_log: RunLog) -> None:
    link_id = uuid.uuid4()
    run_id = uuid.uuid4()
    run = _make_run(run_id, link_id=link_id)
    run_log.record(run)

    # Record 2 intake views
    v1 = ReportView(filename="front.mp4", role=Role.EXTERIOR, label="Front", acquisition_method=None, sha256="1" * 64)
    v2 = ReportView(filename="cabin.mp4", role=Role.INTERIOR, label="Cabin", acquisition_method=None, sha256="2" * 64)
    run_log.record_intake_view(run_id, 0, v1)
    run_log.record_intake_view(run_id, 1, v2)

    rows = run_log.list_runs_for_link(link_id)
    assert len(rows) == 1
    assert rows[0].run_id == run_id
    assert rows[0].view_count == 2
    assert "front.mp4" in (rows[0].filenames or "")
    assert "cabin.mp4" in (rows[0].filenames or "")


def test_list_runs_query_count_no_n_plus_one(run_log: RunLog) -> None:
    link_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Insert 10 runs with intake views
    for i in range(10):
        r_id = uuid.uuid4()
        run = _make_run(r_id, link_id=link_id, created_at=now - timedelta(minutes=i))
        run_log.record(run)
        v = ReportView(
            filename=f"view_{i}.mp4",
            role=Role.EXTERIOR,
            label=f"View {i}",
            acquisition_method=None,
            sha256=f"{i:02x}" * 32,
        )
        run_log.record_intake_view(r_id, 0, v)

    # Intercept queries to assert single query execution
    query_count = 0
    orig_connect = psycopg.connect

    def counting_connect(*args: Any, **kwargs: Any) -> Any:
        conn = orig_connect(*args, **kwargs)
        orig_execute = conn.execute

        def counting_execute(query: Any, *params: Any, **kw: Any) -> Any:
            nonlocal query_count
            query_str = str(query)
            if "SELECT" in query_str.upper():
                query_count += 1
            return orig_execute(query, *params, **kw)

        conn.execute = counting_execute  # type: ignore[method-assign]
        return conn

    with mock.patch("psycopg.connect", side_effect=counting_connect):
        rows = run_log.list_runs_for_link(link_id, limit=50)

    assert len(rows) == 10
    # Must be exactly 1 SELECT query (no N+1 per row)
    assert query_count == 1


def test_list_runs_stale_processing_run_transitions_to_failed(run_log: RunLog) -> None:
    link_id = uuid.uuid4()
    stale_time = datetime.now(UTC) - timedelta(hours=2)
    stale_run = _make_run(uuid.uuid4(), status=RunStatus.PROCESSING, link_id=link_id, created_at=stale_time)
    run_log.record(stale_run)

    rows = run_log.list_runs_for_link(link_id)
    assert len(rows) == 1
    assert rows[0].run_id == stale_run.run_id
    assert rows[0].status_label == "Failed"
    assert rows[0].run.status is RunStatus.FAILED
    assert rows[0].run.error == "Analysis was interrupted."


# ---------------------------------------------------------------------------
# HTTP Route Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def web_clients(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[TestClient, TestClient, uuid.UUID, str]:
    unsafe_test_only_truncate(run_log.database_url, "audit_log", "anonymous_sessions", "share_links")
    admin_secret = "synthetic-test-admin-secret"  # noqa: S105
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", admin_secret)

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("INCIDENT_EVIDENCE_DIR", str(evidence_dir))

    settings = Settings.from_env({"DATABASE_URL": run_log.database_url})
    store = create_evidence_store(settings)

    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=store)
    admin_client = TestClient(
        app,
        base_url="https://testserver",
        headers={"Authorization": f"Bearer {admin_secret}"},
    )
    visitor_client = TestClient(app, base_url="https://testserver")

    # Create link and establish visitor session
    link_resp = admin_client.post("/admin/links").json()
    link_id = uuid.UUID(link_resp["link_id"])
    open_resp = visitor_client.post("/access/open", json={"link_id": str(link_id), "secret": link_resp["secret"]})
    assert open_resp.status_code == 200

    return admin_client, visitor_client, link_id, admin_secret


def test_incidents_without_session_returns_403(recordings_dir: Path, run_log: RunLog, tmp_path: Path) -> None:
    settings = Settings.from_env({"DATABASE_URL": run_log.database_url})
    store = create_evidence_store(settings)
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=store)
    anon_client = TestClient(app, base_url="https://testserver")

    resp = anon_client.get("/incidents")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Open a valid share link to use the tool."}


def test_incidents_with_bearer_token_and_no_session_returns_403(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    admin_secret = "secret-123"  # noqa: S105
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", admin_secret)
    settings = Settings.from_env({"DATABASE_URL": run_log.database_url})
    store = create_evidence_store(settings)
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=store)
    client = TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {admin_secret}"})

    # Visiting non-admin route with Bearer must never bypass share session check
    resp = client.get("/incidents")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Open a valid share link to use the tool."}


def test_admin_incidents_without_bearer_returns_403(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "adm-secret")
    settings = Settings.from_env({"DATABASE_URL": run_log.database_url})
    store = create_evidence_store(settings)
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=store)
    client = TestClient(app, base_url="https://testserver")

    resp = client.get("/admin/incidents")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Admin access refused."}


def test_admin_incidents_with_share_session_returns_403(
    web_clients: tuple[TestClient, TestClient, uuid.UUID, str],
) -> None:
    _, visitor, _, _ = web_clients
    resp = visitor.get("/admin/incidents")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Admin access refused."}


def test_incidents_list_scoping_between_share_links(
    web_clients: tuple[TestClient, TestClient, uuid.UUID, str], run_log: RunLog
) -> None:
    admin, visitor_a, link_a, _ = web_clients

    # Create link B and visitor B
    link_b_resp = admin.post("/admin/links").json()
    link_b = uuid.UUID(link_b_resp["link_id"])
    visitor_b = TestClient(visitor_a.app, base_url="https://testserver")
    opened_b = visitor_b.post("/access/open", json={"link_id": str(link_b), "secret": link_b_resp["secret"]})
    assert opened_b.status_code == 200

    # Create run for link A and run for link B
    run_a = _make_run(uuid.uuid4(), link_id=link_a, status=RunStatus.SUCCEEDED, is_valid=True)
    run_b = _make_run(uuid.uuid4(), link_id=link_b, status=RunStatus.SUCCEEDED, is_valid=True)
    run_log.record(run_a)
    run_log.record(run_b)

    # Visitor A sees run A but NOT run B
    resp_a = visitor_a.get("/incidents")
    assert resp_a.status_code == 200
    assert str(run_a.run_id) in resp_a.text
    assert str(run_b.run_id) not in resp_a.text

    # Visitor B sees run B but NOT run A
    resp_b = visitor_b.get("/incidents")
    assert resp_b.status_code == 200
    assert str(run_b.run_id) in resp_b.text
    assert str(run_a.run_id) not in resp_b.text


def test_status_update_on_reload(web_clients: tuple[TestClient, TestClient, uuid.UUID, str], run_log: RunLog) -> None:
    _, visitor, link_id, _ = web_clients
    run_id = uuid.uuid4()

    # 1. Run is created in PROCESSING status
    processing_run = _make_run(run_id, link_id=link_id, status=RunStatus.PROCESSING)
    run_log.record(processing_run)

    resp1 = visitor.get("/incidents")
    assert resp1.status_code == 200
    assert "Processing" in resp1.text
    assert "Ready" not in resp1.text

    # 2. Run completes: updated to SUCCEEDED with valid report
    succeeded_run = _make_run(run_id, link_id=link_id, status=RunStatus.SUCCEEDED, is_valid=True)
    run_log.record(succeeded_run)

    # 3. On page reload, status updates to "Ready"
    resp2 = visitor.get("/incidents")
    assert resp2.status_code == 200
    assert "Ready" in resp2.text


def test_incidents_page_elements_and_escaping(
    web_clients: tuple[TestClient, TestClient, uuid.UUID, str], run_log: RunLog
) -> None:
    _, visitor, link_id, _ = web_clients
    run_id = uuid.uuid4()
    run = _make_run(run_id, link_id=link_id, status=RunStatus.SUCCEEDED, is_valid=True)
    run_log.record(run)

    # Malicious filename to test escaping
    evil_filename = "<script>alert('xss')</script>.mp4"
    v = ReportView(
        filename=evil_filename,
        role=Role.EXTERIOR,
        label="Dangerous <tag>",
        acquisition_method=None,
        sha256="f" * 64,
    )
    run_log.record_intake_view(run_id, 0, v)

    resp = visitor.get("/incidents")
    assert resp.status_code == 200
    # Must have link to report
    assert f'href="/reports/{run_id}"' in resp.text
    # Must have link to review since it is Ready
    assert f'href="/reports/{run_id}/review"' in resp.text
    # Escaping: evil script tag must NOT be rendered unescaped
    assert "<script>alert" not in resp.text
    assert "&lt;script&gt;alert" in resp.text


def test_admin_incidents_sees_all_and_json(
    web_clients: tuple[TestClient, TestClient, uuid.UUID, str], run_log: RunLog
) -> None:
    admin, visitor, link_a, _ = web_clients
    run_a = _make_run(uuid.uuid4(), link_id=link_a, status=RunStatus.SUCCEEDED)
    run_b = _make_run(uuid.uuid4(), link_id=uuid.uuid4(), status=RunStatus.FAILED, error="Some error")
    run_log.record(run_a)
    run_log.record(run_b)

    # Admin HTML view
    resp_html = admin.get("/admin/incidents")
    assert resp_html.status_code == 200
    assert str(run_a.run_id) in resp_html.text
    assert str(run_b.run_id) in resp_html.text

    # Admin JSON view
    resp_json = admin.get("/admin/incidents", headers={"Accept": "application/json"})
    assert resp_json.status_code == 200
    data = resp_json.json()
    assert "incidents" in data
    ids = [i["run_id"] for i in data["incidents"]]
    assert str(run_a.run_id) in ids
    assert str(run_b.run_id) in ids


def test_incidents_pagination_and_validation(
    web_clients: tuple[TestClient, TestClient, uuid.UUID, str], run_log: RunLog
) -> None:
    _, visitor, link_id, _ = web_clients
    now = datetime.now(UTC)

    # Insert 55 runs for link_id
    for i in range(55):
        run = _make_run(uuid.uuid4(), link_id=link_id, created_at=now - timedelta(minutes=i))
        run_log.record(run)

    # Page 1: has Next link, no Previous link
    resp1 = visitor.get("/incidents?page=1")
    assert resp1.status_code == 200
    assert "Next" in resp1.text
    assert "Previous" not in resp1.text

    # Page 2: has Previous link, no Next link
    resp2 = visitor.get("/incidents?page=2")
    assert resp2.status_code == 200
    assert "Previous" in resp2.text
    assert "Next" not in resp2.text

    # Page 0 or invalid page returns 422
    resp_invalid = visitor.get("/incidents?page=0")
    assert resp_invalid.status_code == 422


def test_incidents_web_request_query_count_no_n_plus_one(
    web_clients: tuple[TestClient, TestClient, uuid.UUID, str], run_log: RunLog
) -> None:
    _, visitor, link_id, _ = web_clients
    now = datetime.now(UTC)

    # Insert 15 runs with intake views
    for i in range(15):
        r_id = uuid.uuid4()
        run = _make_run(r_id, link_id=link_id, created_at=now - timedelta(minutes=i))
        run_log.record(run)
        v = ReportView(
            filename=f"video_{i}.mp4",
            role=Role.EXTERIOR,
            label=f"View {i}",
            acquisition_method=None,
            sha256=f"{i:02x}" * 32,
        )
        run_log.record_intake_view(r_id, 0, v)

    runs_queries: list[str] = []
    orig_connect = psycopg.connect

    def counting_connect(*args: Any, **kwargs: Any) -> Any:
        conn = orig_connect(*args, **kwargs)
        orig_execute = conn.execute

        def counting_execute(query: Any, *params: Any, **kw: Any) -> Any:
            q_str = str(query)
            if "FROM runs" in q_str:
                runs_queries.append(q_str)
            return orig_execute(query, *params, **kw)

        conn.execute = counting_execute  # type: ignore[method-assign]
        return conn

    with mock.patch("psycopg.connect", side_effect=counting_connect):
        resp = visitor.get("/incidents")

    assert resp.status_code == 200
    # Must query runs exactly once for the incident list, never N+1 per row
    list_queries = [q for q in runs_queries if "LEFT JOIN" in q]
    assert len(list_queries) == 1
    # Total queries on runs during the request (1 for list + 1 for access audit) is fixed (O(1), not O(N))
    assert len(runs_queries) == 2
