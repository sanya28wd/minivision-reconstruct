"""Builders for hand-written overview-11 model answers (synthetic; no model call is ever made).

An overview-11 answer is an overview-10 answer whose contacts and contact interval say which video part
(whole_clip or detail_window) their times are measured on.
"""

from __future__ import annotations

import json
from typing import Any

from incident_report.ai.gemini import parse_overview
from incident_report.facts import OverviewFacts
from overview10_helpers import VIEW, answer, assessment, contact, row


def contact11(part: str = "whole_clip", **kwargs: Any) -> dict[str, Any]:
    """A contact; `at` is seconds on the clock of `part`."""
    return {**contact(**kwargs), "source_part": part}


def assessment11(part: str | None = "whole_clip", **overrides: Any) -> dict[str, Any]:
    return {**assessment(**overrides), "contact_interval_part": part}


def answer11(
    rows: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Two cars, one visible contact at 3.0 s on the whole clip, fixed camera."""
    base = answer(
        rows=rows,
        contacts=contacts if contacts is not None else [contact11(at=3.0)],
        incident_assessment=assessment11(),
    )
    return {**base, **overrides}


def negative11(**overrides: Any) -> dict[str, Any]:
    """The answer the model gives when it sees no touch: no contacts, a null interval and part."""
    base: dict[str, Any] = {
        "contacts": [],
        "incident_assessment": assessment11(
            part=None,
            contact_state="no_contact_observed",
            contact_interval_start_seconds=None,
            contact_interval_end_seconds=None,
        ),
        "events": [],
    }
    return answer11(**{**base, **overrides})


def parse11(
    model_answer: dict[str, Any],
    *,
    role: str = "exterior",
    view: str = VIEW,
    window: tuple[float, float] | None = None,
) -> OverviewFacts:
    return parse_overview(
        json.dumps(model_answer), view, prompt_version="overview-11", view_role=role, detail_window=window
    )


__all__ = ["VIEW", "answer11", "assessment11", "contact11", "negative11", "parse11", "row"]
