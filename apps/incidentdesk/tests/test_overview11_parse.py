"""overview-11 answers: the detail-window times are rebased onto the full clip once, in code, before any other check
(spec 003 ticket 8, card SINGLE-REQUEST-MULTIRES sections 2 and 3)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from incident_report.ai import overview11 as o11
from incident_report.ai.errors import AIConnectionError
from incident_report.facts import OverviewFacts
from incident_report.rules.consistency import check_consistency
from overview10_helpers import assessment, parse
from overview11_helpers import VIEW, answer11, assessment11, contact11, negative11, parse11, row

DURATION = 45.0
WINDOW = (30.0, 34.0)  # a window handed to the builder (its start is 30.0, as in video3); not a placement rule


def codes(facts: OverviewFacts) -> set[str]:
    return {i.code for i in check_consistency(facts, {VIEW: DURATION})}


# --- rebasing: full time = window start + part time --------------------------------------------------------------


def test_a_detail_window_contact_at_part_time_1_5_is_stored_at_31_5() -> None:
    facts = parse11(
        answer11(
            contacts=[contact11("detail_window", at=1.5)],
            incident_assessment=assessment11(
                "detail_window", contact_interval_start_seconds=1.0, contact_interval_end_seconds=2.0
            ),
        ),
        window=WINDOW,
    )
    (contact,) = facts.collision_observations
    assert contact.timestamp_seconds == pytest.approx(31.5)
    assert contact.part_time_seconds == 1.5
    assert contact.source_part == "detail_window"
    (stored,) = facts.incident_assessments
    assert stored.contact_interval_start_seconds == pytest.approx(31.0)
    assert stored.contact_interval_end_seconds == pytest.approx(32.0)
    assert stored.contact_interval_part == "detail_window"
    # the candidate collision the rules read carries the full-clip time too
    assert facts.candidate_collisions[0].timestamp_seconds == pytest.approx(31.5)


def test_a_part_time_that_is_really_the_full_clip_time_is_rejected() -> None:
    # 31.5 on a 4 s window: the model used the full-clip clock for a detail_window item. Never guess, never clamp.
    bad = answer11(
        contacts=[contact11("detail_window", at=31.5)],
        incident_assessment=assessment11(
            "detail_window", contact_interval_start_seconds=1.0, contact_interval_end_seconds=2.0
        ),
    )
    with pytest.raises(AIConnectionError):
        parse11(bad, window=WINDOW)


def test_the_error_names_the_field_and_the_part() -> None:
    with pytest.raises(ValueError, match=r"collision_observations timestamp_seconds 31\.5 s is outside detail_window"):
        o11.build_overview_11_data(
            answer11(contacts=[contact11("detail_window", at=31.5)]), VIEW, "exterior", detail_window=WINDOW
        )
    with pytest.raises(ValueError, match=r"contact_interval_end_seconds 31\.5 s is outside detail_window"):
        o11.build_overview_11_data(
            answer11(
                incident_assessment=assessment11(
                    "detail_window", contact_interval_start_seconds=1.0, contact_interval_end_seconds=31.5
                )
            ),
            VIEW,
            "exterior",
            detail_window=WINDOW,
        )


@pytest.mark.parametrize("at, ok", [(0.0, True), (4.0, True), (4.5, True), (4.51, False), (-0.1, False)])
def test_the_part_time_limit_is_the_window_length_plus_half_a_second(at: float, ok: bool) -> None:
    candidate = answer11(
        contacts=[contact11("detail_window", at=at)],
        incident_assessment=assessment11(
            "whole_clip", contact_interval_start_seconds=32.0, contact_interval_end_seconds=33.0
        ),
    )
    if ok:
        assert parse11(candidate, window=WINDOW).collision_observations[0].timestamp_seconds == pytest.approx(30.0 + at)
    else:
        with pytest.raises(AIConnectionError):
            parse11(candidate, window=WINDOW)


def test_the_rebase_check_runs_before_any_other_check() -> None:
    # The answer also has an unknown party code, which the shared builder would report; the time error comes first.
    bad = answer11(
        contacts=[contact11("detail_window", at=40.0, subject="P9")],
        observation_notes=[{"party_code": "P9", "timestamp_seconds": 1.0, "note": "x"}],
    )
    with pytest.raises(ValueError, match="outside detail_window"):
        o11.build_overview_11_data(bad, VIEW, "exterior", detail_window=WINDOW)


def test_whole_clip_times_are_used_as_given() -> None:
    facts = parse11(answer11(contacts=[contact11("whole_clip", at=3.0)]), window=WINDOW)
    (contact,) = facts.collision_observations
    assert contact.timestamp_seconds == 3.0 and contact.part_time_seconds == 3.0 and contact.source_part == "whole_clip"
    (stored,) = facts.incident_assessments
    assert (stored.contact_interval_start_seconds, stored.contact_interval_end_seconds) == (2.5, 3.5)


def test_every_other_time_is_a_full_clip_time_and_untouched() -> None:
    facts = parse11(
        answer11(
            contacts=[contact11("detail_window", at=1.5)],
            events=[{"description": "x", "timestamp_seconds": 12.0, "confidence": 0.9}],
        ),
        window=WINDOW,
    )
    assert facts.events[0].timestamp_seconds == 12.0
    assert facts.party_checklist[0].first_seen_seconds == 1.0


def test_the_rebase_is_applied_once() -> None:
    first = o11.build_overview_11_data(
        answer11(contacts=[contact11("detail_window", at=1.5)]), VIEW, "exterior", detail_window=WINDOW
    )
    assert first["collision_observations"][0]["timestamp_seconds"] == pytest.approx(31.5)
    assert first["collision_observations"][0]["part_time_seconds"] == 1.5


def test_a_detail_window_answer_needs_a_detail_window() -> None:
    with pytest.raises(AIConnectionError):
        parse11(answer11(contacts=[contact11("detail_window", at=1.0)]), window=None)


def test_source_part_is_required_and_closed() -> None:
    missing = answer11(contacts=[{k: v for k, v in contact11().items() if k != "source_part"}])
    with pytest.raises(AIConnectionError):
        parse11(missing, window=WINDOW)
    with pytest.raises(AIConnectionError):
        parse11(answer11(contacts=[contact11("both")]), window=WINDOW)


def test_the_model_may_not_supply_the_part_time_itself() -> None:
    with pytest.raises(AIConnectionError):
        parse11(answer11(contacts=[{**contact11(), "part_time_seconds": 1.0}]), window=WINDOW)


# --- interval part rule ----------------------------------------------------------------------------------------------


def test_a_negative_answer_has_a_null_part_and_null_interval() -> None:
    facts = parse11(negative11(), window=WINDOW)
    (stored,) = facts.incident_assessments
    assert stored.contact_interval_part is None and stored.contact_interval_start_seconds is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"contact_interval_part": None},  # a contact state with an interval but no part
        {"contact_state": "no_contact_observed"},  # part and interval given for a negative state
    ],
)
def test_the_part_and_interval_are_null_if_and_only_if_no_contact(overrides: dict[str, Any]) -> None:
    bad = answer11(incident_assessment={**assessment11(), **overrides})
    with pytest.raises(AIConnectionError):
        parse11(bad, window=WINDOW)


def test_a_part_without_an_interval_is_rejected() -> None:
    bad = negative11()
    bad["incident_assessment"]["contact_interval_part"] = "whole_clip"
    with pytest.raises(AIConnectionError):
        parse11(bad, window=WINDOW)


def test_a_missing_interval_part_key_is_rejected() -> None:
    bad = answer11()
    del bad["incident_assessment"]["contact_interval_part"]
    with pytest.raises(AIConnectionError):
        parse11(bad, window=WINDOW)


def test_cannot_assess_is_negative_too() -> None:
    answer = negative11()
    answer["incident_assessment"]["contact_state"] = "cannot_assess"
    assert parse11(answer, window=WINDOW).incident_assessments[0].contact_state == "cannot_assess"


# --- the 1b rules run after rebasing ---------------------------------------------------------------------------------


def test_the_assessment_contact_match_uses_rebased_times() -> None:
    facts = parse11(
        answer11(
            contacts=[contact11("detail_window", at=1.5)],
            incident_assessment=assessment11(
                "detail_window", contact_interval_start_seconds=1.0, contact_interval_end_seconds=2.0
            ),
        ),
        window=WINDOW,
    )
    assert "assessment_contact_missing" not in codes(facts)


def test_a_whole_clip_interval_with_a_detail_contact_that_does_not_match_is_flagged() -> None:
    facts = parse11(
        answer11(
            contacts=[contact11("detail_window", at=1.5)],
            incident_assessment=assessment11(
                "whole_clip", contact_interval_start_seconds=3.0, contact_interval_end_seconds=4.0
            ),
        ),
        window=WINDOW,
    )
    assert "assessment_contact_missing" in codes(facts)


def test_aftermath_is_compared_with_the_rebased_contact_time() -> None:
    # Contact at part 1.5 -> 31.5 s. An aftermath item at 30.0 s is before it by more than 1 s: flagged.
    # Left unrebased (1.5 s) it would look later than the contact and pass.
    stopped = contact11("whole_clip", subject="P1", other="", at=30.0, kind="vehicle_stopped_at_scene")
    facts = parse11(
        answer11(
            contacts=[contact11("detail_window", at=1.5), stopped],
            incident_assessment=assessment11(
                "detail_window", contact_interval_start_seconds=1.0, contact_interval_end_seconds=2.0
            ),
        ),
        window=WINDOW,
    )
    assert "aftermath_before_contact" in codes(facts)


def test_the_camera_rule_uses_the_rebased_contact() -> None:
    facts = parse11(
        answer11(
            rows=[row("CAM", colour_family="white"), row("P1", colour_family="red", party_type="suv")],
            contacts=[contact11("detail_window", subject="CAM", other="P1", at=1.5)],
            incident_assessment=assessment11(
                "detail_window",
                camera_role="witness_vehicle_not_involved",
                contact_interval_start_seconds=1.0,
                contact_interval_end_seconds=2.0,
            ),
            observation_notes=[],
        ),
        window=WINDOW,
    )
    issues = [i for i in check_consistency(facts, {VIEW: DURATION}) if i.code == "camera_in_contact"]
    assert issues
    assert all(i.cited_facts[1].timestamp_seconds == pytest.approx(31.5) for i in issues)


# --- other_party and older versions ---------------------------------------------------------------------------------


def test_other_party_filled_on_a_single_party_row_is_still_refused() -> None:
    fact = {
        "subject": "P1",
        "field": "impact_zone_on_party",
        "value": "front",
        "other_party": "P2",
        "timestamp_seconds": 3.0,
        "confidence": 0.9,
    }
    with pytest.raises(AIConnectionError):
        parse11(answer11(structured_facts=[fact]), window=WINDOW)


def test_overview_10_still_refuses_the_overview_11_fields() -> None:
    with pytest.raises(AIConnectionError):
        parse(answer11(), version="overview-10")
    base = parse(
        {
            **answer11(),
            "collision_observations": [{k: v for k, v in contact11().items() if k != "source_part"}],
            "incident_assessment": assessment(),
        }
    )
    assert base.incident_assessments[0].contact_interval_part is None
    assert base.collision_observations[0].source_part is None


def test_facts_round_trip_through_json_with_the_new_fields() -> None:
    facts = parse11(answer11(contacts=[contact11("detail_window", at=1.5)]), window=WINDOW)
    again = OverviewFacts.model_validate_json(facts.model_dump_json(), context={"prompt_version": "overview-11"})
    assert again == facts
    assert json.loads(facts.model_dump_json())["collision_observations"][0]["source_part"] == "detail_window"
