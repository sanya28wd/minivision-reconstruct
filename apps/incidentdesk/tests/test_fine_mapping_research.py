"""The checked-in research table is the independent rule-to-schedule oracle."""

import re
from dataclasses import replace
from pathlib import Path

import pytest

from incident_report.fines import FinesTable
from incident_report.rules import evaluate_situation
from incident_report.rules.mr130_1997 import RULE_LANE_CHANGE, RULE_ONCOMING_LEFT_TURN, RULE_SIGNAL
from incident_report.rules.violations import RULE_ID_TO_FINE_KEY, RULE_ID_TO_RESEARCH_ROW, NoFixedItem
from test_core_fault_rules import lane_situation

RESEARCH = Path(__file__).resolve().parents[1] / "docs/research/UAE_ROAD_STRUCTURE_AND_PRIORITY.md"


def research_sources() -> dict[str, str]:
    section = RESEARCH.read_text(encoding="utf-8").split("### 2.2 Rules table", 1)[1].split("\n##", 1)[0]
    lines = [line for line in section.splitlines() if line.startswith("|")]
    headers = [cell.strip() for cell in lines[0].strip("|").split("|")]
    source_column = headers.index("Source and article")
    refs = {}
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == len(headers), f"Malformed research row: {line}"
        ref = cells[0]
        assert re.fullmatch(r"R\d+[a-z]?", ref), f"Unexpected research row id: {ref}"
        assert ref not in refs, f"Duplicate research row: {ref}"
        refs[ref] = cells[source_column]
    assert refs, "Research rules table must not be empty"
    # Ticket 9's owner-mapped turn uses catalogue C1, whose Fine item column
    # explicitly spells out Item 99b. Keep the legacy oracle above unchanged.
    catalogue = RESEARCH.with_name("RTA_RULES_CATALOGUE.md").read_text(encoding="utf-8")
    [turn] = [line for line in catalogue.splitlines() if line.startswith("| C1 |")]
    cells = [cell.strip() for cell in turn.strip("|").split("|")]
    assert len(cells) == 8
    assert "MR 130/1997 Art. 59" in cells[3]
    refs["C1"] = cells[3] + "; " + cells[4]
    return refs


def assert_mapping_supported(rule_id: str, mapping: dict[str, str | NoFixedItem]) -> None:
    reference = RULE_ID_TO_RESEARCH_ROW[rule_id]
    ref = reference.row_id
    rows = research_sources()
    assert ref in rows, f"{rule_id}: research row {ref} does not exist"
    source = rows[ref]
    items = set(re.findall(r"\bItems?\s+(\d+[a-z]?)\b", source))
    value = mapping[rule_id]
    officer_findings = {RULE_LANE_CHANGE.rule_id: evaluate_situation(lane_situation())[0].findings[0]}
    if rule_id in officer_findings:
        assert isinstance(value, NoFixedItem), (
            f"{rule_id}: Finding leaves officer selection open; fixed fine {value!r} is unsupported"
        )
    if isinstance(value, NoFixedItem):
        assert rule_id in officer_findings, f"{rule_id}: no officer-selection Finding fixture"
        finding = officer_findings[rule_id]
        assert finding.rule_id == rule_id
        choices = finding.reason.split("applicable schedule item for the officer to select:", 1)[1].split(
            "; not determined from video", 1
        )[0]
        offered = set(re.findall(r"\b\d+[a-z]?\b", choices))
        assert offered, "Officer choices must not be empty"
        assert offered <= items, (
            f"{rule_id}: {ref} Source and article lacks officer Items {sorted(offered - items)}: {source}"
        )
        return
    table = FinesTable.default()
    referrals = {row.key: row for row in table.referrals}
    if reference.referral_article is not None:
        assert value in referrals, (
            f"{rule_id}: {ref} requires Art. {reference.referral_article} referral, not fixed fine {value!r}"
        )
    if value in referrals:
        article = referrals[value].item_number
        assert reference.referral_article == article, (
            f"{rule_id}: referral article metadata does not match Art. {article}"
        )
        assert re.search(rf"\bArt\.\s*{re.escape(article)}(?![\d.])", source), (
            f"{rule_id}: {ref} Source and article lacks referral Art. {article}: {source}"
        )
    else:
        row = table.get(value)
        components = [item.strip() for item in row.item_number.split(",")]
        if len(components) > 1:
            # Combined rows need all comma-listed source items. Keep the legacy
            # source extraction and support rules unchanged for single-item rows.
            groups = re.findall(r"\bItems?\s+(\d+[a-z]?(?:\s*,\s*\d+[a-z]?)*)\b", source)
            items = {item.strip() for group in groups for item in group.split(",")}
        assert all(re.fullmatch(r"[0-9]+[a-z]?", item) for item in components), (
            f"{rule_id}: malformed schedule item list {row.item_number!r}"
        )
        for item in components:
            supported = item in items
            subitem = re.fullmatch(r"(\d+)[a-z]+", item)
            if not supported and subitem and subitem[1] in items:
                parent = re.search(rf"\bItems?\s+{re.escape(subitem[1])}\b([^;]*)", source)
                assert parent is not None
                context = parent[1].casefold()
                vehicle_class = re.search(r"\b(?:light|heavy) vehicle\b|\bmotorcycle\b", row.en.casefold())
                class_matches = (
                    vehicle_class is not None and re.search(rf"\b{re.escape(vehicle_class[0])}\b", context) is not None
                )
                amounts = {
                    int(amount.replace(",", ""))
                    for amount in re.findall(r"\bAED\s+([\d,]+)(?![\d.,])", parent[1], flags=re.IGNORECASE)
                }
                supported = class_matches or row.aed is not None and row.aed in amounts
            assert supported, (
                f"{rule_id}: {value} is Item {row.item_number}; component {item} absent from {ref} Source and article (Items {sorted(items)}): {source}"
            )


