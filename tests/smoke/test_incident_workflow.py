"""Week 8 incident pipeline on staged incident scenarios: the overlap gate, contribution outcomes, evidence links, no speed,
immutable runs, and reviewer decisions that never rewrite the proposals."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from time import monotonic, sleep
from typing import Any

from playwright.sync_api import sync_playwright
import pytest

from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.incident.collision import IncidentInputs, propose_incidents
from minivision_reconstruct.incident.models import (
    NOT_A_VIOLATION_FINDING, ConflictRecord, ContributionAssessment, IncidentConfig, IncidentError, IncidentEvent, IncidentManifest,
    IncidentReviewRevision, ReviewDecision, ReviewedEvent,
)
from minivision_reconstruct.incident.pet import post_encroachment
from minivision_reconstruct.incident.pipeline import analyze_incident
from minivision_reconstruct.incident.review import effective_view, export_reviewed_view, load_review, save_review_revision, with_decision
from minivision_reconstruct.incident.staged_fixture import IncidentGroundTruth, create_incident_case, incident_case_id
from minivision_reconstruct.incident.validation import evaluate_incident
from minivision_reconstruct.movement.models import MovementEvent, MovementObservation, SmoothedPoint
from minivision_reconstruct.movement.motion import MotionContext
from minivision_reconstruct.movement.pipeline import analyze_movement
from minivision_reconstruct.reconstruction.storage import load_bundle, read_record, read_records


MOVEMENT_CONFIG = Path("configs/movement.json").resolve()
INCIDENT_CONFIG = Path("configs/incident.json").resolve()
CONFIG = read_record(INCIDENT_CONFIG, IncidentConfig)
FORBIDDEN = ("speed", "velocity", "accel", "ttc", "time_to_collision")
SCENARIOS = ("rear_end", "both_lane_change", "near_miss_crossing", "normal_crossing", "occluded_impact", "box_overlap_only")


@pytest.fixture(scope="module")
def cases(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, str]]:
    root = tmp_path_factory.mktemp("week8") / "cases"
    result = {}
    for scenario in SCENARIOS:
        case_id = incident_case_id("smoke", 1, scenario)
        create_incident_case(root, case_id, scenario, 1, 0.7, shutil.which("ffmpeg"), shutil.which("ffprobe"))
        analyze_movement(root, case_id, "week8-incident", MOVEMENT_CONFIG, "movement")
        analyze_incident(root, case_id, "movement", INCIDENT_CONFIG, "incident")
        result[scenario] = (root, case_id)
    return result


def run_dir(cases: dict[str, tuple[Path, str]], scenario: str) -> Path:
    root, case_id = cases[scenario]
    return root / case_id / "analysis" / "incident" / "incident"


def proposals(cases: dict[str, tuple[Path, str]], scenario: str) -> tuple[IncidentEvent, ...]:
    return read_records(run_dir(cases, scenario) / "events.jsonl", IncidentEvent)


def test_box_overlap_alone_never_creates_a_candidate(cases: dict[str, tuple[Path, str]]) -> None:
    root, case_id = cases["box_overlap_only"]
    truth = read_record(root / case_id / "annotations" / "incident_ground_truth.json", IncidentGroundTruth)
    assert truth.pairs[0].box_overlap_frames, "the scenario must contain overlapping image boxes"
    movement = root / case_id / "analysis" / "movement" / "movement"
    smoothed = read_records(movement / "smoothed.jsonl", SmoothedPoint)
    context = MotionContext(case_id, 1 / 25, "m", True, "test")
    # Contact points stay about 6 m apart; with proximity unable to fire, overlapping boxes must not be enough.
    strict = CONFIG.model_copy(update={"collision": CONFIG.collision.model_copy(update={"contact_distance": 1.0})})
    inputs = IncidentInputs(
        smoothed=smoothed, movement=read_records(movement / "movement_observations.jsonl", MovementObservation),
        events=read_records(movement / "events.jsonl", MovementEvent), conflicts=post_encroachment(smoothed, strict.conflict, context),
        observations=load_bundle(root / case_id / "analysis" / "reconstruction" / "week8-incident", "initial", "initial").observations, lanes={},
    )
    assert propose_incidents(inputs, strict, context) == ()


def test_every_candidate_lists_proximity_consequences_and_source_frames(cases: dict[str, tuple[Path, str]]) -> None:
    for scenario in SCENARIOS:
        root, case_id = cases[scenario]
        movement_ids = {event.event_id for event in read_records(root / case_id / "analysis" / "movement" / "movement" / "events.jsonl", MovementEvent)}
        for event in proposals(cases, scenario):
            kinds = [signal.kind for signal in event.signals]
            assert "proximity" in kinds or event.event_type == "single_vehicle_candidate"
            if event.event_type == "collision_candidate":
                assert kinds.count("consequence") >= CONFIG.collision.minimum_consequence_signals
            for signal in event.signals:
                assert signal.source_frames and signal.description
                assert set(signal.supporting_event_ids) <= movement_ids, "cited movement events must exist"


def test_rear_end_is_proposed_near_the_scripted_impact(cases: dict[str, tuple[Path, str]]) -> None:
    root, case_id = cases["rear_end"]
    truth = read_record(root / case_id / "annotations" / "incident_ground_truth.json", IncidentGroundTruth).incidents[0]
    (event,) = [item for item in proposals(cases, "rear_end") if item.event_type == "collision_candidate"]
    assert event.track_ids == (1, 2)
    assert abs(event.peak_time - truth.peak_time) <= CONFIG.validation.impact_time_tolerance_seconds
    assert event.time_window[0] <= event.peak_time <= event.time_window[1]
    assert event.predicted_collision_type == "rear_end"
    assert {signal.name for signal in event.signals} >= {"closest_approach", "post_event_stop"}
    assert event.phases.pre_event[1] == event.time_window[0] and event.phases.post_event[0] == event.time_window[1]


def test_normal_crossing_proposes_nothing_but_records_pet(cases: dict[str, tuple[Path, str]]) -> None:
    assert proposals(cases, "normal_crossing") == ()
    manifest = read_record(run_dir(cases, "normal_crossing") / "manifest.json", IncidentManifest)
    assert manifest.event_count == 0 and manifest.conflict_count == 1


def test_incident_outputs_never_contain_speed(cases: dict[str, tuple[Path, str]]) -> None:
    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for item in value.values() for key in keys(item)}
        return {key for item in value for key in keys(item)} if isinstance(value, list) else set()

    for scenario in SCENARIOS:
        for path in run_dir(cases, scenario).iterdir():
            text = path.read_text()
            records = [json.loads(line) for line in text.splitlines()] if path.suffix == ".jsonl" else [json.loads(text)]
            assert not {key for key in keys(records) if any(word in key.lower() for word in FORBIDDEN)}, path


def test_incident_run_is_immutable_and_hash_linked(cases: dict[str, tuple[Path, str]], tmp_path: Path) -> None:
    root, case_id = cases["rear_end"]
    manifest = read_record(run_dir(cases, "rear_end") / "manifest.json", IncidentManifest)
    assert manifest.source_movement_manifest_sha256 == sha256_file(root / case_id / "analysis" / "movement" / "movement" / "manifest.json")
    with pytest.raises(IncidentError, match="already exists"):
        analyze_incident(root, case_id, "movement", INCIDENT_CONFIG, "incident")
    copy = tmp_path / "cases"
    shutil.copytree(root / case_id, copy / case_id)
    events = copy / case_id / "analysis" / "movement" / "movement" / "events.jsonl"
    events.write_text(events.read_text().replace('"stop"', '"start"', 1))
    with pytest.raises(Exception, match="(?i)hash|sha"):
        analyze_incident(copy, case_id, "movement", INCIDENT_CONFIG, "tampered")


def assessments(cases: dict[str, tuple[Path, str]], scenario: str) -> tuple[ContributionAssessment, ...]:
    return read_records(run_dir(cases, scenario) / "assessments.jsonl", ContributionAssessment)


def test_contribution_outcomes_match_each_scenario(cases: dict[str, tuple[Path, str]]) -> None:
    root, case_id = cases["rear_end"]
    (rear,) = assessments(cases, "rear_end")
    assert (rear.outcome, rear.contributing_track_ids) == ("ONE_CONTRIBUTOR", (2,))
    (evidence,) = rear.supporting_evidence
    stops = {event.event_id for event in read_records(root / case_id / "analysis" / "movement" / "movement" / "events.jsonl", MovementEvent)
             if event.event_type == "stop" and event.track_ids == (1,)}
    assert evidence.rule == "did_not_stop_behind_stopped_road_user" and set(evidence.event_ids) <= stops, "R1 cites the leader's stop event"
    (both,) = assessments(cases, "both_lane_change")
    assert (both.outcome, both.contributing_track_ids) == ("MULTIPLE_CONTRIBUTORS", (1, 2))
    assert {item.rule for item in both.supporting_evidence} == {"lane_change_into_occupied_lane"}
    (occluded,) = assessments(cases, "occluded_impact")
    assert occluded.outcome == "UNDETERMINED" and not occluded.supporting_evidence
    assert any(fact.kind == "unavailable_frames" and fact.decisive for fact in occluded.unresolved_facts)
    (near,) = assessments(cases, "near_miss_crossing")
    assert near.outcome == "NO_SUPPORTED_CONTRIBUTOR"
    assert any(item.rule == "no_visible_stop_at_stop_line" for item in near.counterevidence), "the visible stop is recorded as counterevidence"
    assert assessments(cases, "normal_crossing") == ()


def test_every_contribution_links_to_timed_actions_and_source_frames(cases: dict[str, tuple[Path, str]]) -> None:
    for scenario in SCENARIOS:
        root, case_id = cases[scenario]
        cited = {event.event_id for event in read_records(root / case_id / "analysis" / "movement" / "movement" / "events.jsonl", MovementEvent)}
        cited |= {item.conflict_id for item in read_records(run_dir(cases, scenario) / "conflicts.jsonl", ConflictRecord)}
        events = {event.event_id: event for event in proposals(cases, scenario)}
        for assessment in assessments(cases, scenario):
            event = events[assessment.event_id]
            assert event.event_type != "single_vehicle_candidate"
            assert assessment.rule_source is None and assessment.disclaimer == NOT_A_VIOLATION_FINDING
            for item in assessment.supporting_evidence + assessment.counterevidence:
                assert set(item.event_ids) <= cited and item.source_frames
                assert item.action_time <= event.time_window[1]
            assert set(assessment.observed_actions) <= cited


def test_evaluation_reports_timing_pet_and_contribution(cases: dict[str, tuple[Path, str]], tmp_path: Path) -> None:
    root = next(iter(cases.values()))[0]
    result = evaluate_incident(root, tuple(case_id for _, case_id in cases.values()), "incident", tmp_path / "evaluation", "smoke")
    metrics = json.loads((result / "metrics.json").read_text())
    assert metrics["config_id"] == CONFIG.config_id
    assert metrics["gate"]["event_timing_error_measured"] and metrics["gate"]["occluded_scenarios_undetermined"]
    assert metrics["detection"]["matched"] >= 4 and metrics["impact_time_error_s"]["count"] == metrics["detection"]["matched"]
    assert metrics["pet"]["matched_true_crossings"] >= 2 and metrics["pet"]["within_tolerance"] == 1.0, "PET is compared at the estimator's own radius"
    assert metrics["contribution"]["agreement_given_detection"] == 1.0
    assert {path.name for path in result.iterdir()} == {"metrics.json", "matches.csv", "pet.csv", "contributions.csv", "failures.csv", "config_snapshot.json", "plots.html"}
    with pytest.raises(IncidentError, match="already exists"):
        evaluate_incident(root, tuple(case_id for _, case_id in cases.values()), "incident", tmp_path / "evaluation", "smoke")


def copied_case(cases: dict[str, tuple[Path, str]], scenario: str, destination: Path) -> Path:
    """A private copy, so review revisions never touch the shared module fixture."""
    root, case_id = cases[scenario]
    shutil.copytree(root / case_id, destination / case_id)
    return destination / case_id / "analysis" / "incident" / "incident"


def decision(**changes: Any) -> ReviewDecision:
    values: dict[str, Any] = {"target_kind": "event", "target_id": "", "action": "confirm", "note": "", "modified_peak_time": None,
                              "modified_location": None, "modified_outcome": None, "modified_contributing_track_ids": None,
                              "reviewer": "reviewer", "decided_at": "2026-10-27T00:00:00+00:00"}
    return ReviewDecision(**(values | changes))


def test_review_revisions_change_the_view_but_never_the_proposals(cases: dict[str, tuple[Path, str]], tmp_path: Path) -> None:
    run = copied_case(cases, "rear_end", tmp_path)
    manifest = read_record(run / "manifest.json", IncidentManifest)
    originals = {path.name: sha256_file(path) for path in run.iterdir() if path.is_file()}
    (event,) = read_records(run / "events.jsonl", IncidentEvent)
    (assessment,) = read_records(run / "assessments.jsonl", ContributionAssessment)
    corrected = event.peak_time + 0.08
    first = save_review_revision(run, (decision(target_id=event.event_id, action="modify", note="impact two frames later", modified_peak_time=corrected),), "Reviewer A")
    revision = read_record(first / "revision.json", IncidentReviewRevision)
    assert revision.previous_sha256 == sha256_file(run / "manifest.json"), "the first revision is chained to the run manifest"
    second = save_review_revision(run, with_decision(revision.document.decisions, decision(target_kind="assessment", target_id=assessment.assessment_id)), "Reviewer B")
    assert read_record(second / "revision.json", IncidentReviewRevision).previous_sha256 == sha256_file(first / "revision.json")
    view = effective_view(run, second.name)
    assert (view.events[0].status, view.events[0].peak_time, view.events[0].proposal.peak_time) == ("MODIFIED_BY_REVIEWER", corrected, event.peak_time)
    assert view.assessments[0].status == "CONFIRMED_BY_REVIEWER" and view.assessments[0].event_status == "MODIFIED_BY_REVIEWER"
    assert effective_view(run, "initial").events[0].status == "PROPOSED_BY_SYSTEM"
    with pytest.raises(IncidentError, match="only be rejected"):
        save_review_revision(run, with_decision(load_review(run, second.name).decisions, decision(target_id=event.event_id, action="reject", note="no contact")), "Reviewer C")
    with pytest.raises(IncidentError, match="outside the observed span"):
        save_review_revision(run, (decision(target_id=event.event_id, action="modify", note="typo", modified_peak_time=999.0),), "Reviewer C")
    with pytest.raises(IncidentError, match="does not match"):
        save_review_revision(run, (decision(target_kind="assessment", target_id=assessment.assessment_id, action="modify", note="wrong",
                                            modified_outcome="MULTIPLE_CONTRIBUTORS"),), "Reviewer C")
    rejected = with_decision(with_decision(load_review(run, second.name).decisions, decision(target_id=event.event_id, action="reject", note="no contact")),
                             decision(target_kind="assessment", target_id=assessment.assessment_id, action="reject", note="event rejected"))
    third = save_review_revision(run, rejected, "Reviewer C")
    exported = export_reviewed_view(run, third.name)
    (reviewed,) = read_records(exported / "reviewed_events.jsonl", ReviewedEvent)
    assert reviewed.status == "REJECTED_BY_REVIEWER" and reviewed.proposal == event, "the export embeds the original proposal unchanged"
    with pytest.raises(IncidentError, match="immutable"):
        export_reviewed_view(run, third.name)
    assert {path.name: sha256_file(path) for path in run.iterdir() if path.is_file()} == originals
    assert manifest.artifacts == read_record(run / "manifest.json", IncidentManifest).artifacts
    (first / "revision.json").write_text((first / "revision.json").read_text().replace("Reviewer A", "Reviewer Z"))
    with pytest.raises(Exception, match="(?i)seal|integrity"):
        effective_view(run, first.name)


def test_browser_incident_panel_reviews_and_seeks(cases: dict[str, tuple[Path, str]], tmp_path: Path) -> None:
    root = tmp_path / "cases"
    run = copied_case(cases, "rear_end", root)
    (event,) = read_records(run / "events.jsonl", IncidentEvent)
    events_hash = sha256_file(run / "events.jsonl")
    port, media_port = free_port(), free_port()
    command = [sys.executable, "-m", "streamlit", "run", "scripts/reconstruct_case.py", "--server.headless", "true", "--server.address", "127.0.0.1",
               "--server.port", str(port), "--browser.gatherUsageStats", "false", "--", "--cases-root", str(root), "--media-port", str(media_port), "--ffmpeg", "ffmpeg"]
    with (tmp_path / "streamlit.log").open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, env={**os.environ, "PYTHONPATH": str(Path("src").resolve())})
        try:
            deadline = monotonic() + 20
            while True:
                with socket.socket() as connection:
                    if connection.connect_ex(("127.0.0.1", port)) == 0:
                        break
                if process.poll() is not None or monotonic() > deadline:
                    raise RuntimeError((tmp_path / "streamlit.log").read_text())
                sleep(0.1)
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch()
                page = browser.new_page(viewport={"width": 1500, "height": 6000})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                page.locator(".replay-root[data-source-frame='0']").wait_for(timeout=30000)
                page.get_by_text("Incident analysis", exact=True).wait_for(timeout=30000)
                page.get_by_role("button", name="Go to impact frame").click()
                page.locator(f".replay-root[data-source-frame='{event.peak_frame}']").wait_for(timeout=30000)
                page.get_by_label("Reviewer name").fill("Browser Reviewer")
                page.get_by_label("Reviewer name").press("Enter")
                page.get_by_role("radiogroup", name="Decision", exact=True).get_by_text("Modify", exact=True).click()
                corrected = page.get_by_label("Corrected impact time (s)")
                corrected.fill(f"{event.peak_time + 0.08:.2f}")
                corrected.press("Enter")
                page.get_by_label("Reviewer note (required to reject or modify)").fill("impact two frames later")
                page.get_by_role("button", name="Save review decision").click()
                page.locator("code", has_text="MODIFIED_BY_REVIEWER").first.wait_for(timeout=30000)
                page.get_by_role("radiogroup", name="Decision applies to").get_by_text("Contribution assessment", exact=True).click()
                page.get_by_role("radiogroup", name="Decision", exact=True).get_by_text("Confirm", exact=True).click()
                page.get_by_role("button", name="Save review decision").click()
                page.locator("code", has_text="CONFIRMED_BY_REVIEWER").first.wait_for(timeout=30000)
                assert page.locator("[data-testid='stException']").count() == 0
                assert errors == []
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
    assert sorted(path.name for path in (run / "reviews").glob("revision-*")) == ["revision-001", "revision-002"]
    view = effective_view(run, "revision-002")
    assert view.events[0].status == "MODIFIED_BY_REVIEWER" and abs(view.events[0].peak_time - (event.peak_time + 0.08)) < 0.006
    assert view.assessments[0].status == "CONFIRMED_BY_REVIEWER"
    assert view.events[0].decision is not None and view.events[0].decision.reviewer == "Browser Reviewer"
    assert sha256_file(run / "events.jsonl") == events_hash, "the original proposals are never rewritten"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
