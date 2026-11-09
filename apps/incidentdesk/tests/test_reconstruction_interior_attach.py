"""Synthetic interior attachment boundary fixture, never Gemini output."""

import json
import uuid

import psycopg
import pytest
from psycopg import sql

from evidence_import_fixtures import FIXTURES


def test_interior_attachment_refused_before_any_import_write(database_url: str) -> None:
    from incident_report.reconstruction.config import ReconstructionSettings
    from incident_report.reconstruction.store import Workspace, WorkspaceRefused

    source = json.loads((FIXTURES / "mig02-relative/bundle.json").read_bytes())["sources"][0]
    identifier = uuid.uuid4()
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "INSERT INTO runs(run_id,origin,created_at,input_hash,report,status,total_latency_ms) VALUES (%s,'replay',now(),%s,%s::jsonb,'succeeded',0)",
            (
                identifier,
                "a" * 64,
                json.dumps({"views": [{"sha256": source["original_video_sha256"], "role": "interior"}]}),
            ),
        )
        conn.commit()
        tables = (
            "evidence_imports",
            "evidence_import_artifacts",
            "evidence_import_records",
            "incident_evidence_imports",
        )
        before = [
            conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()
            for table in tables
        ]
        with pytest.raises(WorkspaceRefused, match="reconstruction applies to road views only"):
            Workspace(conn, ReconstructionSettings(True, FIXTURES)).attach(identifier, "mig02-relative", "synthetic")
        assert before == [
            conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()
            for table in tables
        ]


def test_interior_never_renders_cross_check() -> None:
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from incident_report.domain import ReportView, Role
    from incident_report.reconstruction.cross_check import Comparison
    from incident_report.reconstruction.presentation import ViewPanel

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "src/incident_report/web/templates"),
        autoescape=select_autoescape(),
    )
    view = ReportView(
        filename="synthetic.mp4",
        role=Role.INTERIOR,
        label="Synthetic interior",
        acquisition_method=None,
        sha256="b" * 64,
    )
    panel = ViewPanel(
        (), (), (), comparisons=(Comparison("Motion", "cannot_compare", "synthetic", view.sha256, None, None, (), ""),)
    )
    html = env.get_template("reconstruction_view.html").render(view=view, panel=panel, print_view=False)
    assert "Reconstruction cross-check" not in html
