"""Tests for Violations and fines in the Report (RPT-17 round 2, spec 001 Seam 3, ADR 0006, ADR 0007).

Acceptance criteria enforced:
- Each listed Violation cites its supporting fact(s) and its fines-table row.
- Totals correct across multiple Violations and Parties (table-driven tests).
- Provisional figures visibly tagged.
- No fine without a Violation and no Violation without a citation (test).
- Traceability (ADR 0006): no unsourced figure, threshold or category introduced.
- Round 2 fixes:
  1. RULE_ART_6_ROUNDABOUT -> priority_behind_left (Item 84, MR178).
  2. RULE_ART_7_4_PEDESTRIAN_SPEED -> referral_art_39 (Decree-Law Art. 39, court referral, no AED).
  3. Violations derived from engine decisions: At Fault under rule R gives one Violation;
     Not at Fault and Cannot Determine give none. Situation has no violations field.
  4. No fallback from rule id to fine key: unmapped rule id raises KeyError.
  5. Totals show 'includes N provisional' and 'N decided by court' per Party and incident.
"""

from __future__ import annotations

import re
import uuid

import pytest
from pydantic import ValidationError

from incident_report.domain import Report, ReportView, Role
from incident_report.facts import CitedFact, OverviewFacts, PartyFact
from incident_report.rules import (
    RULE_ART_5_1_F_CROSSWALK,
    RULE_ART_6_EMERGENCY,
    RULE_ART_6_MAIN_ROAD,
    RULE_ART_6_ROUNDABOUT,
    RULE_ART_7_4_PEDESTRIAN_SPEED,
    FaultStatus,
    Observed,
    PartySituation,
    Situation,
    evaluate_fault,
    evaluate_situation,
)
from incident_report.rules.models import (
    PartyViolation,
    Violation,
)
from incident_report.rules.mr130_1997 import RULE_ENTRY
from incident_report.rules.violations import (
    evaluate_violations,
    map_violation,
    summarize_violations,
    violations_from_faults,
)
from incident_report.web.templating import build_environment

CAMRY = "silver Toyota Camry sedan"
PATROL = "blue Nissan Patrol SUV"
WALKER = "pedestrian in dark clothes"

FACT_CROSSWALK = CitedFact(
    description="pedestrian started crossing at designated crosswalk",
    view_id="view-front",
    timestamp_seconds=2.0,
    confidence=0.95,
)
FACT_MAIN_ROAD = CitedFact(
    description="Camry travelling on main road",
    view_id="view-front",
    timestamp_seconds=1.5,
    confidence=0.96,
)
FACT_SECONDARY = CitedFact(
    description="Patrol entering from secondary road",
    view_id="view-side",
    timestamp_seconds=1.6,
    confidence=0.94,
)
FACT_COLLISION = CitedFact(
    description="collision at junction",
    view_id="view-front",
    timestamp_seconds=3.2,
    confidence=0.99,
)
FACT_EMERGENCY = CitedFact(
    description="ambulance with active sirens and lights",
    view_id="view-front",
    timestamp_seconds=1.0,
    confidence=0.97,
)
FACT_SPEED_80 = CitedFact(
    description="posted speed limit 100 km/h sign",
    view_id="view-front",
    timestamp_seconds=0.5,
    confidence=0.98,
)


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


# --- Finding 1: Roundabout maps to priority_behind_left (Item 84) ---
def test_roundabout_rule_maps_to_priority_behind_left() -> None:
    """Finding 1: RULE_ART_6_ROUNDABOUT maps to priority_behind_left (Item 84, not Item 53)."""
    v = Violation(
        rule_id=RULE_ART_6_ROUNDABOUT.rule_id,
        party_description=PATROL,
        cited_facts=(FACT_COLLISION,),
    )
    mapped = map_violation(v)
    assert mapped.item_number == "84"
    assert mapped.name_en == "Not giving priority to vehicles from behind / left"
    assert mapped.aed == 400
    assert mapped.black_points is None
    assert mapped.impound_days is None
    assert mapped.status == "verified"
    assert mapped.is_provisional is False


def test_main_road_rule_stays_failure_to_yield() -> None:
    """Finding 1: Main road entering violation stays failure_to_yield (Item 53)."""
    v = Violation(
        rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
        party_description=PATROL,
        cited_facts=(FACT_COLLISION,),
    )
    mapped = map_violation(v)
    assert mapped.item_number == "53"
    assert mapped.name_en == "Entering road without ensuring clear way (failure to yield)"
    assert mapped.aed == 400
    assert mapped.black_points == 4
    assert mapped.status == "verified"


