"""Run only in the collaborator environment; no shared app imports or dependencies."""

import os
from pathlib import Path

import uvicorn

from integration.tools_worker.app import create_app


def main():
    if os.environ.get("INCIDENTDESK_NATIVE_TOOLS", "").strip().lower() not in {
        "true",
        "1",
    }:
        raise SystemExit("Native tools are disabled")
    required = (
        "INCIDENTDESK_CASES_ROOT",
        "INCIDENTDESK_EVIDENCE_EXPORTS_ROOT",
        "INCIDENTDESK_TOOLS_TOKEN",
    )
    if any(not os.environ.get(name) for name in required):
        raise SystemExit("Configure cases root, exports root and shared local token")
    app = create_app(
        Path(os.environ[required[0]]),
        os.environ[required[2]],
        Path(os.environ[required[1]]),
    )
    uvicorn.run(app, host="127.0.0.1", port=8520, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
