"""Seam 2: the AI connection. Every model call in the product passes through this boundary.

Replay and the public-research Gemini overview share this interface. Vertex AI is RPT-12.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from incident_report.domain import Role
from incident_report.facts import OverviewFacts
from incident_report.model_calls import ModelCall


@dataclass(frozen=True)
class AIViewRef:
    """A view fingerprint. The live adapter binds it to an explicitly declared public video."""

    role: Role
    sha256: str
    duration_seconds: float | None = None


@dataclass(frozen=True)
class AIRequest:
    input_hash: str
    prompt_version: str
    views: tuple[AIViewRef, ...]


@dataclass(frozen=True)
class AIResponse:
    summary: str
    # Where the answer came from; "synthetic" for hand-written fixtures.
    provenance: str
    facts: OverviewFacts | None = None
    calls: tuple[ModelCall, ...] = ()


class AIConnection(Protocol):
    def analyse(self, request: AIRequest) -> AIResponse:
        """Return the model's answer, or raise AIConnectionError with a plain message.

        Never returns a made-up answer.
        """
        ...
