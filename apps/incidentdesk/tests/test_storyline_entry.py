"""REPORT-STORYLINE: the live placeholder summary is replaced by the code-built line; recorded reports keep theirs."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from conftest import make_view
from incident_report.ai import AIRequest, AIResponse
from incident_report.domain import Origin
from incident_report.entry import analyse_incident
from incident_report.facts import OverviewFacts
from incident_report.runlog import RunLog
from incident_report.storyline import CANNOT_DETERMINE_SUMMARY, NO_COLLISION_SUMMARY, PLACEHOLDER_SUMMARIES

EMPTY = OverviewFacts(parties=[], events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[])


@dataclass
class Answer:
    summary: str
    provenance: str

    def analyse(self, request: AIRequest) -> AIResponse:
        return AIResponse(summary=self.summary, provenance=self.provenance, facts=EMPTY)


@pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_SUMMARIES))
def test_live_placeholder_summary_is_replaced_by_the_code_built_line(run_log: RunLog, placeholder: str) -> None:
    report = analyse_incident([make_view()], Origin.REPLAY, ai=Answer(placeholder, "gemini-api"), run_log=run_log)
    assert report.summary in {NO_COLLISION_SUMMARY, CANNOT_DETERMINE_SUMMARY}
    assert run_log.get(report.run_id).report == report


def test_recorded_report_keeps_its_old_summary(run_log: RunLog) -> None:
    old = "Overview facts extracted from one view."
    report = analyse_incident([make_view()], Origin.REPLAY, ai=Answer(old, "recorded-gemini-api"), run_log=run_log)
    assert report.summary == old


def test_the_gemini_adapter_uses_the_shared_placeholder_constants() -> None:
    from pathlib import Path

    source = Path("src/incident_report/ai/gemini.py").read_text(encoding="utf-8")
    for text in PLACEHOLDER_SUMMARIES:
        assert f'"{text}"' not in source
