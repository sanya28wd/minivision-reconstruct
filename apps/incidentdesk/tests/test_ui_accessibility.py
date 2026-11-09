"""Accessibility and RTL-readiness of the shell's CSS (RPT-04). No browser needed.

What is checked, and the source of each threshold:
- Text contrast >= 4.5:1 (WCAG 2.2 SC 1.4.3 Contrast (Minimum), level AA, normal text).
- Focus ring and input borders >= 3:1 against what they sit on (SC 1.4.11 Non-text Contrast).
- A visible :focus-visible style exists and is never removed (SC 2.4.7 Focus Visible).
- Only logical CSS properties, so html dir="rtl" mirrors the layout (research note section 2, W3C
  and MDN guidance on logical properties).
The UAE Design System requires WCAG 2.2 AA (docs/research/UI_BEST_PRACTICE.md, section 1).
"""

from __future__ import annotations

import re

import pytest

from css_tools import APP_CSS, TOKENS_CSS, contrast_ratio, parse_css, relative_luminance, resolve, tokens

AA_NORMAL_TEXT = 4.5  # WCAG 2.2 SC 1.4.3
AA_NON_TEXT = 3.0  # WCAG 2.2 SC 1.4.11

# Every foreground/background token pair the shell paints text with. A rule in app.css that uses a pair
# not listed here fails test_every_text_colour_pair_in_the_stylesheet_is_one_we_measured.
TEXT_PAIRS = [
    ("color-text", "color-bg"),
    ("color-text", "color-surface"),
    ("color-text", "color-accent-tint"),
    ("color-text-muted", "color-bg"),
    ("color-text-muted", "color-surface"),
    ("color-link", "color-bg"),
    ("color-link", "color-surface"),
    ("color-link", "color-accent-tint"),
    ("color-accent-strong", "color-bg"),
    ("color-accent-strong", "color-surface"),
    ("color-accent-strong", "color-accent-tint"),
    ("color-on-accent", "color-accent"),
    ("color-on-accent", "color-accent-strong"),
    ("color-badge-amber-text", "color-badge-amber-bg"),
    ("color-badge-amber-text", "color-bg"),
    ("color-badge-amber-text", "color-surface"),
]
NON_TEXT_PAIRS = [
    ("color-focus-ring", "color-bg"),
    ("color-focus-ring", "color-surface"),
    ("color-focus-ring", "color-accent-tint"),
    ("color-input-border", "color-surface"),
    ("color-input-border", "color-bg"),
]
# Backgrounds a rule inherits from when it sets only `color`: the page and a card.
INHERITED_BACKGROUNDS = ["color-bg", "color-surface"]


@pytest.fixture(scope="module")
def table() -> dict[str, str]:
    return tokens()


def _hex(table: dict[str, str], token: str) -> str:
    value = resolve(token, table)
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"{token} must be a #RRGGBB colour, got {value!r}"
    return value


def test_the_contrast_maths_matches_known_values() -> None:
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast_ratio("#767676", "#FFFFFF") == pytest.approx(4.54, abs=0.01)  # well-known AA limit grey
    assert contrast_ratio("#777777", "#FFFFFF") < AA_NORMAL_TEXT
    assert relative_luminance("#FFFFFF") == pytest.approx(1.0)


def test_the_accent_is_the_specified_teal(table: dict[str, str]) -> None:
    assert _hex(table, "color-accent").upper() == "#006E6D"  # spec 001, "Look"


@pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
def test_text_colour_pairs_meet_aa(table: dict[str, str], fg: str, bg: str) -> None:
    ratio = contrast_ratio(_hex(table, fg), _hex(table, bg))
    assert ratio >= AA_NORMAL_TEXT, f"{fg} on {bg} is {ratio:.2f}:1"


@pytest.mark.parametrize(("fg", "bg"), NON_TEXT_PAIRS)
def test_focus_ring_and_input_borders_meet_non_text_contrast(table: dict[str, str], fg: str, bg: str) -> None:
    ratio = contrast_ratio(_hex(table, fg), _hex(table, bg))
    assert ratio >= AA_NON_TEXT, f"{fg} on {bg} is {ratio:.2f}:1"


def _colour_tokens_in(value: str) -> list[str]:
    return re.findall(r"var\(--(color-[\w-]+)\)", value)


def test_every_text_colour_pair_in_the_stylesheet_is_one_we_measured(table: dict[str, str]) -> None:
    measured = set(TEXT_PAIRS)
    seen_foregrounds: set[str] = set()
    for rule in parse_css(APP_CSS.read_text(encoding="utf-8")):
        fg = rule.get("color")
        if fg is None:
            continue
        (fg_token,) = _colour_tokens_in(fg)
        seen_foregrounds.add(fg_token)
        bg = rule.get("background") or rule.get("background-color")
        if bg is not None:
            (bg_token,) = _colour_tokens_in(bg)
            assert (fg_token, bg_token) in measured, f"{rule.selector}: {fg_token} on {bg_token} is not measured"
        else:
            # Text on the page or on a card: measured against both.
            for bg_token in INHERITED_BACKGROUNDS:
                assert (fg_token, bg_token) in measured, f"{rule.selector}: {fg_token} on {bg_token} is not measured"
    assert seen_foregrounds  # the stylesheet really sets text colours through tokens


