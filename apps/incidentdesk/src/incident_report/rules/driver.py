"""Deterministic RPT-30 classifications. Observation sources: card and professor L17.

Thresholds are project assumptions (COMPARABLE_SYSTEMS.md L21), never vendor claims.
Only handheld use selects mobile_phone_distraction; seatbelt uses the existing
provisional row. Neither changes collision Fault Status.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from incident_report.facts import CitedFact, DriverObservation, OverviewFacts

if TYPE_CHECKING:
    from incident_report.config import PhoneUseSettings

PHONE_RULE_ID = "driver_handheld_phone"
SEATBELT_RULE_ID = "driver_seatbelt"


class DriverAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    party_description: str
    phone_use: Literal["Entertainment", "GPS", "Cannot Determine"]
    reason: str
    cited_facts: tuple[CitedFact, ...]


def _longest_look(observations: list[DriverObservation]) -> float:
    intervals = sorted((o.start_seconds, o.end_seconds) for o in observations if o.kind == "looking_at_phone")
    longest = 0.0
    start = end = -1.0
    for left, right in intervals:
        if left > end:
            start, end = left, right
        else:
            end = max(end, right)
        longest = max(longest, end - start)
    return longest


def classify_driver_observations(
    facts: OverviewFacts, settings: PhoneUseSettings | None = None
) -> tuple[DriverAssessment, ...]:
    from incident_report.config import PhoneUseSettings

    settings = settings if settings is not None else PhoneUseSettings.from_env()
    grouped: dict[str, dict[str, list[DriverObservation]]] = defaultdict(lambda: defaultdict(list))
    for o in facts.driver_observations:
        grouped[o.subject][o.view_id].append(o)
    results = []
    for subject, views in grouped.items():
        observations = [o for items in views.values() for o in items]
        handheld = any(o.kind == "phone_in_hand" for o in observations)
        mounted = any(o.kind == "phone_on_mount" for o in observations)
        has_looks = any(o.kind == "looking_at_phone" for o in observations)
        longest = max(_longest_look(items) for items in views.values()) if has_looks else None
        burst = False
        for items in views.values():
            taps = sorted({o.start_seconds for o in items if o.kind == "tapping_phone"})
            n = settings.tapping_count
            burst |= any(taps[i + n - 1] - taps[i] <= settings.tapping_window_seconds for i in range(len(taps) - n + 1))
        classification: Literal["Entertainment", "GPS", "Cannot Determine"] = "Cannot Determine"
        if handheld or burst or (longest is not None and longest >= settings.continuous_look_seconds):
            classification = "Entertainment"
        elif mounted and has_looks:
            # A mount in another view or at another time does not establish mounted use.
            looks = [o for o in observations if o.kind == "looking_at_phone"]
            if all(
                any(
                    m.kind == "phone_on_mount" and m.start_seconds <= o.start_seconds and m.end_seconds >= o.end_seconds
                    for m in views[o.view_id]
                )
                for o in looks
            ):
                classification = "GPS"
        look_detail = f"{longest:g}s" if longest is not None else "Cannot Determine (no look evidence)"
        reason = (
            f"Phone use: {classification}; longest continuous look {look_detail}; "
            f"tapping burst observed: {burst}; handheld observed: {handheld}; mounted observed: {mounted}. "
            f"N={settings.tapping_count}, W={settings.tapping_window_seconds:g}s, T={settings.continuous_look_seconds:g}s "
            "(project assumption, to be validated; COMPARABLE_SYSTEMS.md L21)."
        )
        results.append(
            DriverAssessment(
                party_description=subject,
                phone_use=classification,
                reason=reason,
                cited_facts=tuple(o.citation() for o in observations),
            )
        )
    return tuple(results)
