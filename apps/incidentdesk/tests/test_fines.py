"""RPT-08: public fines catalogue seam; expected values come from the research note."""

import json
from pathlib import Path

import pytest

from incident_report.fines import DEFAULT_FILE, Fine, FinesTable, Referral


def test_below_minimum_speed_has_corrected_points() -> None:
    row = FinesTable.default().get("below_minimum_speed")
    assert (row.item_number, row.aed, row.black_points, row.impound_days) == ("28", 400, None, None)


@pytest.mark.parametrize("field", ["source_url", "item_number"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_loader_rejects_unsourced_rows(tmp_path: Path, field: str, value: str | None) -> None:
    raw = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    raw["fines"][0][field] = value
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        FinesTable.from_file(path)


# Nine definitive WRONG entries in section 3; 'likely WRONG' obstruction is excluded.
@pytest.mark.parametrize(
    ("key", "item", "aed", "points", "days"),
    [
        ("below_minimum_speed", "28", 400, None, None),
        ("overtaking_shoulder", "42", 1000, 6, None),
        ("seatbelt", "51a, 51b", 400, 4, None),
        ("child_front_seat", "49", 400, None, None),
        ("minor_hit_run_light", "48", 500, 8, 7),
        ("minor_hit_run_heavy", "48", 1000, 16, None),
        ("foreign_licence", "23", 400, None, None),
        ("noisy_vehicle", "20", 2000, 12, None),
        ("pollution", "21", 1000, 6, None),
    ],
)
def test_nine_corrections(key: str, item: str, aed: int, points: int | None, days: int | None) -> None:
    row = FinesTable.default().get(key)
    assert (row.item_number, row.aed, row.black_points, row.impound_days) == (item, aed, points, days)


ROOT = Path(__file__).resolve().parents[1]
NOTE = (ROOT / "docs/research/UAE_TRAFFIC_FINES_VERIFIED.md").read_text(encoding="utf-8")


def test_referrals_are_separate_and_never_contain_schedule_amounts() -> None:
    table = FinesTable.default()
    assert {row.item_number for row in table.referrals} == {"38", "39", "40"}
    for row in table.referrals:
        assert row.disposition == "Referral to court / prosecution"
        assert row.source_url == "https://uaelegislation.gov.ae/en/legislations/2598"
        assert f"**Art. {row.item_number}**" in NOTE
        assert not hasattr(row, "aed")


def test_arabic_is_explicitly_missing_or_copied_from_glossary() -> None:
    glossary = (ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
    rows: tuple[Fine | Referral, ...] = (*FinesTable.default().fines, *FinesTable.default().referrals)
    for row in rows:
        if row.ar is None:
            assert row.ar_source is None
        else:
            assert row.ar in glossary
            assert row.ar_source and row.ar_source.startswith("GLOSSARY.md")


# Independently read the research Markdown, not the shipped JSON.
def _research_rows() -> dict[str, list[str]]:
    section = NOTE.split("## 2.")[1].split("## 3.")[0]
    return {
        cells[0]: cells
        for line in section.splitlines()
        if line.startswith("| ") and len(cells := [cell.strip() for cell in line.strip("|").split("|")]) == 6
    }


# Complex/combined source cells and section-3-only rows need explicit worked expectations.
SPECIAL = {
    "valley_rain_113": ("113", 1000, 6, None, "113: 1,000 / 6"),
    "valley_rain_114": ("114", 2000, 23, 60, "114: 2,000 / 23 / 60 d"),
    "valley_rain_117": ("117", 1000, 4, 60, "117: 1,000 / 4 / 60 d"),
    "wrong_turn": ("99b", 500, 4, None, "99b wrong turn 500 / 4 pts"),
    "failure_to_signal": ("83", 400, 0, None, "83 no signal 400 / 0"),
    "lane_discipline_light": ("86", 400, 0, None, "86 not in compulsory lane (light) 400 / 0"),
    "prohibited_area": ("19b", 1000, 8, 7, "19b entering prohibited area: 1,000 / 8 / 7 d"),
    "serious_accident": ("47", None, 23, 30, '"Decided by court"'),
    "causing_death": ("46", None, 23, 60, "Causing someone's **death**"),
    "minor_hit_run_light": ("48", 500, 8, 7, "500 / 8 / **7 days** (item 48)"),
    "minor_hit_run_heavy": (
        "48",
        1000,
        16,
        None,
        "1000 / 16 / 7 d stated only for light vehicles; heavy impound not stated",
    ),
    "fleeing_police_light": ("45", 800, 12, 30, "800/12/30; 1000/16/30"),
    "fleeing_police_heavy": ("45", 1000, 16, None, "CONFIRMED light; heavy impound not stated"),
    "emergency_priority": ("54", 3000, 6, 30, "amended 1 July 2019 to 3,000, 6 pts, 30 d impound"),
    "drugs": ("3", None, None, 60, "60 d; licence suspended 1 yr"),
    "foreign_licence": ("23", 400, None, None, "400 / none (item 23)"),
    "noisy_vehicle": ("20", 2000, 12, None, "2000 / 12 / none (item 20)"),
    "pollution": ("21", 1000, 6, None, "1000 / 6 / none (item 21)"),
    "fire_hydrant_disabled_bay": ("55", 1000, 6, None, "Fire hydrant / disabled bay | 1000/6"),
    "jaywalking": ("89", 400, None, None, "Jaywalking (pedestrian) | 400"),
}


@pytest.mark.parametrize("row", FinesTable.default().fines, ids=lambda row: row.key)
def test_every_row_reproduces_the_research_note(row: Fine) -> None:
    if row.key in SPECIAL:
        item, aed, points, days, excerpt = SPECIAL[row.key]
        assert excerpt in NOTE
        assert (row.item_number, row.aed, row.black_points, row.impound_days) == (item, aed, points, days)
    else:
        cells = _research_rows()[row.en]
        assert row.item_number == cells[1]
        aed_text, points_text, days_text = cells[2:5]
        assert row.aed == (None if aed_text == "court" else int(aed_text.replace(",", "")))
        assert row.black_points == (None if points_text == "-" else int(points_text))
        assert row.impound_days == (int(days_text.split()[0]) if days_text[0].isdigit() else None)
    expected_source = "https://assets.u.ae/api/public/content/a6279069a68b41e58c266703a7d519c9?v=48de4313"
    if row.key == "emergency_priority":
        expected_source = "https://gulfnews.com/uae/dh3000-fine-for-impeding-emergency-vehicles-in-uae-1.1560764331464"
    elif row.key.startswith("valley_rain_"):
        expected_source = "https://dnd.com.pk/uae-announces-new-traffic-violations-and-fines/290736/"
    assert row.source_url == expected_source
    assert expected_source in NOTE
    assert row.research_reference.startswith("docs/research/UAE_TRAFFIC_FINES_VERIFIED.md §")
    assert row.status in {"verified", "provisional"}
    if row.key in {
        "seatbelt",
        "against_traffic",
        "prohibited_area",
        "minor_hit_run_light",
        "minor_hit_run_heavy",
        "emergency_priority",
    }:
        assert row.status == "provisional"


def test_unresolved_behaviours_have_no_invented_fine() -> None:
    for key in (
        "drowsiness",
        "unsafe_lane_change",
        "roundabout_priority",
        "dubai_report_fee",
        "dubai_impound_release",
        "obstructing_traffic",
    ):
        with pytest.raises(KeyError):
            FinesTable.default().get(key)


@pytest.mark.parametrize("section", ["fines", "referrals"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "guessed"),
        ("key", ""),
        ("en", ""),
        ("source_url", "not-a-url"),
        ("item_number", "unknown"),
        ("research_reference", ""),
        ("ar", "invented"),
        ("source_url", None),
        ("item_number", None),
    ],
)
def test_invalid_metadata_is_rejected(tmp_path: Path, section: str, field: str, value: object) -> None:
    raw = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    raw[section][0][field] = value
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        FinesTable.from_file(path)


@pytest.mark.parametrize("field", ["aed", "black_points", "impound_days"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "400"])
def test_legal_figures_are_nonnegative_integers_or_explicitly_missing(
    tmp_path: Path, field: str, value: object
) -> None:
    raw = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    raw["fines"][0][field] = value
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        FinesTable.from_file(path)


@pytest.mark.parametrize("section", ["fines", "referrals"])
def test_duplicate_keys_fail_instead_of_silently_shadowing(tmp_path: Path, section: str) -> None:
    raw = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))
    raw[section].append(raw[section][0])
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        FinesTable.from_file(path)


