from __future__ import annotations

from incident_report.model_calls import ModelCall


class AIConnectionError(Exception):
    """The AI connection could not give an answer. The message is plain and safe to show."""

    def __init__(self, message: str, *, calls: tuple[ModelCall, ...] = ()) -> None:
        super().__init__(message)
        self.calls = calls


class RecordingNotFound(AIConnectionError):
    """No recorded response exists for this input and prompt version."""


class RecordingInvalid(AIConnectionError):
    """A recording exists but is unreadable or does not match its own key."""
