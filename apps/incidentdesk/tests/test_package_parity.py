"""MIG-05 A: approved screen/print parity through share-session HTTP routes."""

import re
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from incident_report.domain import Report
from incident_report.runlog import RunLog
from test_report_print import workspace as workspace


class ReportFields(HTMLParser):
    """Read visible section text, colour chips, citations and legal links independently."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.sections: dict[str, list[str]] = {}
        self.active: list[str] = []
        self.stack: list[str | None] = []
        self.chips: list[str] = []
        self.sources: list[str | None] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        section = values.get("data-report-section") if tag == "section" else None
        if section:
            self.active.append(section)
            self.sections[section] = []
        if tag not in {"br", "hr", "input", "meta", "link", "source", "img"}:
            self.stack.append(section)
        classes = (values.get("class") or "").split()
        if "chip" in classes:
            self.chips.append(values["class"] or "")
        if tag == "a" and (values.get("href") or "").startswith("https://"):
            self.sources.append(values["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag in {"br", "hr", "input", "meta", "link", "source", "img"}:
            return
        if self.stack:
            section = self.stack.pop()
            if section:
                self.active.remove(section)

    def handle_data(self, data: str) -> None:
        for section in self.active:
            self.sections[section].extend(data.split())


def csrf_token(client: TestClient, report: Report) -> str:
    page = client.get(f"/reports/{report.run_id}/review")
    token = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page.text)
    assert token
    return token.group(1)


def approve(client: TestClient, report: Report) -> None:
    response = client.post(
        f"/reports/{report.run_id}/approve",
        data={"csrf_token": csrf_token(client, report)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_approved_version_has_equal_findings_decisions_and_citations_on_screen_and_print(
    workspace: tuple[TestClient, Report], run_log: RunLog
) -> None:
    client, report = workspace
    approve(client, report)
    root = f"/reports/{report.run_id}"
    screen = client.get(root + "?version=1")
    printed = client.get(root + "/print?version=1")
    assert screen.status_code == printed.status_code == 200
    screen_fields, print_fields = ReportFields(screen.text), ReportFields(printed.text)
    for section in ("summary", "storyline", "parties", "violations", "decisions"):
        assert screen_fields.sections[section]
        assert screen_fields.sections[section] == print_fields.sections[section]
    assert screen_fields.chips == print_fields.chips
    screen_citations = citation_text(screen.text)
    assert screen_citations
    assert screen_citations == citation_text(printed.text)
    assert all(
        text == f"{report.views[0].sha256} @ 2.5s" or text == f"{report.views[0].sha256} @ 2.0s"
        for text in screen_citations
    )
    assert screen_fields.sources == print_fields.sources == ["https://example.com/source"]
    for page in (screen, printed):
        # Match the actual violation row, so dates/hashes cannot satisfy the fine/points assertions.
        rows = re.findall(r"<tr>(.*?)</tr>", page.text, re.S)
        row = next(row for row in rows if "<strong>item-1</strong>" in row)
        cells = [visible_text(cell) for cell in re.findall(r"<td>(.*?)</td>", row, re.S)]
        assert cells[0] == "item-1"
        assert cells[2:6] == ["1,000 AED", "4", "—", "Provisional"]
        assert "MR 178/2017 as amended, item item-1" in cells[7]
    for expected in (
        "At Fault",
        "pink",
        "item-1",
        "1,000",
        "4",
        "silver Toyota sedan",
        "@ 2.5s",
        report.views[0].sha256,
    ):
        assert expected in screen.text and expected in printed.text
    approved_at = run_log.decisions.state(report.run_id, 1).approved_at
    assert approved_at is not None
    header = f"Version 1 · Approved {approved_at.isoformat()}"
    for page in (screen, printed):
        assert header in visible_text(page.text)


class ReportFieldsText(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.text = ""
        self.feed(html)

    def handle_data(self, data: str) -> None:
        self.text += data


def visible_text(html: str) -> str:
    return " ".join(ReportFieldsText(html).text.split())


def citation_text(html: str) -> list[str]:
    return [visible_text(body) for body in re.findall(r'<span class="citation">(.*?)</span>', html, re.S)]


def test_draft_print_is_explicitly_not_approved(workspace: tuple[TestClient, Report]) -> None:
    client, report = workspace
    page = client.get(f"/reports/{report.run_id}/print?version=1")
    assert page.status_code == 200
    assert "Draft, not approved" in page.text


def test_old_approved_print_stays_identical_after_new_version_and_override(
    workspace: tuple[TestClient, Report],
) -> None:
    client, report = workspace
    approve(client, report)
    root = f"/reports/{report.run_id}"
    before = client.get(root + "/print?version=1").content
    data = {"csrf_token": csrf_token(client, report)}
    assert client.post(root + "/versions", data=data, follow_redirects=False).status_code == 303
    assert (
        client.post(
            root + "/overrides",
            data={
                **data,
                "finding_key": "party:0",
                "new_value": "Not at Fault",
                "reason_code": "other",
                "reason_text": "Synthetic second version <script>bad()</script>",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    approve(client, report)
    assert client.get(root + "/print?version=1").content == before
    pages = []
    for suffix in ("?version=2", "/print?version=2"):
        page = client.get(root + suffix)
        pages.append(ReportFields(page.text))
        assert "Synthetic second version &lt;script&gt;bad()&lt;/script&gt;" in page.text
        assert "<script>bad()" not in page.text
        assert "Officer override" in page.text
    for section in ("summary", "storyline", "parties", "violations", "decisions"):
        assert pages[0].sections[section] == pages[1].sections[section]
    old_screen = client.get(root + "?version=1")
    assert f'href="{root}/print?version=1"' in old_screen.text
    assert "Synthetic second version" not in old_screen.text


def test_print_embeds_css_and_fonts_without_active_external_resources(workspace: tuple[TestClient, Report]) -> None:
    import base64

    from test_review_workspace_r2 import Markup

    client, report = workspace
    page = client.get(f"/reports/{report.run_id}/print")
    styles = [
        attrs.get("href") or ""
        for tag, attrs in Markup(page.text).tags
        if tag == "link" and attrs.get("rel") == "stylesheet"
    ]
    assert styles and all(href.startswith("data:text/css;base64,") for href in styles)
    css = "\n".join(base64.b64decode(href.split(",", 1)[1].split("#")[0]).decode() for href in styles)
    assert "@page" in css and "size: A4" in css
    assert "data:font/woff2;base64," in css
    assert "../fonts/" not in css and "https://" not in css
    policy = page.headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy and "unsafe-inline" not in policy
    assert "style-src" in policy and "font-src" in policy
