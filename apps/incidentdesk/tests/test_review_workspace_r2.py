"""Round 2 regressions at the workspace HTTP and rendered-marker-source seams."""

import io
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from incident_report.domain import Origin, Report, Run, RunStatus
from incident_report.hashing import sha256_hex
from incident_report.rules.models import FaultStatus, Finding, PartyFault
from incident_report.runlog import RunLog
from test_review_workspace import workspace as workspace


class Markup(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def store_report(log: RunLog, report: Report) -> Report:
    report = report.model_copy(update={"run_id": uuid.uuid4()})
    log.record(
        Run(
            run_id=report.run_id,
            origin=Origin.REPLAY,
            created_at=datetime.now(UTC),
            input_hash=report.input_hash,
            report=report,
            status=RunStatus.SUCCEEDED,
            total_latency_ms=0,
        )
    )
    return report


def test_consolidated_events_and_marker_sources_appear_once_after_dense_pass(
    workspace: tuple[TestClient, Report, bytes], run_log: RunLog
) -> None:
    client, report, _ = workspace
    assert report.facts is not None
    consolidated = store_report(run_log, report.model_copy(update={"dense_facts": (report.facts,)}))
    page = client.get(f"/reports/{consolidated.run_id}/review")
    assert page.status_code == 200
    markup = Markup(page.text)
    # review.js creates exactly one scrubber marker per data-timeline-event source.
    sources = [attrs for tag, attrs in markup.tags if "data-timeline-event" in attrs]
    assert len(sources) == 2
    view_id = report.views[1].sha256
    assert (
        sum(
            attrs.get("data-kind") == "event"
            and attrs.get("data-view-id") == view_id
            and attrs.get("data-seconds") == "8.0"
            for attrs in sources
        )
        == 1
    )
    assert (
        sum(
            attrs.get("data-kind") == "collision"
            and attrs.get("data-view-id") == view_id
            and attrs.get("data-seconds") == "2.5"
            for attrs in sources
        )
        == 1
    )
    assert page.text.count("Later event") == 1
    assert sum(attrs.get("data-marker-view") == view_id for _, attrs in markup.tags) == 1


def test_factors_and_behaviour_citations_seek_only_in_workspace(
    workspace: tuple[TestClient, Report, bytes], run_log: RunLog
) -> None:
    client, report, _ = workspace
    assert report.facts is not None
    fact = report.facts.candidate_collisions[0]
    factor = Finding(
        party_description="تويوتا Toyota sedan ABC 123",
        rule_id="synthetic-test-duty",
        legal_source="Synthetic fixture only, not a legal claim",
        article="synthetic fixture",
        source_url="/synthetic-fixture",
        reason="سبب <script>reason</script> ABC",
        classification="Contributing Factor",
        cited_facts=(fact.model_copy(update={"timestamp_seconds": 4.0}),),
    )
    party = PartyFault(
        party_description=factor.party_description,
        status=FaultStatus.CANNOT_DETERMINE,
        color="grey",
        reason="Synthetic fixture",
        cited_facts=(fact,),
        findings=(factor,),
    )
    observation = fact.model_copy(update={"description": "سلوك <img src=x> ABC", "timestamp_seconds": 6.0})
    report = store_report(
        run_log,
        report.model_copy(
            update={
                "parties": (party,),
                "facts": report.facts.model_copy(update={"behaviour_observations": [observation]}),
            }
        ),
    )
    for suffix, expected_tag in (("/review", "button"), ("", "span")):
        page = client.get(f"/reports/{report.run_id}{suffix}")
        assert page.status_code == 200
        for section in ("Contributing Factors", "Behaviour observations"):
            fragment = page.text.split(f'<section aria-label="{section}">', 1)[1].split("</section>", 1)[0]
            citations = [(tag, attrs) for tag, attrs in Markup(fragment).tags if attrs.get("class") == "citation"]
            assert len(citations) == 1
            tag, attrs = citations[0]
            assert tag == expected_tag
            if suffix:
                assert attrs["data-view-id"] == fact.view_id
                assert attrs["data-seconds"] == ("4.0" if section == "Contributing Factors" else "6.0")
            else:
                assert "data-seconds" not in attrs
        assert '<bdi dir="auto">تويوتا Toyota sedan ABC 123</bdi>' in page.text
        assert '<bdi dir="auto">سبب &lt;script&gt;reason&lt;/script&gt; ABC</bdi>' in page.text
        assert '<bdi dir="auto">سلوك &lt;img src=x&gt; ABC</bdi>' in page.text
        assert "<script>reason</script>" not in page.text
        assert "<img src=x>" not in page.text


@pytest.mark.parametrize("failed", [False, True], ids=["unknown", "failed"])
def test_workspace_missing_or_failed_run_uses_the_html_error_page(
    workspace: tuple[TestClient, Report, bytes], run_log: RunLog, failed: bool
) -> None:
    client, report, _ = workspace
    run_id = uuid.uuid4()
    if failed:
        run_log.record(
            Run(
                run_id=run_id,
                origin=Origin.REPLAY,
                created_at=datetime.now(UTC),
                input_hash=report.input_hash,
                report=None,
                status=RunStatus.FAILED,
                total_latency_ms=0,
                error="Synthetic failed run",
            )
        )
    page = client.get(f"/reports/{run_id}/review")
    assert page.status_code == 404
    assert page.headers["content-type"].startswith("text/html")
    assert "No report with that id." in page.text
    assert "<main" in page.text and "<h1" in page.text


def test_workspace_requires_a_session(workspace: tuple[TestClient, Report, bytes]) -> None:
    client, report, _ = workspace
    client.cookies.clear()
    assert client.get(f"/reports/{report.run_id}/review").status_code == 403


@pytest.mark.parametrize(
    "content,mime,status",
    [
        (b"<html>not a video</html>", None, 415),
        (b"\x1a\x45\xdf\xa3synthetic webm fixture", "video/webm", 200),
        (b"RIFF\x20\x00\x00\x00AVI synthetic fixture", "video/x-msvideo", 200),
    ],
)
def test_stored_media_type_is_sniffed_and_unsupported_formats_refused(
    workspace: tuple[TestClient, Report, bytes], run_log: RunLog, content: bytes, mime: str | None, status: int
) -> None:
    client, report, _ = workspace
    key = sha256_hex(content)
    cast(FastAPI, client.app).state.evidence.put(io.BytesIO(content), sha256=key)
    view = report.views[0].model_copy(update={"sha256": key, "filename": "attacker.html", "mime_type": "text/html"})
    report = store_report(run_log, report.model_copy(update={"views": (view,)}))
    response = client.get(f"/reports/{report.run_id}/views/{key}")
    assert response.status_code == status
    assert response.headers["x-content-type-options"] == "nosniff"
    if mime:
        assert response.headers["content-type"] == mime
        assert response.content == content
    else:
        assert response.json()["detail"] == "This video format cannot be played here."


def test_range_end_past_eof_is_clamped(workspace: tuple[TestClient, Report, bytes]) -> None:
    client, report, content = workspace
    response = client.get(
        f"/reports/{report.run_id}/views/{report.views[0].sha256}", headers={"Range": "bytes=0-99999"}
    )
    assert response.status_code == 206
    assert response.content == content
    assert response.headers["content-range"] == f"bytes 0-{len(content) - 1}/{len(content)}"
    assert response.headers["content-length"] == str(len(content))


@pytest.mark.parametrize(
    "range_value",
    ["bytes=" + "9" * 5000 + "-", "bytes=0-" + "9" * 5000, "bytes=-" + "9" * 5000],
    ids=["start", "end", "suffix"],
)
def test_huge_digit_ranges_fail_safely(workspace: tuple[TestClient, Report, bytes], range_value: str) -> None:
    client, report, content = workspace
    response = client.get(f"/reports/{report.run_id}/views/{report.views[0].sha256}", headers={"Range": range_value})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(content)}"
