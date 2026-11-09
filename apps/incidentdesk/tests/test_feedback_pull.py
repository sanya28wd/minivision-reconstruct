"""Tests for the local feedback pull command and admin feedback JSON endpoint (RPT-45)."""

from __future__ import annotations

import io
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from audit_test_helpers import unsafe_test_only_truncate
from incident_report.ai import ReplayAIConnection
from incident_report.config import Settings
from incident_report.feedback import (
    FeedbackCategory,
    FeedbackStore,
    FeedbackSubmission,
)
from incident_report.feedback_pull import main, pull_feedback
from incident_report.hashing import sha256_hex
from incident_report.runlog import RunLog
from incident_report.storage import create_evidence_store
from incident_report.web import create_app


@pytest.fixture
def test_setup(
    recordings_dir: Path, run_log: RunLog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, Any]:
    unsafe_test_only_truncate(run_log.database_url, "audit_log", "anonymous_sessions", "share_links")
    import psycopg

    with psycopg.connect(run_log.database_url) as conn:
        conn.execute("DELETE FROM feedback")
    admin_secret = "synthetic-test-admin-secret-45"  # noqa: S105
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", admin_secret)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("INCIDENT_EVIDENCE_DIR", str(evidence_dir))

    settings = Settings.from_env(
        {
            "DATABASE_URL": run_log.database_url,
            "INCIDENT_EVIDENCE_DIR": str(evidence_dir),
        }
    )
    store = create_evidence_store(settings)
    app = create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=store)
    admin_client = TestClient(app, base_url="https://testserver", headers={"Authorization": f"Bearer {admin_secret}"})
    anon_client = TestClient(app, base_url="https://testserver")

    # Create a valid share link for visitor testing
    link_info = admin_client.post("/admin/links").json()
    visitor_client = TestClient(app, base_url="https://testserver")
    opened = visitor_client.post(
        "/access/open",
        json={"link_id": link_info["link_id"], "secret": link_info["secret"]},
    )
    assert opened.status_code == 200

    return {
        "app": app,
        "admin_client": admin_client,
        "visitor_client": visitor_client,
        "anon_client": anon_client,
        "admin_secret": admin_secret,
        "fb_store": FeedbackStore(run_log.database_url),
        "evidence": store,
        "database_url": run_log.database_url,
    }


def test_feedback_inbox_is_gitignored() -> None:
    root = Path(__file__).resolve().parent.parent
    paths = [
        "feedback_inbox/",
        "feedback_inbox/2026-10-16.md",
        "feedback_inbox/.pulled",
        "feedback_inbox/screenshots/test.png",
    ]
    for p in paths:
        proc = subprocess.run(["git", "check-ignore", p], cwd=root, capture_output=True, text=True)
        assert proc.returncode == 0, f"{p} is not gitignored"


def test_admin_feedback_json_requires_admin_bearer_auth(test_setup: dict[str, Any]) -> None:
    admin_client: TestClient = test_setup["admin_client"]
    visitor_client: TestClient = test_setup["visitor_client"]
    anon_client: TestClient = test_setup["anon_client"]
    fb_store: FeedbackStore = test_setup["fb_store"]

    fb_id = uuid.uuid4()
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb_id,
            created_at=datetime.now(UTC),
            text="Testing json endpoint auth.",
            category=FeedbackCategory.BUG,
        )
    )

    # 1. Anonymous -> 403
    assert anon_client.get("/admin/feedback.json").status_code == 403

    # 2. Share session visitor -> 403
    assert visitor_client.get("/admin/feedback.json").status_code == 403

    # 3. Invalid admin bearer token -> 403
    bad_admin = TestClient(test_setup["app"], base_url="https://testserver", headers={"Authorization": "Bearer wrong"})
    assert bad_admin.get("/admin/feedback.json").status_code == 403

    # 4. Valid admin bearer token -> 200 JSON
    resp = admin_client.get("/admin/feedback.json")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["feedback_id"] == str(fb_id)
    assert data[0]["text"] == "Testing json endpoint auth."
    assert data[0]["category"] == "bug"


