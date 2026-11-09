"""Tests for visible marking of invalid Reports and model text escaping (RPT-11)."""

from __future__ import annotations

import uuid
from html.parser import HTMLParser

from incident_report.domain import Report, ReportView, Role
from incident_report.facts import OverviewFacts, PartyFact
from incident_report.web.templating import build_environment


class Page(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[dict[str, str | None]] = []
        self.text_parts: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attrs.append(dict(attrs))

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self.text_parts).split())


def make_report(*, is_valid: bool, summary: str, errors: tuple[str, ...] = ()) -> Report:
    view = ReportView(
        filename="cam_front.mp4",
        role=Role.EXTERIOR,
        label="front dashcam",
        acquisition_method="USB export",
        sha256="aa" * 32,
        duration_seconds=10.0,
    )
    return Report(
        run_id=uuid.uuid4(),
        input_hash="bb" * 32,
        prompt_version="overview-1",
        provenance="gemini-api",
        summary=summary,
        views=(view,),
        facts=OverviewFacts(
            behaviour_observations=[],
            location_conditions=[],
            parties=[
                PartyFact(
                    description="silver Toyota sedan",
                    view_id="aa" * 32,
                    timestamp_seconds=2.0,
                    confidence=0.9,
                )
            ],
            events=[],
            candidate_collisions=[],
        ),
        is_valid=is_valid,
        validation_errors=errors,
    )


def test_invalid_report_is_visibly_marked_with_banner_and_errors() -> None:
    env = build_environment()
    template = env.get_template("report.html")

    report = make_report(
        is_valid=False,
        summary="Car collision at junction",
        errors=("party[0]: cited view_id not found", "event[0]: timestamp 25.0s exceeds duration 10.0s"),
    )

    rendered = template.render(report=report)
    page = Page(rendered)

    # Must be visibly marked as invalid
    assert 'data-report-validity="invalid"' in rendered
    assert "INVALID REPORT" in page.text
    assert "party[0]: cited view_id not found" in page.text
    assert "event[0]: timestamp 25.0s exceeds duration 10.0s" in page.text


def test_valid_report_does_not_display_invalid_marking() -> None:
    env = build_environment()
    template = env.get_template("report.html")

    report = make_report(is_valid=True, summary="Normal clean report", errors=())
    rendered = template.render(report=report)
    page = Page(rendered)

    assert 'data-report-validity="invalid"' not in rendered
    assert "INVALID REPORT" not in page.text


def test_model_text_is_always_escaped_on_invalid_report() -> None:
    env = build_environment()
    template = env.get_template("report.html")

    evil_summary = "<script>alert('pwned')</script><b>bold</b>"
    evil_error = "<img src=x onerror=alert('err')>"

    report = make_report(
        is_valid=False,
        summary=evil_summary,
        errors=(evil_error,),
    )

    rendered = template.render(report=report)
    page = Page(rendered)

    # No unescaped executable script or img tags
    assert page.tags.count("script") == 0
    assert page.tags.count("img") == 0

    # Escaped entities present in HTML source
    assert "&lt;script&gt;alert(&#39;pwned&#39;)&lt;/script&gt;" in rendered
    assert "&lt;img src=x onerror=alert(&#39;err&#39;)&gt;" in rendered