def test_every_mapping_names_a_research_row() -> None:
    assert RULE_ID_TO_FINE_KEY.keys() == RULE_ID_TO_RESEARCH_ROW.keys()


def test_owner_turn_mapping_uses_the_catalogue_and_rejects_an_unlisted_item() -> None:
    assert RULE_ID_TO_RESEARCH_ROW[RULE_ONCOMING_LEFT_TURN.rule_id].row_id == "C1"
    assert_mapping_supported(RULE_ONCOMING_LEFT_TURN.rule_id, RULE_ID_TO_FINE_KEY)
    with pytest.raises(AssertionError):
        assert_mapping_supported(
            RULE_ONCOMING_LEFT_TURN.rule_id, {**RULE_ID_TO_FINE_KEY, RULE_ONCOMING_LEFT_TURN.rule_id: "tailgating"}
        )


@pytest.mark.parametrize("rule_id", RULE_ID_TO_FINE_KEY)
def test_mapping_is_supported_by_its_research_source_column(rule_id: str) -> None:
    assert_mapping_supported(rule_id, RULE_ID_TO_FINE_KEY)


@pytest.mark.parametrize(
    "rule_id,key",
    [
        ("decree-law-14-2024-art-6-roundabout", "failure_to_yield"),
        ("decree-law-14-2024-art-7-4-pedestrian-over-80", "jaywalking"),
        (RULE_LANE_CHANGE.rule_id, "sudden_swerving"),
    ],
)
def test_historical_wrong_mappings_are_rejected(rule_id: str, key: str) -> None:
    bad_mapping = {**RULE_ID_TO_FINE_KEY, rule_id: key}
    with pytest.raises(AssertionError):
        assert_mapping_supported(rule_id, bad_mapping)


@pytest.mark.parametrize(
    "item,vehicle_class,amount,supported",
    [
        ("33a", "light vehicle", 1000, True),
        ("33a", "light vehicle", 9999, True),
        ("33a", "unclassified vehicle", 1000, True),
        ("33b", "heavy vehicle", 3000, False),
        ("33b", "heavy vehicle", 100, False),
        ("34a", "light vehicle", 1000, False),
    ],
)
def test_parent_item_requires_own_vehicle_class_or_amount(
    item: str, vehicle_class: str, amount: int, supported: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Synthetic policy fixtures, not claims about an official schedule sub-item.
    table = FinesTable.default()
    synthetic = replace(
        table.get("red_light_light"),
        key="synthetic_subitem",
        item_number=item,
        en=f"Red light, {vehicle_class}",
        aed=amount,
    )
    fixture_table = replace(table, fines=(*table.fines, synthetic))
    monkeypatch.setattr(FinesTable, "default", lambda: fixture_table)
    mapping = {**RULE_ID_TO_FINE_KEY, RULE_SIGNAL.rule_id: synthetic.key}
    if supported:
        assert_mapping_supported(RULE_SIGNAL.rule_id, mapping)
    else:
        with pytest.raises(AssertionError):
            assert_mapping_supported(RULE_SIGNAL.rule_id, mapping)


@pytest.mark.parametrize(
    "item_number,source,supported",
    [
        ("32a, 32b", "Item 32a, 32b (mobile phone, AED 800, 4 points)", True),
        ("32a, 32b", "Item 32a; Item 32b (mobile phone, AED 800, 4 points)", True),
        ("32a, 32b", "Item 32a (mobile phone, AED 800, 4 points)", False),
        ("32a, 32b", "Item 32b (mobile phone, AED 800, 4 points)", False),
        ("32a, 32b", "Item 32 (mobile phone, AED 800, 4 points)", True),
        ("32a, 32b", "Item 32 (mobile phone, AED 9999, 4 points)", False),
        ("32a, 99b", "Item 32 (mobile phone, AED 800, 4 points)", False),
        ("", "Item 32a, 32b (mobile phone, AED 800)", False),
        ("32a,", "Item 32a (mobile phone, AED 800)", False),
        (",32a", "Item 32a (mobile phone, AED 800)", False),
        ("32a,,32b", "Item 32a, 32b (mobile phone, AED 800)", False),
    ],
)
def test_combined_schedule_items_require_every_item_and_reject_empty_entries(
    item_number: str, source: str, supported: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # synthetic-fixture: policy checks, not new official schedule claims.
    from incident_report.rules.driver import PHONE_RULE_ID

    table = FinesTable.default()
    synthetic = replace(table.get("mobile_phone_distraction"), key="synthetic_combined")
    # Deliberately bypass Fine's format validator to test the guard's own rejection.
    object.__setattr__(synthetic, "item_number", item_number)
    fixture_table = replace(table, fines=(*table.fines, synthetic))
    monkeypatch.setattr(FinesTable, "default", lambda: fixture_table)
    monkeypatch.setattr(__name__ + ".research_sources", lambda: {"R18": source})
    mapping = {**RULE_ID_TO_FINE_KEY, PHONE_RULE_ID: synthetic.key}
    if supported:
        assert_mapping_supported(PHONE_RULE_ID, mapping)
    else:
        with pytest.raises(AssertionError):
            assert_mapping_supported(PHONE_RULE_ID, mapping)
