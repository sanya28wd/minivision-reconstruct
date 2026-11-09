"""Small helpers for tests that read the shipped CSS: a rule parser and WCAG contrast maths.

WCAG 2.x formulas (W3C, "Web Content Accessibility Guidelines 2.2", definitions "relative luminance"
and "contrast ratio"; the site could not be reached from the build sandbox, so the implementation is
checked against known values in tests/test_ui_accessibility.py: black on white is 21:1 and #767676 on
white is 4.54:1):
- each sRGB channel c in 0..1 becomes c / 12.92 when c <= 0.04045, else ((c + 0.055) / 1.055) ** 2.4
- L = 0.2126 R + 0.7152 G + 0.0722 B
- contrast = (L_lighter + 0.05) / (L_darker + 0.05)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "incident_report" / "web" / "static"
TOKENS_CSS = STATIC_DIR / "css" / "tokens.css"
APP_CSS = STATIC_DIR / "css" / "app.css"


@dataclass(frozen=True)
class Rule:
    selector: str
    context: str  # enclosing at-rule prelude, e.g. "@media print", or ""
    declarations: tuple[tuple[str, str], ...]

    def get(self, prop: str) -> str | None:
        for name, value in self.declarations:
            if name == prop:
                return value
        return None


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def parse_css(css: str) -> list[Rule]:
    """Parse flat rules and rules nested one level inside @media/@supports. Good enough for our files."""
    text = strip_comments(css)
    rules: list[Rule] = []

    def block(start: int, context: str) -> int:
        i = start
        while i < len(text):
            while i < len(text) and text[i].isspace():
                i += 1
            if i >= len(text) or text[i] == "}":
                return i + 1
            brace = text.index("{", i)
            prelude = text[i:brace].strip()
            if prelude.startswith("@media") or prelude.startswith("@supports"):
                i = block(brace + 1, prelude)
                continue
            end = text.index("}", brace)
            body = text[brace + 1 : end]
            decls = []
            for part in body.split(";"):
                if ":" in part:
                    name, value = part.split(":", 1)
                    decls.append((name.strip().lower(), " ".join(value.split())))
            rules.append(Rule(" ".join(prelude.split()), context, tuple(decls)))
            i = end + 1
        return i

    block(0, "")
    return rules


def tokens(css: str | None = None) -> dict[str, str]:
    """All custom properties declared in tokens.css (name without the leading --)."""
    source = css if css is not None else TOKENS_CSS.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for rule in parse_css(source):
        for name, value in rule.declarations:
            if name.startswith("--"):
                found[name[2:]] = value
    return found


def resolve(token: str, table: dict[str, str]) -> str:
    value = table[token]
    seen = {token}
    while (m := re.fullmatch(r"var\(--([\w-]+)\)", value)) is not None:
        name = m.group(1)
        if name in seen:
            raise ValueError(f"token cycle at {name}")
        seen.add(name)
        value = table[name]
    return value


def _channels(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"not a #RRGGBB colour: {hex_colour!r}")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def relative_luminance(hex_colour: str) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in _channels(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)
