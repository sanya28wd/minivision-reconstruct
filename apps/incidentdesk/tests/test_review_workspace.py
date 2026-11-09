"""Review workspace HTTP seams; browser playback remains a manual check (no Playwright dependency)."""

import asyncio
import io
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from starlette.types import Message

from access_client import authorized_client
from incident_report.access import AccessStore
from incident_report.ai import ReplayAIConnection
from incident_report.domain import Attribution, Origin, Report, ReportView, Role, Run, RunStatus
from incident_report.facts import CitedFact, OverviewFacts
from incident_report.hashing import sha256_hex
from incident_report.runlog import RunLog
from incident_report.storage.local import LocalEvidenceStore
from incident_report.web import create_app
from incident_report.web.review import VideoResponse


@pytest.fixture
def workspace(run_log: RunLog, tmp_path: Path) -> tuple[TestClient, Report, bytes]:
    store = LocalEvidenceStore(tmp_path / "media")
    content = b"\x00\x00\x00\x18ftypisom" + bytes(range(256))
    key = sha256_hex(content)
    store.put(io.BytesIO(content), sha256=key)
    other = b"\x1a\x45\xdf\xa3" + b"synthetic webm fixture"
    other_key = sha256_hex(other)
    store.put(io.BytesIO(other), sha256=other_key)
    fact = CitedFact(
        description="لوحة ABC 123 <script>x</script>", view_id=other_key, timestamp_seconds=2.5, confidence=1.0
    )
    report = Report(
        run_id=uuid.uuid4(),
        input_hash=key,
        prompt_version="synthetic",
        provenance="synthetic",
        summary="Review fixture",
        views=tuple(
            ReportView(
                filename="untrusted.html",
                role=Role.EXTERIOR,
                label="كاميرا ABC",
                acquisition_method=None,
                sha256=k,
                duration_seconds=10.0,
            )
            for k in (key, other_key)
        ),
        facts=OverviewFacts(
            parties=[],
            events=[fact.model_copy(update={"timestamp_seconds": 8.0, "description": "Later event"})],
            candidate_collisions=[fact],
            behaviour_observations=[],
            location_conditions=[],
        ),
    )
    run_log.record(
        Run(
            run_id=report.run_id,
            origin=Origin.REPLAY,
            created_at=datetime.now(UTC),
            input_hash=key,
            report=report,
            status=RunStatus.SUCCEEDED,
            total_latency_ms=0,
        )
    )
    app = create_app(ai=ReplayAIConnection(), run_log=run_log, evidence=store)
    return authorized_client(app, run_log.database_url), report, content


def test_video_stream_supports_seeking_and_does_not_trust_filename(workspace: tuple[TestClient, Report, bytes]) -> None:
    client, report, content = workspace
    url = f"/reports/{report.run_id}/views/{report.views[0].sha256}"
    response = client.get(url, headers={"Range": "bytes=12-21"})
    assert response.status_code == 206
    assert response.content == content[12:22]
    assert response.headers["content-range"] == f"bytes 12-21/{len(content)}"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get(url).content == content


@pytest.mark.parametrize("range_header,expected", [("bytes=12-", slice(12, None)), ("bytes=-8", slice(-8, None))])
def test_open_and_suffix_ranges(
    workspace: tuple[TestClient, Report, bytes], range_header: str, expected: slice
) -> None:
    client, report, content = workspace
    response = client.get(f"/reports/{report.run_id}/views/{report.views[0].sha256}", headers={"Range": range_header})
    assert response.status_code == 206
    assert response.content == content[expected]


@pytest.mark.parametrize("value", ["bytes=99999-", "bytes=9-2", "bytes=-0", "bytes=0-1,4-5", "bogus"])
def test_invalid_ranges_are_refused(workspace: tuple[TestClient, Report, bytes], value: str) -> None:
    client, report, content = workspace
    response = client.get(f"/reports/{report.run_id}/views/{report.views[0].sha256}", headers={"Range": value})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(content)}"


