"""Local reconstruction API, used only by the IncidentDesk server.

Starlette routes and pure ASGI middleware follow the official routing/middleware
documentation. The launcher, rather than this factory, owns the loopback listener.
"""

from hmac import compare_digest
from pathlib import Path
import re

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route


class LocalAccess:
    """Reject remote clients and require a token even for health and unknown URLs."""

    def __init__(self, app, token):
        self.app = app
        self.expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        peer = scope.get("client")
        credentials = [
            value for key, value in scope["headers"] if key.lower() == b"authorization"
        ]
        if peer is None or peer[0] != "127.0.0.1":
            response = JSONResponse({"error": "Local access required"}, status_code=403)
        elif len(credentials) != 1 or not compare_digest(credentials[0], self.expected):
            response = JSONResponse(
                {"error": "Authentication required"}, status_code=401
            )
        else:
            if scope["method"] not in {"GET", "HEAD"}:
                # Project assumption: JSON edits fit 2 MiB. Bound chunked bodies too.
                body = bytearray()
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > 2 * 1024 * 1024:
                        response = JSONResponse(
                            {"error": "Request too large"},
                            status_code=413,
                            headers={"Cache-Control": "no-store"},
                        )
                        await response(scope, receive, send)
                        return
                    if not message.get("more_body", False):
                        break

                original_receive = receive
                delivered = False

                async def buffered_receive():
                    nonlocal delivered
                    if delivered:
                        return await original_receive()
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }

                receive = buffered_receive

            async def no_cache(message):
                if message["type"] == "http.response.start":
                    message.setdefault("headers", []).append(
                        (b"cache-control", b"no-store")
                    )
                await send(message)

            await self.app(scope, receive, no_cache)
            return
        response.headers["Cache-Control"] = "no-store"
        await response(scope, receive, send)


def create_app(
    cases_root: Path, token: str, exports_root: Path | None = None
) -> Starlette:
    """Create an authenticated worker; never discover tokens or cases implicitly."""
    if (
        not token
        or not token.strip()
        or any(ord(char) < 33 or ord(char) > 126 for char in token)
    ):
        raise ValueError("Configure a nonempty printable local token")
    if (
        not cases_root.is_absolute()
        or cases_root.is_symlink()
        or not cases_root.is_dir()
    ):
        raise ValueError("Configure an existing absolute cases directory without links")
    root = cases_root.resolve(strict=True)
    if exports_root is not None and (
        not exports_root.is_absolute()
        or not exports_root.is_dir()
        or any(path.is_symlink() for path in (exports_root, *exports_root.parents))
    ):
        raise ValueError("Configure an existing local exports directory without links")
    if exports_root is not None and (
        exports_root.resolve().is_relative_to(root)
        or root.is_relative_to(exports_root.resolve())
    ):
        raise ValueError("Cases and exports directories must be separate")

    async def health(request):
        return JSONResponse({"status": "ok"})

    async def selected_run(request):
        case_id = request.path_params["case_id"]
        run_id = request.path_params["run_id"]
        try:
            case = select_case(root, case_id)
            run = confined_child(case / "analysis" / "reconstruction", run_id)
            if not (run / "manifest.json").is_file():
                raise ValueError("Missing run")
        except (ValueError, OSError):
            return JSONResponse({"error": "Case or run unavailable"}, status_code=404)
        return JSONResponse({"case_id": case_id, "run_id": run_id})

    from integration.tools_worker.tracks import routes as track_routes
    from integration.tools_worker.calibration import routes as calibration_routes
    from integration.tools_worker.replay import routes as replay_routes

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/cases/{case_id}/runs/{run_id}", selected_run),
            *track_routes(root, select_case),
            *calibration_routes(root, select_case),
            *replay_routes(root, select_case, exports_root),
        ],
        middleware=[Middleware(LocalAccess, token=token)],
    )


def confined_child(root: Path, identifier: str) -> Path:
    """IDs are single path components, never paths; links are refused, not followed."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identifier):
        raise ValueError("Invalid identifier")
    path = root / identifier
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Unavailable directory")
    if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ValueError("Outside root")
    return path


def select_case(root: Path, identifier: str) -> Path:
    case = confined_child(root, identifier)
    if any(path.is_symlink() for path in case.rglob("*")):
        raise ValueError("Linked case")
    return case