def test_components_use_tokens_not_raw_colours() -> None:
    css = "\n".join(decl for rule in parse_css(APP_CSS.read_text(encoding="utf-8")) for _, decl in rule.declarations)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|oklch\(", css), "use var(--color-*) in app.css"


def test_no_frosted_glass_or_blur_effects() -> None:
    for path in (APP_CSS, TOKENS_CSS):
        text = path.read_text(encoding="utf-8")
        assert "backdrop-filter" not in text and "blur(" not in text, path.name


def test_a_visible_focus_style_exists_and_is_never_removed() -> None:
    rules = parse_css(APP_CSS.read_text(encoding="utf-8"))
    focus_rules = [r for r in rules if ":focus-visible" in r.selector]
    assert focus_rules, "no :focus-visible rule"
    assert any((r.get("outline") or "").startswith("3px solid") for r in focus_rules)
    for rule in rules:
        outline = rule.get("outline")
        if outline is not None and ":focus-visible" not in rule.selector:
            assert outline.split()[0] not in {"none", "0"}, f"{rule.selector} removes the outline"
    # No rule may switch the outline off (even on :focus-visible).
    for rule in focus_rules:
        assert rule.get("outline-style") != "none"
        assert (rule.get("outline") or "").split()[0] not in {"none", "0"}


def test_reduced_motion_is_respected() -> None:
    rules = parse_css(APP_CSS.read_text(encoding="utf-8"))
    transitions = [r for r in rules if r.get("transition") not in (None, "none")]
    if transitions:
        assert any("prefers-reduced-motion" in r.context and r.get("transition") == "none" for r in rules)


_PHYSICAL_PROPERTY = re.compile(
    r"(^|-)(left|right)(-|$)|^(float|clear)$|^border-(top|bottom)-(left|right)-radius$",
)
_SHORTHANDS_WITH_SIDES = {"margin", "padding", "inset", "border-width", "border-color", "border-style", "scroll-margin"}


def physical_direction_problems(css: str) -> list[str]:
    problems: list[str] = []
    for rule in parse_css(css):
        for name, value in rule.declarations:
            if name.startswith("--"):
                continue
            where = f"{rule.selector} {{ {name}: {value} }}"
            if _PHYSICAL_PROPERTY.search(name):
                problems.append(f"physical property: {where}")
            if name in {"text-align", "float", "clear", "caption-side"} and re.search(r"\b(left|right)\b", value):
                problems.append(f"physical value: {where}")
            if name.startswith("background-position") and re.search(r"\b(left|right)\b", value):
                problems.append(f"physical value: {where}")
            if name in _SHORTHANDS_WITH_SIDES and len(value.replace(" / ", " ").split()) > 2 and "(" not in value:
                problems.append(f"3 or 4 value shorthand has physical sides: {where}")
            if name == "border-radius" and len(value.split()) > 1:
                problems.append(f"multi-value border-radius has physical corners: {where}")
            if re.search(r"translate[xX]?\(|rotate|scaleX", value):
                problems.append(f"transform is not direction-aware: {where}")
    return problems


def test_the_stylesheets_use_logical_properties_only() -> None:
    for path in (APP_CSS, TOKENS_CSS):
        assert physical_direction_problems(path.read_text(encoding="utf-8")) == [], path.name


def test_the_direction_check_actually_catches_physical_properties() -> None:
    bad = """
    .a { margin-left: 1rem; }
    .b { padding-right: 1px; }
    .c { text-align: left; }
    .d { position: absolute; left: 0; }
    .e { margin: 1px 2px 3px 4px; }
    .f { float: right; }
    .g { border-top-left-radius: 4px; }
    .h { border-radius: 4px 0 0 4px; }
    """
    problems = physical_direction_problems(bad)
    flagged = {re.search(r"\.(\w) \{", p).group(1) for p in problems}  # type: ignore[union-attr]
    assert flagged == set("abcdefgh"), problems
    good = ".a { margin-inline-start: 1rem; padding: 0.5rem 1rem; text-align: start; inset-inline-end: 0; }"
    assert physical_direction_problems(good) == []


def test_fonts_have_fallback_stacks_for_both_scripts(table: dict[str, str]) -> None:
    latin = table["font-body"]
    arabic = table["font-arabic"]
    assert latin.startswith('"Roboto",') and "sans-serif" in latin
    assert arabic.startswith('"Noto Kufi Arabic",') and "sans-serif" in arabic
    # Latin letters and digits inside Arabic text fall back to Roboto, not to a random system face.
    assert '"Roboto"' in arabic
