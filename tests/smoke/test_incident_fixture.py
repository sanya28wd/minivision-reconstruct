"""Week 8 phase 3: incident scenarios carry exact, self-consistent ground truth before any detection logic exists."""

from pathlib import Path
import shutil

import pytest

from minivision_reconstruct.incident.models import IncidentConfig
from minivision_reconstruct.incident.staged_fixture import (
    SCENARIOS, IncidentGroundTruth, create_incident_case, incident_case_id, incident_ground_truth,
)
from minivision_reconstruct.reconstruction.storage import read_record


CONFIG = read_record(Path("configs/incident.json").resolve(), IncidentConfig)
SEEDS = (1, 2, 101)


def truths(seed: int) -> dict[str, IncidentGroundTruth]:
    return {scenario: incident_ground_truth(f"case-{seed}", scenario, seed, 0.7) for scenario in SCENARIOS}


@pytest.fixture(scope="module")
def by_seed() -> dict[int, dict[str, IncidentGroundTruth]]:
    return {seed: truths(seed) for seed in SEEDS}


def test_incident_scenarios_cover_every_outcome_and_type(by_seed: dict[int, dict[str, IncidentGroundTruth]]) -> None:
    for scenarios in by_seed.values():
        incidents = {name: truth.incidents for name, truth in scenarios.items()}
        assert len(incidents) == 10
        assert {name for name, found in incidents.items() if not found} == {"normal_crossing", "box_overlap_only"}
        outcomes = {found[0].contribution.outcome for found in incidents.values() if found and found[0].contribution}
        assert outcomes == {"ONE_CONTRIBUTOR", "MULTIPLE_CONTRIBUTORS", "NO_SUPPORTED_CONTRIBUTOR", "UNDETERMINED"}
        types = {found[0].collision_type for found in incidents.values() if found and found[0].event_type != "near_miss_candidate"}
        assert types == {"rear_end", "t_bone", "sideswipe", "single"}
        assert incidents["single_vehicle"][0].contribution is None


def test_incident_truth_matches_each_scenario_label(by_seed: dict[int, dict[str, IncidentGroundTruth]]) -> None:
    for scenarios in by_seed.values():
        rear = scenarios["rear_end"].incidents[0]
        assert rear.contact_separation <= CONFIG.collision.contact_distance
        assert rear.contribution.contributing_track_ids == (2,)
        assert rear.contribution.actions[0].rule == "did_not_stop_behind_stopped_road_user"
        tbone = scenarios["t_bone"].incidents[0]
        assert tbone.relative_heading_degrees == pytest.approx(90.0, abs=1.0)
        assert tbone.contribution.actions[0].action_time < tbone.peak_time, "stop-line crossing precedes impact"
        both = scenarios["both_lane_change"].incidents[0]
        assert both.contribution.contributing_track_ids == (1, 2)
        assert all(action.action_time < both.peak_time for action in both.contribution.actions)
        for name in ("near_miss_crossing", "pedestrian_near_miss"):
            (conflict,) = scenarios[name].conflicts
            assert 0.7 <= conflict.point_pet_seconds <= 1.3
            assert scenarios[name].incidents[0].collision_type is None
        (normal,) = scenarios["normal_crossing"].conflicts
        assert normal.point_pet_seconds > CONFIG.near_miss.near_miss_pet_seconds


def test_incident_truth_hard_cases_are_what_they_claim(by_seed: dict[int, dict[str, IncidentGroundTruth]]) -> None:
    for scenarios in by_seed.values():
        occluded = scenarios["occluded_impact"]
        window = occluded.road_users[0].unavailable_window
        assert window[0] < occluded.incidents[0].peak_time < window[1]
        assert occluded.incidents[0].contribution.outcome == "UNDETERMINED"
        (pair,) = scenarios["box_overlap_only"].pairs
        assert pair.box_overlap_frames, "the gate test needs image boxes that overlap"
        assert pair.minimum_separation > CONFIG.collision.contact_distance + 1.0, "but road-plane contact points stay well apart"
        single = scenarios["single_vehicle"]
        assert all(item.minimum_separation > CONFIG.single_vehicle.partner_exclusion_distance for item in single.pairs)


def test_incident_truth_is_seeded() -> None:
    assert truths(1) == truths(1)
    assert truths(1)["rear_end"].incidents[0].peak_time != truths(2)["rear_end"].incidents[0].peak_time


def test_incident_case_renders_with_ground_truth(tmp_path: Path) -> None:
    case_id = incident_case_id("smoke", 1, "rear_end")
    assert case_id == "smoke-001-01-rear-end"
    case = create_incident_case(tmp_path / "cases", case_id, "rear_end", 1, 0.7, shutil.which("ffmpeg"), shutil.which("ffprobe"))
    assert (case.reconstruction_directory / "manifest.json").exists()
    saved = read_record(case.ground_truth_path, IncidentGroundTruth)
    assert saved == incident_ground_truth(case_id, "rear_end", 1, 0.7)
    lost = [line for line in (tmp_path / "cases" / case_id / "analysis" / "tracks" / "staged-tracks" / "tracks.jsonl").read_text().splitlines() if '"lost"' in line]
    assert not lost, "the rear-end scenario has no occlusion"
    with pytest.raises(Exception, match="already exists"):
        create_incident_case(tmp_path / "cases", case_id, "rear_end", 1, 0.7, shutil.which("ffmpeg"), shutil.which("ffprobe"))
