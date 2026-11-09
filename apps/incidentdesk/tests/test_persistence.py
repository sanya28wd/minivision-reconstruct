"""Nothing persistent lives on the app server's disk (RPT-03, ADR 0011; spec 001, "Storage and integrity").

Through the HTTP interface and the evidence storage seam: a run and its evidence survive an app restart,
and a running app writes nothing under its own package or working directory.
"""

from __future__ import annotations

import io
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import incident_report
from access_client import authorized_client
from conftest import write_recording
from incident_report.domain import Role
from incident_report.entry import PROMPT_VERSION
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.storage import EvidenceStore
from incident_report.web import create_app
from video_fixtures import synthetic_video

PACKAGE_DIR = Path(incident_report.__file__).resolve().parent
VIDEO = synthetic_video(
    b"restart-test-view-bytes " * 100_000
)  # about 2.3 MB: above Starlette's 1 MB in-memory upload limit


@pytest.fixture
def deployment(
    database_url: str, tmp_path: Path, recordings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Environment variables as a deployment would set them. Yields the evidence directory."""
    evidence = tmp_path / "evidence"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("INCIDENT_RECORDINGS_DIR", str(recordings_dir))
    monkeypatch.setenv("INCIDENT_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("INCIDENT_ENV", "dev")
    write_recording(
        recordings_dir,
        incident_input_hash([(Role.EXTERIOR, sha256_hex(VIDEO))]),
        PROMPT_VERSION,
        "The summary that must survive a restart.",
    )
    yield evidence


def _upload(client: TestClient) -> str:
    page = client.post(
        "/reports",
        data={"role": "exterior"},
        files={"video": ("front.mp4", VIDEO, "video/mp4")},
        follow_redirects=False,
    )
    assert page.status_code == 303
    return str(page.headers["location"])


def _evidence_store(client: TestClient) -> EvidenceStore:
    store = client.app.state.evidence  # type: ignore[attr-defined]
    assert store is not None
    return store  # type: ignore[no-any-return]


def test_a_run_and_its_evidence_survive_an_app_restart(deployment: Path) -> None:
    first = authorized_client(create_app())
    report_path = _upload(first)
    key = sha256_hex(VIDEO)
    _evidence_store(first).put(io.BytesIO(VIDEO), sha256=key)
    first.close()
    del first  # the first app instance is gone; only the database and the evidence directory remain

    second = authorized_client(create_app())  # a new instance with the same settings

    page = second.get(report_path)
    assert page.status_code == 200
    assert "The summary that must survive a restart." in page.text
    assert key in page.text
    with _evidence_store(second).get(key) as stream:
        assert stream.read() == VIDEO


def test_a_running_app_writes_nothing_under_its_package_or_working_directory(
    deployment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    os_temp = tmp_path / "os-temp"
    os_temp.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(tempfile, "tempdir", str(os_temp))  # where the upload is spooled (not persistent storage)
    before = _tree(PACKAGE_DIR)

    client = authorized_client(create_app())
    report_path = _upload(client)
    assert client.get(report_path).status_code == 200
    _evidence_store(client).put(io.BytesIO(VIDEO), sha256=sha256_hex(VIDEO))

    assert _tree(PACKAGE_DIR) == before  # no file added, removed or changed in the package directory
    assert list(cwd.iterdir()) == []  # nothing in the working directory
    assert list(os_temp.iterdir()) == []  # the spooled upload was removed when the request ended
    assert any(deployment.rglob(sha256_hex(VIDEO)))  # the evidence went to the configured directory


def test_an_app_with_no_evidence_directory_has_no_evidence_store(
    database_url: str, recordings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("INCIDENT_RECORDINGS_DIR", str(recordings_dir))
    monkeypatch.delenv("INCIDENT_EVIDENCE_DIR", raising=False)

    assert authorized_client(create_app()).app.state.evidence is None  # type: ignore[attr-defined]


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under root (ignoring Python's bytecode cache) as {path: (size, mtime_ns)}."""
    found: dict[str, tuple[int, int]] = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            path = Path(directory) / name
            stat = path.stat()
            found[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return found