# --- Finding 2: Art. 7(4) maps to referral_art_39 ---
def test_pedestrian_speed_rule_maps_to_referral_art_39() -> None:
    """Finding 2: RULE_ART_7_4_PEDESTRIAN_SPEED maps to referral_art_39 (court referral, no AED)."""
    v = Violation(
        rule_id=RULE_ART_7_4_PEDESTRIAN_SPEED.rule_id,
        party_description=WALKER,
        cited_facts=(FACT_COLLISION, FACT_SPEED_80),
    )
    mapped = map_violation(v)
    assert mapped.item_number == "39"
    assert "Crossing outside designated places" in mapped.name_en
    assert mapped.aed is None  # criminal court referral, no schedule AED amount
    assert mapped.black_points is None
    assert mapped.impound_days is None
    assert mapped.status == "verified"
    assert mapped.source_url == "https://uaelegislation.gov.ae/en/legislations/2598"


def test_crosswalk_rule_maps_to_pedestrian_priority() -> None:
    """Art. 5(1)(f) maps to pedestrian_priority (Item 69)."""
    v = Violation(
        rule_id=RULE_ART_5_1_F_CROSSWALK.rule_id,
        party_description=CAMRY,
        cited_facts=(FACT_COLLISION, FACT_CROSSWALK),
    )
    mapped = map_violation(v)
    assert mapped.item_number == "69"
    assert mapped.name_en == "Not giving priority to pedestrians at crossings"
    assert mapped.aed == 500
    assert mapped.black_points == 6
    assert mapped.status == "verified"


def test_emergency_rule_maps_to_emergency_priority() -> None:
    """Art. 6 emergency maps to emergency_priority (Item 54, provisional)."""
    v = Violation(
        rule_id=RULE_ART_6_EMERGENCY.rule_id,
        party_description=CAMRY,
        cited_facts=(FACT_COLLISION, FACT_EMERGENCY),
    )
    mapped = map_violation(v)
    assert mapped.item_number == "54"
    assert mapped.aed == 3000
    assert mapped.black_points == 6
    assert mapped.impound_days == 30
    assert mapped.status == "provisional"
    assert mapped.is_provisional is True


# --- Finding 3: Violations derived from engine decisions; Situation has no violations ---
def test_situation_and_party_situation_have_no_violations_field() -> None:
    """Finding 3 (ADR 0007): Facts never carry legal conclusions. violations is removed from Situation."""
    assert "violations" not in Situation.model_fields
    assert "violations" not in PartySituation.model_fields

    with pytest.raises(ValidationError):
        Situation(
            parties=(PartySituation(description=CAMRY),),
            violations=(),  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        PartySituation(
            description=CAMRY,
            violations=(),  # type: ignore[call-arg]
        )


def test_violations_derived_from_engine_fault_decisions() -> None:
    """Finding 3: Party At Fault under rule R gives one Violation; Not at Fault / Cannot Determine give none."""

    # Same road-entry scenario, now supplied as visible origin/movement/control facts.
    # The retired main/secondary legal-rank inputs never reach the decision engine.
    def cited(value: object) -> object:
        return Observed(value=value, evidence=(FACT_COLLISION,))

    # model_validate accepts these explicitly typed scalar observations at the test boundary.
    sit = Situation.model_validate(
        {
            "parties": [
                {
                    "description": CAMRY,
                    "party_type": cited("car"),
                    "movement": cited("straight"),
                    "approach_control_faced": cited("none_confirmed"),
                    "entry_origin": cited("public_road_same_road"),
                    "relative_position_of_other": {PATROL: cited("B_from_A_left")},
                },
                {
                    "description": PATROL,
                    "party_type": cited("car"),
                    "movement": cited("entering_from_side_road"),
                    "approach_control_faced": cited("none_confirmed"),
                    "entry_origin": cited("side_road"),
                    "relative_position_of_other": {CAMRY: cited("B_from_A_right")},
                },
            ],
            "collisions": (FACT_COLLISION,),
            "conflict_zone_type": cited("unsignalised_junction"),
            "officer_directing_traffic": cited("no"),
        }
    )
    faults = evaluate_situation(sit)
    assert faults[0].status == FaultStatus.NOT_AT_FAULT
    assert faults[1].status == FaultStatus.AT_FAULT

    violations = violations_from_faults(faults)
    # Exactly one violation for the At Fault party, zero for the Not at Fault party
    assert len(violations) == 1
    assert violations[0].party_description == PATROL
    assert violations[0].rule_id == RULE_ENTRY.rule_id
    assert violations[0].item_number == "53"
    assert violations[0].aed == 400
    assert violations[0].black_points == 4

    # Direct helper evaluate_violations matches
    assert evaluate_violations(sit) == violations


def test_cannot_determine_situation_produces_zero_violations() -> None:
    """Finding 3: Cannot Determine produces no violations."""
    # Unknown junction control
    sit = Situation(
        parties=(
            PartySituation(description=CAMRY),
            PartySituation(description=PATROL),
        ),
        collisions=(FACT_COLLISION,),
    )
    faults = evaluate_situation(sit)
    assert all(f.status == FaultStatus.CANNOT_DETERMINE for f in faults)
    assert violations_from_faults(faults) == ()
    assert evaluate_violations(sit) == ()


def test_live_overview_adapter_yields_zero_violations() -> None:
    """Today's live overview adapter produces zero violations because all parties are Cannot Determine."""
    overview = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(description=CAMRY, view_id="view-front", timestamp_seconds=1.0, confidence=0.9),
            PartyFact(description=PATROL, view_id="view-front", timestamp_seconds=1.0, confidence=0.9),
        ],
        events=[],
        candidate_collisions=[FACT_COLLISION],
    )
    faults = evaluate_fault(overview)
    violations = violations_from_faults(faults)
    assert violations == ()


