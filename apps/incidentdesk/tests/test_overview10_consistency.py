"""overview-10 consistency checks, contact evidence contract and Report rendering (spec 003 ticket 3, card 1b + 5)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import make_view
from incident_report.agreement import AgreementSettings
from incident_report.ai import AIRequest, AIViewRef
from incident_report.ai.dense import consolidate_facts
from incident_report.ai.gemini import GeminiSettings, overview_request_hash, parse_overview
from incident_report.ai.overview_replay import ReplayOverviewAIConnection
from incident_report.domain import Origin, Report, Role
from incident_report.entry import analyse_incident
from incident_report.facts import CitedFact, OverviewFacts
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.model_calls import ModelCall
from incident_report.rules import evaluate_fault
from incident_report.rules.consistency import CONTACT_MATCH_SECONDS, ConsistencyIssue, check_consistency
from incident_report.rules.models import FaultStatus
from incident_report.runlog import RunLog
from incident_report.web.templating import build_environment
from overview10_helpers import VIEW, answer, assessment, contact, parse, row
from test_invalid_report_rendering import make_report
from test_live_gemini import PUBLIC_VIDEO

DURATION = 30.0
REPORT_VIEW = "aa" * 32


def issues_of(facts: OverviewFacts) -> tuple[ConsistencyIssue, ...]:
    return check_consistency(facts, {facts.parties[0].view_id: DURATION})


def codes_of(facts: OverviewFacts) -> set[str]:
    return {i.code for i in issues_of(facts)}


def zone(value: str) -> dict[str, Any]:
    return {
        "subject": "scene",
        "field": "conflict_zone_type",
        "value": value,
        "other_party": "",
        "timestamp_seconds": 1.0,
        "confidence": 0.9,
    }


def test_the_default_answer_raises_no_consistency_issue() -> None:
    assert issues_of(parse(answer())) == ()


def test_match_limit_is_one_second() -> None:
    assert CONTACT_MATCH_SECONDS == 1.0


# --- section 5: checklist checks, each with a positive and a negative test ---------------------------------

CHECKLIST_CASES = [
    # (name, row overrides for P1, extra answer overrides, expected code or None)
    (
        "side_road_straight",
        {"manoeuvre": "into_side_road", "turn_direction": "straight"},
        {},
        "checklist_turn_direction",
    ),
    ("side_road_left", {"manoeuvre": "into_side_road", "turn_direction": "left"}, {}, None),
    (
        "lane_change_straight",
        {"manoeuvre": "lane_change", "turn_direction": "straight"},
        {},
        "checklist_turn_direction",
    ),
    ("lane_change_left", {"manoeuvre": "lane_change", "turn_direction": "left"}, {}, None),
    ("merging_straight", {"manoeuvre": "merging", "turn_direction": "straight"}, {}, "checklist_turn_direction"),
    ("merging_right", {"manoeuvre": "merging", "turn_direction": "right"}, {}, None),
    (
        "circulating_in_signalised_junction",
        {
            "manoeuvre": "circulating",
            "road_class_on_approach": "roundabout",
            "road_class_basis": "roundabout_carriageway",
        },
        {"structured_facts": [zone("signalised_junction")]},
        "checklist_roundabout_zone",
    ),
    (
        "circulating_in_roundabout",
        {
            "manoeuvre": "circulating",
            "road_class_on_approach": "roundabout",
            "road_class_basis": "roundabout_carriageway",
        },
        {"structured_facts": [zone("roundabout_unsignalised")]},
        None,
    ),
    (
        "circulating_zone_unknown",
        {
            "manoeuvre": "circulating",
            "road_class_on_approach": "roundabout",
            "road_class_basis": "roundabout_carriageway",
        },
        {"structured_facts": [zone("unknown")]},
        None,
    ),
    (
        "entry_zone_other_is_harmless",
        {"manoeuvre": "roundabout_entry"},
        {"structured_facts": [zone("other")]},
        None,
    ),
    (
        "entry_in_mid_block",
        {"manoeuvre": "roundabout_entry"},
        {"structured_facts": [zone("mid_block")]},
        "checklist_roundabout_zone",
    ),
    (
        "exit_in_mid_block",
        {"manoeuvre": "roundabout_exit", "roundabout_exit_position": "unknown"},
        {"structured_facts": [zone("mid_block")]},
        "checklist_roundabout_zone",
    ),
    ("circulating_on_main_road", {"manoeuvre": "circulating"}, {}, "checklist_circulating_road_class"),
    (
        "circulating_road_class_unknown",
        {"manoeuvre": "circulating", "road_class_on_approach": "unknown", "road_class_basis": "unknown"},
        {},
        None,
    ),
    (
        "exit_position_without_exit",
        {"roundabout_exit_position": "on_exit_curve_before_straight_lane"},
        {},
        "checklist_exit_position",
    ),
    (
        "straight_lane_without_exit",
        {"roundabout_exit_position": "reached_straight_lane_of_exit_road"},
        {},
        "checklist_exit_position",
    ),
    ("exit_position_unknown_is_harmless", {"roundabout_exit_position": "unknown"}, {}, None),
    (
        "exit_not_applicable",
        {"manoeuvre": "roundabout_exit", "roundabout_exit_position": "not_applicable"},
        {},
        "checklist_exit_position",
    ),
    (
        "exit_with_position",
        {"manoeuvre": "roundabout_exit", "roundabout_exit_position": "on_exit_curve_before_straight_lane"},
        {},
        None,
    ),
    (
        "main_road_wrong_basis",
        {"road_class_on_approach": "main_road", "road_class_basis": "two_lanes_or_fewer"},
        {},
        "checklist_road_class_basis",
    ),
    (
        "main_road_right_basis",
        {"road_class_on_approach": "main_road", "road_class_basis": "more_than_two_lanes_and_long"},
        {},
        None,
    ),
    (
        "side_road_long_basis",
        {"road_class_on_approach": "side_road", "road_class_basis": "more_than_two_lanes_and_long"},
        {},
        "checklist_road_class_basis",
    ),
    (
        "side_road_short_basis",
        {"road_class_on_approach": "side_road", "road_class_basis": "short_or_ends_at_junction"},
        {},
        None,
    ),
    (
        "roundabout_wrong_basis",
        {"road_class_on_approach": "roundabout", "road_class_basis": "two_lanes_or_fewer"},
        {},
        "checklist_road_class_basis",
    ),
    ("roundabout_basis_unknown", {"road_class_on_approach": "roundabout", "road_class_basis": "unknown"}, {}, None),
    (
        "roundabout_right_basis",
        {"road_class_on_approach": "roundabout", "road_class_basis": "roundabout_carriageway"},
        {},
        None,
    ),
    ("car_crossing_on_foot", {"manoeuvre": "crossing_on_foot"}, {}, "checklist_pedestrian"),
    (
        "pedestrian_crossing_on_foot",
        {"manoeuvre": "crossing_on_foot", "party_type": "pedestrian", "colour_family": "unknown"},
        {},
        None,
    ),
    (
        "pedestrian_turning_into_side_road",
        {
            "manoeuvre": "into_side_road",
            "turn_direction": "left",
            "party_type": "pedestrian",
            "colour_family": "unknown",
        },
        {},
        "checklist_pedestrian",
    ),
    ("motorcycle_taxi", {"party_type": "motorcycle", "marking": "taxi"}, {}, "checklist_taxi_marking"),
    ("bicycle_taxi", {"party_type": "bicycle", "marking": "taxi"}, {}, "checklist_taxi_marking"),
    ("sedan_taxi", {"party_type": "sedan", "marking": "taxi"}, {}, None),
    (
        "movement_before_first_seen",
        {"first_seen_seconds": 2.0, "attributes_seen_at_seconds": 2.0, "movement_seen_at_seconds": 1.0},
        {},
        "checklist_time_order",
    ),
    (
        "attributes_before_first_seen",
        {"first_seen_seconds": 2.0, "attributes_seen_at_seconds": 1.5, "movement_seen_at_seconds": 2.5},
        {},
        "checklist_time_order",
    ),
    (
        "times_in_order",
        {"first_seen_seconds": 1.0, "attributes_seen_at_seconds": 1.0, "movement_seen_at_seconds": 1.0},
        {},
        None,
    ),
    ("movement_after_first_contact", {"movement_seen_at_seconds": 3.5}, {}, "checklist_movement_after_contact"),
    ("movement_at_first_contact", {"movement_seen_at_seconds": 3.0}, {}, None),
    (
        "first_seen_outside_view",
        {"first_seen_seconds": 31.0, "attributes_seen_at_seconds": 31.0, "movement_seen_at_seconds": 31.0},
        {},
        "checklist_time_outside_view",
    ),
    ("movement_at_view_end", {"movement_seen_at_seconds": 30.0, "first_seen_seconds": 1.0}, {"contacts": []}, None),
]


@pytest.mark.parametrize(
    ("name", "overrides", "extra", "expected"), CHECKLIST_CASES, ids=[c[0] for c in CHECKLIST_CASES]
)
def test_checklist_check(name: str, overrides: dict[str, Any], extra: dict[str, Any], expected: str | None) -> None:
    contacts = extra.pop("contacts", None)
    first = row("P1", **overrides)
    facts = parse(
        answer(rows=[first, row("P2", colour_family="red", party_type="suv")], contacts=contacts, **extra)
        if contacts is not None
        else answer(rows=[first, row("P2", colour_family="red", party_type="suv")], **extra)
    )
    found = {i.code: i for i in issues_of(facts) if i.code.startswith(("checklist_", "note_"))}
    if expected is None:
        assert not found
    else:
        assert expected in found
        assert found[expected].subject == facts.party_checklist[0].display_name
        assert found[expected].cited_facts


def test_a_checklist_issue_makes_the_named_party_cannot_determine_through_the_existing_path() -> None:
    facts = parse(
        answer(
            rows=[
                row("P1", manoeuvre="lane_change", turn_direction="straight"),
                row("P2", colour_family="red", party_type="suv"),
            ]
        )
    )
    issues = issues_of(facts)
    faults = {f.party_description: f for f in evaluate_fault(facts, consistency_issues=issues)}
    assert faults["P1 silver sedan"].status is FaultStatus.CANNOT_DETERMINE
    assert faults["P1 silver sedan"].reason.startswith("Cannot Determine: inconsistent typed answers.")
    assert "P2 red SUV" in faults


def test_note_time_outside_the_view_is_an_issue_for_its_party() -> None:
    facts = parse(answer(observation_notes=[{"party_code": "P2", "timestamp_seconds": 31.0, "note": "late"}]))
    [issue] = [i for i in issues_of(facts) if i.code == "note_time_outside_view"]
    assert issue.subject == "P2 red SUV"
    inside = parse(answer(observation_notes=[{"party_code": "scene", "timestamp_seconds": 30.0, "note": "ok"}]))
    assert not [i for i in issues_of(inside) if i.code == "note_time_outside_view"]
    scene = parse(answer(observation_notes=[{"party_code": "scene", "timestamp_seconds": 30.5, "note": "late"}]))
    assert [i.subject for i in issues_of(scene) if i.code == "note_time_outside_view"] == ["scene"]


# --- section 1b C: contact_state ----------------------------------------------------------------------------


def with_interval(start: float, end: float, state: str = "contact_observed", at: float = 3.0) -> OverviewFacts:
    return parse(
        answer(
            contacts=[contact(at=at)],
            incident_assessment=assessment(
                contact_state=state, contact_interval_start_seconds=start, contact_interval_end_seconds=end
            ),
        )
    )


@pytest.mark.parametrize("state", ["contact_observed", "contact_possible_obscured"])
@pytest.mark.parametrize(
    ("start", "end", "ok"),
    [
        (2.5, 3.5, True),
        (4.0, 5.0, True),  # contact exactly 1.0 s before the interval start
        (4.01, 5.0, False),  # 1.01 s
        (1.0, 2.0, True),  # contact exactly 1.0 s after the interval end
        (1.0, 1.99, False),
    ],
)
def test_contact_states_need_a_typed_contact_inside_the_interval_plus_or_minus_the_limit(
    state: str, start: float, end: float, ok: bool
) -> None:
    found = [i for i in issues_of(with_interval(start, end, state)) if i.code == "assessment_contact_missing"]
    assert bool(found) is (not ok)
    for issue in found:
        assert issue.subject == "scene" and issue.fields == ("incident_assessment", "collision_observations")


def test_no_contact_observed_requires_no_typed_contact() -> None:
    quiet = parse(
        answer(
            contacts=[],
            incident_assessment=assessment(
                contact_state="no_contact_observed",
                contact_interval_start_seconds=None,
                contact_interval_end_seconds=None,
            ),
        )
    )
    assert "assessment_contact_unexpected" not in codes_of(quiet)
    contradicting = parse(
        answer(
            incident_assessment=assessment(
                contact_state="no_contact_observed",
                contact_interval_start_seconds=None,
                contact_interval_end_seconds=None,
            )
        )
    )
    [issue] = [i for i in issues_of(contradicting) if i.code == "assessment_contact_unexpected"]
    assert issue.subject == "scene"


def test_cannot_assess_has_no_requirement_either_way() -> None:
    for contacts in ([], [contact()]):
        facts = parse(
            answer(
                contacts=contacts,
                incident_assessment=assessment(
                    contact_state="cannot_assess",
                    contact_interval_start_seconds=None,
                    contact_interval_end_seconds=None,
                ),
            )
        )
        assert not {c for c in codes_of(facts) if c.startswith("assessment_")}


def test_assessment_interval_outside_the_view_is_an_issue() -> None:
    facts = with_interval(29.0, 31.0, at=30.0)
    assert "assessment_time_outside_view" in codes_of(facts)
    assert "assessment_time_outside_view" not in codes_of(with_interval(29.0, 30.0, at=30.0))


# --- aftermath is not contact -------------------------------------------------------------------------------


def aftermath(kind: str, subject: str, at: float) -> dict[str, Any]:
    return contact(subject, "", at=at, kind=kind)


def test_aftermath_two_seconds_later_is_fine_and_never_counted_as_a_contact() -> None:
    # MAJOR_06-shaped: the contact at 3.0 s, a car seen stopped at the scene at +3 s.
    facts = parse(answer(contacts=[contact(), aftermath("vehicle_stopped_at_scene", "P1", 6.0)]))
    assert not {c for c in codes_of(facts) if c.startswith("aftermath_")}
    assert len(facts.candidate_collisions) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "vehicle_stopped_at_scene",
        "vehicle_driven_away",
        "vehicle_left_scene",
        "no_injury_visible",
        "person_injured_visible",
    ],
)
def test_aftermath_for_a_party_in_no_contact_is_an_issue_naming_that_subject_only(kind: str) -> None:
    facts = parse(
        answer(
            rows=[
                row("P1"),
                row("P2", colour_family="red", party_type="suv"),
                row("P3", colour_family="blue", party_type="van"),
            ],
            contacts=[contact(), aftermath(kind, "P3", 6.0)],
        )
    )
    found = [i for i in issues_of(facts) if i.code == "aftermath_without_contact"]
    assert [i.subject for i in found] == ["P3 blue van"]


@pytest.mark.parametrize(("at", "flagged"), [(2.5, False), (2.0, False), (1.99, True)])
def test_aftermath_may_precede_the_contact_by_at_most_the_limit(at: float, flagged: bool) -> None:
    facts = parse(answer(contacts=[contact(), aftermath("vehicle_stopped_at_scene", "P1", at)]))
    found = [i for i in issues_of(facts) if i.code == "aftermath_before_contact"]
    assert bool(found) is flagged
    assert all(i.subject == "P1 silver sedan" for i in found)


def test_person_dead_visible_needs_no_contact_or_time_order() -> None:
    facts = parse(answer(contacts=[contact(), aftermath("person_dead_visible", "P2", 0.5)]))
    assert not {c for c in codes_of(facts) if c.startswith("aftermath_")}


def test_an_overturn_after_the_same_partys_contact_is_a_contact_of_its_own() -> None:
    facts = parse(answer(contacts=[contact(), contact("P1", "", at=4.5, kind="vehicle_overturned")]))
    assert not {c for c in codes_of(facts) if c.startswith("aftermath_") or c.startswith("assessment_")}
    assert len(facts.candidate_collisions) == 2


# --- the camera vehicle in a contact ------------------------------------------------------------------------


def camera_answer(role: str, *, camera_row: bool = True, other: str = "P1") -> OverviewFacts:
    rows = [row("P1", colour_family="red")]
    if camera_row:
        rows.append(row("CAM"))
    return parse(
        answer(
            rows=rows,
            contacts=[contact(other, "CAM")],
            observation_notes=[],
            incident_assessment=assessment(camera_role=role),
        )
    )


@pytest.mark.parametrize("role", ["fixed_camera", "witness_vehicle_not_involved"])
def test_camera_in_a_contact_is_an_issue_for_the_camera_and_the_other_party(role: str) -> None:
    # video3-shaped: a witness dashcam whose model put the camera car into the crash.
    facts = camera_answer(role)
    found = [i for i in issues_of(facts) if i.code == "camera_in_contact"]
    assert {i.subject for i in found} == {"camera vehicle (silver sedan)", "P1 red sedan"}
    assert all(i.fields == ("incident_assessment", "collision_observations") for i in found)


@pytest.mark.parametrize("role", ["camera_vehicle_involved", "interior_view", "unknown"])
def test_camera_in_a_contact_is_allowed_for_involved_interior_and_unknown(role: str) -> None:
    assert "camera_in_contact" not in codes_of(camera_answer(role))


def test_a_camera_row_with_a_fixed_camera_is_an_issue() -> None:
    facts = parse(
        answer(
            rows=[row("P1"), row("P2", colour_family="red"), row("CAM")],
            incident_assessment=assessment(camera_role="fixed_camera"),
        )
    )
    [issue] = [i for i in issues_of(facts) if i.code == "camera_listed_for_fixed_camera"]
    assert issue.subject == "camera vehicle (silver sedan)"
    ok = parse(
        answer(
            rows=[row("P1"), row("P2", colour_family="red"), row("CAM")],
            incident_assessment=assessment(camera_role="witness_vehicle_not_involved"),
        )
    )
    assert "camera_listed_for_fixed_camera" not in codes_of(ok)


# --- scope: only overview-10 views, never old versions or dense output --------------------------------------


def test_an_overview_9_payload_raises_no_1b_issue() -> None:
    stored = {
        "parties": [
            {"description": "silver sedan", "timestamp_seconds": 1.0, "confidence": 0.9},
            {"description": "red SUV", "timestamp_seconds": 1.0, "confidence": 0.9},
        ],
        "events": [],
        "candidate_collisions": [
            {"description": "silver sedan hits red SUV", "timestamp_seconds": 3.0, "confidence": 0.9}
        ],
        "behaviour_observations": [],
        "location_conditions": [],
        "structured_facts": [],
        "text_readings": [],
        "driver_observations": [],
        "collision_observations": [
            {
                "kind": "vehicle_stopped_at_scene",
                "subject": "silver sedan",
                "other_party": "",
                "injury_grade": "unknown",
                "timestamp_seconds": 6.0,
                "confidence": 0.9,
            }
        ],
        "drowsiness_observations": [],
    }
    facts = parse_overview(json.dumps(stored), VIEW, prompt_version="overview-9", view_role="exterior")
    assert check_consistency(facts, {VIEW: DURATION}) == ()


def test_a_dense_added_candidate_raises_no_1b_issue() -> None:
    facts = parse(answer())
    dense = OverviewFacts(
        parties=[],
        events=[],
        candidate_collisions=[
            CitedFact(description="dense contact", view_id=VIEW, timestamp_seconds=20.0, confidence=0.9)
        ],
        behaviour_observations=[],
        location_conditions=[],
    )
    merged = consolidate_facts(facts, [dense])
    assert any(c.timestamp_seconds == 20.0 for c in merged.candidate_collisions)
    assert not {c for c in codes_of(merged) if c.startswith(("assessment_", "aftermath_", "camera_"))}


# --- incident_status and Report rendering -------------------------------------------------------------------


def report_for(facts: OverviewFacts) -> Report:
    return make_report(is_valid=True, summary="s").model_copy(update={"facts": facts, "prompt_version": "overview-10"})


def facts_on_report_view(model_answer: dict[str, Any]) -> OverviewFacts:
    return parse(model_answer, view=REPORT_VIEW)


def test_contact_claimed_but_no_candidate_is_cannot_determine_not_no_incident() -> None:
    for state in ("contact_observed", "contact_possible_obscured"):
        facts = facts_on_report_view(answer(incident_assessment=assessment(contact_state=state)))
        orphan = facts.model_copy(update={"candidate_collisions": []})
        assert report_for(orphan).incident_status == "Cannot Determine"
    # No rows, notes, events or contacts: without the cannot_assess rule this would read No Incident.
    cannot = facts_on_report_view(
        answer(
            rows=[],
            observation_notes=[],
            events=[],
            contacts=[],
            incident_assessment=assessment(
                contact_state="cannot_assess", contact_interval_start_seconds=None, contact_interval_end_seconds=None
            ),
        )
    )
    assert report_for(cannot).incident_status == "Cannot Determine"


def test_no_contact_observed_with_no_candidate_stays_no_incident() -> None:
    facts = facts_on_report_view(
        answer(
            rows=[],
            observation_notes=[],
            events=[],
            contacts=[],
            incident_assessment=assessment(
                contact_state="no_contact_observed",
                contact_interval_start_seconds=None,
                contact_interval_end_seconds=None,
            ),
        )
    )
    assert report_for(facts).incident_status == "No Incident"


def test_header_stays_collision_when_the_only_contact_is_obscured() -> None:
    facts = facts_on_report_view(
        answer(
            contacts=[contact(visibility="possible_obscured")],
            incident_assessment=assessment(contact_state="contact_possible_obscured"),
        )
    )
    assert report_for(facts).incident_status == "Collision"


def test_an_unrelated_cannot_assess_view_does_not_hide_a_collision_elsewhere() -> None:
    facts = facts_on_report_view(answer())
    other = facts.incident_assessments[0].model_copy(
        update={
            "view_id": "bb" * 32,
            "contact_state": "cannot_assess",
            "contact_interval_start_seconds": None,
            "contact_interval_end_seconds": None,
        }
    )
    mixed = facts.model_copy(update={"incident_assessments": [*facts.incident_assessments, other]})
    assert report_for(mixed).incident_status == "Collision"


def render(facts: OverviewFacts) -> str:
    return build_environment().get_template("report.html").render(report=report_for(facts), review_workspace=False)


def test_notes_render_escaped_in_the_observation_section_and_never_in_the_fault_section() -> None:
    facts = facts_on_report_view(
        answer(
            observation_notes=[
                {"party_code": "P1", "timestamp_seconds": 1.0, "note": "<script>alert(1)</script> P1 turns"},
            ]
        )
    )
    html = render(facts)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; P1 silver sedan turns" in html
    heading = html.index("What the model saw (observations, not findings)")
    assert html.index('data-report-section="location"') < heading < html.index('data-report-section="uncertainty"')
    # owner approval 2026-10-26: the note text may also appear in the "What happened" storyline; it is still
    # required under "What the model saw", so the search starts at that heading.
    note_at = html.index("&lt;script&gt;", heading)
    assert note_at > heading
    parties_at, timeline_at = (
        html.index('aria-label="Parties"') if 'aria-label="Parties"' in html else -1,
        html.index('data-report-section="timeline"'),
    )
    assert not (parties_at != -1 and parties_at < note_at < timeline_at)
    assert "owner's working definitions (spec 003 decisions 9-10), not legal text" in html


def test_obscured_contact_is_shown_as_possible_and_cited() -> None:
    facts = facts_on_report_view(
        answer(
            contacts=[contact(visibility="possible_obscured")],
            incident_assessment=assessment(contact_state="contact_possible_obscured"),
        )
    )
    html = render(facts)
    assert (
        "Contact possible but not clearly visible, 2.5&ndash;3.5 s" in html
        or "Contact possible but not clearly visible, 2.5–3.5 s" in html
    )
    assert f"{REPORT_VIEW} @ 2.5s" in html
    assert "touch possible, not clearly visible" in html
    assert 'data-incident-status="Collision"' in html


def test_a_clear_contact_shows_no_obscured_line() -> None:
    assert "Contact possible but not clearly visible" not in render(facts_on_report_view(answer()))


def test_replayed_overview_10_run_gives_a_valid_report_that_survives_the_run_log(run_log: RunLog) -> None:
    """The consumer on the producer's real output: Gemini-shaped JSON -> analyse_incident -> stored Report."""
    digest = sha256_hex(PUBLIC_VIDEO)
    request = AIRequest(
        incident_input_hash([(Role.EXTERIOR, digest)]), "overview-10", (AIViewRef(Role.EXTERIOR, digest),)
    )
    settings = GeminiSettings(model="gemini-3.8-flash")
    shaped = answer(
        rows=[
            row("P1", first_seen_seconds=0.0, attributes_seen_at_seconds=0.0, movement_seen_at_seconds=0.1),
            row(
                "P2",
                colour_family="red",
                party_type="suv",
                first_seen_seconds=0.0,
                attributes_seen_at_seconds=0.0,
                movement_seen_at_seconds=0.1,
            ),
        ],
        contacts=[contact(at=0.4)],
        observation_notes=[{"party_code": "P1", "timestamp_seconds": 0.1, "note": "P1 drives straight."}],
        incident_assessment=assessment(contact_interval_start_seconds=0.3, contact_interval_end_seconds=0.5),
        events=[{"description": "P1 hits P2", "timestamp_seconds": 0.4, "confidence": 0.9}],
    )
    call = ModelCall(
        request_hash=overview_request_hash(request, settings),
        input_hash=request.input_hash,
        prompt_version="overview-10",
        model=settings.model,
        view_id=digest,
        fps=settings.fps,
        raw_response=json.dumps(shaped),
        latency_ms=0,
        retry_index=0,
    )
    report = analyse_incident(
        [make_view(PUBLIC_VIDEO)],
        Origin.REPLAY,
        ai=ReplayOverviewAIConnection(settings, (call,)),
        run_log=run_log,
        prompt_version="overview-10",
        agreement_settings=AgreementSettings(n_runs=1),
    )
    assert report.is_valid, report.validation_errors
    assert report.facts is not None
    assert [p.description for p in report.facts.parties] == ["P1 silver sedan", "P2 red SUV"]
    assert report.incident_status == "Collision"
    assert [i for i in report.consistency_issues] == []
    stored = run_log.get(report.run_id)
    assert stored.report is not None and stored.report.facts is not None
    assert stored.report.facts.incident_assessments == report.facts.incident_assessments
    assert stored.report.facts.party_checklist == report.facts.party_checklist
    assert stored.report.facts.observation_notes[0].note == "P1 silver sedan drives straight."
    html = build_environment().get_template("report.html").render(report=stored.report, review_workspace=False)
    assert "What the model saw (observations, not findings)" in html


