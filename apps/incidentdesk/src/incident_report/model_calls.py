"""Persisted call attempts, shared by the live connection, Run log and replay."""

from pydantic import BaseModel, ConfigDict, Field


class ProviderError(BaseModel):
    """What the provider said went wrong, already sanitized (see incident_report.ai.provider_error)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    http_status: int | None = None
    status: str | None = None  # provider status word, e.g. UNAVAILABLE or RESOURCE_EXHAUSTED
    message: str | None = Field(default=None, max_length=300)


class ModelCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_hash: str
    input_hash: str
    prompt_version: str
    model: str
    view_id: str
    fps: float
    raw_response: str | None
    latency_ms: float
    retry_index: int = 0
    status_code: int | None = None
    error: str | None = None
    provider_error: ProviderError | None = None  # sanitized; the plain `error` text stays provider-free
