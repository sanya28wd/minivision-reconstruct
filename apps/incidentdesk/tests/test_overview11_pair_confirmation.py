"""OVERVIEW-11-PAIR-CONFIRMATION: overview-11's close-up check for the selected collision pair (ADR 0008).

A selected pair is confirmed when the facts hold a typed collision observation of exactly that unordered pair that was
seen in a video part sampled at 15 fps or more. The decision is by the part's sampled fps, never its name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from incident_report.ai import AIConnectionError
from incident_report.ai import overview11 as o11
from incident_report.ai.dense import CLOSE_UP_MIN_FPS, close_up_confirmed_pairs
from incident_report.ai.gemini import parse_overview
from incident_report.facts import OverviewFacts
from incident_report.rules import FaultStatus, evaluate_fault
from overview11_helpers import VIEW, answer11, assessment11, contact11
from test_collision_pair_selection import A, B, C, collision, contact, signal_facts
from test_overview11_recovery import DURATION, SETTINGS, connect, request_for

NOT_CONFIRMED = "Collision pair not confirmed by the close-up check"


def seen_at(
    fps: float | None, *, pair: tuple[str, str] = (A, B), kind: str = "vehicle_contact_vehicle"
) -> OverviewFacts:
    obs = contact(pair[0], pair[1], 3.0, kind=kind).model_copy(update={"part_fps": fps})
    return signal_facts((A, B, C), [collision(3.0)], [obs])


def faults_of(facts: OverviewFacts) -> dict[str, Any]:
    confirmed = close_up_confirmed_pairs(facts)
    return {f.party_description: f for f in evaluate_fault(facts, dense_confirmed_pairs=confirmed)}


# --- the rule ------------------------------------------------------------------------------------------------------


def test_the_floor_is_the_adr_0008_close_up_rate() -> None:
    assert CLOSE_UP_MIN_FPS == 15.0


@pytest.mark.parametrize("fps", [15.0, 24.0])
def test_a_pair_seen_at_15_fps_or_more_is_confirmed_and_the_rules_run(fps: float) -> None:
    facts = seen_at(fps)
    assert close_up_confirmed_pairs(facts) == {frozenset((A, B))}
    faults = faults_of(facts)
    assert faults[A].status == FaultStatus.AT_FAULT
    assert faults[B].status == FaultStatus.NOT_AT_FAULT
    assert faults[C].status == FaultStatus.CANNOT_DETERMINE  # a bystander, never blamed


@pytest.mark.parametrize("fps", [1.0, 4.0, 14.99, None])
def test_a_pair_seen_below_15_fps_or_with_no_known_rate_is_not_confirmed(fps: float | None) -> None:
    facts = seen_at(fps)
    assert close_up_confirmed_pairs(facts) == frozenset()
    faults = faults_of(facts)
    assert {f.status for f in faults.values()} == {FaultStatus.CANNOT_DETERMINE}
    assert faults[A].reason == NOT_CONFIRMED


def test_the_pair_is_unordered() -> None:
    assert close_up_confirmed_pairs(seen_at(15.0, pair=(B, A))) == {frozenset((A, B))}


def test_a_close_up_observation_of_a_different_pair_does_not_confirm_the_selected_pair() -> None:
    # B-C seen at 15 fps (in another scene, say); the rules select A-B, which was seen only at 1 fps.
    close_up = contact(B, C, 4.0).model_copy(update={"part_fps": 15.0})
    only_other = close_up_confirmed_pairs(signal_facts((A, B, C), [collision(3.0)], [close_up]))
    assert only_other == {frozenset((B, C))}
    facts = seen_at(1.0)
    faults = {f.party_description: f for f in evaluate_fault(facts, dense_confirmed_pairs=only_other)}
    assert faults[A].reason == NOT_CONFIRMED
    assert {f.status for f in faults.values()} == {FaultStatus.CANNOT_DETERMINE}


@pytest.mark.parametrize("kind", ["contact_other", "vehicle_overturned", "person_injured_visible"])
def test_a_kind_that_is_not_a_pair_kind_never_confirms(kind: str) -> None:
    other = "light pole" if kind == "contact_other" else ""
    obs = contact(A, other, 3.0, kind=kind).model_copy(update={"part_fps": 15.0})
    assert close_up_confirmed_pairs(signal_facts((A, B), [collision(3.0)], [obs])) == frozenset()


# --- how the fps reaches the observation (code sets it; the model never does) ---------------------------------------

WINDOW = (13.0, 17.0)
MAIN_FPS = {"whole_clip": 1.0, "detail_window": 15.0}


def parse(
    contacts: list[dict[str, Any]], part_fps: dict[str, float] | None, window: tuple[float, float] | None = WINDOW
) -> OverviewFacts:
    return parse_overview(
        json.dumps(answer11(contacts=contacts)),
        VIEW,
        prompt_version="overview-11",
        view_role="exterior",
        detail_window=window,
        part_fps=part_fps,
    )


def test_each_observation_carries_the_fps_of_the_part_it_was_seen_in() -> None:
    detail = parse([contact11("detail_window", at=1.5)], MAIN_FPS).collision_observations[0]
    whole = parse([contact11("whole_clip", at=3.0)], MAIN_FPS).collision_observations[0]
    assert (detail.source_part, detail.part_fps) == ("detail_window", 15.0)
    assert (whole.source_part, whole.part_fps) == ("whole_clip", 1.0)


def test_with_no_part_rates_given_the_fps_stays_unknown() -> None:
    assert parse([contact11("whole_clip", at=3.0)], None).collision_observations[0].part_fps is None


def test_the_model_cannot_supply_part_fps() -> None:
    with pytest.raises(AIConnectionError):
        parse([{**contact11("whole_clip", at=3.0), "part_fps": 24.0}], MAIN_FPS)


def test_the_rate_follows_the_plan_not_the_part_name() -> None:
    # A short clip is one whole_clip part sent at the detail rate: that observation is close-up.
    plans = o11.main_plan(3.0, SETTINGS)
    assert [p.name for p in plans] == ["whole_clip"]
    obs = parse([contact11("whole_clip", at=1.0)], o11.part_fps_of(plans), None).collision_observations[0]
    assert obs.part_fps == 15.0
    # A 30 s clip: the whole_clip part is 1 fps.
    assert o11.part_fps_of(o11.main_plan(30.0, SETTINGS)) == MAIN_FPS


def test_the_recovery_rate_is_the_adaptive_rate() -> None:
    # 120 frames over 8 s = 15 fps (close-up); over 30 s = 4 fps (not).
    assert o11.part_fps_of(o11.recovery_plan(8.0, SETTINGS))["whole_clip"] == 15.0
    assert o11.part_fps_of(o11.recovery_plan(30.0, SETTINGS))["whole_clip"] == 4.0


# --- through the real request path (fake transport, no network) -----------------------------------------------------


def test_a_main_answer_from_the_detail_window_carries_15_fps(tmp_path: Path) -> None:
    detail_first = answer11(
        contacts=[contact11("detail_window", at=1.5)],
        incident_assessment=assessment11(
            "detail_window", contact_interval_start_seconds=1.0, contact_interval_end_seconds=2.0
        ),
    )
    connection, _ = connect(tmp_path, [detail_first])
    (obs,) = connection.analyse(request_for(DURATION)).facts.collision_observations
    assert (obs.source_part, obs.part_fps) == ("detail_window", 15.0)


def _early(at: float) -> dict[str, Any]:
    return answer11(
        contacts=[contact11("whole_clip", at=at)],
        incident_assessment=assessment11(
            "whole_clip", contact_interval_start_seconds=at - 0.25, contact_interval_end_seconds=at + 0.25
        ),
    )


def test_a_recovery_answer_on_a_short_clip_is_close_up(tmp_path: Path) -> None:
    # 8 s clip: a contact at 0.5 s is outside the window, so recovery runs at 120 / 8 = 15 fps.
    connection, _ = connect(tmp_path, [_early(0.5), _early(0.5)])
    (obs,) = connection.analyse(request_for(8.0)).facts.collision_observations
    assert obs.part_fps == 15.0


def test_a_recovery_answer_on_a_long_clip_is_not_close_up(tmp_path: Path) -> None:
    # 60 s clip: recovery whole_clip is 120 / 60 = 2 fps.
    connection, _ = connect(tmp_path, [_early(0.5), _early(0.5)])
    (obs,) = connection.analyse(request_for(60.0)).facts.collision_observations
    assert obs.part_fps == 2.0


# --- the entry path: v11 uses the fps rule, older versions keep dense ------------------------------------------------


def test_entry_helper_uses_the_fps_rule_for_overview_11_and_dense_for_older_versions() -> None:
    from incident_report.entry import confirmed_pairs_for

    low = seen_at(1.0)
    close = seen_at(15.0)
    assert confirmed_pairs_for("overview-11", close, []) == {frozenset((A, B))}
    assert confirmed_pairs_for("overview-11", low, []) == frozenset()
    # older versions: only dense-2 facts count, never the part fps
    assert confirmed_pairs_for("overview-10", close, []) == frozenset()
    assert confirmed_pairs_for("overview-10", low, [close]) == {frozenset((A, B))}


def test_the_vote_stage_gets_the_confirmation_only_for_overview_11() -> None:
    from incident_report.entry import _vote_stage_pairs

    close = seen_at(15.0)
    assert _vote_stage_pairs("overview-11", close) == {"dense_confirmed_pairs": {frozenset((A, B))}}
    assert _vote_stage_pairs("overview-10", close) == {}  # called exactly as before
