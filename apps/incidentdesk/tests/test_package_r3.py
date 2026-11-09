"""MIG-05 R3: CSS escapes must not hide active offline-report resources."""

import base64
import io
import zipfile
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from incident_report.domain import Report
from incident_report.runlog import RunLog
from test_package_parity import approve
from test_report_print import workspace as workspace

ESCAPED_CSS = [
    "body{background:" + chr(92) + "75 rl(https://e.org/a.png)}",
    "body{background:u" + chr(92) + "rl(https://e.org/a.png)}",
    "body{font-family:Escaped" + chr(92) + "20Name}",
]


@pytest.mark.parametrize("css", ESCAPED_CSS)
@pytest.mark.parametrize("location", ["style_tag", "style_attribute", "embedded_stylesheet"])
def test_css_escapes_are_refused_in_offline_html(css: str, location: str) -> None:
    from incident_report.web.package_safety import UnsafePayload, validate_payload

    if location == "style_tag":
        markup = f"<style>{css}</style>"
    elif location == "style_attribute":
        markup = f'<p style="{css}">text</p>'
    else:
        encoded = base64.b64encode(css.encode()).decode("ascii")
        markup = f'<link rel="stylesheet" href="data:text/css;base64,{encoded}">'
    with pytest.raises(UnsafePayload, match="report.html"):
        validate_payload("report.html", markup.encode())


def test_actual_print_output_and_built_package_still_pass_css_validation(
    workspace: tuple[TestClient, Report], run_log: RunLog
) -> None:
    from incident_report.package import build, verify
    from incident_report.web.package_safety import validate_payload

    client, report = workspace
    approve(client, report)
    response = client.get(f"/reports/{report.run_id}/print?version=1")
    assert response.status_code == 200
    validate_payload("report.html", response.content)
    content = build(report.run_id, 1, log=run_log, evidence=cast(FastAPI, client.app).state.evidence)
    assert verify(content)["version_number"] == 1
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.read("report.html") == response.content
