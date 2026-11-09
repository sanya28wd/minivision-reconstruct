"""The shared visual shell (RPT-04): every page extends it; banner, notices, landmarks, RTL, escaping.

Seams: the HTTP interface (TestClient) for what an officer sees on each real page, and the Jinja
environment the app renders with (`build_environment`) for the shell in right-to-left mode.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from access_client import authorized_client
from conftest import write_recording
from incident_report.ai import ReplayAIConnection
from incident_report.domain import Role
from incident_report.entry import PROMPT_VERSION
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.runlog import RunLog
from incident_report.web import create_app
from incident_report.web.templating import TEMPLATES_DIR, build_environment
from video_fixtures import synthetic_video

PRODUCT_NAME = "Incident Video Report — prototype"
BANNER = "prototype, advisory only, the officer decides"
AI_NOTICE = "videos are processed by an external ai service under a zero-retention setup"
FOCUSABLE = {"a", "button", "input", "select", "textarea", "summary"}
VOID = {"meta", "link", "input", "br", "hr", "img"}


class Page(HTMLParser):
    """Minimal DOM facts we assert on: landmarks, headings, focusable order, text."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.focus_order: list[tuple[str, dict[str, str | None]]] = []
        self.text_parts: list[str] = []
        self.in_title = False
        self.title = ""
        self.root: dict[str, str | None] = {}
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self.tags.append((tag, a))
        if tag == "html":
            self.root = a
        if tag == "title":
            self.in_title = True
        if tag in FOCUSABLE and not (tag == "input" and a.get("type") == "hidden"):
            if tag != "a" or "href" in a:
                self.focus_order.append((tag, a))
        elif a.get("tabindex") not in (None, "-1"):
            self.focus_order.append((tag, a))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        self.text_parts.append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.text_parts).split())

    def count(self, tag: str) -> int:
        return sum(1 for t, _ in self.tags if t == tag)

    def ids(self) -> set[str]:
        return {a["id"] for _, a in self.tags if a.get("id")}  # type: ignore[misc]


@pytest.fixture
def client(recordings_dir: Path, run_log: RunLog) -> TestClient:
    return authorized_client(create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log), run_log.database_url)


def _every_page(client: TestClient, recordings_dir: Path) -> dict[str, str]:
    """The three real pages: upload form, report, and a plain error page."""
    content = synthetic_video(b"shell-view")
    write_recording(
        recordings_dir, incident_input_hash([(Role.EXTERIOR, sha256_hex(content))]), PROMPT_VERSION, "Shell summary."
    )
    report = client.post("/reports", data={"role": "exterior"}, files={"video": ("a.mp4", content, "video/mp4")})
    return {
        "upload": client.get("/").text,
        "report": report.text,
        "error": client.get("/reports/00000000-0000-0000-0000-000000000000").text,
    }


def test_every_page_shares_the_shell_name_banner_and_ai_notice(client: TestClient, recordings_dir: Path) -> None:
    pages = _every_page(client, recordings_dir)

    assert set(pages) == {"upload", "report", "error"}
    for name, html in pages.items():
        page = Page(html)
        text = page.text.lower()
        assert PRODUCT_NAME.lower() in text, name
        assert "bits pilani dubai prototype" in text, name
        assert BANNER in text, name
        assert AI_NOTICE in text, name
        assert "/static/css/tokens.css" in html and "/static/css/app.css" in html, name


def test_every_page_has_header_main_footer_one_h1_and_a_skip_link_to_main(
    client: TestClient, recordings_dir: Path
) -> None:
    for name, html in _every_page(client, recordings_dir).items():
        page = Page(html)
        assert (page.count("header"), page.count("main"), page.count("footer")) == (1, 1, 1), name
        assert page.count("h1") == 1, name
        assert page.root.get("lang") == "en" and page.root.get("dir") == "ltr", name
        first_tag, first_attrs = page.focus_order[0]
        assert first_tag == "a" and first_attrs.get("href") == "#main", f"{name}: first Tab stop is the skip link"
        assert "main" in page.ids(), name
        main_attrs = next(a for t, a in page.tags if t == "main")
        assert main_attrs.get("id") == "main" and main_attrs.get("tabindex") == "-1", name
        positive = [
            a
            for _, a in page.tags
            if (a.get("tabindex") or "0").lstrip("-").isdigit() and int(a.get("tabindex") or 0) > 0
        ]
        assert positive == [], f"{name}: a positive tabindex would break the logical tab order"


def test_the_title_names_the_page_and_the_product(client: TestClient) -> None:
    page = Page(client.get("/").text)

    assert page.title.strip() == f"New incident | {PRODUCT_NAME}"


def test_no_government_logos_emblems_or_official_names_are_used_as_branding(
    client: TestClient, recordings_dir: Path
) -> None:
    forbidden = [
        r"\bRTA\b",
        r"Roads and Transport",
        r"Dubai Police",
        r"Government of Dubai",
        r"Dubai Taxi",
        r"\bemblem\b",
        r"\blogo\b",
        r"Ministry",
    ]
    for name, html in _every_page(client, recordings_dir).items():
        page = Page(html)
        assert page.count("img") == 0 and page.count("svg") == 0, f"{name}: no images in the shell"
        for pattern in forbidden:
            assert not re.search(pattern, html, flags=re.I), f"{name}: {pattern}"
    for css in (TEMPLATES_DIR.parent / "static" / "css").glob("*.css"):
        text = css.read_text(encoding="utf-8")
        for target in re.findall(r"url\(([^)]*)\)", text):
            assert target.strip("\"'").startswith("../fonts/"), f"{css.name}: only the self-hosted fonts are loaded"


