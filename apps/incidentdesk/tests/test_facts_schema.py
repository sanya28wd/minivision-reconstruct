"""Tests for RPT-11 fact schema and Gemini response-schema limits."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from incident_report.facts import (
    OVERVIEW_SCHEMA,
    CitedFact,
    OverviewFacts,
    PartyFact,
    is_raw_id,
)


def _walk_schema(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Traverse all nodes in a JSON schema."""
    items = [(path, node)]
    if isinstance(node, dict):
        for k, v in node.items():
            items.extend(_walk_schema(v, f"{path}.{k}" if path else k))
    elif isinstance(node, list):
        for idx, elem in enumerate(node):
            items.extend(_walk_schema(elem, f"{path}[{idx}]"))
    return items


def test_schema_respects_gemini_limits_no_literal_enums_in_list_items_and_no_max_items() -> None:
    """Gemini structured output constraints:

    1. No Literal/enum constraints inside list items.
    2. No maxItems on nested arrays.
    3. Flat structure without excessive nesting.
    Docs: https://ai.google.dev/gemini-api/docs/structured-output. The page only says "schema subset" and
    "very large or deeply nested schemas may be rejected"; these three rules are stricter project rules.
    """
    assert OVERVIEW_SCHEMA["type"] == "OBJECT"
    assert "properties" in OVERVIEW_SCHEMA
    raw_props = OVERVIEW_SCHEMA["properties"]
    assert isinstance(raw_props, dict)
    props: dict[str, Any] = raw_props

    expected_categories = {
        "parties",
        "events",
        "candidate_collisions",
        "behaviour_observations",
        "location_conditions",
    }
    assert expected_categories.issubset(set(props.keys()))

    all_nodes = _walk_schema(OVERVIEW_SCHEMA)

    # Constraint 1: no maxItems anywhere
    for path, node in all_nodes:
        assert "maxItems" not in path, f"maxItems found at {path}"
        if isinstance(node, dict):
            assert "maxItems" not in node, f"maxItems found in node at {path}"

    # Constraint 2: no enum or Literal inside list items
    for cat in expected_categories:
        cat_schema = props[cat]
        assert isinstance(cat_schema, dict)
        assert cat_schema.get("type") == "ARRAY"
        item_schema = cat_schema.get("items", {})
        assert "enum" not in item_schema, f"enum constraint inside list item for {cat}"
        item_nodes = _walk_schema(item_schema, f"properties.{cat}.items")
        for p, _n in item_nodes:
            assert "enum" not in p, f"enum found in item schema at {p}"


def test_raw_id_detector_flags_synthetic_ids() -> None:
    raw_ids = [
        "Vehicle 1",
        "vehicle_2",
        "Vehicle-3",
        "Car 1",
        "car_0",
        "V1",
        "v2",
        "Truck 4",
        "object_0",
        "Party 1",
        "Actor 2",
        "ID_123",
        "42",
    ]
    for identifier in raw_ids:
        assert is_raw_id(identifier), f"Expected {identifier!r} to be identified as a raw ID"

    descriptive_parties = [
        "silver Toyota sedan",
        "white Nissan Patrol SUV",
        "black Honda motorcycle",
        "yellow school bus",
        "pedestrian in red jacket",
        "cyclist with green backpack",
    ]
    for desc in descriptive_parties:
        assert not is_raw_id(desc), f"Expected {desc!r} to NOT be identified as a raw ID"


def test_party_fact_rejects_raw_id_on_validation() -> None:
    with pytest.raises(ValidationError, match="raw ID"):
        PartyFact(
            description="Vehicle 1",
            view_id="view_sha",
            timestamp_seconds=2.5,
            confidence=0.9,
        )


def test_full_facts_typed_contract_instantiation_and_aliases() -> None:
    party = PartyFact(
        description="silver Toyota sedan",
        view_id="view_abc",
        timestamp_seconds=1.5,
        confidence=0.95,
        color="silver",
        make_model="Toyota",
        body_type="sedan",
    )
    event = CitedFact(
        description="Toyota sedan brakes abruptly",
        view_id="view_abc",
        timestamp_seconds=2.0,
        confidence=0.9,
    )
    collision = CitedFact(
        description="Rear impact between Toyota sedan and following vehicle",
        view_id="view_abc",
        timestamp_seconds=2.5,
        confidence=0.85,
    )
    behaviour = CitedFact(
        description="Driver glance towards dashboard",
        view_id="view_abc",
        timestamp_seconds=1.8,
        confidence=0.75,
    )
    location = CitedFact(
        description="Intersection signboard reads Sheikh Zayed Rd",
        view_id="view_abc",
        timestamp_seconds=0.5,
        confidence=0.99,
    )

    facts = OverviewFacts(
        parties=[party],
        events=[event],
        candidate_collisions=[collision],
        behaviour_observations=[behaviour],
        location_conditions=[location],
    )

    # Aliases
    assert facts.collision_candidates == [collision]
    assert facts.location_and_conditions == [location]

    # Serialization roundtrip
    dumped = json.loads(facts.model_dump_json())
    restored = OverviewFacts.model_validate(dumped)
    assert restored == facts


def test_confidence_is_required_so_a_missing_value_can_never_become_full_confidence() -> None:
    """ADR 0006: an omitted confidence must fail, not default to 1.0."""
    cited: dict[str, Any] = {"description": "silver Toyota sedan", "view_id": "view_abc", "timestamp_seconds": 1.0}
    with pytest.raises(ValidationError, match="confidence"):
        CitedFact(**cited)
    with pytest.raises(ValidationError, match="confidence"):
        PartyFact(**cited)
    with pytest.raises(ValidationError, match="confidence"):
        OverviewFacts.model_validate({"parties": [cited], "events": [], "candidate_collisions": []})
    assert CitedFact(**cited, confidence=0.0).confidence == 0.0


def test_response_schema_requires_confidence_in_every_fact_category() -> None:
    props = OVERVIEW_SCHEMA["properties"]
    assert isinstance(props, dict)
    for category, array_schema in props.items():
        item_schema = array_schema["items"]
        assert "confidence" in item_schema["required"], category
        assert "confidence" in item_schema["properties"], category


def test_response_schema_does_not_ask_the_model_for_a_view_id() -> None:
    """RPT-07 review follow-up 2: the code attaches the view id; the model never copies a 64-hex fingerprint."""
    props = OVERVIEW_SCHEMA["properties"]
    assert isinstance(props, dict)
    for category, array_schema in props.items():
        item_schema = array_schema["items"]
        assert "view_id" not in item_schema["properties"], category
        assert "view_id" not in item_schema["required"], category
        if category == "structured_facts":
            assert set(item_schema["required"]) == {
                "subject",
                "field",
                "value",
                "other_party",
                "timestamp_seconds",
                "confidence",
            }, category
        else:
            assert set(item_schema["required"]) == {"description", "timestamp_seconds", "confidence"}, category
