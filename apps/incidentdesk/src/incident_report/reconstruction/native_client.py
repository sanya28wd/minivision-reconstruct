"""Server-only loopback transport. The browser never receives the local token."""

import json
import os
import re
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ToolsUnavailable(ValueError):
    """Sanitized worker/configuration failure, suitable for the app's error page."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class ToolsRefused(ToolsUnavailable):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class ToolsWorker:
    def exchange(
        self, path: str, body: object = None, byte_range: str | None = None
    ) -> tuple[bytes, str, int, dict[str, str]]:
        token = os.environ.get("INCIDENTDESK_TOOLS_TOKEN", "")
        if not token or not path.startswith("/cases/") or "\r" in path or "\n" in path:
            raise ToolsUnavailable("Local tools are not configured.")
        headers = {"Authorization": f"Bearer {token}"}
        if byte_range is not None:
            headers["Range"] = byte_range
        data = None if body is None else json.dumps(body, allow_nan=False).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        # Fixed loopback URL, no redirects and no environment proxy (token stays local).
        request = Request("http://127.0.0.1:8520" + path, data=data, headers=headers)
        try:
            timeout = (
                600
                if isinstance(body, dict)
                and body.get("action") in {"reconstruction_export", "export_attach", "snapshot_request"}
                else 60
            )
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
                # Project assumption, to be validated: a JSON/PNG response fits 32 MiB.
                payload = response.read(32 * 1024 * 1024 + 1)
                if len(payload) > 32 * 1024 * 1024:
                    raise ToolsUnavailable("Local tools response exceeds the configured limit.")
                forwarded = {
                    key: response.headers[key] for key in ("Content-Range", "Accept-Ranges") if key in response.headers
                }
                return payload, response.headers.get_content_type(), response.status, forwarded
        except HTTPError as error:
            try:
                failure = json.loads(error.read(4096))
            except (ValueError, OSError):
                failure = None
            if error.code in {400, 409, 422} and isinstance(failure, dict) and failure.get("write_state") == "refused":
                raise ToolsRefused(
                    str(failure.get("error", "Local tool refused the action"))[:500], error.code
                ) from error
            raise ToolsUnavailable("Local tools are unavailable or refused the request.") from error
        except (URLError, OSError, ValueError) as error:
            raise ToolsUnavailable("Local tools are unavailable or refused the request.") from error

    def json(self, path: str, body: object = None) -> dict[str, Any]:
        try:
            value = json.loads(self.exchange(path, body)[0])
            if not isinstance(value, dict):
                raise ValueError("Expected object")
            return value
        except ToolsUnavailable:
            raise
        except (ValueError, TypeError) as error:
            raise ToolsUnavailable("Local tools returned an invalid response.") from error

    def stream(self, path: str, byte_range: str | None) -> tuple[Iterator[bytes], str, int, dict[str, str]]:
        token = os.environ.get("INCIDENTDESK_TOOLS_TOKEN", "")
        if not token or not path.startswith("/cases/") or "\r" in path or "\n" in path:
            raise ToolsUnavailable("Local tools are not configured.")
        headers = {"Authorization": f"Bearer {token}"}
        if byte_range:
            if not re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", byte_range):
                raise ToolsUnavailable("Use a single valid byte range.")
            headers["Range"] = byte_range
        try:
            response = build_opener(ProxyHandler({}), NoRedirect()).open(
                Request("http://127.0.0.1:8520" + path, headers=headers), timeout=60
            )
        except (HTTPError, URLError, OSError, ValueError) as error:
            raise ToolsUnavailable("Local replay video is unavailable.") from error

        def chunks() -> Iterator[bytes]:
            try:
                while block := response.read(64 * 1024):
                    yield block
            finally:
                response.close()

        forwarded = {
            key: response.headers[key]
            for key in ("Content-Range", "Accept-Ranges", "Content-Length")
            if key in response.headers
        }
        return chunks(), response.headers.get_content_type(), response.status, forwarded