def test_guardrail_accepts_built_display_names_but_still_flags_any_other_code() -> None:
    from incident_report.guardrails import validate_report

    clean = validate_report(report_for(facts_on_report_view(answer())))
    assert clean.guardrail_results["raw_ids"] == ()
    stray = validate_report(
        report_for(
            facts_on_report_view(
                answer(events=[{"description": "P7 appears at the kerb", "timestamp_seconds": 1.0, "confidence": 0.9}])
            )
        )
    )
    assert stray.guardrail_results["raw_ids"]
    assert any("events" in reason for reason in stray.guardrail_results["raw_ids"])


def test_two_views_keep_one_assessment_each_and_their_own_codes_through_the_merge(run_log: RunLog) -> None:
    from video_fixtures import synthetic_video

    clips = [synthetic_video(b"view one overview ten"), synthetic_video(b"view two overview ten")]
    settings = GeminiSettings(model="gemini-3.8-flash")
    calls = []
    digests = []
    for index, clip in enumerate(clips):
        digest = sha256_hex(clip)
        digests.append(digest)
        request = AIRequest(
            incident_input_hash([(Role.EXTERIOR, digest)]), "overview-10", (AIViewRef(Role.EXTERIOR, digest),)
        )
        colours = ("silver", "blue") if index == 0 else ("white", "black")
        shaped = answer(
            rows=[
                row(
                    "P1",
                    colour_family=colours[0],
                    first_seen_seconds=0.0,
                    attributes_seen_at_seconds=0.0,
                    movement_seen_at_seconds=0.1,
                ),
                row(
                    "P2",
                    colour_family=colours[1],
                    party_type="suv",
                    first_seen_seconds=0.0,
                    attributes_seen_at_seconds=0.0,
                    movement_seen_at_seconds=0.1,
                ),
            ],
            contacts=[contact(at=0.4)],
            observation_notes=[{"party_code": "P1", "timestamp_seconds": 0.1, "note": f"view {index} note about P1"}],
            incident_assessment=assessment(contact_interval_start_seconds=0.3, contact_interval_end_seconds=0.5),
            events=[],
        )
        calls.append(
            ModelCall(
                request_hash=overview_request_hash(request, settings),
                input_hash=request.input_hash,
                prompt_version="overview-10",
                model=settings.model,
                view_id=digest,
                fps=settings.fps,
                raw_response=json.dumps(shaped),
                latency_ms=0,
                retry_index=0,
            )
        )
    report = analyse_incident(
        [make_view(c, filename=f"view{i}.mp4") for i, c in enumerate(clips)],
        Origin.REPLAY,
        ai=ReplayOverviewAIConnection(settings, tuple(calls)),
        run_log=run_log,
        prompt_version="overview-10",
        agreement_settings=AgreementSettings(n_runs=1),
    )
    assert report.facts is not None, report.validation_errors
    assert sorted(a.view_id for a in report.facts.incident_assessments) == sorted(digests)
    assert len(report.facts.party_checklist) == 4 and len(report.facts.observation_notes) == 2
    assert len(report.facts.candidate_collisions) == 2
    assert {p.party_code for p in report.facts.parties} == {"P1", "P2"}
    assert len({p.description for p in report.facts.parties}) == 4
    stored = run_log.get(report.run_id).report
    assert stored is not None and stored.facts is not None
    assert stored.facts.incident_assessments == report.facts.incident_assessments


@pytest.mark.parametrize(
    "overrides",
    [{"party_type": "other", "party_type_other": "vehicle 2"}, {"colour_family": "other", "colour_other": "v3"}],
)
def test_guardrail_still_scans_model_words_inside_a_display_name(overrides: dict[str, Any]) -> None:
    from incident_report.guardrails import validate_report

    facts = facts_on_report_view(
        answer(rows=[row("P1", **overrides), row("P2", colour_family="red", party_type="suv")])
    )
    raw = validate_report(report_for(facts)).guardrail_results["raw_ids"]
    # The built name itself (party description) must be flagged, not only the raw *_other field.
    assert any("parties[0].description" in reason for reason in raw)
