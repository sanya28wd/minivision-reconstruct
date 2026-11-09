"""plan_parts: the one place that places a detail window and picks frame rates (spec 003 ticket 8).

PROVISIONAL policy (middle of the clip, or the earliest given moment). These are the only tests that may assert where
a window sits; every other test asks the plan.
"""

from __future__ import annotations

import pytest

from incident_report.ai.part_plan import (
    DETAIL_WINDOW_SECONDS,
    RECOVERY_FRAME_BUDGET,
    PartPlan,
    plan_parts,
    recovery_fps,
)

WHOLE_FPS, DETAIL_FPS = 1.0, 15.0


def main(duration: float) -> list[PartPlan]:
    return plan_parts(duration, mode="main", whole_fps=WHOLE_FPS, detail_fps=DETAIL_FPS)


def recovery(duration: float, contacts: list[float] | None = None) -> list[PartPlan]:
    return plan_parts(duration, mode="recovery", contacts_seconds=contacts, whole_fps=WHOLE_FPS, detail_fps=DETAIL_FPS)


def test_the_window_is_four_seconds() -> None:
    assert DETAIL_WINDOW_SECONDS == 4.0  # owner ruling 2026-10-24: +-2 s
    assert RECOVERY_FRAME_BUDGET == 120.0


def test_main_is_the_whole_clip_plus_a_window_centred_on_the_middle() -> None:
    whole, window = main(30.0)
    assert whole == PartPlan("whole_clip", 0.0, 30.0, 1.0)
    assert window == PartPlan("detail_window", 13.0, 17.0, 15.0)
    assert main(7.8)[1] == PartPlan("detail_window", 1.9, 5.9, 15.0)


@pytest.mark.parametrize("duration", [4.0, 3.0, 0.5])
def test_a_clip_no_longer_than_the_window_is_sent_whole_at_the_detail_rate(duration: float) -> None:
    assert main(duration) == [PartPlan("whole_clip", 0.0, duration, 15.0)]


def test_window_ends_are_whole_milliseconds_and_stay_inside_the_clip() -> None:
    window = main(33.3337)[1]
    assert window.start_seconds == round(window.start_seconds, 3) and window.end_seconds == round(window.end_seconds, 3)
    assert window.end_seconds - window.start_seconds == pytest.approx(4.0)
    assert window.end_seconds <= 33.3337


def test_recovery_is_the_whole_clip_at_the_adaptive_rate() -> None:
    assert recovery(30.0) == [PartPlan("whole_clip", 0.0, 30.0, 4.0)]


def test_recovery_adds_a_window_around_the_earliest_given_moment_shifted_not_shrunk_at_the_edges() -> None:
    assert recovery(30.0, [20.0, 25.0])[1] == PartPlan("detail_window", 18.0, 22.0, 15.0)
    assert recovery(30.0, [0.2])[1] == PartPlan("detail_window", 0.0, 4.0, 15.0)
    assert recovery(30.0, [29.9])[1] == PartPlan("detail_window", 26.0, 30.0, 15.0)


def test_a_short_clip_gets_no_recovery_window() -> None:
    assert [p.name for p in recovery(3.0, [1.0])] == ["whole_clip"]


@pytest.mark.parametrize("duration, expected", [(8.0, 15.0), (38.5, 120 / 38.5), (70.0, 2.0), (4.0, 24.0), (1.0, 24.0)])
def test_recovery_fps_is_adaptive_with_a_floor_and_a_cap(duration: float, expected: float) -> None:
    assert recovery_fps(duration, 120.0) == pytest.approx(expected)
