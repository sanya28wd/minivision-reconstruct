"""RPT-28 report display contract through the shared template seam."""

import re
from pathlib import Path
from typing import cast

import pytest

from css_tools import contrast_ratio, resolve, tokens
from incident_report.domain import Report
from incident_report.facts import CitedFact, StructuredFact, StructuredField
from incident_report.rules.models import FaultStatus, PartyFault
from incident_report.web.templating import build_environment
from test_invalid_report_rendering import make_report
from test_report_parity import full_report
from test_review_workspace_r2 import Markup


@pytest.mark.parametrize("review", [False, True])
def test_report_sections_are_fixed_even_without_findings(review: bool) -> None:
    report = make_report(is_valid=True, summary="Synthetic report")
    assert report.facts is not None
    report = report.model_copy(
        update={
            "facts": report.facts.model_copy(
                update={
                    "structured_facts": [
                        StructuredFact(
                            subject=subject,
                            field=cast(StructuredField, field),
                            value=value,
                            other_party="",
                            view_id=report.views[0].sha256,
                            timestamp_seconds=2.0,
                            confidence=0.9,
                        )
                        for subject, field, value in [
                            ("scene", "officer_directing_traffic", "no"),
                            ("silver Toyota sedan", "party_type", "car"),
                            ("silver Toyota sedan", "movement", "straight"),
                            ("silver Toyota sedan", "approach_control_faced", "signal"),
                            ("silver Toyota sedan", "signal_state_when_crossing_stop_line", "green"),
                        ]
                    ]
                }
            )
        }
    )
    html = build_environment().get_template("report.html").render(report=report, review_workspace=review)
    assert re.findall(r'data-report-section="([^"]+)"', html) == [
        "summary",
        "storyline",
        "collisions",
        "parties",
        "violations",
        "timeline",
        "behaviour",
        "location",
        "uncertainty",
        "review",
    ]
    if not review:
        assert "<script" not in html
    assert "No Incident (checked: collision, red-signal crossing)" in html


def test_same_report_can_render_through_an_independent_minimal_template() -> None:
    report = make_report(is_valid=True, summary="Another template, same data")
    env = build_environment()
    assert env.from_string("{{ report.summary }} / {{ report.views[0].filename }}").render(report=report) == (
        "Another template, same data / cam_front.mp4"
    )


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("same_view", [False, True])
def test_changed_and_contradictory_readings_retain_both_citations_and_bidi(review: bool, same_view: bool) -> None:
    report = make_report(is_valid=True, summary="Synthetic changed readings")
    assert report.facts is not None
    party = report.facts.parties[0].model_copy(update={"description": "فضي Toyota sedan <img src=x>"})
    other = party.model_copy(update={"description": "أبيض Nissan sedan <script>x</script>"})
    views = report.views + (report.views[0].model_copy(update={"sha256": "cc" * 32}),)
    readings = [
        StructuredFact(
            subject=party.description,
            field="relative_position_of_other",
            value=value,
            other_party=other.description,
            view_id=views[0 if same_view else index].sha256,
            timestamp_seconds=float(index + 2),
            confidence=0.9,
        )
        for index, value in enumerate(("B_from_A_left", "B_from_A_right"))
    ]
    facts = report.facts.model_copy(update={"parties": [party, other], "structured_facts": readings})
    report = report.model_copy(update={"views": views, "facts": facts})
    html = build_environment().get_template("report.html").render(report=report, review_workspace=review)
    fragment = html.split('data-report-section="uncertainty"', 1)[1].split('data-report-section="review"', 1)[0]
    assert ("This view changed its reading" if same_view else "These views disagree") in fragment
    if not same_view:
        assert '<div class="alert" data-contradictions-box>' in fragment
    citations = [(tag, attrs) for tag, attrs in Markup(fragment).tags if attrs.get("class") == "citation"]
    assert len(citations) == 2
    assert [tag for tag, _ in citations] == ["button" if review else "span"] * 2
    if review:
        assert [(attrs["data-view-id"], attrs["data-seconds"]) for _, attrs in citations] == [
            (reading.view_id, str(reading.timestamp_seconds)) for reading in readings
        ]
        assert all(
            attrs.get("type") == "button" and "disabled" not in attrs and "tabindex" not in attrs
            for _, attrs in citations
        )
    assert '<bdi dir="auto">فضي Toyota sedan &lt;img src=x&gt;</bdi>' in fragment
    assert '<bdi dir="auto">أبيض Nissan sedan &lt;script&gt;x&lt;/script&gt;</bdi>' in fragment
    assert "<img src=x>" not in html and "<script>x</script>" not in html


