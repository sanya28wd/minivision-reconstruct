"""Replay dense collision pass attempts read from a persisted Run. No files, SDK, or network needed."""

from __future__ import annotations

from incident_report.ai.dense import (
    CandidateWindow,
    DenseSettings,
    dense_request_hash,
    parse_dense,
)
from incident_report.ai.errors import AIConnectionError
from incident_report.ai.interface import AIRequest
from incident_report.facts import OverviewFacts
from incident_report.model_calls import ModelCall


class ReplayDenseAIConnection:
    def __init__(self, settings: DenseSettings, calls: tuple[ModelCall, ...]) -> None:
        self.settings = settings
        self.calls = calls

    def analyse_window(self, request: AIRequest, window: CandidateWindow) -> OverviewFacts:
        key = dense_request_hash(request, window, self.settings)
        for call in reversed(self.calls):
            if call.request_hash == key and call.raw_response is not None:
                return parse_dense(call.raw_response, window.view_id)
        raise AIConnectionError(
            "No dense recording matches this input, window, prompt, model and sampling configuration."
        )