# --- Finding 4: No fallback from rule id to fine key ---
def test_unmapped_rule_id_raises_error() -> None:
    """Finding 4: An unmapped rule id raises KeyError; no silent fallback to fine key."""
    # Passing a fine key directly as a rule id is rejected
    with pytest.raises(KeyError, match="Unmapped rule id"):
        map_violation(
            Violation(
                rule_id="seatbelt",
                party_description=CAMRY,
                cited_facts=(FACT_COLLISION,),
            )
        )

    with pytest.raises(KeyError, match="Unmapped rule id"):
        map_violation(
            Violation(
                rule_id="red_light_light",
                party_description=CAMRY,
                cited_facts=(FACT_COLLISION,),
            )
        )

    with pytest.raises(KeyError, match="Unmapped rule id"):
        map_violation(
            Violation(
                rule_id="unknown_rule_123",
                party_description=CAMRY,
                cited_facts=(FACT_COLLISION,),
            )
        )


# --- Citation & Raw ID validation ---
def test_no_violation_without_citation() -> None:
    """ADR 0006: A violation must cite at least one fact (view + timestamp)."""
    with pytest.raises(ValueError, match="at least one fact"):
        Violation(
            rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
            party_description=CAMRY,
            cited_facts=(),
        )

    with pytest.raises(ValueError, match="at least one fact"):
        PartyViolation(
            party_description=CAMRY,
            rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
            item_number="53",
            name_en="Failure to yield",
            source_url="https://uaelegislation.gov.ae/en/legislations/2598",
            status="verified",
            cited_facts=(),
        )


@pytest.mark.parametrize("raw_id", ["Vehicle 1", "car_0", "V1", "vehicle_2", "party 1", "p_1"])
def test_violation_refuses_raw_ids(raw_id: str) -> None:
    """Party description must be colour + make/model + body type; raw IDs are rejected."""
    with pytest.raises(ValueError, match="raw ID"):
        Violation(
            rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
            party_description=raw_id,
            cited_facts=(FACT_COLLISION,),
        )

    with pytest.raises(ValueError, match="raw ID"):
        PartyViolation(
            party_description=raw_id,
            rule_id=RULE_ART_6_MAIN_ROAD.rule_id,
            item_number="53",
            name_en="Failure to yield",
            source_url="https://uaelegislation.gov.ae/en/legislations/2598",
            status="verified",
            cited_facts=(FACT_COLLISION,),
        )