@pytest.mark.parametrize("raw", [{}, [], {"version": "", "fines": [], "referrals": []}])
def test_invalid_config_never_returns_a_partial_table(tmp_path: Path, raw: object) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        FinesTable.from_file(path)


@pytest.mark.parametrize(
    ("key", "item", "aed", "points", "days"),
    [
        ("valley_rain_113", "113", 1000, 6, None),
        ("valley_rain_114", "114", 2000, 23, 60),
        ("valley_rain_117", "117", 1000, 4, 60),
    ],
)
def test_secondary_amendments_are_provisional(key: str, item: str, aed: int, points: int, days: int | None) -> None:
    row = FinesTable.default().get(key)
    assert (row.item_number, row.aed, row.black_points, row.impound_days) == (item, aed, points, days)
    assert row.status == "provisional"
    assert row.source_url == "https://dnd.com.pk/uae-announces-new-traffic-violations-and-fines/290736/"


def test_catalogue_does_not_silently_drop_researched_rows() -> None:
    # Section 2 plus individually identifiable section 3 additions. Ambiguous combined
    # items 82/22/25 and Dubai fees are documented as unresolved in the ticket result.
    assert {row.key for row in FinesTable.default().fines} == {
        "below_minimum_speed",
        "red_light_light",
        "red_light_motorcycle",
        "red_light_heavy",
        "sudden_swerving",
        "wrong_turn",
        "failure_to_signal",
        "lane_discipline_light",
        "tailgating",
        "speeding_over_80",
        "speeding_over_60",
        "speeding_up_to_60",
        "speeding_up_to_50",
        "speeding_up_to_40",
        "speeding_up_to_30",
        "speeding_up_to_20",
        "mobile_phone_distraction",
        "seatbelt",
        "child_front_seat",
        "child_seat",
        "pedestrian_priority",
        "dangerous_road_entry",
        "failure_to_yield",
        "priority_behind_left",
        "dangerous_reversing",
        "overtaking_shoulder",
        "wrong_overtaking",
        "prohibited_overtaking",
        "yellow_box",
        "stop_pedestrian_crossing",
        "stop_middle_road",
        "against_traffic",
        "prohibited_area",
        "dangerous_driving",
        "heavy_dangerous_driving",
        "serious_accident",
        "causing_death",
        "minor_hit_run_light",
        "minor_hit_run_heavy",
        "traffic_signs",
        "breakdown_safety",
        "rubbernecking",
        "traffic_policeman",
        "fleeing_police_light",
        "fleeing_police_heavy",
        "emergency_priority",
        "alcohol",
        "drugs",
        "foreign_licence",
        "noisy_vehicle",
        "pollution",
        "fire_hydrant_disabled_bay",
        "jaywalking",
        "valley_rain_113",
        "valley_rain_114",
        "valley_rain_117",
    }
