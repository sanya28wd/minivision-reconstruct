"""Sanitized provider error for the model-call record.

A provider failure is kept as three small facts: the HTTP status, the provider's own status word (for example
UNAVAILABLE or RESOURCE_EXHAUSTED) and its message, redacted and cut to 300 characters. Nothing else from the
exception is kept: never headers, request bodies, URLs, the uploaded file's address or any key. The user-facing
error text stays plain and carries none of this (it only lives on the stored call, for diagnosis).
"""

from __future__ import annotations

import os
import re

from incident_report.model_calls import ProviderError

MAX_MESSAGE_CHARS = 300
_REDACTED = "[redacted]"

_STATUS_WORD = re.compile(r"[A-Z][A-Z0-9_]{1,39}")
# Every URL goes, whole: a query string can carry a key and a Files API address names the uploaded video.
_URL = re.compile(r"(?:https?|wss?|ftp)://\S+", re.IGNORECASE)
_FILE_NAME = re.compile(r"\bfiles/[A-Za-z0-9_\-]+")  # "files/abc123" as the Files API names an upload
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")
_AUTH_HEADER = re.compile(r"(?i)\b(?:authorization|proxy-authorization)\b\s*[:=]\s*(?:bearer\s+|basic\s+)?\S+")
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_NAMED_SECRET = re.compile(
    r"(?i)\b(?:x-goog-api-key|api[_-]?key|access[_-]?token|key|token|secret|password)\b\s*[:=]\s*\S+"
)
_KEY_ENV_NAME = re.compile(r"(?:GOOGLE|GEMINI)_API_KEY(?:_\d+)?")


def _configured_keys() -> list[str]:
    """The keys this process holds in its environment (the owner's GOOGLE_API_KEY_n names included)."""
    values = [v.strip() for name, v in os.environ.items() if _KEY_ENV_NAME.fullmatch(name) and len(v.strip()) >= 8]
    return sorted(set(values), key=len, reverse=True)


def redact(text: str) -> str:
    """Remove keys, credential headers, URLs and uploaded-file names from provider text."""
    for key in _configured_keys():
        text = text.replace(key, _REDACTED)
    text = _URL.sub(_REDACTED, text)
    text = _AUTH_HEADER.sub(_REDACTED, text)
    text = _BEARER.sub(_REDACTED, text)
    text = _NAMED_SECRET.sub(_REDACTED, text)
    text = _GOOGLE_KEY.sub(_REDACTED, text)
    return _FILE_NAME.sub(f"files/{_REDACTED}", text)


def clean_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> str | None:
    """Redact, flatten whitespace and control characters, then cut to `limit` characters (last is an ellipsis)."""
    flat = " ".join(redact(text).split())
    flat = "".join(ch for ch in flat if ch.isprintable())
    if not flat:
        return None
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _message_text(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message
    return str(exc)  # an APIError's own text is "<code> <STATUS>. <details>"; redaction and the cap still apply


def sanitize_provider_error(exc: BaseException) -> ProviderError:
    """Reduce a provider exception to its safe diagnostic fields. Never raises."""
    try:
        code = getattr(exc, "code", None)
        http_status = code if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599 else None
        status = getattr(exc, "status", None)
        status_word = status if isinstance(status, str) and _STATUS_WORD.fullmatch(status) else None
        return ProviderError(http_status=http_status, status=status_word, message=clean_message(_message_text(exc)))
    except Exception:
        return ProviderError()