def test_feedback_pull_downloads_entries_and_screenshots(test_setup: dict[str, Any], tmp_path: Path) -> None:
    fb_store: FeedbackStore = test_setup["fb_store"]
    evidence = test_setup["evidence"]
    admin_client: TestClient = test_setup["admin_client"]
    admin_secret: str = test_setup["admin_secret"]

    # Store a synthetic screenshot
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    shot_sha = sha256_hex(png_bytes)
    evidence.put(io.BytesIO(png_bytes), sha256=shot_sha)

    fb_id = uuid.uuid4()
    run_id = uuid.uuid4()
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb_id,
            created_at=datetime.now(UTC),
            text="The speed calculation seems slightly off on camera 2.",
            category=FeedbackCategory.WRONG_RESULT,
            name="Officer Al Mansoori",
            screenshot_sha256=shot_sha,
            screen="/reports/123",
            run_id=run_id,
            incident_id="INC-2026-001",
        )
    )

    inbox_dir = tmp_path / "inbox"
    result = pull_feedback(
        base_url="https://testserver",
        admin_secret=admin_secret,
        inbox_dir=inbox_dir,
        client=admin_client,
    )
    assert result.pulled_count == 1

    # Verify .pulled contains feedback id
    pulled_file = inbox_dir / ".pulled"
    assert pulled_file.exists()
    pulled_ids = pulled_file.read_text(encoding="utf-8").splitlines()
    assert str(fb_id) in pulled_ids

    # Verify dated markdown file created
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    md_file = inbox_dir / f"{today}.md"
    assert md_file.exists()
    content = md_file.read_text(encoding="utf-8")
    assert str(fb_id) in content
    assert "Officer Al Mansoori" in content
    assert "wrong result" in content
    assert "The speed calculation seems slightly off on camera 2." in content
    assert str(run_id) in content
    assert "INC-2026-001" in content
    assert f"screenshots/{shot_sha}.png" in content

    # Verify screenshot downloaded
    shot_file = inbox_dir / "screenshots" / f"{shot_sha}.png"
    assert shot_file.exists()
    assert shot_file.read_bytes() == png_bytes


def test_feedback_pull_is_idempotent(test_setup: dict[str, Any], tmp_path: Path) -> None:
    fb_store: FeedbackStore = test_setup["fb_store"]
    admin_client: TestClient = test_setup["admin_client"]
    admin_secret: str = test_setup["admin_secret"]

    fb_id1 = uuid.uuid4()
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb_id1,
            created_at=datetime.now(UTC),
            text="First feedback entry.",
            category=FeedbackCategory.CONFUSING,
        )
    )

    inbox_dir = tmp_path / "inbox"
    res1 = pull_feedback(
        base_url="https://testserver",
        admin_secret=admin_secret,
        inbox_dir=inbox_dir,
        client=admin_client,
    )
    assert res1.pulled_count == 1

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    md_file = inbox_dir / f"{today}.md"
    content_run1 = md_file.read_text(encoding="utf-8")

    # Second pull: nothing new
    res2 = pull_feedback(
        base_url="https://testserver",
        admin_secret=admin_secret,
        inbox_dir=inbox_dir,
        client=admin_client,
    )
    assert res2.pulled_count == 0
    assert md_file.read_text(encoding="utf-8") == content_run1

    # Add a second feedback item
    fb_id2 = uuid.uuid4()
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb_id2,
            created_at=datetime.now(UTC),
            text="Second feedback entry.",
            category=FeedbackCategory.OTHER,
        )
    )

    res3 = pull_feedback(
        base_url="https://testserver",
        admin_secret=admin_secret,
        inbox_dir=inbox_dir,
        client=admin_client,
    )
    assert res3.pulled_count == 1
    content_run3 = md_file.read_text(encoding="utf-8")
    assert str(fb_id1) in content_run3
    assert str(fb_id2) in content_run3
    # First entry was not duplicated
    assert content_run3.count(str(fb_id1)) == 1


def test_feedback_pull_never_logs_or_writes_admin_secret(
    test_setup: dict[str, Any], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    admin_client: TestClient = test_setup["admin_client"]
    admin_secret: str = test_setup["admin_secret"]
    fb_store: FeedbackStore = test_setup["fb_store"]

    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=uuid.uuid4(),
            created_at=datetime.now(UTC),
            text="Secret leak testing.",
        )
    )

    inbox_dir = tmp_path / "inbox"
    pull_feedback(
        base_url="https://testserver",
        admin_secret=admin_secret,
        inbox_dir=inbox_dir,
        client=admin_client,
    )

    # 1. Check caplog
    assert admin_secret not in caplog.text

    # 2. Check all files in inbox_dir recursively
    for p in inbox_dir.rglob("*"):
        if p.is_file():
            raw = p.read_bytes()
            assert admin_secret.encode("utf-8") not in raw, f"Secret found in {p}"


