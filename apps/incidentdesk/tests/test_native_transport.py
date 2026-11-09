"""Local worker transport failure and timeout contracts."""

from email.message import Message

import pytest

from incident_report.reconstruction import native_client


@pytest.mark.parametrize("action", ["reconstruction_export", "export_attach", "snapshot_request"])
def test_exports_and_snapshots_use_long_timeout(action: str, monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[int] = []

    class Reply:
        status = 200
        headers = Message()

        def __enter__(self) -> "Reply":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self, limit: int) -> bytes:
            return b"{}"

    class Opener:
        def open(self, request: object, *, timeout: int) -> Reply:
            timeouts.append(timeout)
            return Reply()

    monkeypatch.setenv("INCIDENTDESK_TOOLS_TOKEN", "test-local-token")
    monkeypatch.setattr(native_client, "build_opener", lambda *args: Opener())
    native_client.ToolsWorker().exchange("/cases/synthetic/replay/run", {"action": action})
    assert timeouts == [600]
