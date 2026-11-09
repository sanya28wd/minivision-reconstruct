"""Replay overview attempts read from a persisted Run. No files, SDK, or network needed."""

from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import OVERVIEW_PROMPT_VERSIONS, GeminiSettings, overview_request_hash, parse_overview
from incident_report.ai.interface import AIRequest, AIResponse
from incident_report.model_calls import ModelCall


class ReplayOverviewAIConnection:
    def __init__(self, settings: GeminiSettings, calls: tuple[ModelCall, ...]) -> None:
        self.settings = settings
        self.calls = calls

    def analyse(self, request: AIRequest) -> AIResponse:
        if len(request.views) != 1 or request.prompt_version not in OVERVIEW_PROMPT_VERSIONS:
            raise AIConnectionError("No overview recording matches this request.")
        key = overview_request_hash(request, self.settings)
        for call in reversed(self.calls):
            if call.request_hash == key and call.raw_response is not None:
                facts = parse_overview(
                    call.raw_response,
                    request.views[0].sha256,
                    prompt_version=request.prompt_version,
                    view_role=request.views[0].role.value,
                )
                return AIResponse("Overview facts extracted from one view.", "recorded-gemini-api", facts)
        raise AIConnectionError("No overview recording matches this input, prompt, model and sampling configuration.")
