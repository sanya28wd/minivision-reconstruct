"""Where the video parts of an overview-11 request go, and at what frame rate (spec 003 ticket 8).

This is the ONLY place that places a detail window or picks a frame rate. The request builder, the time rebasing
and the recovery request consume the PartPlan values it returns and never compute a window themselves, so the policy
can be replaced here without touching them.

PROVISIONAL POLICY: the owner rejected the fixed middle window on 2026-10-24. The rule below (a window centred on the
middle of the clip, or on the earliest given contact) is only what the card says until the placement policy (from
LOCAL-CRASH-FINDER-C) replaces this function's body.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

# Project assumption, to be validated (ADR 0006): a 4 s window (+-2 s, owner ruling 2026-10-24). The earlier locator
# review (.agent_bridge/reviews/SECOND-OPINION-CRASH-LOCATOR.md, finding 1) put the real crash inside a middle window
# for 5 of 6 clips at k=1, with 3 s; the owner widened it.
DETAIL_WINDOW_SECONDS = 4.0
# Project assumption, to be validated (ADR 0006): the recovery request samples the whole clip at about this many frames
# in all (GEMINI_RECOVERY_FRAME_BUDGET), never below 2 fps (the owner's floor) and never above 24 (the SDK limit).
RECOVERY_FRAME_BUDGET = 120.0

# Project assumption, to be validated (ADR 0006), PERF-07: the coverage policy samples the whole clip at about this many
# frames in all, never below 1 fps and never above 24 (the SDK limit). Replaces the guessed middle window.
COVERAGE_FRAME_BUDGET = 180.0

PartMode = Literal["main", "recovery"]
# middle: today's window on the middle of the clip. coverage: one whole-clip part at the budget rate. oracle:
# EVALUATION ONLY, the window is centred on a time the benchmark passes in (never read from truth by runtime code).
PartPolicy = Literal["middle", "coverage", "oracle"]
# Per-part media resolution. "default" sends no field (the model's own default); "high" sends the SDK's per-part
# media_resolution level MEDIA_RESOLUTION_HIGH (google.genai.types.Part.media_resolution).
MediaResolution = Literal["default", "high"]
PART_POLICIES: tuple[str, ...] = ("middle", "coverage", "oracle")
MEDIA_RESOLUTIONS: tuple[str, ...] = ("default", "high")


@dataclass(frozen=True)
class PartPlan:
    """One video part to send: its name, its span on the full clip (seconds) and its sampling rate."""

    name: Literal["whole_clip", "detail_window"]
    start_seconds: float
    end_seconds: float
    fps: float
    resolution: MediaResolution = "default"


def coverage_fps(duration_seconds: float, frame_budget: float) -> float:
    """About `frame_budget` frames over the whole clip: at least 1 fps, at most 24."""
    return min(24.0, max(1.0, frame_budget / duration_seconds))


def recovery_fps(duration_seconds: float, frame_budget: float) -> float:
    """About `frame_budget` frames over the whole clip: at least 2 fps, at most 24."""
    return min(24.0, max(2.0, frame_budget / duration_seconds))


def _window(duration: float, centre: float) -> tuple[float, float]:
    """DETAIL_WINDOW_SECONDS long around `centre`, moved inside [0, duration] rather than shortened. Both ends are
    whole milliseconds (start rounded down) so the text, the offsets and the rebasing agree and the end never passes
    the end of the clip."""
    start = min(max(centre - DETAIL_WINDOW_SECONDS / 2, 0.0), duration - DETAIL_WINDOW_SECONDS)
    start = math.floor(start * 1000 + 1e-6) / 1000
    return start, round(start + DETAIL_WINDOW_SECONDS, 3)


def plan_parts(
    duration_seconds: float,
    *,
    contacts_seconds: Sequence[float] | None = None,
    mode: PartMode,
    whole_fps: float,
    detail_fps: float,
    recovery_frame_budget: float = RECOVERY_FRAME_BUDGET,
    policy: PartPolicy = "middle",
    media_resolution: MediaResolution = "default",
    coverage_frame_budget: float = COVERAGE_FRAME_BUDGET,
    oracle_centre_seconds: float | None = None,
) -> list[PartPlan]:
    """The parts of one request, whole clip first.

    main: the whole clip at `whole_fps` plus one detail window at `detail_fps`; a clip no longer than the window is
    sent whole, at `detail_fps`.
    recovery: the whole clip at the adaptive recovery rate, plus, when `contacts_seconds` names a moment seen only at the
    whole-clip rate, a detail window around the earliest one.

    `policy`, `media_resolution`, `coverage_frame_budget` and `oracle_centre_seconds` change the main request only
    (PERF-07); recovery is the same for every policy. The resolution is set on the detail part, or on the single
    coverage (or short-clip) part.
    """
    whole = PartPlan("whole_clip", 0.0, duration_seconds, whole_fps)
    if mode == "main":
        if policy == "coverage":
            fps = coverage_fps(duration_seconds, coverage_frame_budget)
            return [PartPlan("whole_clip", 0.0, duration_seconds, fps, media_resolution)]
        if policy == "oracle" and oracle_centre_seconds is None:
            raise ValueError("The oracle policy needs the time the benchmark passes in.")
        if duration_seconds <= DETAIL_WINDOW_SECONDS:
            return [PartPlan("whole_clip", 0.0, duration_seconds, detail_fps, media_resolution)]
        centre = oracle_centre_seconds if policy == "oracle" and oracle_centre_seconds is not None else None
        start, end = _window(duration_seconds, duration_seconds / 2 if centre is None else centre)
        return [whole, PartPlan("detail_window", start, end, detail_fps, media_resolution)]
    recovery_whole = PartPlan(
        "whole_clip", 0.0, duration_seconds, recovery_fps(duration_seconds, recovery_frame_budget)
    )
    if contacts_seconds and duration_seconds > DETAIL_WINDOW_SECONDS:
        start, end = _window(duration_seconds, min(contacts_seconds))
        return [recovery_whole, PartPlan("detail_window", start, end, detail_fps)]
    return [recovery_whole]
