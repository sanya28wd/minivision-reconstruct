"""Tests for Parties table rendering in the Report (RPT-15).

Constraints enforced:
- Parties table shows each Party by its description (colour + make/model + body type).
- Never raw IDs like 'Vehicle 1'.
- Colour chip plus the text label (never colour alone, for accessibility).
- Model text is escaped.
- Existing UI shell and CSS tokens are used.
"""

from __future__ import annotations

import re
import uuid

import pytest

from incident_report.domain import Report, ReportView, Role
from incident_report.facts import CitedFact
from incident_report.rules import FaultStatus, PartyFault
from incident_report.web.templating import build_environment

_FACT = CitedFact(description="contact at the junction", view_id="ext-1", timestamp_seconds=4.0, confidence=0.9)


@pytest.fixture
def dummy_views() -> tuple[ReportView, ...]:
    return (
        ReportView(
            filename="front.mp4",
            role=Role.EXTERIOR,
            label="Front Dashcam",
            acquisition_method="DVR export",
            sha256="a" * 64,
        ),
    )


def test_parties_table_renders_colour_chip_and_text_label(dummy_views: tuple[ReportView, ...]) -> None:
    env = build_environment()
    template = env.get_template("report.html")

    parties = (
        PartyFault(
            cited_facts=(_FACT,),
            party_description="silver Toyota Camry sedan",
            status=FaultStatus.NOT_AT_FAULT,
            color="green",
            rule_id="decree-law-14-2024-art-6-main-road",
            legal_source="Federal Decree-Law No. 14 of 2024",
            article="Art. 6",
            reason="Main road traffic has priority.",
        ),
        PartyFault(
            cited_facts=(_FACT,),
            party_description="blue Nissan Patrol SUV",
            status=FaultStatus.AT_FAULT,
            color="pink",
            rule_id="decree-law-14-2024-art-6-main-road",
            legal_source="Federal Decree-Law No. 14 of 2024",
            article="Art. 6",
            reason="Failed to yield entering from secondary road.",
        ),
        PartyFault(
            cited_facts=(_FACT,),
            party_description="white delivery motorcycle",
            status=FaultStatus.CANNOT_DETERMINE,
            color="grey",
            rule_id=None,
            legal_source=None,
            article=None,
            reason="Insufficient facts to determine right of way.",
        ),
    )

    report = Report(
        run_id=uuid.uuid4(),
        input_hash="b" * 64,
        prompt_version="skeleton-1",
        provenance="synthetic",
        summary="A collision between two vehicles.",
        views=dummy_views,
        parties=parties,
    )

    html = template.render(report=report)

    # 1. Party descriptions appear
    assert "silver Toyota Camry sedan" in html
    assert "blue Nissan Patrol SUV" in html
    assert "white delivery motorcycle" in html

    # 2. Chips appear for each color
    assert re.search(r'class="[^"]*chip--green[^"]*"', html)
    assert re.search(r'class="[^"]*chip--pink[^"]*"', html)
    assert re.search(r'class="[^"]*chip--grey[^"]*"', html)

    # 3. Text labels appear alongside the chips (never color alone)
    assert "Not at Fault" in html
    assert "At Fault" in html
    assert "Cannot Determine" in html

    # 4. No raw IDs
    assert not re.search(r"\bVehicle\s*1\b", html, re.I)
    assert not re.search(r"\bcar_0\b", html, re.I)
    assert not re.search(r"\bV1\b", html)


def test_party_fault_refuses_raw_ids() -> None:
    with pytest.raises(ValueError, match="raw ID"):
        PartyFault(
            cited_facts=(_FACT,),
            party_description="Vehicle 1",
            status=FaultStatus.AT_FAULT,
            color="pink",
            reason="Breached rule",
        )

    with pytest.raises(ValueError, match="raw ID"):
        PartyFault(
            cited_facts=(_FACT,),
            party_description="car_0",
            status=FaultStatus.NOT_AT_FAULT,
            color="green",
            reason="Followed rule",
        )


def test_parties_table_escapes_model_text(dummy_views: tuple[ReportView, ...]) -> None:
    env = build_environment()
    template = env.get_template("report.html")

    parties = (
        PartyFault(
            cited_facts=(_FACT,),
            party_description="silver sedan <script>alert('xss')</script>",
            status=FaultStatus.AT_FAULT,
            color="pink",
            reason="Unsafe entry <img src=x onerror=alert(1)>",
        ),
    )

    report = Report(
        run_id=uuid.uuid4(),
        input_hash="c" * 64,
        prompt_version="skeleton-1",
        provenance="synthetic",
        summary="Summary text.",
        views=dummy_views,
        parties=parties,
    )

    html = template.render(report=report)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in html
    assert "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