@pytest.mark.parametrize(
    "status,color",
    [(FaultStatus.AT_FAULT, "pink"), (FaultStatus.NOT_AT_FAULT, "green"), (FaultStatus.CANNOT_DETERMINE, "grey")],
)
def test_fault_chips_always_have_visible_text_with_aa_contrast(status: FaultStatus, color: str) -> None:
    report = full_report()
    party = PartyFault(
        party_description="silver Toyota sedan",
        status=status,
        color=color,
        reason="Synthetic reason",
        cited_facts=report.parties[0].cited_facts,
    )
    html = (
        build_environment().get_template("report.html").render(report=report.model_copy(update={"parties": (party,)}))
    )
    assert f'class="chip chip--{color}" aria-hidden="true"' in html
    assert f'<span class="fault-status-text">{party.display_label}</span>' in html
    table = tokens()
    # Labels sit beside decorative chips, on the page surface, rather than on pink/green.
    for background in ("color-bg", "color-surface"):
        assert contrast_ratio(resolve("color-text", table), resolve(background, table)) >= 4.5
    assert contrast_ratio(resolve("color-badge-amber-text", table), resolve("color-badge-amber-bg", table)) >= 4.5


def test_location_text_and_provisional_fines_are_visible_and_escaped() -> None:
    report = full_report()
    assert report.facts is not None
    reading = report.facts.text_readings[0].model_copy(update={"text": "شارع <img src=x> ABC 12"})
    facts = report.facts.model_copy(update={"text_readings": [reading]})
    html = build_environment().get_template("report.html").render(report=report.model_copy(update={"facts": facts}))
    assert '<bdi dir="auto">شارع &lt;img src=x&gt; ABC 12</bdi>' in html
    assert "camera overlay, unverified" in html
    assert '<span class="badge badge--provisional">Provisional</span>' in html
    assert "MR 178/2017 as amended, item fixture" in html
    assert "Synthetic notes" in html and "Check" in html


def test_analysis_modules_do_not_import_report_templates() -> None:
    root = Path(__file__).parents[1] / "src" / "incident_report"
    for path in root.rglob("*.py"):
        if "web" not in path.relative_to(root).parts:
            source = path.read_text(encoding="utf-8")
            assert "web.templating" not in source and "report.html" not in source, path


def _storyline_report(text: str) -> Report:
    report = make_report(is_valid=True, summary="Collision: x at 4.0 s.")
    assert report.facts is not None
    view = report.views[0].sha256
    event = CitedFact(description=text, view_id=view, timestamp_seconds=3.0, confidence=0.9)
    return report.model_copy(update={"facts": report.facts.model_copy(update={"events": [event]})})


@pytest.mark.parametrize("review", [False, True])
def test_what_happened_lists_the_cited_storyline_and_escapes_model_text(review: bool) -> None:
    report = _storyline_report("silver Toyota sedan <img src=x onerror=alert(1)> turns")
    html = build_environment().get_template("report.html").render(report=report, review_workspace=review)
    section = html.split('data-report-section="storyline"', 1)[1].split("data-report-section=", 1)[0]
    assert "<ol" in section and "<img" not in section
    assert "&lt;img src=x onerror=alert(1)&gt;" in section
    assert "@ 3.0s" in section
    if review:
        assert 'data-seconds="3.0"' in section


def test_old_report_without_events_says_no_timeline_events_recorded() -> None:
    report = make_report(is_valid=True, summary="Overview facts extracted from one view.")
    html = build_environment().get_template("report.html").render(report=report)
    section = html.split('data-report-section="storyline"', 1)[1].split("data-report-section=", 1)[0]
    assert "No timeline events recorded" in section
    assert "Overview facts extracted from one view." in html


def test_report_without_facts_renders_an_empty_storyline() -> None:
    report = make_report(is_valid=False, summary="s").model_copy(update={"facts": None})
    html = build_environment().get_template("report.html").render(report=report)
    assert "No timeline events recorded" in html
