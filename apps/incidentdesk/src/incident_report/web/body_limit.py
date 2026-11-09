"""Request body size limiter for streaming and chunked uploads (RPT-52 carry-over)."""

from __future__ import annotations

from starlette.requests import Request


class BodyTooLargeError(Exception):
    """Raised when request body stream exceeds the allowed byte limit."""

    def __init__(self, message: str = "Request body exceeds maximum size limit.") -> None:
        super().__init__(message)


async def read_limited_body(request: Request, max_bytes: int) -> bytes:
    """Read request body stream in chunks up to max_bytes.

    If the body exceeds max_bytes, stops reading immediately and raises BodyTooLargeError
    without buffering the excess bytes into memory or spooling to disk.
    If the body is within max_bytes, saves the buffered bytes on `request._body` so
    subsequent `request.form()` or `request.body()` calls consume the buffered content
    without re-reading or raising 'Stream consumed'.
    """
    if hasattr(request, "_body"):
        if len(request._body) > max_bytes:
            raise BodyTooLargeError(f"Request body exceeds limit of {max_bytes} bytes.")
        return request._body

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise BodyTooLargeError(f"Request body exceeds limit of {max_bytes} bytes.")
        chunks.append(chunk)

    body = b"".join(chunks)
    request._body = body
    return body
