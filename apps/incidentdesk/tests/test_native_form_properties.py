"""Native forms must not rely on HTMLFormElement properties that their own controls shadow.

In a browser a control named `action` replaces `form.action` (a <select> or a RadioNodeList), so reading
`form.action` posted to `/.../[object HTMLSelectElement]`. TestClient and stubbed fetch never see this.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "incident_report" / "web"
JS_FILES = sorted((WEB / "static" / "js").glob("*.js"))
NATIVE_TEMPLATES = sorted((WEB / "templates").glob("native_*.html"))
# Form properties a control name or id would shadow; JS may only read these if no control collides.
FORM_PROPERTIES = {
    "action",
    "method",
    "target",
    "enctype",
    "encoding",
    "id",
    "name",
    "elements",
    "dataset",
    "length",
    "submit",
    "reset",
    "reportValidity",
    "checkValidity",
    "requestSubmit",
    "addEventListener",
    "getAttribute",
    "inert",
    "hidden",
    "className",
    "style",
    "children",
    "parentElement",
}
# Properties that carry a URL or request setting: never read as a property, always via getAttribute/data-*.
REQUEST_PROPERTIES = ("action", "method", "target", "enctype", "encoding")


def _code(path: Path) -> str:
    """Script source without comments, so a comment mentioning form.action is not a read."""
    text = path.read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/|(?<!:)//[^\n]*", "", text, flags=re.S)


class _Controls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.names: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "form":
            self.depth += 1
        elif self.depth and tag in {"input", "select", "textarea", "button", "fieldset", "output", "object"}:
            for key, value in attrs:
                if key in {"name", "id"} and value and "{{" not in value:
                    self.names.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.depth -= 1


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_scripts_never_read_request_settings_from_form_properties(path: Path) -> None:
    source = _code(path)
    pattern = r"\b(?:form|this\.form|event\.target|event\.currentTarget)\.(" + "|".join(REQUEST_PROPERTIES) + r")\b"
    assert re.findall(pattern, source) == []


@pytest.mark.parametrize("path", NATIVE_TEMPLATES, ids=lambda p: p.name)
def test_native_form_controls_do_not_shadow_properties_the_scripts_read(path: Path) -> None:
    parser = _Controls()
    parser.feed(path.read_text(encoding="utf-8"))
    scripts = "\n".join(_code(p) for p in JS_FILES)
    read = {name for name in FORM_PROPERTIES if re.search(r"\bform\." + name + r"\b", scripts)}
    # `action` is a control name on purpose (it selects the write), so scripts must never read form.action.
    assert "action" not in read
    assert read & set(parser.names) == set()
