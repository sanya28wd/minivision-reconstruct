"""Tests for the L0 citation validator (RPT-11, ADR 0005, ADR 0006).

Checks:
- Rejects unknown views
- Rejects out-of-range timestamps (negative or exceeding view duration)
- Rejects uncited facts (missing view_id or empty description)
- Rejects party descriptions that are raw IDs
- Rejects facts with confidence below the minimum threshold
- Accepts valid facts within duration and with known views
"""

from __future__ import annotations

from incident_report.facts import (
    DEFAULT_MIN_CONFIDENCE,
    CitedFact,
    OverviewFacts,
    PartyFact,
    validate_citations,
)


def make_valid_facts(view_id: str = "view_123") -> OverviewFacts:
    return OverviewFacts(
        parties=[
            PartyFact(
                description="silver Toyota sedan",
                view_id=view_id,
                timestamp_seconds=2.0,
                confidence=0.95,
            )
        ],
        events=[
            CitedFact(
                description="silver Toyota sedan enters roundabout",
                view_id=view_id,
                timestamp_seconds=3.5,
                confidence=0.90,
            )
        ],
        candidate_collisions=[
            CitedFact(
                description="contact between silver Toyota sedan and curb",
                view_id=view_id,
                timestamp_seconds=5.0,
                confidence=0.85,
            )
        ],
        behaviour_observations=[
            CitedFact(
                description="driver head turned towards mounted GPS",
                view_id=view_id,
                timestamp_seconds=1.0,
                confidence=0.80,
            )
        ],
        location_conditions=[
            CitedFact(
                description="signboard shows Al Khail Road, clear daylight",
                view_id=view_id,
                timestamp_seconds=0.5,
                confidence=0.98,
            )
        ],
    )


def test_validator_accepts_valid_cited_facts_within_view_duration() -> None:
    facts = make_valid_facts("view_abc")
    view_durations = {"view_abc": 10.0}

    result = validate_citations(facts, ["view_abc"], view_durations=view_durations)

    assert result.is_valid is True
    assert result.errors == ()


def test_validator_rejects_unknown_view_id() -> None:
    facts = make_valid_facts("unknown_view_xyz")
    known_views = ["view_real_1", "view_real_2"]

    result = validate_citations(facts, known_views)

    assert result.is_valid is False
    assert any("unknown_view_xyz" in err for err in result.errors)
    assert any("view_real_1" in err for err in result.errors)


def test_validator_rejects_negative_timestamp() -> None:
    # Bypassing Pydantic check directly to test validator layer on arbitrary inputs
    bad_fact = object.__new__(CitedFact)
    object.__setattr__(bad_fact, "description", "sudden swerve")
    object.__setattr__(bad_fact, "view_id", "view_abc")
    object.__setattr__(bad_fact, "timestamp_seconds", -1.5)
    object.__setattr__(bad_fact, "confidence", 0.9)

    facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(
                description="white Nissan Patrol SUV",
                view_id="view_abc",
                timestamp_seconds=1.0,
                confidence=0.9,
            )
        ],
        events=[bad_fact],
        candidate_collisions=[],
    )

    result = validate_citations(facts, ["view_abc"], view_durations={"view_abc": 10.0})

    assert result.is_valid is False
    assert any("negative" in err for err in result.errors)


def test_validator_rejects_timestamp_exceeding_view_duration() -> None:
    facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(
                description="blue Hyundai sedan",
                view_id="view_abc",
                timestamp_seconds=1.0,
                confidence=0.9,
            )
        ],
        events=[
            CitedFact(
                description="vehicle changes lane",
                view_id="view_abc",
                timestamp_seconds=15.5,  # Exceeds 10.0s duration
                confidence=0.88,
            )
        ],
        candidate_collisions=[],
    )

    result = validate_citations(facts, ["view_abc"], view_durations={"view_abc": 10.0})

    assert result.is_valid is False
    assert any("exceeds view duration" in err and "15.5" in err for err in result.errors)


def test_validator_rejects_uncited_facts_with_empty_view_id_or_description() -> None:
    bad_party = object.__new__(PartyFact)
    object.__setattr__(bad_party, "description", "")
    object.__setattr__(bad_party, "view_id", "view_abc")
    object.__setattr__(bad_party, "timestamp_seconds", 1.0)
    object.__setattr__(bad_party, "confidence", 0.9)
    object.__setattr__(bad_party, "color", None)
    object.__setattr__(bad_party, "make_model", None)
    object.__setattr__(bad_party, "body_type", None)

    bad_event = object.__new__(CitedFact)
    object.__setattr__(bad_event, "description", "collision heard")
    object.__setattr__(bad_event, "view_id", "   ")
    object.__setattr__(bad_event, "timestamp_seconds", 2.0)
    object.__setattr__(bad_event, "confidence", 0.9)

    facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[bad_party],
        events=[bad_event],
        candidate_collisions=[],
    )

    result = validate_citations(facts, ["view_abc"], view_durations={"view_abc": 10.0})

    assert result.is_valid is False
    assert any("empty" in err for err in result.errors)
    assert any("missing view_id citation" in err for err in result.errors)


def test_validator_rejects_confidence_below_minimum_threshold() -> None:
    facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(
                description="green delivery motorcycle",
                view_id="view_abc",
                timestamp_seconds=1.0,
                confidence=0.3,  # Below default min confidence 0.5
            )
        ],
        events=[],
        candidate_collisions=[],
    )

    result = validate_citations(
        facts,
        ["view_abc"],
        view_durations={"view_abc": 10.0},
        min_confidence=DEFAULT_MIN_CONFIDENCE,
    )

    assert result.is_valid is False
    assert any("confidence" in err and "below minimum threshold" in err for err in result.errors)


def test_validator_rejects_party_with_raw_id() -> None:
    raw_party = object.__new__(PartyFact)
    object.__setattr__(raw_party, "description", "Vehicle 1")
    object.__setattr__(raw_party, "view_id", "view_abc")
    object.__setattr__(raw_party, "timestamp_seconds", 1.0)
    object.__setattr__(raw_party, "confidence", 0.95)
    object.__setattr__(raw_party, "color", None)
    object.__setattr__(raw_party, "make_model", None)
    object.__setattr__(raw_party, "body_type", None)

    facts = object.__new__(OverviewFacts)
    object.__setattr__(facts, "parties", [raw_party])
    object.__setattr__(facts, "events", [])
    object.__setattr__(facts, "candidate_collisions", [])
    object.__setattr__(facts, "behaviour_observations", [])
    object.__setattr__(facts, "location_conditions", [])
    object.__setattr__(facts, "structured_facts", [])
    object.__setattr__(facts, "text_readings", [])

    result = validate_citations(facts, ["view_abc"], view_durations={"view_abc": 10.0})

    assert result.is_valid is False
    assert any("raw ID" in err and "Vehicle 1" in err for err in result.errors)