def test_feedback_pull_escapes_untrusted_text_and_caps_lengths(test_setup: dict[str, Any], tmp_path: Path) -> None:
    fb_store: FeedbackStore = test_setup["fb_store"]
    admin_client: TestClient = test_setup["admin_client"]
    admin_secret: str = test_setup["admin_secret"]

    injection_text = (
        "# Injected Heading\n"
        "[Click Here For Free Gold](https://evil.example)\n"
        "<script>alert('xss')</script>\n"
        "```python\nprint('fenced injection')\n```"
    )
    overlong_text = "x" * 6000

    fb_id = uuid.uuid4()
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=fb_id,
            created_at=datetime.now(UTC),
            text=injection_text + "\n" + overlong_text,
            name="<evil name>[malicious link](https://evil.example)",
            screen="/reports/<evil>",
        )
    )

    inbox_dir = tmp_path / "inbox"
    pull_feedback(
        base_url="https://testserver",
        admin_secret=admin_secret,
        inbox_dir=inbox_dir,
        client=admin_client,
    )

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    md_file = inbox_dir / f"{today}.md"
    content = md_file.read_text(encoding="utf-8")

    # Verify untrusted text is placed inside the code fence and cannot escape
    assert "````text\n" in content
    assert "\n````" in content
    fence_start = content.index("````text\n")
    fence_end = content.index("\n````", fence_start)

    # Outside the fence, no injected heading, script, or malicious link exists
    outside_fence = content[:fence_start] + content[fence_end:]
    assert "# Injected Heading" not in outside_fence
    assert "https://evil.example" not in outside_fence
    assert "<script>" not in outside_fence

    # Length capped: total entry text should not exceed bounded length
    assert len(content) < 6500


def test_cli_main_feedback_pull(test_setup: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin_secret: str = test_setup["admin_secret"]
    fb_store: FeedbackStore = test_setup["fb_store"]
    fb_store.record_feedback(
        FeedbackSubmission(
            feedback_id=uuid.uuid4(),
            created_at=datetime.now(UTC),
            text="CLI main test.",
        )
    )

    inbox_dir = tmp_path / "cli_inbox"
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", admin_secret)

    # Missing base-url argument -> exits 2
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2

    # With missing secret in env -> exits 1
    monkeypatch.delenv("INCIDENT_ADMIN_SECRET")
    exit_code = main(["--base-url", "https://testserver", "--inbox-dir", str(inbox_dir)])
    assert exit_code == 1


def test_admin_bearer_rejected_on_non_admin_routes(test_setup: dict[str, Any]) -> None:
    admin_client: TestClient = test_setup["admin_client"]
    # 1. Bearer to GET /reports/{id} without visitor session -> 403
    some_id = uuid.uuid4()
    assert admin_client.get(f"/reports/{some_id}").status_code == 403

    # 2. Bearer to POST /reports without visitor session -> 403
    assert admin_client.post("/reports", data={"role": "exterior"}).status_code == 403

    # 3. Bearer to GET /feedback without visitor session -> 403
    assert admin_client.get("/feedback").status_code == 403


def test_pull_refuses_redirects_and_insecure_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "test-secret")
    # A. http on remote host is refused
    with pytest.raises(ValueError, match="HTTPS"):
        pull_feedback("http://insecure.example.com", inbox_dir=tmp_path)

    # B. Redirect raises and does not follow
    class RedirectClient:
        def get(self, url: str, headers: dict[str, str]) -> Any:
            class DummyResp:
                status_code = 302
                content = b""
                headers = {"location": "https://attacker.example.com"}

            return DummyResp()

    with pytest.raises(RuntimeError, match="Redirect"):
        pull_feedback("https://secure.example.com", inbox_dir=tmp_path, client=RedirectClient())


