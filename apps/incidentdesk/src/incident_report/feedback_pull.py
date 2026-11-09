"""Local feedback pull command (RPT-45).

Downloads new feedback submissions and attached screenshots from the hosted app's
admin feedback endpoint into a local, gitignored Markdown inbox for triage.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incident_report.feedback import (
    MAX_FEEDBACK_TEXT_CHARS,
    MAX_NAME_CHARS,
    MAX_SCREENSHOT_SIZE_BYTES,
    FeedbackCategory,
    FeedbackStatus,
    detect_image_type,
    sanitize_screen_path,
)
from incident_report.hashing import sha256_hex
from incident_report.storage.interface import validate_sha256_key


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects to prevent Authorization header leakage (ADR 0010)."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise RuntimeError("Redirects are disallowed on authenticated feedback pull to prevent secret leakage.")

    def http_error_301(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        raise RuntimeError("Redirects are disallowed on authenticated feedback pull to prevent secret leakage.")

    def http_error_302(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        raise RuntimeError("Redirects are disallowed on authenticated feedback pull to prevent secret leakage.")

    def http_error_303(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        raise RuntimeError("Redirects are disallowed on authenticated feedback pull to prevent secret leakage.")

    def http_error_307(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        raise RuntimeError("Redirects are disallowed on authenticated feedback pull to prevent secret leakage.")

    def http_error_308(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> None:
        raise RuntimeError("Redirects are disallowed on authenticated feedback pull to prevent secret leakage.")


@dataclasses.dataclass(frozen=True)
class PullResult:
    """Outcome of pulling feedback from the hosted app."""

    pulled_count: int
    total_available: int
    inbox_path: Path | None
    pulled_ids: list[str]


def _escape_markdown_inline(text: str) -> str:
    """Escape markdown formatting characters to prevent injection in metadata lines."""
    for ch in ("\\", "`", "*", "_", "{", "}", "[", "]", "(", ")", "#", "+", "-", ".", "!", "<", ">"):
        text = text.replace(ch, f"\\{ch}")
    return text


def _format_feedback_entry(item: dict[str, Any], screenshot_link: str | None) -> str:
    """Format a single feedback item as a safe Markdown entry."""
    fb_id = str(item["feedback_id"])
    created_at = str(item.get("created_at") or "")
    category = str(item.get("category") or "unspecified")
    status = str(item.get("status") or "new")

    raw_name = item.get("name")
    if raw_name and isinstance(raw_name, str):
        # Normalize newlines in name to single spaces to prevent markdown injection
        clean_name = raw_name.replace("\r", " ").replace("\n", " ").strip()[:MAX_NAME_CHARS]
        safe_name = _escape_markdown_inline(clean_name)
    else:
        safe_name = "*Anonymous*"

    screen = sanitize_screen_path(item.get("screen"))
    run_id = item.get("run_id")
    incident_id = item.get("incident_id")
    browser = item.get("browser")

    lines = [
        f"## Feedback `{fb_id}`",
        "",
        f"- **Date:** {created_at}",
        f"- **Category:** {category}",
        f"- **Status:** {status}",
        f"- **Author:** {safe_name}",
        f"- **Screen:** `{screen}`",
    ]

    if run_id:
        lines.append(f"- **Report:** `/reports/{run_id}`")
    if incident_id:
        clean_incident = str(incident_id).replace("`", "")[:100]
        lines.append(f"- **Incident:** `{clean_incident}`")
    if browser:
        clean_browser = str(browser).replace("`", "")[:200]
        lines.append(f"- **Browser:** `{clean_browser}`")
    if screenshot_link:
        lines.append(f"- **Screenshot:** [{screenshot_link}]({screenshot_link})")

    # Untrusted feedback text is capped to MAX_FEEDBACK_TEXT_CHARS.
    # To prevent escaping the 4-backtick code fence, any 4-backtick sequences inside are neutralized.
    raw_text = str(item.get("text") or "")[:MAX_FEEDBACK_TEXT_CHARS]
    safe_text = raw_text
    while "````" in safe_text:
        safe_text = safe_text.replace("````", "``` `")

    lines.extend(
        [
            "",
            "````text",
            safe_text,
            "````",
        ]
    )
    return "\n".join(lines)


def _check_base_url(base_url: str) -> None:
    """Validate base_url: reject plaintext HTTP except for localhost/127.0.0.1."""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http":
        host = (parsed.hostname or "").lower()
        if host in ("localhost", "127.0.0.1"):
            return
        raise ValueError(f"Plaintext HTTP is not permitted for remote host '{host}'; base-url must use HTTPS.")
    raise ValueError(f"Invalid URL scheme: {base_url}")


def _http_get(
    url: str,
    headers: dict[str, str],
    client: Any = None,
    max_bytes: int | None = None,
) -> tuple[int, bytes]:
    """Perform an HTTP GET request, using a test client if provided or standard urllib with NoRedirectHandler.

    With ``max_bytes`` the urllib path reads at most that many bytes of the body, so an oversized
    response is never fully downloaded.
    """
    _check_base_url(url)

    if client is not None:
        resp = client.get(url, headers=headers)
        if getattr(resp, "status_code", None) in (301, 302, 303, 307, 308):
            raise RuntimeError("Redirects are disallowed on authenticated feedback pull to prevent secret leakage.")
        return resp.status_code, resp.content

    opener = urllib.request.build_opener(NoRedirectHandler())
    req = urllib.request.Request(url, headers=headers)  # noqa: S310
    try:
        with opener.open(req, timeout=30.0) as resp:  # noqa: S310
            return resp.status, resp.read(max_bytes) if max_bytes is not None else resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(max_bytes) if max_bytes is not None else exc.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error connecting to {url}: {exc.reason}") from None


def _find_already_rendered_ids(inbox_dir: Path) -> set[str]:
    """Scan existing Markdown files in inbox to extract feedback IDs already rendered."""
    rendered: set[str] = set()
    if not inbox_dir.exists():
        return rendered
    for p in inbox_dir.glob("*.md"):
        try:
            text = p.read_text(encoding="utf-8")
            matches = re.findall(r"## Feedback `([0-9a-f-]{36})`", text)
            rendered.update(matches)
        except (OSError, UnicodeDecodeError):
            continue
    return rendered


def pull_feedback(
    base_url: str,
    *,
    admin_secret: str | None = None,
    inbox_dir: Path | str = "feedback_inbox",
    client: Any = None,
) -> PullResult:
    """Download unpulled feedback and screenshots into the dated local inbox.

    Args:
        base_url: Base URL of the hosted application (e.g. 'https://example.com').
        admin_secret: The owner admin secret. Defaults to INCIDENT_ADMIN_SECRET env var.
        inbox_dir: Directory where the inbox markdown and screenshots are saved.
        client: Optional HTTP client (e.g. TestClient for tests).
    """
    secret = admin_secret or os.environ.get("INCIDENT_ADMIN_SECRET", "").strip()
    if not secret:
        raise ValueError("INCIDENT_ADMIN_SECRET is not configured.")

    _check_base_url(base_url)

    inbox = Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    pulled_file = inbox / ".pulled"

    pulled_ids: set[str] = set()
    if pulled_file.exists():
        pulled_ids = {line.strip() for line in pulled_file.read_text(encoding="utf-8").splitlines() if line.strip()}

    # Crash-safety: also consider IDs already rendered in existing inbox md files
    already_rendered = _find_already_rendered_ids(inbox)
    all_known_ids = pulled_ids | already_rendered

    # If any IDs were in md files but missing from .pulled (e.g. crash before .pulled write), backfill them
    missing_from_pulled = already_rendered - pulled_ids
    if missing_from_pulled:
        with pulled_file.open("a", encoding="utf-8") as pf:
            for mid in missing_from_pulled:
                pf.write(f"{mid}\n")
        pulled_ids.update(missing_from_pulled)

    clean_base = base_url.rstrip("/")
    api_url = f"{clean_base}/admin/feedback.json"
    headers = {"Authorization": f"Bearer {secret}"}

    status_code, body = _http_get(api_url, headers, client=client)
    if status_code == 403:
        raise PermissionError("Admin authentication refused. Verify INCIDENT_ADMIN_SECRET.")
    if status_code != 200:
        raise RuntimeError(f"Failed to fetch feedback: HTTP {status_code}")

    try:
        all_items_raw = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Failed to decode feedback JSON: {exc}") from exc

    if not isinstance(all_items_raw, list):
        raise ValueError("Expected a JSON list of feedback submissions.")

    # Defensive validation of each item
    valid_items: list[dict[str, Any]] = []
    for raw in all_items_raw:
        if not isinstance(raw, dict):
            sys.stderr.write("Skipping malformed feedback item: item is not a dictionary.\n")
            continue
        raw_id = raw.get("feedback_id")
        if not raw_id:
            sys.stderr.write("Skipping malformed feedback item: missing feedback_id.\n")
            continue
        try:
            valid_uuid = str(uuid.UUID(str(raw_id)))
        except (ValueError, TypeError):
            sys.stderr.write(f"Skipping malformed feedback item: invalid UUID '{raw_id}'.\n")
            continue

        item = dict(raw)
        item["feedback_id"] = valid_uuid

        # Validate run_id if present
        if item.get("run_id"):
            try:
                item["run_id"] = str(uuid.UUID(str(item["run_id"])))
            except (ValueError, TypeError):
                item["run_id"] = None

        # Validate category if present
        if item.get("category"):
            cat_str = str(item["category"]).strip()
            if cat_str in [c.value for c in FeedbackCategory]:
                item["category"] = cat_str
            else:
                item["category"] = "other"

        # Validate status if present
        if item.get("status"):
            stat_str = str(item["status"]).strip()
            if stat_str in [s.value for s in FeedbackStatus]:
                item["status"] = stat_str
            else:
                item["status"] = "new"

        valid_items.append(item)

    new_items = [it for it in valid_items if str(it["feedback_id"]) not in all_known_ids]

    if not new_items:
        return PullResult(
            pulled_count=0,
            total_available=len(valid_items),
            inbox_path=None,
            pulled_ids=[],
        )

    # Download screenshots for new items; any failure aborts that item from being marked pulled
    screenshot_map: dict[str, str] = {}
    failed_screenshot_items: set[str] = set()

    for idx, it in enumerate(new_items):
        sha = it.get("screenshot_sha256")
        if not sha:
            continue
        fb_id_str = str(it["feedback_id"])

        try:
            clean_sha = validate_sha256_key(str(sha))
        except Exception:
            sys.stderr.write(f"Screenshot download failed for feedback {fb_id_str}: invalid sha256 key '{sha}'.\n")
            failed_screenshot_items.add(fb_id_str)
            continue

        if clean_sha in screenshot_map:
            continue

        # Space requests slightly to respect admin rate limiter (30/min)
        if idx > 0 and client is None:
            time.sleep(0.05)

        shot_url = f"{clean_base}/admin/feedback/screenshots/{clean_sha}"
        # Read one byte past the cap so oversize is detected without downloading the whole body
        code, content = _http_get(shot_url, headers, client=client, max_bytes=MAX_SCREENSHOT_SIZE_BYTES + 1)

        if code == 429:
            sys.stderr.write(f"Screenshot download rate limited (HTTP 429) for feedback {fb_id_str}.\n")
            # Rate limited: stop fetching and leave every remaining item that needs a screenshot
            # unpulled, so it is retried on the next run instead of being written without one.
            for pending in new_items[idx:]:
                if pending.get("screenshot_sha256"):
                    failed_screenshot_items.add(str(pending["feedback_id"]))
            break

        if code != 200:
            sys.stderr.write(f"Screenshot download failed for feedback {fb_id_str}: HTTP {code}.\n")
            failed_screenshot_items.add(fb_id_str)
            continue

        if len(content) > MAX_SCREENSHOT_SIZE_BYTES:
            sys.stderr.write(
                f"Screenshot download failed for feedback {fb_id_str}: size {len(content)} exceeds {MAX_SCREENSHOT_SIZE_BYTES} bytes.\n"
            )
            failed_screenshot_items.add(fb_id_str)
            continue

        if sha256_hex(content) != clean_sha:
            sys.stderr.write(f"Screenshot download failed for feedback {fb_id_str}: SHA-256 hash mismatch.\n")
            failed_screenshot_items.add(fb_id_str)
            continue

        mime = detect_image_type(content)
        if mime is None:
            sys.stderr.write(f"Screenshot download failed for feedback {fb_id_str}: unrecognized or non-image bytes.\n")
            failed_screenshot_items.add(fb_id_str)
            continue

        ext = ".png"
        if mime == "image/jpeg":
            ext = ".jpg"
        elif mime == "image/gif":
            ext = ".gif"
        elif mime == "image/webp":
            ext = ".webp"

        filename = f"{clean_sha}{ext}"
        shot_dir = inbox / "screenshots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        shot_file = shot_dir / filename
        shot_file.write_bytes(content)
        screenshot_map[clean_sha] = f"screenshots/{filename}"

    # Only process items whose screenshots succeeded (if any was required)
    pullable_items = [it for it in new_items if str(it["feedback_id"]) not in failed_screenshot_items]

    if not pullable_items and failed_screenshot_items:
        raise RuntimeError("One or more screenshots failed to download; unpulled items will be retried on next run.")

    if not pullable_items:
        return PullResult(
            pulled_count=0,
            total_available=len(valid_items),
            inbox_path=None,
            pulled_ids=[],
        )

    # Sort items chronologically
    def _parse_time(item: dict[str, Any]) -> str:
        return str(item.get("created_at") or "")

    sorted_items = sorted(pullable_items, key=_parse_time)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    md_file = inbox / f"{today}.md"
    file_exists = md_file.exists()

    rendered_entries: list[str] = []
    newly_pulled_ids: list[str] = []

    for item in sorted_items:
        sha = item.get("screenshot_sha256")
        shot_link = screenshot_map.get(str(sha)) if sha else None
        rendered_entries.append(_format_feedback_entry(item, shot_link))
        newly_pulled_ids.append(str(item["feedback_id"]))

    with md_file.open("a", encoding="utf-8") as f:
        if not file_exists:
            f.write(f"# Feedback Inbox - {today}\n\n")
        else:
            f.write("\n\n---\n\n")
        f.write("\n\n---\n\n".join(rendered_entries))
        f.write("\n")

    # Update .pulled file
    with pulled_file.open("a", encoding="utf-8") as pf:
        for fid in newly_pulled_ids:
            pf.write(f"{fid}\n")

    if failed_screenshot_items:
        raise RuntimeError(
            f"Pulled {len(newly_pulled_ids)} feedback item(s), but {len(failed_screenshot_items)} item(s) had screenshot failures and were left unpulled for retry."
        )

    return PullResult(
        pulled_count=len(newly_pulled_ids),
        total_available=len(valid_items),
        inbox_path=md_file,
        pulled_ids=newly_pulled_ids,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for python -m incident_report.feedback_pull."""
    parser = argparse.ArgumentParser(
        description="Pull new feedback from hosted admin endpoint into local inbox.",
        prog="python -m incident_report.feedback_pull",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="Base URL of the hosted application (e.g. https://example.com)",
    )
    parser.add_argument(
        "--inbox-dir",
        default="feedback_inbox",
        help="Local directory for the feedback inbox (default: feedback_inbox)",
    )
    args = parser.parse_args(argv)

    secret = os.environ.get("INCIDENT_ADMIN_SECRET", "").strip()
    if not secret:
        sys.stderr.write("Error: INCIDENT_ADMIN_SECRET environment variable is not set.\n")
        return 1

    try:
        result = pull_feedback(
            base_url=args.base_url,
            admin_secret=secret,
            inbox_dir=args.inbox_dir,
        )
    except PermissionError as exc:
        sys.stderr.write(f"Authentication error: {exc}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"Failed to pull feedback: {exc}\n")
        return 1

    if result.pulled_count == 0:
        sys.stdout.write(f"No new feedback to pull. Total in database: {result.total_available}.\n")
    else:
        sys.stdout.write(f"Successfully pulled {result.pulled_count} feedback item(s) into {result.inbox_path}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
