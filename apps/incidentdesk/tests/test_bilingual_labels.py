"""The bilingual label helper (RPT-04): Arabic + English pairs, escaped, never invented.

Seam: the Jinja environment the app renders with (`build_environment`) and the label catalogue file.
ADR 0004/0006: every Arabic string must come from GLOSSARY.md (or the spec), or be marked
"translation pending owner review".
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from incident_report.web.labels import ARABIC_PENDING, ARABIC_SOURCED, Label, LabelCatalog, UnknownLabelError
from incident_report.web.templating import build_environment

REPO_ROOT = Path(__file__).resolve().parents[1]


class _Spans(HTMLParser):
    """Collect (lang, dir, text) for every element that declares a lang."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, str | None, str]] = []
        self._open: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if a.get("lang"):
            self._open.append((a["lang"] or "", a.get("dir")))
            self.found.append((a["lang"] or "", a.get("dir"), ""))

    def handle_data(self, data: str) -> None:
        if self._open and self.found:
            lang, direction, text = self.found[-1]
            self.found[-1] = (lang, direction, text + data)

    def handle_endtag(self, tag: str) -> None:
        if self._open:
            self._open.pop()


def _render(catalog: LabelCatalog, template: str) -> str:
    return build_environment(catalog).from_string(template).render()


def _catalog(*labels: Label) -> LabelCatalog:
    return LabelCatalog(labels)


def test_a_label_pair_renders_arabic_with_lang_and_rtl_and_english_with_lang() -> None:
    catalog = _catalog(
        Label(key="outcome.fatal", en="Fatal Accident", ar="وفيات", ar_status=ARABIC_SOURCED, source="GLOSSARY.md")
    )

    html = _render(catalog, '{% from "_components.html" import bilingual %}{{ bilingual("outcome.fatal") }}')

    parser = _Spans()
    parser.feed(html)
    assert ("en", "ltr", "Fatal Accident") in parser.found
    assert ("ar", "rtl", "وفيات") in parser.found


def test_both_halves_of_a_pair_are_escaped() -> None:
    catalog = _catalog(
        Label(
            key="x.evil",
            en="<script>alert('en')</script>",
            ar="<img src=x onerror=alert('ar')>",
            ar_status=ARABIC_PENDING,
            source="test",
        )
    )

    html = _render(catalog, '{% from "_components.html" import bilingual %}{{ bilingual("x.evil") }}')

    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;alert(&#39;en&#39;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(&#39;ar&#39;)&gt;" in html


def test_a_term_without_an_official_arabic_form_renders_english_only_and_never_a_guess() -> None:
    catalog = _catalog(
        Label(key="outcome.injury", en="Injury Accident", ar=None, ar_status="none", source="GLOSSARY.md")
    )

    html = _render(catalog, '{% from "_components.html" import bilingual %}{{ bilingual("outcome.injury") }}')

    assert "Injury Accident" in html
    assert 'lang="ar"' not in html


def test_an_unknown_key_fails_loudly_instead_of_rendering_something_made_up() -> None:
    with pytest.raises(UnknownLabelError):
        _render(_catalog(), '{% from "_components.html" import bilingual %}{{ bilingual("nope") }}')


def test_a_pending_translation_must_say_so_and_a_sourced_one_must_cite_its_source() -> None:
    with pytest.raises(ValueError, match="source"):
        Label(key="a.b", en="A", ar="ب", ar_status=ARABIC_SOURCED, source="")
    with pytest.raises(ValueError, match="Arabic"):
        Label(key="a.b", en="A", ar=None, ar_status=ARABIC_PENDING, source="x")
    with pytest.raises(ValueError, match="no Arabic"):
        Label(key="a.b", en="A", ar="ب", ar_status="none", source="x")
    with pytest.raises(ValueError, match="status"):
        Label(key="a.b", en="A", ar="ب", ar_status="guessed", source="x")


def test_duplicate_keys_are_refused() -> None:
    label = Label(key="a.b", en="A", ar=None, ar_status="none", source="x")
    with pytest.raises(ValueError, match="duplicate"):
        LabelCatalog([label, label])


def test_the_shipped_catalogue_uses_only_glossary_arabic_or_marks_it_pending() -> None:
    glossary = (REPO_ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
    catalog = LabelCatalog.default()

    assert len(catalog.labels) > 0
    for label in catalog.labels:
        if label.ar is None:
            continue
        if label.ar_status == ARABIC_SOURCED:
            # Independent source of truth: the owner-approved glossary text, not our own file.
            assert label.source.startswith("GLOSSARY.md"), label.key
            assert label.ar in glossary, f"{label.key}: Arabic {label.ar!r} is not in GLOSSARY.md"
        else:
            assert label.ar_status == ARABIC_PENDING, label.key


def test_the_shipped_catalogue_file_is_the_only_place_arabic_lives_in_source() -> None:
    web = REPO_ROOT / "src" / "incident_report" / "web"
    offenders = []
    for path in web.rglob("*"):
        if path.suffix in {".html", ".css", ".py"}:
            text = path.read_text(encoding="utf-8")
            if any("\u0600" <= ch <= "\u06ff" for ch in text):
                offenders.append(path.name)
    assert offenders == []
    raw = json.loads((web / "labels.json").read_text(encoding="utf-8"))
    assert isinstance(raw["labels"], list)


def test_the_glossary_accident_outcome_terms_render_as_a_pair() -> None:
    html = (
        build_environment()
        .from_string('{% from "_components.html" import bilingual %}{{ bilingual("accident_outcome.simple") }}')
        .render()
    )

    assert "Simple Accident" in html
    assert "حادث بسيط" in html
