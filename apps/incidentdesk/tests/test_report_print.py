"""RPT-39 print / PDF route and layout tests."""

import io
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from access_client import authorized_client
from css_tools import STATIC_DIR
from incident_report.domain import Origin, Report, ReportView, Role, Run, RunStatus
from incident_report.facts import CitedFact, OverviewFacts, PartyFact, StructuredFact
from incident_report.hashing import sha256_hex
from incident_report.rules.models import FaultStatus, PartyFault, PartyViolation
from incident_report.runlog import RunLog
from incident_report.storage.local import LocalEvidenceStore
from incident_report.web import create_app
from test_review_workspace_r2 import Markup
from test_ui_accessibility import physical_direction_problems

PRINT_CSS = STATIC_DIR / "css" / "print.css"


@pytest.fixture
def workspace(run_log: RunLog, tmp_path: Path) -> tuple[TestClient, Report]:
    store = LocalEvidenceStore(tmp_path / "media")
    content = b"\x00\x00\x00\x18ftypisom" + bytes(range(256))
    key = sha256_hex(content)
    store.put(io.BytesIO(content), sha256=key)

    fact = CitedFact(
        description="لوحة ABC 123 <script>alert(1)</script>",
        view_id=key,
        timestamp_seconds=2.5,
        confidence=1.0,
    )
    party = PartyFault(
        party_description="silver Toyota sedan <img src=x>",
        status=FaultStatus.AT_FAULT,
        color="pink",
        rule_id="r1",
        legal_source="MR 178/2017",
        article="Article 1",
        reason="Failed to maintain safe distance",
        cited_facts=(fact,),
        agreement_status="flagged",
        agreement_detail="Disagreed",
    )
    violation = PartyViolation(
        party_description=party.party_description,
        rule_id="v1",
        item_number="item-1",
        name_en="Sudden swerving",
        name_ar="الانحراف المفاجئ",
        aed=1000,
        black_points=4,
        impound_days=None,
        source_url="https://example.com/source",
        status="provisional",
        cited_facts=(fact,),
        notes="Provisional finding",
    )
    report = Report(
        run_id=uuid.uuid4(),
        input_hash=key,
        prompt_version="synthetic-print",
        provenance="synthetic",
        summary="Print test incident summary <script>x</script>",
        views=(
            ReportView(
                filename="dashcam.mp4",
                role=Role.EXTERIOR,
                label="كاميرا ABC",
                acquisition_method=None,
                sha256=key,
                duration_seconds=15.0,
            ),
        ),
        facts=OverviewFacts(
            parties=[
                PartyFact(
                    description=party.party_description,
                    view_id=key,
                    timestamp_seconds=2.0,
                    confidence=1.0,
                    color="silver",
                    make_model="Toyota",
                    body_type="sedan",
                )
            ],
            events=[fact],
            candidate_collisions=[fact],
            behaviour_observations=[],
            location_conditions=[],
            structured_facts=[
                StructuredFact(
                    subject=party.party_description,
                    field="movement",
                    value="straight",
                    other_party="",
                    view_id=key,
                    timestamp_seconds=2.5,
                    confidence=1.0,
                )
            ],
        ),
        parties=(party,),
        violations=(violation,),
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
    from incident_report.ai import ReplayAIConnection

    app = create_app(ai=ReplayAIConnection(), run_log=run_log, evidence=store)
    return authorized_client(app, run_log.database_url), report


def test_print_css_conforms_to_specs() -> None:
    assert PRINT_CSS.exists(), f"Print stylesheet missing: {PRINT_CSS}"
    css = PRINT_CSS.read_text(encoding="utf-8")
    assert re.search(r"@page\s*\{[^}]*size:\s*A4", css), "@page { size: A4 } is required in print.css"
    problems = physical_direction_problems(css)
    assert problems == [], f"Physical CSS properties found in print.css: {problems}"


def test_print_route_requires_authenticated_session(workspace: tuple[TestClient, Report]) -> None:
    client, report = workspace
    client.cookies.clear()
    resp = client.get(f"/reports/{report.run_id}/print")
    assert resp.status_code == 403


def test_print_route_non_succeeded_runs_return_404_error_page(
    workspace: tuple[TestClient, Report], run_log: RunLog
) -> None:
    client, _ = workspace
    missing_id = uuid.uuid4()
    resp_missing = client.get(f"/reports/{missing_id}/print")
    assert resp_missing.status_code == 404
    assert "No report with that id." in resp_missing.text
    assert "<!doctype html>" in resp_missing.text

    proc_id = uuid.uuid4()
    run_log.record(
        Run(
            run_id=proc_id,
            origin=Origin.REPLAY,
            created_at=datetime.now(UTC),
            input_hash="11" * 32,
            report=None,
            status=RunStatus.PROCESSING,
            total_latency_ms=0,
        )
    )
    resp_proc = client.get(f"/reports/{proc_id}/print")
    assert resp_proc.status_code == 404
    assert "No report with that id." in resp_proc.text

    fail_id = uuid.uuid4()
    run_log.record(
        Run(
            run_id=fail_id,
            origin=Origin.REPLAY,
            created_at=datetime.now(UTC),
            input_hash="22" * 32,
            report=None,
            status=RunStatus.FAILED,
            error="Model analysis failed",
            total_latency_ms=0,
        )
    )
    resp_fail = client.get(f"/reports/{fail_id}/print")
    assert resp_fail.status_code == 404
    assert "No report with that id." in resp_fail.text


def test_print_route_succeeded_run_renders_print_page(workspace: tuple[TestClient, Report]) -> None:
    client, report = workspace
    resp = client.get(f"/reports/{report.run_id}/print")
    assert resp.status_code == 200
    html = resp.text

    markup = Markup(html)
    tags = [tag for tag, _ in markup.tags]

    # No buttons, videos, or scripts in print view
    assert "button" not in tags
    assert "video" not in tags
    assert "script" not in tags

    # Links print.css
    link_hrefs = [attrs.get("href") for tag, attrs in markup.tags if tag == "link"]
    assert any(href and "print.css" in href for href in link_hrefs)

    # Citations show as plain text span with timestamp, no seek buttons
    citations = [(tag, attrs) for tag, attrs in markup.tags if attrs.get("class") == "citation"]
    assert len(citations) > 0
    assert all(tag == "span" for tag, _ in citations)
    assert "@ 2.5s" in html

    # Details are open by default for printing
    details_attrs = [attrs for tag, attrs in markup.tags if tag == "details"]
    assert len(details_attrs) > 0
    assert all("open" in attrs for attrs in details_attrs)

    # ADR 0004: Fault text sits next to colour chip
    assert 'class="chip chip--pink"' in html
    assert '<span class="fault-status-text">At Fault</span>' in html

    # Badges are rendered as words
    assert "Check" in html
    assert "Provisional" in html

    # Arabic and untrusted input escaping
    assert "الانحراف المفاجئ" in html
    assert "<script>" not in html
    assert "<img src=x>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;img src=x&gt;" in html

    # Screen-only navigation links are omitted
    assert "Open video review workspace" not in html
    assert "Feedback on this report" not in html
    assert "New incident" not in html
    assert "Skip to main content" not in html


def test_report_screen_view_links_to_print(workspace: tuple[TestClient, Report]) -> None:
    client, report = workspace
    resp = client.get(f"/reports/{report.run_id}")
    assert resp.status_code == 200
    assert f"/reports/{report.run_id}/print" in resp.text