def test_failed_or_invalid_screenshot_not_marked_pulled(test_setup: dict[str, Any], tmp_path: Path) -> None:
    admin_secret: str = test_setup["admin_secret"]
    fb_id = uuid.uuid4()
    valid_sha = "a" * 64

    class FakeClient:
        def __init__(self, shot_status: int, shot_body: bytes) -> None:
            self.shot_status = shot_status
            self.shot_body = shot_body

        def get(self, url: str, headers: dict[str, str]) -> Any:
            class Resp:
                def __init__(self, code: int, content: bytes) -> None:
                    self.status_code = code
                    self.content = content

            if "feedback.json" in url:
                return Resp(
                    200,
                    json.dumps(
                        [
                            {
                                "feedback_id": str(fb_id),
                                "created_at": "2026-10-16T12:00:00Z",
                                "text": "Screenshot test.",
                                "screenshot_sha256": valid_sha,
                            }
                        ]
                    ).encode(),
                )
            return Resp(self.shot_status, self.shot_body)

    inbox = tmp_path / "inbox"
    # Case A: 404 on screenshot -> raises, not in .pulled
    with pytest.raises(RuntimeError, match="screenshot|failed"):
        pull_feedback("https://testserver", admin_secret=admin_secret, inbox_dir=inbox, client=FakeClient(404, b""))
    pulled = inbox / ".pulled"
    if pulled.exists():
        assert str(fb_id) not in pulled.read_text(encoding="utf-8")

    # Case B: 429 on screenshot -> raises, not in .pulled
    with pytest.raises(RuntimeError, match="screenshot|failed|rate limit|429"):
        pull_feedback("https://testserver", admin_secret=admin_secret, inbox_dir=inbox, client=FakeClient(429, b""))

    # Case C: non-image bytes -> raises, not in .pulled
    with pytest.raises(RuntimeError, match="screenshot|failed|image"):
        pull_feedback(
            "https://testserver",
            admin_secret=admin_secret,
            inbox_dir=inbox,
            client=FakeClient(200, b"not image bytes"),
        )

    # Case D: overlong bytes (>5MB) -> raises, not in .pulled
    with pytest.raises(RuntimeError, match="screenshot|failed|size|exceed"):
        pull_feedback(
            "https://testserver",
            admin_secret=admin_secret,
            inbox_dir=inbox,
            client=FakeClient(200, b"x" * (6 * 1024 * 1024)),
        )


def test_pull_crash_safety_skips_entries_already_in_markdown(test_setup: dict[str, Any], tmp_path: Path) -> None:
    admin_secret: str = test_setup["admin_secret"]
    fb_id = uuid.uuid4()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    md_file = inbox / f"{today}.md"
    md_file.write_text(f"# Feedback Inbox - {today}\n\n## Feedback `{fb_id}`\n\nExisting entry.\n", encoding="utf-8")

    class FakeClient:
        def get(self, url: str, headers: dict[str, str]) -> Any:
            class Resp:
                status_code = 200
                content = json.dumps(
                    [
                        {
                            "feedback_id": str(fb_id),
                            "created_at": "2026-10-16T12:00:00Z",
                            "text": "Existing entry.",
                        }
                    ]
                ).encode()

            return Resp()

    res = pull_feedback("https://testserver", admin_secret=admin_secret, inbox_dir=inbox, client=FakeClient())
    assert res.pulled_count == 0
    content = md_file.read_text(encoding="utf-8")
    assert content.count(str(fb_id)) == 1


def test_main_cli_success_and_403_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    admin_secret = "secret-super-123"  # noqa: S105
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", admin_secret)
    inbox = tmp_path / "cli_inbox"

    import incident_report.feedback_pull as fp

    # Success path: one real item is pulled, written to the md and recorded in .pulled
    fb_id = uuid.uuid4()
    one_item = [
        {
            "feedback_id": str(fb_id),
            "created_at": "2026-10-16T12:00:00Z",
            "text": "Real item pulled through main().",
        }
    ]
    with patch.object(fp, "_http_get", return_value=(200, json.dumps(one_item).encode())):
        code = fp.main(["--base-url", "https://localhost:8000", "--inbox-dir", str(inbox)])
        assert code == 0
        captured = capsys.readouterr()
        assert "Successfully pulled 1 feedback item" in captured.out
        assert admin_secret not in captured.out
        assert admin_secret not in captured.err
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    md_text = (inbox / f"{today}.md").read_text(encoding="utf-8")
    assert str(fb_id) in md_text
    assert "Real item pulled through main()." in md_text
    assert str(fb_id) in (inbox / ".pulled").read_text(encoding="utf-8").splitlines()

    # 403 path
    with patch.object(fp, "_http_get", return_value=(403, b'{"detail":"Forbidden"}')):
        code = fp.main(["--base-url", "https://localhost:8000", "--inbox-dir", str(inbox)])
        assert code == 1
        captured = capsys.readouterr()
        assert "Authentication error" in captured.err
        assert admin_secret not in captured.out
        assert admin_secret not in captured.err


