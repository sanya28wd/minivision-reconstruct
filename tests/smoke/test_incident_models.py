"""Week 8 phase 1: incident configuration and record contracts enforce the spec before any algorithm exists."""

import json
from pathlib import Path
from typing import Any, get_args

from pydantic import BaseModel, ValidationError
import pytest

from minivision_reconstruct.incident import models
from minivision_reconstruct.incident.models import (
    NOT_A_VIOLATION_FINDING, ActionEvidence, ConflictRecord, ContributionAssessment, ContributionOutcome,
    EvidenceSignal, IncidentConfig, IncidentEvent, PhaseBounds, ReviewDecision, UnresolvedFact,
)
from minivision_reconstruct.reconstruction.storage import read_record


CONFIG = Path("configs/incident.json").resolve()
FORBIDDEN = ("speed", "velocity", "accel", "ttc", "time_to_collision")


def config_data() -> dict[str, Any]:
    return json.loads(CONFIG.read_text())


def signal(name: str, kind: str) -> EvidenceSignal:
    return EvidenceSignal(name=name, kind=kind, track_ids=(1, 2), source_frames=(100,), supporting_event_ids=(), description=name)


def event(**changes: Any) -> IncidentEvent:
    values: dict[str, Any] = {
        "event_id": "collision_candidate-t1-2-f100", "case_id": "case", "event_type": "collision_candidate", "track_ids": (1, 2),
        "start_frame": 95, "peak_frame": 100, "end_frame": 105, "start_time": 3.8, "peak_time": 4.0, "end_time": 4.2,
        "time_window": (3.9, 4.1), "location": (10.0, 20.0), "location_uncertainty": 0.5, "unit": "m",
        "signals": (signal("closest_approach", "proximity"), signal("post_event_stop", "consequence")),
        "conflict_id": None, "phases": PhaseBounds(pre_event=(0.0, 3.9), event=(3.9, 4.1), post_event=(4.1, 10.0)),
        "predicted_collision_type": "rear_end", "confidence": "high", "provenance": "inferred",
        "status": "PROPOSED_BY_SYSTEM", "created_by": "test",
    }
    return IncidentEvent(**(values | changes))


def evidence(track_id: int) -> ActionEvidence:
    return ActionEvidence(rule="did_not_stop_behind_stopped_road_user", track_id=track_id, description="no stop",
                          event_ids=("stop-t1-f80",), source_frames=(80,), action_time=3.2, seconds_before_peak=0.8)


def assessment(**changes: Any) -> ContributionAssessment:
    values: dict[str, Any] = {
        "assessment_id": "assessment-1", "case_id": "case", "event_id": "collision_candidate-t1-2-f100",
        "involved_track_ids": (1, 2), "contributing_track_ids": (2,), "outcome": "ONE_CONTRIBUTOR",
        "lookback_window": (0.0, 4.0), "observed_actions": ("stop-t1-f80",), "supporting_evidence": (evidence(2),),
        "counterevidence": (), "applicable_road_context_refs": (), "unresolved_facts": (), "rule_source": None,
        "disclaimer": NOT_A_VIOLATION_FINDING, "confidence": "medium", "provenance": "inferred",
        "status": "PROPOSED_BY_SYSTEM", "created_by": "test",
    }
    return ContributionAssessment(**(values | changes))


def decision(**changes: Any) -> ReviewDecision:
    values: dict[str, Any] = {
        "target_kind": "event", "target_id": "collision_candidate-t1-2-f100", "action": "confirm", "note": "",
        "modified_peak_time": None, "modified_location": None, "modified_outcome": None,
        "modified_contributing_track_ids": None, "reviewer": "reviewer", "decided_at": "2026-10-25T00:00:00+00:00",
    }
    return ReviewDecision(**(values | changes))


def test_config_loads_and_every_field_is_required() -> None:
    config = read_record(CONFIG, IncidentConfig)
    assert config.config_id == "incident-week8-002"
    for section, fields in config_data().items():
        if not isinstance(fields, dict):
            continue
        for field in fields:
            data = config_data()
            del data[section][field]
            with pytest.raises(ValidationError):
                IncidentConfig.model_validate(data)


def test_config_rejects_unknown_fields_and_inconsistent_thresholds() -> None:
    data = config_data()
    data["collision"]["speed_threshold"] = 1.0
    with pytest.raises(ValidationError):
        IncidentConfig.model_validate(data)
    data = config_data()
    data["near_miss"]["near_miss_distance"] = data["collision"]["contact_distance"]
    with pytest.raises(ValidationError, match="near_miss_distance"):
        IncidentConfig.model_validate(data)
    data = config_data()
    data["collision_type"]["t_bone_min_degrees"] = 130.0
    with pytest.raises(ValidationError, match="bands"):
        IncidentConfig.model_validate(data)


def test_no_record_has_a_speed_or_ttc_field() -> None:
    records = [value for value in vars(models).values() if isinstance(value, type) and issubclass(value, BaseModel)]
    assert records
    for record in records:
        for field in record.model_fields:
            assert not any(word in field.lower() for word in FORBIDDEN), f"{record.__name__}.{field}"


