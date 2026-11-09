"""overview-11: one request per view with the whole clip plus a fixed high-rate detail window, and the recovery
request that follows every negative (spec 003 ticket 8, card SINGLE-REQUEST-MULTIRES).

Prompt wording lives in overview_11_system.md and overview_11_user.md (Claude owns it); this module loads them
(through ai.overview10, which owns the loading and the allowed-values block), lays out the video parts, rebases the
detail-window times onto the full clip exactly once, and decides when a recovery request must run.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from incident_report.ai import overview10 as o10
from incident_report.ai.part_plan import PartPlan, plan_parts
from incident_report.facts import (
    CONTACT_KINDS,
    CollisionObservation,
    IncidentAssessment,
    OverviewFacts,
    RecoveryOutcome,
    RecoveryTrigger,
    outside_detail_window,
)

PROMPT_VERSION_11 = "overview-11"

# Project assumption, to be validated (ADR 0006): a detail-window time may overrun the window by this much before the
# answer is refused as having used the wrong clock.
TIME_TOLERANCE_SECONDS = 0.5
# Project assumption, to be validated (ADR 0006): the explicit thinking level of every overview-11 request; the older
# versions send none.
THINKING_LEVEL = "low"
_NEGATIVE_STATES = frozenset({"no_contact_observed", "cannot_assess"})


@dataclass(frozen=True)
class VideoPart:
    """One video part of the request: the text line that introduces it and how the file is sampled."""

    label: str
    intro: str
    fps: float
    start_offset: str | None = None
    end_offset: str | None = None
    media_resolution: str = "default"

    def metadata(self) -> dict[str, object]:
        out: dict[str, object] = {"fps": self.fps}
        if self.start_offset is not None and self.end_offset is not None:
            out["start_offset"] = self.start_offset
            out["end_offset"] = self.end_offset
        return out


class MultiResPrompt(o10.StructuredPrompt):
    """The user text of an overview-11 request, carrying the video parts and the thinking level the transport sends.

    Still a str (the user text), so the model-call log and every fake transport see plain text.
    """

    video_parts: tuple[VideoPart, ...]
    thinking_level: str
    window: tuple[float, float] | None
    generate_stage: str

    def __new__(
        cls,
        text: str,
        *,
        video_parts: tuple[VideoPart, ...],
        window: tuple[float, float] | None,
        generate_stage: str,
    ) -> MultiResPrompt:
        from incident_report.facts import OVERVIEW_11_SCHEMA

        prompt = cast(
            MultiResPrompt,
            super().__new__(cls, text, system_instruction=system_instruction(), response_schema=OVERVIEW_11_SCHEMA),
        )
        prompt.video_parts = video_parts
        prompt.thinking_level = THINKING_LEVEL
        prompt.window = window
        prompt.generate_stage = generate_stage
        return prompt


def system_instruction() -> str:
    """The overview-11 system file plus the same generated allowed-values block overview-10 uses."""
    return o10._load("overview_11_system.md").rstrip("\n") + "\n\n" + o10.allowed_values_block() + "\n"


def user_prompt() -> str:
    return o10._load("overview_11_user.md")


def _whole_part(duration: float, fps: float, resolution: str = "default") -> VideoPart:
    return VideoPart(
        "whole_clip",
        f"Part whole_clip: the full clip, 0.000 to {duration:.3f} s, at {fps:g} frames per second.",
        fps,
        media_resolution=resolution,
    )


def _detail_part(window: tuple[float, float], fps: float, resolution: str = "default") -> VideoPart:
    start, end = window
    return VideoPart(
        "detail_window",
        f"Part detail_window: seconds {start:.3f} to {end:.3f} of the full clip, at {fps:g} frames per second. "
        "Its own clock starts at 0.000.",
        fps,
        f"{start:.3f}s",
        f"{end:.3f}s",
        resolution,
    )


def _video_parts(plans: list[PartPlan], duration: float) -> tuple[VideoPart, ...]:
    return tuple(
        _whole_part(duration, plan.fps, plan.resolution)
        if plan.name == "whole_clip"
        else _detail_part((plan.start_seconds, plan.end_seconds), plan.fps, plan.resolution)
        for plan in plans
    )


def window_of(plans: list[PartPlan]) -> tuple[float, float] | None:
    """The detail window of a plan, if it has one: the only way the rest of the code learns where it is."""
    for plan in plans:
        if plan.name == "detail_window":
            return plan.start_seconds, plan.end_seconds
    return None


def part_fps_of(plans: list[PartPlan]) -> dict[str, float]:
    """The sampling rate of each part of a plan, by part name: how the close-up check learns the rate an observation
    was seen at (the plan is the only place a rate is chosen)."""
    return {plan.name: plan.fps for plan in plans}


def main_plan(duration: float, settings: Any) -> list[PartPlan]:
    return plan_parts(
        duration,
        mode="main",
        whole_fps=settings.fps,
        detail_fps=settings.detail_fps,
        policy=settings.part_policy,
        media_resolution=settings.media_resolution,
        coverage_frame_budget=settings.coverage_frame_budget,
        oracle_centre_seconds=settings.oracle_contact_seconds,
    )


def recovery_plan(duration: float, settings: Any, centre: float | None = None) -> list[PartPlan]:
    return plan_parts(
        duration,
        contacts_seconds=None if centre is None else [centre],
        mode="recovery",
        whole_fps=settings.fps,
        detail_fps=settings.detail_fps,
        recovery_frame_budget=settings.recovery_frame_budget,
    )


def main_prompt(duration: float, settings: Any, suffix: str = "", *, stage: str = "generate") -> MultiResPrompt:
    plans = main_plan(duration, settings)
    return MultiResPrompt(
        user_prompt() + suffix,
        video_parts=_video_parts(plans, duration),
        window=window_of(plans),
        generate_stage=stage,
    )


def recovery_prompt(
    duration: float, settings: Any, suffix: str = "", *, centre: float | None = None, stage: str = "recovery.generate"
) -> MultiResPrompt:
    """The recovery request: same system text, schema and user text, the whole clip at the recovery rate, and, when a
    contact was seen only at the low rate, a detail window around the earliest such contact. It never mentions the
    first answer (round 5/6: a story hint gave 0 of 4)."""
    plans = recovery_plan(duration, settings, centre)
    return MultiResPrompt(
        user_prompt() + suffix,
        video_parts=_video_parts(plans, duration),
        window=window_of(plans),
        generate_stage=stage,
    )


def parts_for_hash(parts: tuple[VideoPart, ...]) -> list[list[object]]:
    # The resolution joins a row only when it is not the default, so a default request keeps today's hash.
    return [
        [
            p.label,
            p.intro,
            p.fps,
            p.start_offset,
            p.end_offset,
            *([p.media_resolution] * (p.media_resolution != "default")),
        ]
        for p in parts
    ]


# ---------------------------------------------------------------------------------------------------------
# Model output -> OverviewFacts payload, with the detail-window times rebased once
# ---------------------------------------------------------------------------------------------------------


def _part_time(value: Any, what: str, part: str, span: float | None) -> float:
    """A model time as a finite number; on the detail_window clock it must lie in [0, span + tolerance]. Never
    clamped, and never guessed from another clock (card section 3)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{what} must be a number")
    if part == "detail_window" and span is not None:  # span is set whenever a detail_window was sent
        if not 0 <= value <= span + TIME_TOLERANCE_SECONDS:
            raise ValueError(
                f"{what} {value:g} s is outside detail_window (0 to {span + TIME_TOLERANCE_SECONDS:g} s on its own clock)"
            )
    return float(value)