def test_media_requires_session_and_membership_in_report(workspace: tuple[TestClient, Report, bytes]) -> None:
    client, report, _ = workspace
    assert client.get(f"/reports/{report.run_id}/views/{'a' * 64}").status_code == 404
    assert client.get(f"/reports/{uuid.uuid4()}/views/{report.views[0].sha256}").status_code == 404
    client.cookies.clear()
    assert client.get(f"/reports/{report.run_id}/views/{report.views[0].sha256}").status_code == 403


def test_workspace_citations_markers_and_bidi_are_rendered_from_report(
    workspace: tuple[TestClient, Report, bytes],
) -> None:
    client, report, _ = workspace
    page = client.get(f"/reports/{report.run_id}/review")
    assert page.status_code == 200
    assert "data-review-workspace" in page.text
    assert f'data-view-id="{report.views[1].sha256}" data-seconds="2.5"' in page.text
    assert 'data-kind="event"' in page.text and 'data-kind="collision"' in page.text
    assert '<bdi dir="auto">لوحة ABC 123 &lt;script&gt;x&lt;/script&gt;</bdi>' in page.text
    assert 'id="review-secondary"' in page.text
    assert "/static/js/review.js" in page.text
    assert "<script>x</script>" not in page.text
    assert page.text.index("<h2>Summary</h2>") < page.text.index("<h2>Cited timeline</h2>")
    assert page.text.index('data-seconds="2.5"') < page.text.index('data-seconds="8.0"')


def test_report_workspace_and_video_accept_a_different_valid_share_session(
    workspace: tuple[TestClient, Report, bytes], run_log: RunLog
) -> None:
    client, report, content = workspace
    owner = AccessStore(run_log.database_url).create_link()
    opening = client.post("/access/open", json={"link_id": owner["link_id"], "secret": owner["secret"]})
    attribution = Attribution(session_id=uuid.UUID(opening.json()["session_id"]), link_id=uuid.UUID(owner["link_id"]))
    run_id = uuid.uuid4()
    owned_report = report.model_copy(update={"run_id": run_id})
    run_log.record(
        Run(
            run_id=run_id,
            origin=Origin.REPLAY,
            created_at=datetime.now(UTC),
            input_hash=report.input_hash,
            report=owned_report,
            status=RunStatus.SUCCEEDED,
            total_latency_ms=0,
            attribution=attribution,
        )
    )
    page_url = f"/reports/{run_id}/review"
    report_url = f"/reports/{run_id}"
    video_url = f"/reports/{run_id}/views/{report.views[0].sha256}"
    assert client.get(page_url).status_code == 200
    assert client.get(video_url).content == content
    stranger = AccessStore(run_log.database_url).create_link()
    client.post("/access/open", json={"link_id": stranger["link_id"], "secret": stranger["secret"]})
    assert client.get(report_url).status_code == 200
    assert client.get(page_url).status_code == 200
    video = client.get(video_url)
    assert video.status_code == 200
    assert video.content == content


def test_an_unrelated_stored_blob_cannot_be_served_as_video(workspace: tuple[TestClient, Report, bytes]) -> None:
    client, report, _ = workspace
    data = b"private unrelated image"
    key = sha256_hex(data)
    cast(FastAPI, client.app).state.evidence.put(io.BytesIO(data), sha256=key)
    assert client.get(f"/reports/{report.run_id}/views/{key}").status_code == 404


@pytest.mark.parametrize("disconnect_at", ["http.response.start", "http.response.body"])
def test_video_response_closes_evidence_on_early_or_midstream_disconnect(disconnect_at: str) -> None:
    stream = io.BytesIO(b"original video bytes")
    response = VideoResponse(stream, 20, status_code=200, media_type="video/mp4", headers={})

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == disconnect_at:
            raise OSError("synthetic disconnect")

    with pytest.raises(ClientDisconnect):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert stream.closed
