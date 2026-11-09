"""Builders for hand-written overview-10 model answers (synthetic; no model call is ever made)."""

from __future__ import annotations

import json
from typing import Any

from incident_report.ai.gemini import parse_overview
from incident_report.facts import OverviewFacts

VIEW = "v" * 64
OTHER_VIEW = "w" * 64


def row(code: str = "P1", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "party_code": code,
        "first_seen_seconds": 1.0,
        "attributes_seen_at_seconds": 1.0,
        "movement_seen_at_seconds": 1.5,
        "party_type": "sedan",
        "party_type_other": "",
        "marking": "none_seen",
        "colour_family": "silver",
        "colour_other": "",
        "manoeuvre": "continue_same_road",
        "manoeuvre_other": "",
        "turn_direction": "straight",
        "roundabout_exit_position": "not_applicable",
        "road_class_on_approach": "main_road",
        "road_class_basis": "more_than_two_lanes_and_long",
        "confidence": 0.9,
    }
    return {**base, **overrides}


def contact(
    subject: str = "P1",
    other: str = "P2",
    at: float = 3.0,
    kind: str = "vehicle_contact_vehicle",
    visibility: str = "visible",
    **overrides: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": kind,
        "subject": subject,
        "other_party": other,
        "injury_grade": "unknown",
        "visibility": visibility,
        "timestamp_seconds": at,
        "confidence": 0.9,
    }
    return {**base, **overrides}


def assessment(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "assessment_note": "P1 meets P2 at the junction.",
        "camera_role": "fixed_camera",
        "contact_state": "contact_observed",
        "contact_interval_start_seconds": 2.5,
        "contact_interval_end_seconds": 3.5,
    }
    return {**base, **overrides}


def answer(
    rows: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A complete overview-10 model answer: two cars, one visible contact, fixed camera."""
    base: dict[str, Any] = {
        "observation_notes": [{"party_code": "P1", "timestamp_seconds": 1.0, "note": "P1 drives straight on."}],
        "incident_assessment": assessment(),
        "party_checklist": rows if rows is not None else [row("P1"), row("P2", colour_family="red", party_type="suv")],
        "collision_observations": contacts if contacts is not None else [contact()],
        "structured_facts": [],
        "events": [{"description": "P1 hits P2", "timestamp_seconds": 3.0, "confidence": 0.9}],
        "behaviour_observations": [],
        "location_conditions": [],
        "text_readings": [],
        "driver_observations": [],
        "drowsiness_observations": [],
    }
    return {**base, **overrides}


def parse(
    model_answer: dict[str, Any], *, role: str = "exterior", view: str = VIEW, version: str = "overview-10"
) -> OverviewFacts:
    return parse_overview(json.dumps(model_answer), view, prompt_version=version, view_role=role)
