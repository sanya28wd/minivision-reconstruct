"""RPT-56: police White and evidence abstention are distinct fault states."""

import pytest
from pydantic import ValidationError

from css_tools import APP_CSS, STATIC_DIR, contrast_ratio, parse_css, resolve, tokens
from incident_report.rules.models import FaultStatus, PartyFault
from incident_report.sample_reports import load_samples
from incident_report.web.templating import build_environment
from test_report_parity import full_report


def test_evidence_abstention_is_grey_and_has_report_owned_label() -> None:
    party = PartyFault(
        party_description="silver Toyota sedan",
        status=FaultStatus.CANNOT_DETERMINE,
        color="grey",
        reason="Evidence is insufficient.",
        cited_facts=(),
    )
    assert party.display_label == "Not determined by this report"
    assert party.model_dump(mode="json")["status"] == "Cannot Determine"


def test_white_requires_cited_facts_and_f1_rule() -> None:
    data = dict(
        party_description="silver Toyota sedan",
        status="White",
        color="white",
        reason="Synthetic example only: the other party is unidentified.",
        cited_facts=full_report().parties[0].cited_facts,
        rule_id="F1",
        legal_source="RTA Light Motor Vehicle Handbook",
        article="Collision responsibilities, Step 3",
    )
    party = PartyFault.model_validate(data)
    assert party.status == FaultStatus.WHITE
    assert party.display_label == "White"
    for update in ({"cited_facts": ()}, {"rule_id": None}, {"legal_source": None}, {"article": None}):
        with pytest.raises(ValidationError):
            PartyFault.model_validate(data | update)


def test_old_report_abstention_is_read_as_grey_without_becoming_white() -> None:
    party = PartyFault.model_validate_json(
        '{"party_description":"silver Toyota sedan","status":"Cannot Determine",'
        '"color":"white","reason":"Legacy evidence gap","cited_facts":[]}'
    )
    assert party.status == FaultStatus.CANNOT_DETERMINE
    assert party.color == "grey"
    assert party.display_label == "Not determined by this report"


@pytest.mark.parametrize("template", ["report.html", "report_print.html"])
@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize(
    "status,color,label",
    [
        ("At Fault", "pink", "At Fault"),
        ("Not at Fault", "green", "Not at Fault"),
        ("White", "white", "White"),
        ("Cannot Determine", "grey", "Not determined by this report"),
    ],
)
def test_four_status_chips_and_labels_in_shared_and_print_reports(
    template: str, review: bool, status: str, color: str, label: str
) -> None:
    report = full_report()
    party = PartyFault(
        party_description="silver Toyota sedan",
        status=FaultStatus(status),
        color=color,
        reason="Synthetic presentation fixture only.",
        cited_facts=report.parties[0].cited_facts,
        rule_id="F1" if status == "White" else None,
        legal_source="RTA Light Motor Vehicle Handbook" if status == "White" else None,
        article="Collision responsibilities, Step 3" if status == "White" else None,
    )
    html = (
        build_environment()
        .get_template(template)
        .render(report=report.model_copy(update={"parties": (party,)}), review_workspace=review)
    )
    assert (
        f'<span class="chip chip--{color}" aria-hidden="true"></span>\n        <span class="fault-status-text">{label}</span>'
        in html
    )
    legend = html.split('aria-label="Fault status legend"', 1)[1].split("</aside>", 1)[0]
    for chip, text in [
        ("pink", "At Fault"),
        ("green", "Not at Fault"),
        ("white", "White"),
        ("grey", "Not determined by this report"),
    ]:
        assert f"chip--{chip}" in legend and text in legend
    assert "neither party is accused" in legend and "other party is unknown" in legend
    assert "Grey is this report's evidence abstention" in legend
    assert "F1" in legend


def test_grey_chip_contrast_and_print_preserve_distinction() -> None:
    table = tokens()
    grey = resolve("color-fault-grey", table)
    assert contrast_ratio(grey, "#ffffff") >= 3
    assert contrast_ratio(resolve("color-text", table), "#ffffff") >= 4.5
    app_rules = parse_css(APP_CSS.read_text(encoding="utf-8"))
    assert any(r.selector == ".chip--grey" and r.get("background") == "var(--color-fault-grey)" for r in app_rules)
    print_rules = parse_css((STATIC_DIR / "css" / "print.css").read_text(encoding="utf-8"))
    assert any(r.selector == ".chip" and r.get("print-color-adjust") == "exact" for r in print_rules)


def test_recorded_samples_keep_unsupported_white_as_grey_abstention() -> None:
    samples = load_samples()
    assert samples
    abstentions = []
    for sample in samples:
        html = build_environment().get_template("report.html").render(report=sample.report)
        for party in sample.report.parties:
            assert party.status != FaultStatus.WHITE
            if party.status == FaultStatus.CANNOT_DETERMINE:
                abstentions.append(party)
                assert party.color == "grey"
                assert '<span class="fault-status-text">Not determined by this report</span>' in html
    assert abstentions


def test_officer_cannot_select_white_without_a_supported_f1_rule() -> None:
    from incident_report.decisions import findings_of

    finding = findings_of(full_report())["party:0"]
    assert "White" not in finding.allowed_values
    assert set(finding.allowed_values) == {"At Fault", "Not at Fault", "Cannot Determine"}


@pytest.mark.parametrize("ai_status", ["At Fault", "Cannot Determine"])
@pytest.mark.parametrize("template", ["report.html", "report_print.html"])
def test_officer_party_abstention_labels_preserve_stored_values(ai_status: str, template: str) -> None:
    import uuid
    from datetime import UTC, datetime

    from incident_report.decisions import OverrideReason, OverrideRecord, ReportState, VersionInfo, findings_of

    report = full_report()
    party = report.parties[0].model_copy(
        update={"status": FaultStatus(ai_status), "color": "pink" if ai_status == "At Fault" else "grey"}
    )
    report = report.model_copy(update={"parties": (party,)})
    now = datetime.now(UTC)
    override = OverrideRecord(
        override_id=uuid.uuid4(),
        version_number=1,
        finding_key="party:0",
        ai_original={"fault_status": ai_status},
        new_value="Cannot Determine",
        reason_code=OverrideReason.OTHER,
        reason_text="Synthetic evidence gap",
        created_at=now,
    )
    decision = ReportState(
        run_id=report.run_id,
        version_number=1,
        versions=(VersionInfo(version_number=1, created_at=now, approved_at=None),),
        overrides={"party:0": override},
    )
    html = (
        build_environment()
        .get_template(template)
        .render(
            report=report,
            review_workspace=True,
            decision=decision,
            findings=findings_of(report),
            override_reasons=(),
            reason_max_chars=100,
            csrf_token=uuid.uuid4().hex,
        )
    )
    assert "Officer override: Not determined by this report</span>" in html
    assert "<strong>Officer override</strong>: Not determined by this report" in html
    expected = "At Fault" if ai_status == "At Fault" else "Not determined by this report"
    assert f'<span class="ai-original">{expected}</span>' in html
    if ai_status == "At Fault" and template == "report.html":
        assert '<option value="Cannot Determine">Not determined by this report</option>' in html
    assert override.new_value == "Cannot Determine"