def _source_part(value: Any, what: str, window: tuple[float, float] | None) -> str:
    if value not in {"whole_clip", "detail_window"}:
        raise ValueError(f"{what} source_part must be whole_clip or detail_window")
    if value == "detail_window" and window is None:
        raise ValueError(f"{what} names detail_window but this request had no detail window")
    return str(value)


def _rebase(
    data: dict[str, Any], window: tuple[float, float] | None, part_fps: Mapping[str, float] | None = None
) -> dict[str, Any]:
    """The only place a detail-window time becomes a full-clip time: full = window start + part time."""
    start = window[0] if window else 0.0
    span = window[1] - window[0] if window else None
    out = dict(data)

    raw_contacts = data.get("collision_observations")
    if isinstance(raw_contacts, list):
        contacts = []
        for raw in raw_contacts:
            if not isinstance(raw, dict):
                contacts.append(raw)  # the shared builder refuses it with its own message
                continue
            if "part_time_seconds" in raw:
                raise ValueError("collision_observations item must not carry part_time_seconds; code sets it")
            if "part_fps" in raw:
                raise ValueError("collision_observations item must not carry part_fps; code sets it")
            part = _source_part(raw.get("source_part"), "collision_observations", window)
            seen = _part_time(raw.get("timestamp_seconds"), "collision_observations timestamp_seconds", part, span)
            contacts.append(
                {
                    **raw,
                    "part_time_seconds": seen,
                    "part_fps": None if part_fps is None else part_fps.get(part),
                    "timestamp_seconds": start + seen if part == "detail_window" else seen,
                }
            )
        out["collision_observations"] = contacts

    raw_assessment = data.get("incident_assessment")
    if isinstance(raw_assessment, dict):
        if "contact_interval_part" not in raw_assessment:
            raise ValueError("incident_assessment needs contact_interval_part (null when there is no interval)")
        part_value = raw_assessment["contact_interval_part"]
        first, last = (
            raw_assessment.get("contact_interval_start_seconds"),
            raw_assessment.get("contact_interval_end_seconds"),
        )
        if (part_value is None) != (first is None and last is None) or (first is None) != (last is None):
            raise ValueError("contact_interval_part and both contact interval times are null together or not at all")
        if part_value is not None:
            part = _source_part(part_value, "incident_assessment contact_interval_part", window)
            first = _part_time(first, "contact_interval_start_seconds", part, span)
            last = _part_time(last, "contact_interval_end_seconds", part, span)
            if part == "detail_window":
                first, last = start + first, start + last
            out["incident_assessment"] = {
                **raw_assessment,
                "contact_interval_start_seconds": first,
                "contact_interval_end_seconds": last,
            }
    return out


