"""MIG-05 D: approved package download uses the report's share-session gate."""

import html
import io
import zipfile
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from access_client import authorized_client
from incident_report.domain import Report
from incident_report.runlog import RunLog
from test_package_parity import approve, csrf_token
from test_report_print import workspace as workspace


def test_draft_download_is_readable_conflict_and_has_no_button(workspace: tuple[TestClient, Report]) -> None:
    client, report = workspace
    url = f"/reports/{report.run_id}"
    response = client.get(url + "/versions/1/package")
    assert response.status_code == 409
    assert "draft" in response.text and response.headers["content-type"].startswith("text/html")
    assert url + "/versions/1/package" not in client.get(url + "/review").text


def test_approved_download_matches_print_and_records_actual_builder(
    workspace: tuple[TestClient, Report], run_log: RunLog
) -> None:
    from incident_report.package import verify

    client, report = workspace
    approve(client, report)
    url = f"/reports/{report.run_id}"
    response = client.get(url + "/versions/1/package")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == f'attachment; filename="incident-report-{report.run_id}-v1.zip"'
    assert verify(response.content)["version_number"] == 1
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("report.html") == client.get(url + "/print?version=1").content
    assert url + "/versions/1/package" in client.get(url + "/review?version=1").text
    build_events = [
        e for e in run_log.audit.list_entries() if e.run_id == report.run_id and e.action == "package.built"
    ]
    assert len(build_events) == 1
    assert build_events[0].actor == "visitor"
    assert build_events[0].session_id is not None and build_events[0].link_id is not None
    other = authorized_client(cast(FastAPI, client.app), run_log.database_url)
    assert other.get(url + "/versions/1/package").content == response.content
    from incident_report.access import AccessStore
    from incident_report.web.access import COOKIE

    access = AccessStore(run_log.database_url)
    sessions = [access.session(browser.cookies.get(COOKIE)) for browser in (client, other)]
    assert all(session is not None and session.valid for session in sessions)
    expected = [session.attribution for session in sessions if session is not None]
    assert expected[0].session_id != expected[1].session_id
    builds = [e for e in run_log.audit.list_entries() if e.run_id == report.run_id and e.action == "package.built"]
    downloads = [
        e
        for e in run_log.audit.list_entries()
        if e.run_id == report.run_id
        and e.action == "GET /reports/{run_id}/versions/{n}/package"
        and e.status_code == 200
    ]
    assert len(builds) == len(downloads) == 2
    for event, attribution in zip(builds, expected, strict=True):
        assert event.session_id == attribution.session_id and event.link_id == attribution.link_id
        request = next(request for request in downloads if request.request_id == event.request_id)
        assert request.session_id == attribution.session_id and request.link_id == attribution.link_id
    assert builds[0].request_id != builds[1].request_id
    client.cookies.clear()
    assert client.get(url + "/versions/1/package").status_code == 403


def test_older_approved_package_stays_equal_after_new_approved_override(workspace: tuple[TestClient, Report]) -> None:
    client, report = workspace
    approve(client, report)
    url = f"/reports/{report.run_id}"
    before = client.get(url + "/versions/1/package")
    assert before.status_code == 200
    fields = {"csrf_token": csrf_token(client, report)}
    assert client.post(url + "/versions", data=fields, follow_redirects=False).status_code == 303
    assert (
        client.post(
            url + "/overrides",
            data={
                **fields,
                "finding_key": "party:0",
                "new_value": "Not at Fault",
                "reason_code": "other",
                "reason_text": "Synthetic version two",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    approve(client, report)
    assert client.get(url + "/versions/1/package").content == before.content
    assert url + "/versions/1/package" in client.get(url + "/review?version=1").text
    newer = client.get(url + "/versions/2/package")
    assert newer.status_code == 200 and newer.content != before.content
    with zipfile.ZipFile(io.BytesIO(newer.content)) as archive:
        assert b"Synthetic version two" in archive.read("report.html")
        assert archive.read("report.html") == client.get(url + "/print?version=2").content


def test_corrupt_evidence_download_refuses_before_any_zip_or_build_audit(
    workspace: tuple[TestClient, Report], run_log: RunLog
) -> None:
    client, report = workspace
    approve(client, report)
    store = cast(FastAPI, client.app).state.evidence
    with store.get(report.views[0].sha256) as stream:
        path = Path(stream.name)
    path.chmod(0o600)
    path.write_bytes(b"broken")
    response = client.get(f"/reports/{report.run_id}/versions/1/package")
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("text/html")
    assert "hash mismatch" in response.text
    assert html.escape(report.views[0].label or report.views[0].filename) in response.text
    assert not response.content.startswith(b"PK")
    assert not any(e.run_id == report.run_id and e.action == "package.built" for e in run_log.audit.list_entries())


def test_missing_evidence_download_is_readable_conflict(
    workspace: tuple[TestClient, Report], run_log: RunLog, tmp_path: Path
) -> None:
    client, report = workspace
    approve(client, report)
    with cast(FastAPI, client.app).state.evidence.get(report.views[0].sha256) as stream:
        path = Path(stream.name)
    assert path.resolve().is_relative_to(tmp_path.resolve())
    path.chmod(0o600)
    path.unlink()
    response = client.get(f"/reports/{report.run_id}/versions/1/package")
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("text/html")
    assert "Missing evidence" in response.text
    assert html.escape(report.views[0].label or report.views[0].filename) in response.text
    assert not any(e.run_id == report.run_id and e.action == "package.built" for e in run_log.audit.list_entries())
