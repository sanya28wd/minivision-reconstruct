"""The one place the Jinja environment is built, so the app and the tests render the same way.

Autoescape is on for every template, whatever its file name (CLAUDE.md: all model and user text is
escaped when rendered). The `label(key)` global feeds the `bilingual` macro in `_components.html`.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import jinja2

from incident_report.rules.models import FaultStatus
from incident_report.web.labels import LabelCatalog

TEMPLATES_DIR = Path(__file__).parent / "templates"


def build_environment(catalog: LabelCatalog | None = None) -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
    )
    env.filters["utc"] = lambda value: value.astimezone(UTC)
    env.filters["fault_status_label"] = lambda value: FaultStatus(value).display_label
    env.globals["label"] = (catalog or LabelCatalog.default()).get
    return env