# --- Finding 5: Table-driven totals and HTML rendering notes ---
@pytest.mark.parametrize(
    (
        "party_a_rules",
        "party_b_rules",
        "expected_a_aed",
        "expected_a_pts",
        "expected_b_aed",
        "expected_b_pts",
        "exp_total_aed",
        "exp_total_pts",
        "exp_prov_count",
        "exp_court_count",
    ),
    [
        # Single party, main road rule (verified: 400 AED, 4 pts)
        ([RULE_ART_6_MAIN_ROAD.rule_id], [], 400, 4, 0, 0, 400, 4, 0, 0),
        # Emergency (provisional: 3000 AED, 6 pts) + crosswalk (verified: 500 AED, 6 pts)
        (
            [RULE_ART_6_EMERGENCY.rule_id],
            [RULE_ART_5_1_F_CROSSWALK.rule_id],
            3000,
            6,
            500,
            6,
            3500,
            12,
            1,  # 1 provisional
            0,
        ),
        # Court referral (pedestrian speed: aed=None, pts=None)
        (
            [RULE_ART_7_4_PEDESTRIAN_SPEED.rule_id],
            [RULE_ART_6_ROUNDABOUT.rule_id],
            0,
            0,
            400,
            0,
            400,
            0,
            0,
            1,  # 1 decided by court
        ),
        # Empty
        ([], [], 0, 0, 0, 0, 0, 0, 0, 0),
    ],
)
def test_table_driven_totals_across_violations_and_parties(
    party_a_rules: list[str],
    party_b_rules: list[str],
    expected_a_aed: int,
    expected_a_pts: int,
    expected_b_aed: int,
    expected_b_pts: int,
    exp_total_aed: int,
    exp_total_pts: int,
    exp_prov_count: int,
    exp_court_count: int,
) -> None:
    """Totals correct across multiple Violations and Parties (table-driven tests)."""
    violations: list[PartyViolation] = []
    for r in party_a_rules:
        v = Violation(rule_id=r, party_description=CAMRY, cited_facts=(FACT_COLLISION,))
        violations.append(map_violation(v))
    for r in party_b_rules:
        v = Violation(rule_id=r, party_description=PATROL, cited_facts=(FACT_COLLISION,))
        violations.append(map_violation(v))

    summary = summarize_violations(violations)

    if party_a_rules:
        sum_a = next(p for p in summary.parties if p.party_description == CAMRY)
        assert sum_a.total_aed == expected_a_aed
        assert sum_a.total_black_points == expected_a_pts
    if party_b_rules:
        sum_b = next(p for p in summary.parties if p.party_description == PATROL)
        assert sum_b.total_aed == expected_b_aed
        assert sum_b.total_black_points == expected_b_pts

    assert summary.total_aed == exp_total_aed
    assert summary.total_black_points == exp_total_pts
    assert summary.totals.provisional_count == exp_prov_count
    assert summary.totals.court_decided_count == exp_court_count


def test_report_html_renders_provisional_and_court_decided_totals_notes(
    dummy_views: tuple[ReportView, ...],
) -> None:
    """Finding 5: Totals state 'includes N provisional' and 'N decided by court' per Party and incident."""
    env = build_environment()
    template = env.get_template("report.html")

    violations = (
        # Provisional violation
        map_violation(
            Violation(rule_id=RULE_ART_6_EMERGENCY.rule_id, party_description=CAMRY, cited_facts=(FACT_COLLISION,))
        ),
        # RPT-27: official court-decided administrative row; criminal referrals render separately.
        map_violation(
            Violation(
                rule_id="court-decided-fixture",
                party_description=WALKER,
                cited_facts=(FACT_COLLISION,),
            ),
            rule_mapping={"court-decided-fixture": "alcohol"},
        ),
    )

    report = Report(
        run_id=uuid.uuid4(),
        input_hash="e" * 64,
        prompt_version="skeleton-1",
        provenance="synthetic",
        summary="A multi-party collision.",
        views=dummy_views,
        parties=(),
        violations=violations,
    )

    html = template.render(report=report)

    # 1. Section rendered
    assert "Violations and fines" in html

    # 2. Provisional tag on emergency row
    assert re.search(r'class="[^"]*badge--provisional[^"]*"', html)
    assert "Provisional" in html

    # 3. Court decided rendered on walker row
    assert "Decided by court" in html

    # 4. Finding 5: Party total notes
    # Camry party total includes 1 provisional
    assert "includes 1 provisional" in html

    # Walker party total states 1 decided by court
    assert "1 decided by court" in html

    # 5. Finding 5: Incident total notes
    assert "Incident Total:" in html
    # Incident has both 1 provisional and 1 court-decided
    assert re.search(r"Incident Total:.*?includes 1 provisional.*?1 decided by court", html, re.DOTALL)


def test_violations_table_escapes_model_text(dummy_views: tuple[ReportView, ...]) -> None:
    """Model text in violation descriptions is properly escaped against XSS attacks."""
    env = build_environment()
    template = env.get_template("report.html")

    xss_party = "silver sedan <script>alert('xss')</script>"
    v = map_violation(
        Violation(rule_id=RULE_ART_6_MAIN_ROAD.rule_id, party_description=xss_party, cited_facts=(FACT_COLLISION,))
    )

    report = Report(
        run_id=uuid.uuid4(),
        input_hash="f" * 64,
        prompt_version="skeleton-1",
        provenance="synthetic",
        summary="Summary text.",
        views=dummy_views,
        parties=(),
        violations=(v,),
    )

    html = template.render(report=report)
    assert "<script>" not in html
    assert "&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in html