def test_box_overlap_alone_cannot_form_a_candidate() -> None:
    with pytest.raises(ValidationError, match="proximity"):
        event(signals=(signal("image_box_overlap", "supporting"),))
    with pytest.raises(ValidationError, match="supporting signal"):
        signal("image_box_overlap", "proximity")
    assert event().predicted_collision_type == "rear_end"


def test_single_vehicle_candidate_shape() -> None:
    single = event(event_type="single_vehicle_candidate", track_ids=(1,), predicted_collision_type="single",
                   signals=(signal("heading_jump", "consequence"),))
    assert single.track_ids == (1,)
    with pytest.raises(ValidationError, match="consequence"):
        event(event_type="single_vehicle_candidate", track_ids=(1,), predicted_collision_type="single",
              signals=(signal("closest_approach", "proximity"),))
    with pytest.raises(ValidationError, match="one road user"):
        event(event_type="single_vehicle_candidate", predicted_collision_type="single")


def test_phases_and_time_window_are_ordered() -> None:
    with pytest.raises(ValidationError):
        PhaseBounds(pre_event=(0.0, 4.0), event=(3.9, 4.1), post_event=(4.1, 10.0))
    with pytest.raises(ValidationError):
        event(time_window=(4.1, 4.2))


def test_pet_record_is_internally_consistent() -> None:
    values: dict[str, Any] = {
        "conflict_id": "conflict-t1-2-f100", "case_id": "case", "track_first": 1, "track_second": 2, "location": (5.0, 5.0),
        "radius": 1.0, "unit": "m", "first_enter_frame": 90, "first_exit_frame": 95, "second_enter_frame": 120,
        "second_exit_frame": 125, "first_exit_time": 3.8, "second_enter_time": 4.8, "pet_seconds": 1.0, "pet_window": (0.92, 1.08),
        "simultaneous_occupancy": False, "evidence_signals": ("paths cross",), "confidence": "high",
    }
    assert ConflictRecord(**values).pet_seconds == 1.0
    with pytest.raises(ValidationError, match="simultaneous_occupancy"):
        ConflictRecord(**(values | {"simultaneous_occupancy": True}))
    with pytest.raises(ValidationError, match="pet_seconds must equal"):
        ConflictRecord(**(values | {"pet_seconds": 2.0, "pet_window": (1.9, 2.1)}))


def test_contribution_outcome_matches_contributors_and_evidence() -> None:
    assert assessment().outcome == "ONE_CONTRIBUTOR"
    assert set(get_args(ContributionOutcome)) == {"ONE_CONTRIBUTOR", "MULTIPLE_CONTRIBUTORS", "NO_SUPPORTED_CONTRIBUTOR", "UNDETERMINED"}
    with pytest.raises(ValidationError, match="does not match"):
        assessment(outcome="MULTIPLE_CONTRIBUTORS")
    with pytest.raises(ValidationError, match="supporting evidence"):
        assessment(supporting_evidence=(evidence(1),))
    with pytest.raises(ValidationError, match="involved"):
        assessment(contributing_track_ids=(3,), supporting_evidence=(evidence(3),))
    with pytest.raises(ValidationError):
        assessment(rule_source="RTA rule 12")


def test_unresolved_facts_force_undetermined() -> None:
    lost = UnresolvedFact(kind="unavailable_frames", track_ids=(2,), time_window=(3.5, 4.0), description="track 2 lost", decisive=True)
    with pytest.raises(ValidationError, match="UNDETERMINED"):
        assessment(unresolved_facts=(lost,))
    undetermined = assessment(outcome="UNDETERMINED", contributing_track_ids=(), supporting_evidence=(), unresolved_facts=(lost,))
    assert undetermined.outcome == "UNDETERMINED"
    minor = lost.model_copy(update={"decisive": False})
    with pytest.raises(ValidationError, match="complete data"):
        assessment(outcome="NO_SUPPORTED_CONTRIBUTOR", contributing_track_ids=(), supporting_evidence=(), unresolved_facts=(minor,))


def test_review_decisions_only_change_what_their_action_allows() -> None:
    assert decision().action == "confirm"
    with pytest.raises(ValidationError, match="Only a 'modify'"):
        decision(modified_peak_time=4.2)
    with pytest.raises(ValidationError, match="note"):
        decision(action="reject")
    assert decision(action="modify", note="impact one frame later", modified_peak_time=4.04).modified_peak_time == 4.04
    with pytest.raises(ValidationError, match="its own fields"):
        decision(action="modify", note="wrong field", modified_outcome="UNDETERMINED")
    assessment_change = decision(target_kind="assessment", target_id="assessment-1", action="modify", note="occluded",
                                 modified_outcome="UNDETERMINED", modified_contributing_track_ids=())
    assert assessment_change.modified_outcome == "UNDETERMINED"