def test_injection_fencing_exact_cap_and_multiline_name(test_setup: dict[str, Any], tmp_path: Path) -> None:
    admin_secret: str = test_setup["admin_secret"]
    fb_id = uuid.uuid4()
    long_text = "a" * 6000
    bad_name = "Officer Smith\n# Injected Markdown Heading\n[link](https://evil.example)"

    class FakeClient:
        def get(self, url: str, headers: dict[str, str]) -> Any:
            class Resp:
                status_code = 200
                content = json.dumps(
                    [
                        {
                            "feedback_id": str(fb_id),
                            "created_at": "2026-10-16T12:00:00Z",
                            "text": long_text,
                            "name": bad_name,
                        }
                    ]
                ).encode()

            return Resp()

    inbox = tmp_path / "inbox"
    pull_feedback("https://testserver", admin_secret=admin_secret, inbox_dir=inbox, client=FakeClient())
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    content = (inbox / f"{today}.md").read_text(encoding="utf-8")

    # Extract text from fenced block
    fence_start = content.index("````text\n") + len("````text\n")
    fence_end = content.index("\n````", fence_start)
    extracted = content[fence_start:fence_end]
    # Exact length cap check
    assert len(extracted) == 5000

    # Outside fence: no newline-injected heading
    outside = content[:fence_start] + content[fence_end:]
    for line in outside.splitlines():
        assert not line.startswith("# Injected")


def test_malformed_feedback_items_handled_defensively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "sec")
    valid_id = uuid.uuid4()
    items = [
        "not a dict",
        {"no_feedback_id": 123},
        {"feedback_id": "not-a-valid-uuid"},
        {"feedback_id": str(valid_id), "text": "Valid item."},
    ]

    class FakeClient:
        def get(self, url: str, headers: dict[str, str]) -> Any:
            class Resp:
                status_code = 200
                content = json.dumps(items).encode()

            return Resp()

    res = pull_feedback("https://localhost:8000", inbox_dir=tmp_path, client=FakeClient())
    assert res.pulled_count == 1
    assert res.pulled_ids == [str(valid_id)]
    captured = capsys.readouterr()
    assert "malformed" in captured.err.lower()


def _shot_item(fb_id: uuid.UUID, sha: str | None, created: str) -> dict[str, Any]:
    item: dict[str, Any] = {"feedback_id": str(fb_id), "created_at": created, "text": f"Item {fb_id}."}
    if sha:
        item["screenshot_sha256"] = sha
    return item


def test_429_marks_every_remaining_screenshot_item_unpulled_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import incident_report.feedback_pull as fp

    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "secret-429")
    ids = [uuid.uuid4() for _ in range(3)]
    items = [_shot_item(fid, f"{i}" * 64, f"2026-10-16T12:0{i}:00Z") for i, fid in enumerate(ids, start=1)]

    def fake_get(url: str, headers: dict[str, str], client: Any = None, **kwargs: Any) -> tuple[int, bytes]:
        if "feedback.json" in url:
            return 200, json.dumps(items).encode()
        return 429, b""

    inbox = tmp_path / "inbox"
    with patch.object(fp, "_http_get", side_effect=fake_get):
        code = fp.main(["--base-url", "https://localhost:8000", "--inbox-dir", str(inbox)])
    assert code != 0
    capsys.readouterr()

    pulled = inbox / ".pulled"
    pulled_text = pulled.read_text(encoding="utf-8") if pulled.exists() else ""
    for fid in ids:
        assert str(fid) not in pulled_text
    for md in inbox.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        for fid in ids:
            assert str(fid) not in text


def test_429_still_writes_items_without_screenshots_but_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import incident_report.feedback_pull as fp

    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "secret-429b")
    plain_id, shot_a, shot_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    items = [
        _shot_item(shot_a, "a" * 64, "2026-10-16T12:01:00Z"),
        _shot_item(plain_id, None, "2026-10-16T12:02:00Z"),
        _shot_item(shot_b, "b" * 64, "2026-10-16T12:03:00Z"),
    ]

    def fake_get(url: str, headers: dict[str, str], client: Any = None, **kwargs: Any) -> tuple[int, bytes]:
        if "feedback.json" in url:
            return 200, json.dumps(items).encode()
        return 429, b""

    inbox = tmp_path / "inbox"
    with patch.object(fp, "_http_get", side_effect=fake_get), pytest.raises(RuntimeError):
        fp.pull_feedback("https://localhost:8000", inbox_dir=inbox)
    pulled = (inbox / ".pulled").read_text(encoding="utf-8").splitlines()
    assert pulled == [str(plain_id)]