def build_overview_11_data(
    data: Any,
    view_id: str,
    view_role: str | None,
    *,
    detail_window: tuple[float, float] | None,
    part_fps: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Turn the model's overview-11 answer into the payload OverviewFacts validates: rebase first, before any other
    check, then the same builder overview-10 uses (its checks and the 1b rules run on full-clip times)."""
    if not isinstance(data, dict):
        raise ValueError("overview-11 answer must be an object")
    return o10.build_overview_10_data(_rebase(data, detail_window, part_fps), view_id, view_role, allow_parts=True)


# ---------------------------------------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------------------------------------


def _contacts(facts: OverviewFacts) -> list[CollisionObservation]:
    return [c for c in facts.collision_observations if c.kind in CONTACT_KINDS]


def recovery_triggers(facts: OverviewFacts, window: tuple[float, float] | None) -> tuple[RecoveryTrigger, ...]:
    """Why a recovery request must run for this answer; empty when none of the three cases holds. Confidence never
    skips it."""
    (assessment,) = facts.incident_assessments
    contacts = _contacts(facts)
    triggers: list[RecoveryTrigger] = []
    if assessment.contact_state in _NEGATIVE_STATES:
        triggers.append("negative_state")
    if not contacts:
        triggers.append("no_typed_contact")
    if window is not None and any(outside_detail_window(c.timestamp_seconds, window) for c in contacts):
        triggers.append("contact_outside_detail_window")
    return tuple(triggers)


def earliest_outside_contact(facts: OverviewFacts, window: tuple[float, float]) -> CollisionObservation | None:
    outside = [c for c in _contacts(facts) if outside_detail_window(c.timestamp_seconds, window)]
    return min(outside, key=lambda c: c.timestamp_seconds) if outside else None


def is_negative(facts: OverviewFacts) -> bool:
    (assessment,) = facts.incident_assessments
    return assessment.contact_state in _NEGATIVE_STATES and not _contacts(facts)


def with_record(
    facts: OverviewFacts,
    *,
    window: tuple[float, float] | None,
    triggers: tuple[RecoveryTrigger, ...] = (),
    outcome: RecoveryOutcome | None = None,
) -> OverviewFacts:
    """The facts with the window that was sent and the recovery record stored on the (single) assessment."""
    (assessment,) = facts.incident_assessments
    stored: IncidentAssessment = assessment.model_copy(
        update={
            "detail_window_start_seconds": window[0] if window else None,
            "detail_window_end_seconds": window[1] if window else None,
            "recovery_triggers": list(triggers),
            "recovery": outcome,
        }
    )
    return facts.model_copy(update={"incident_assessments": [stored]})


__all__ = [
    "PROMPT_VERSION_11",
    "THINKING_LEVEL",
    "TIME_TOLERANCE_SECONDS",
    "MultiResPrompt",
    "VideoPart",
    "build_overview_11_data",
    "earliest_outside_contact",
    "is_negative",
    "main_plan",
    "part_fps_of",
    "window_of",
    "main_prompt",
    "recovery_plan",
    "recovery_prompt",
    "recovery_triggers",
    "system_instruction",
    "user_prompt",
    "with_record",
]
