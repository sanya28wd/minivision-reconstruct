"""One offline print renderer, shared by HTTP and evidence packages."""

import base64
import re
from functools import lru_cache
from pathlib import Path

from incident_report.decisions import MAX_REASON_TEXT_CHARS, ReportState, findings_of
from incident_report.domain import Report
from incident_report.web.templating import build_environment

STATIC = Path(__file__).parent / "static"


@lru_cache(maxsize=1)
def embedded_css() -> dict[str, str]:
    resources = {}
    for name in ("fonts", "tokens", "app", "print"):
        css = (STATIC / "css" / f"{name}.css").read_text(encoding="utf-8")
        if name == "fonts":
            for font in ("roboto-latin-ext.woff2", "roboto-latin.woff2", "noto-kufi-arabic-arabic.woff2"):
                encoded = base64.b64encode((STATIC / "fonts" / font).read_bytes()).decode("ascii")
                css = css.replace(f"../fonts/{font}", f"data:font/woff2;base64,{encoded}")
        # Comments may mention remote documentation; they are not required by the offline stylesheet.
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        resources[name] = "data:text/css;base64," + base64.b64encode(css.encode()).decode("ascii") + f"#{name}.css"
    return resources


def render_print(report: Report, state: ReportState) -> bytes:
    return (
        build_environment()
        .get_template("report_print.html")
        .render(
            report=report,
            decision=state,
            findings=findings_of(report),
            violation_override_count=sum(key.startswith("violation:") for key in state.overrides),
            reason_max_chars=MAX_REASON_TEXT_CHARS,
            print_view=True,
            embedded_css=embedded_css(),
        )
        .encode("utf-8")
    )