def test_text_with_backtick_runs_cannot_close_the_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "secret-fence")
    fb_id = uuid.uuid4()
    run_lines = [("`" * n) for n in (3, 4, 5, 6, 8, 20)]
    hostile = "\n".join([*run_lines, "````", "# Breakout heading", "[x](https://evil.example)"])

    class FakeClient:
        def get(self, url: str, headers: dict[str, str]) -> Any:
            class Resp:
                status_code = 200
                content = json.dumps(
                    [{"feedback_id": str(fb_id), "created_at": "2026-10-16T12:00:00Z", "text": hostile}]
                ).encode()

            return Resp()

    inbox = tmp_path / "inbox"
    pull_feedback("https://testserver", inbox_dir=inbox, client=FakeClient())
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    content = (inbox / f"{today}.md").read_text(encoding="utf-8")

    lines = content.splitlines()
    open_idx = lines.index("````text")
    # Exactly one line of 4+ backticks after the opener: the real closing fence
    fence_lines = [i for i, ln in enumerate(lines) if i > open_idx and ln.startswith("````")]
    assert len(fence_lines) == 1
    body = lines[open_idx + 1 : fence_lines[0]]
    assert any("# Breakout heading" in ln for ln in body)
    for ln in body:
        assert "````" not in ln
    # The closing fence is the last line of the entry (nothing hostile follows it)
    assert all("Breakout" not in ln for ln in lines[fence_lines[0] :])


class _RedirectServer:
    """Local 127.0.0.1 server answering every request with a 302 and recording what it saw."""

    def __init__(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        outer = self
        self.requests: list[tuple[str, str | None]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append((self.path, self.headers.get("Authorization")))
                if self.path.startswith("/redirect-target"):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"[]")
                    return
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{outer.port}/redirect-target")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                return

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _RedirectServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def test_real_urllib_path_refuses_redirect_and_never_requests_target() -> None:
    import incident_report.feedback_pull as fp

    with _RedirectServer() as srv:
        with pytest.raises(RuntimeError, match="Redirect"):
            fp._http_get(
                f"http://127.0.0.1:{srv.port}/admin/feedback.json",
                {"Authorization": "Bearer local-secret"},
            )
        paths = [p for p, _ in srv.requests]
    assert paths == ["/admin/feedback.json"]
    assert not any(p.startswith("/redirect-target") for p in paths)


def test_real_urllib_path_reads_at_most_cap_plus_one_byte() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import incident_report.feedback_pull as fp
    from incident_report.feedback import MAX_SCREENSHOT_SIZE_BYTES

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(b"x" * (MAX_SCREENSHOT_SIZE_BYTES + 100_000))
            except OSError:
                pass

        def log_message(self, *args: Any) -> None:
            return

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        code, body = fp._http_get(
            f"http://127.0.0.1:{httpd.server_address[1]}/x",
            {},
            max_bytes=MAX_SCREENSHOT_SIZE_BYTES + 1,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert code == 200
    assert len(body) == MAX_SCREENSHOT_SIZE_BYTES + 1


def test_screenshot_fetch_asks_for_bounded_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import incident_report.feedback_pull as fp
    from incident_report.feedback import MAX_SCREENSHOT_SIZE_BYTES

    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "secret-cap")
    fb_id = uuid.uuid4()
    items = [_shot_item(fb_id, "c" * 64, "2026-10-16T12:00:00Z")]
    seen: list[int | None] = []

    def fake_get(
        url: str, headers: dict[str, str], client: Any = None, max_bytes: int | None = None
    ) -> tuple[int, bytes]:
        if "feedback.json" in url:
            return 200, json.dumps(items).encode()
        seen.append(max_bytes)
        return 200, b"x" * (MAX_SCREENSHOT_SIZE_BYTES + 1)

    with patch.object(fp, "_http_get", side_effect=fake_get), pytest.raises(RuntimeError):
        fp.pull_feedback("https://localhost:8000", inbox_dir=tmp_path / "inbox")
    assert seen == [MAX_SCREENSHOT_SIZE_BYTES + 1]


def test_testserver_host_is_not_allowed_over_plain_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENT_ADMIN_SECRET", "s")
    with pytest.raises(ValueError, match="HTTPS"):
        pull_feedback("http://testserver", inbox_dir=tmp_path)