def test_the_shell_loads_nothing_from_other_sites(client: TestClient, recordings_dir: Path) -> None:
    for name, html in _every_page(client, recordings_dir).items():
        assert not re.search(r"(src|href|action)=\"(https?:)?//", html), name
    for css in (TEMPLATES_DIR.parent / "static" / "css").glob("*.css"):
        assert not re.search(r"https?://|@import", css.read_text(encoding="utf-8")), css.name


def test_pages_use_no_javascript_inline_style_or_event_handlers(client: TestClient, recordings_dir: Path) -> None:
    for name, html in _every_page(client, recordings_dir).items():
        page = Page(html)
        # Owner ruling 2026-10-25 (PERF-08): only the upload page may enhance its form.
        if name == "upload":
            assert page.count("script") == 1, name
            (script,) = [attrs for tag, attrs in page.tags if tag == "script"]
            assert script == {"src": "/static/early-upload.js", "defer": None}, name
            assert re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, flags=re.I | re.S) == [""], name
        else:
            assert page.count("script") == 0, name
        for tag, attrs in page.tags:
            assert "style" not in attrs, f"{name}: inline style on <{tag}>"
            assert not [k for k in attrs if k.startswith("on")], f"{name}: event handler on <{tag}>"
        assert page.count("style") == 0, name


def test_the_existing_form_still_has_real_labels_and_a_real_submit_button(client: TestClient) -> None:
    page = Page(client.get("/").text)

    assert page.count("button") == 1
    assert next(a for t, a in page.tags if t == "button").get("type") == "submit"
    labelled_for = {a["for"] for t, a in page.tags if t == "label" and a.get("for")}
    for tag, attrs in page.tags:
        if tag == "input" and attrs.get("type") in {"file", "text"}:
            assert attrs.get("id") in labelled_for, attrs


def test_the_shell_renders_in_right_to_left_mode_with_the_same_structure() -> None:
    env = build_environment()
    ltr = env.get_template("error.html").render(message="Plain message.")
    rtl = env.get_template("error.html").render(message="Plain message.", ui_lang="ar")

    ltr_page, rtl_page = Page(ltr), Page(rtl)
    assert (ltr_page.root["lang"], ltr_page.root["dir"]) == ("en", "ltr")
    assert (rtl_page.root["lang"], rtl_page.root["dir"]) == ("ar", "rtl")
    assert [t for t, _ in ltr_page.tags if t != "html"] == [t for t, _ in rtl_page.tags if t != "html"]
    assert rtl_page.count("header") == rtl_page.count("main") == rtl_page.count("footer") == 1
    assert PRODUCT_NAME.lower() in rtl_page.text.lower() and BANNER in rtl_page.text.lower()


def test_an_unexpected_language_value_falls_back_to_english_ltr() -> None:
    html = build_environment().get_template("error.html").render(message="m", ui_lang='"><script>x</script>')

    page = Page(html)
    assert (page.root["lang"], page.root["dir"]) == ("en", "ltr")
    assert "<script" not in html


def test_model_and_user_text_is_escaped_on_the_shell_pages() -> None:
    env = build_environment()
    evil = "<script>alert('x')</script>"

    error = env.get_template("error.html").render(message=evil)

    assert evil not in error
    assert "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in error
    assert Page(error).count("script") == 0


def test_a_script_tag_in_model_text_and_in_a_user_label_renders_as_text_on_the_report(
    client: TestClient, recordings_dir: Path
) -> None:
    content = synthetic_video(b"inject-me")
    write_recording(
        recordings_dir,
        incident_input_hash([(Role.EXTERIOR, sha256_hex(content))]),
        PROMPT_VERSION,
        "<script>alert('model')</script>",
    )

    response = client.post(
        "/reports",
        data={"role": "exterior", "label": "<script>alert('user')</script>", "acquisition_method": "<b>bold</b>"},
        files={"video": ("<script>alert('file')</script>.mp4", content, "video/mp4")},
    )

    page = Page(response.text)
    assert page.count("script") == 0 and page.count("b") == 0
    assert "<script>alert('model')</script>" in page.text  # visible as text
    assert "<script>alert('user')</script>" in page.text
    assert "<b>bold</b>" in page.text


def test_static_assets_are_served_with_the_right_types(client: TestClient) -> None:
    css = client.get("/static/css/app.css")
    font = client.get("/static/fonts/roboto-latin.woff2")

    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2"
    assert font.content[:4] == b"wOF2"


def test_static_files_cannot_escape_the_static_folder(client: TestClient) -> None:
    for path in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/..%2fapp.py", "/static/css/../../app.py"):
        assert client.get(path).status_code == 404, path


def test_csp_keeps_same_origin_default_without_unsafe_inline(
    client: TestClient, recordings_dir: Path, run_log: RunLog
) -> None:
    # Check each response class used by the shell, including the external script.
    pages = _every_page(client, recordings_dir)
    assert set(pages) == {"upload", "report", "error"}
    paths = (
        "/",
        f"/reports/{run_log.list_runs()[0].run_id}",
        "/samples",
        "/static/early-upload.js",
        "/reports/00000000-0000-0000-0000-000000000000",
    )
    for path in paths:
        response = client.get(path)
        policy = response.headers["Content-Security-Policy"]
        directives = {part.split()[0]: part.split()[1:] for part in policy.split(";") if part.strip()}
        assert directives["default-src"] == ["'self'"], path
        assert "unsafe-inline" not in policy.lower(), path
        # A future script-src override must preserve the same origin restriction.
        if "script-src" in directives:
            assert directives["script-src"] == ["'self'"], path
