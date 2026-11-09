"""Shared fixtures.

PostgreSQL only (ADR 0011): tests create a throwaway database on the server named by
TEST_DATABASE_URL, migrate it, and drop it afterwards. If the server cannot be reached the
tests FAIL with a plain message; they are never skipped (test-integrity rule).
"""

from __future__ import annotations

import io
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from incident_report.db import migrate
from incident_report.domain import Role, View
from incident_report.runlog import RunLog
from video_fixtures import synthetic_video

# Default suits a local developer with peer authentication (see README).
DEFAULT_TEST_DATABASE_URL = "postgresql:///postgres"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    admin_url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    name = f"incident_test_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(admin_url, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.fail(
            f"Cannot reach PostgreSQL with TEST_DATABASE_URL={admin_url!r}. Start PostgreSQL and "
            f"set TEST_DATABASE_URL to a role that may CREATE DATABASE (see README). {exc}"
        )
    with admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        info = conninfo_to_dict(admin_url)
        info["dbname"] = name
        test_url = make_conninfo(**info)  # type: ignore[arg-type]  # psycopg stubs type kwargs as str
        try:
            with psycopg.connect(test_url) as conn:
                migrate(conn)
            yield test_url
        finally:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def run_log(database_url: str) -> RunLog:
    with psycopg.connect(database_url) as conn:
        conn.execute("TRUNCATE runs")
    return RunLog(database_url)


@pytest.fixture
def recordings_dir(tmp_path: Path) -> Path:
    path = tmp_path / "recordings"
    path.mkdir()
    return path


def write_recording(directory: Path, input_hash: str, prompt_version: str, summary: str) -> Path:
    """Write a hand-made recording for tests. Labelled synthetic like every hand-made one."""
    path = directory / f"{input_hash}.{prompt_version}.json"
    path.write_text(
        json.dumps(
            {
                "provenance": "synthetic",
                "note": "Test fixture written by hand. Not model output and not real footage.",
                "input_hash": input_hash,
                "prompt_version": prompt_version,
                "response": {"summary": summary},
            }
        ),
        encoding="utf-8",
    )
    return path


def make_view(
    content: bytes = synthetic_video(b"tiny-dummy-view-bytes"),
    role: Role = Role.EXTERIOR,
    filename: str = "front.mp4",
    label: str | None = None,
    acquisition_method: str | None = None,
) -> View:
    return View(
        stream=io.BytesIO(content),
        filename=filename,
        role=role,
        label=label,
        acquisition_method=acquisition_method,
    )


@pytest.fixture(autouse=True)
def local_evidence_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test deployment uses disk-backed evidence outside the source tree."""
    monkeypatch.setenv("INCIDENT_EVIDENCE_DIR", str(tmp_path / "evidence"))


@pytest.fixture(autouse=True)
def default_agreement_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default tests to single run unless INCIDENT_AGREEMENT_RUNS is explicitly set."""
    if "INCIDENT_AGREEMENT_RUNS" not in os.environ:
        monkeypatch.setenv("INCIDENT_AGREEMENT_RUNS", "1")
