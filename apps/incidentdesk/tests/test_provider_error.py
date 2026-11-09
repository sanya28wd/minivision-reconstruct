"""The sanitized provider error kept on each model-call record (chore: retro diagnostics).

A Gemini failure used to be recorded as "Gemini request failed." plus a status code; the real message was lost.
Now the HTTP status, the provider status word and a redacted, 300-character message are kept, and nothing that
could carry a key, a header, a URL or the uploaded file's address. No live call and no real key anywhere: the
"key" below is assembled at run time from obvious filler so no key-shaped literal sits in the source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from incident_report.ai import AIConnectionError, AIRequest, AIViewRef
from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, TransportError
from incident_report.ai.provider_error import MAX_MESSAGE_CHARS, sanitize_provider_error
from incident_report.domain import Role
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.model_calls import ModelCall, ProviderError
from test_google_sdk_wire import _BYTES, FakeGoogle, _transport, _video

FAKE_KEY = "AI" + "za" + "SyFAKE" + "x" * 29  # same shape as a Gemini key, plainly fake
HIGH_DEMAND = "This model is currently experiencing high demand. Spikes in demand are usually temporary."


class FakeApiError(Exception):
    """Same attributes as google.genai.errors.APIError, whose str() holds the raw details."""

    def __init__(self, code: object, status: object, message: object, details: object = None) -> None:
        self.code, self.status, self.message, self.details = code, status, message, details
        super().__init__(f"{code} {status}. {details}")


def test_the_real_provider_message_status_word_and_http_status_are_kept() -> None:
    result = sanitize_provider_error(FakeApiError(503, "UNAVAILABLE", HIGH_DEMAND))

    assert result == ProviderError(http_status=503, status="UNAVAILABLE", message=HIGH_DEMAND)


def test_a_key_and_a_key_url_in_the_exception_text_are_removed() -> None:
    message = (
        f"Request to https://generativelanguage.googleapis.com/v1beta/models/m:generateContent?key={FAKE_KEY} failed "
        f"for key {FAKE_KEY}; uploaded files/abc123def, uri "
        "https://generativelanguage.googleapis.com/v1beta/files/abc123def; x-goog-api-key: secretvalue12345; "
        "Authorization: Bearer tokenvalue12345; api_key=hunter2hunter2 ... and then high demand."
    )
    details = {"error": {"message": message}, "headers": {"x-goog-api-key": FAKE_KEY}}

    for exc in (
        FakeApiError(503, "UNAVAILABLE", message, details),
        FakeApiError(503, "UNAVAILABLE", None, details),  # no message attribute: falls back to the exception text
    ):
        result = sanitize_provider_error(exc)
        dumped = result.model_dump_json()

        assert result.message is not None and "high demand" in result.message
        for leaked in (FAKE_KEY, "AIza", "key=", "?key", "googleapis.com", "abc123def", "secretvalue", "tokenvalue"):
            assert leaked not in dumped, leaked
        assert "hunter2" not in dumped
        assert "http://" not in dumped and "https://" not in dumped and "Bearer" not in dumped


def test_a_key_held_in_the_environment_is_removed_even_when_it_has_no_known_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY_3", "owner-key-with-odd-shape-0001")
    monkeypatch.setenv("GEMINI_API_KEY", "another-odd-key-value-0002")

    result = sanitize_provider_error(
        FakeApiError(
            429, "RESOURCE_EXHAUSTED", "quota for owner-key-with-odd-shape-0001 and another-odd-key-value-0002"
        )
    )

    assert result.message is not None
    assert "odd" not in result.message and "[redacted]" in result.message


def test_the_message_is_cut_to_300_characters_after_redaction() -> None:
    long_text = "demand " * 200
    result = sanitize_provider_error(FakeApiError(503, "UNAVAILABLE", long_text))

    assert result.message is not None and len(result.message) <= MAX_MESSAGE_CHARS == 300
    assert result.message.endswith("…")
    # A key that straddles the cut point is still removed, never half-kept.
    straddling = "x" * (MAX_MESSAGE_CHARS - 10) + " " + FAKE_KEY
    cut = sanitize_provider_error(FakeApiError(500, "INTERNAL", straddling)).message
    assert cut is not None and "AIza" not in cut and "SyFAKE" not in cut


def test_control_characters_and_whitespace_are_flattened() -> None:
    result = sanitize_provider_error(FakeApiError(500, "INTERNAL", "line one\r\nline\ttwo\x00 \x1b[31mred"))

    assert result.message is not None and "\n" not in result.message and "\x00" not in result.message
    assert "\x1b" not in result.message


@pytest.mark.parametrize(
    ("code", "status"),
    [("503", "unavailable"), (True, "unavailable"), (99, "UNAVAILABLE WITH SPACE"), (None, 5), (700, "x" * 80)],
)
def test_odd_status_values_are_dropped_not_stored(code: object, status: object) -> None:
    result = sanitize_provider_error(FakeApiError(code, status, "msg"))

    assert result.http_status is None and result.status is None and result.message == "msg"


def test_an_exception_that_misbehaves_still_yields_an_empty_record_not_a_crash() -> None:
    class Hostile(Exception):
        @property
        def code(self) -> int:
            raise RuntimeError("boom")

    assert sanitize_provider_error(Hostile("x")) == ProviderError()


def test_the_record_type_refuses_a_message_longer_than_300() -> None:
    with pytest.raises(ValueError, match="at most 300"):
        ProviderError(message="x" * 301)


# --- end to end over the real SDK with an in-memory HTTP server: no network, no key ----------------------


def _request() -> AIRequest:
    digest = sha256_hex(_BYTES)
    return AIRequest(incident_input_hash([(Role.EXTERIOR, digest)]), "overview-1", (AIViewRef(Role.EXTERIOR, digest),))


def _unavailable_body() -> dict[str, object]:
    leak = (
        f" Debug: https://generativelanguage.googleapis.com/v1beta/models/m:generateContent?key={FAKE_KEY}"
        " file https://generativelanguage.googleapis.com/v1beta/files/synthetic"
    )
    return {"error": {"code": 503, "message": HIGH_DEMAND + leak, "status": "UNAVAILABLE"}}


def test_a_503_over_the_real_sdk_is_recorded_with_its_real_reason_and_no_secret(tmp_path: Path) -> None:
    settings = GeminiSettings(model="gemini-3.8-flash", max_retries=0)
    fake = FakeGoogle(generate=(503, _unavailable_body()))
    connection = GeminiAIConnection(settings, _video(tmp_path), transport=_transport(fake, settings))

    with pytest.raises(AIConnectionError) as failure:
        connection.analyse(_request())

    # The message people see is unchanged and plain.
    assert str(failure.value) == "Gemini overview failed; retry later. No report was produced."
    (call,) = failure.value.calls
    assert call.error == "Gemini request failed." and call.status_code == 503
    assert call.provider_error is not None
    assert call.provider_error.http_status == 503 and call.provider_error.status == "UNAVAILABLE"
    assert call.provider_error.message is not None and call.provider_error.message.startswith(HIGH_DEMAND)
    stored = json.dumps(call.model_dump(mode="json"))  # what the Run log persists
    for leaked in (FAKE_KEY, "AIza", "key=", "googleapis.com", "files/synthetic", "unit-test-placeholder"):
        assert leaked not in stored, leaked
    assert ModelCall.model_validate(json.loads(stored)) == call  # round-trips through the stored JSON


def test_the_transport_error_text_stays_plain_whatever_the_provider_said(tmp_path: Path) -> None:
    settings = GeminiSettings(model="gemini-3.8-flash")
    fake = FakeGoogle(generate=(503, _unavailable_body()))

    with pytest.raises(TransportError) as raised:
        _transport(fake, settings).generate(_video(tmp_path), "observe", settings)

    assert str(raised.value) == "Gemini request failed." and raised.value.status_code == 503
    assert "high demand" not in str(raised.value)
    assert raised.value.provider_error is not None and raised.value.provider_error.status == "UNAVAILABLE"


def test_calls_recorded_before_this_change_still_load_without_a_provider_error() -> None:
    old = {
        "request_hash": "a" * 64,
        "input_hash": "b" * 64,
        "prompt_version": "overview-1",
        "model": "gemini-3.8-flash",
        "view_id": "c" * 64,
        "fps": 1.0,
        "raw_response": None,
        "latency_ms": 5.0,
        "retry_index": 0,
        "status_code": 503,
        "error": "Gemini request failed.",
    }

    assert ModelCall.model_validate(old).provider_error is None


def test_the_dense_pass_records_the_provider_error_too(tmp_path: Path) -> None:
    from incident_report.ai.dense import CandidateWindow, DensePassAIConnection, DenseSettings

    class FailingDense:
        def generate_window(self, *args: object, **kwargs: object) -> str:
            raise TransportError(503, sanitize_provider_error(FakeApiError(503, "UNAVAILABLE", HIGH_DEMAND)))

    digest = sha256_hex(_BYTES)
    request = AIRequest(
        incident_input_hash([(Role.EXTERIOR, digest)]),
        "dense-1",
        (AIViewRef(Role.EXTERIOR, digest, duration_seconds=5.0),),
    )
    window = CandidateWindow(
        view_id=digest, candidate_timestamp=0.75, start_seconds=0.0, end_seconds=2.75, candidate_description="contact"
    )
    connection = DensePassAIConnection(
        DenseSettings(model="gemini-3.8-flash", fps=20.0, max_retries=0),
        _video(tmp_path),
        transport=FailingDense(),
        sleep=lambda _s: None,
    )

    with pytest.raises(AIConnectionError) as failure:
        connection.analyse_window(request, window)

    (call,) = failure.value.calls
    assert call.provider_error == ProviderError(http_status=503, status="UNAVAILABLE", message=HIGH_DEMAND)
