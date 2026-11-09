"""MIG-05 packages at the public build and CLI seams; synthetic report evidence only."""

import io
import json
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from incident_report.domain import Report
from incident_report.runlog import RunLog
from test_package_parity import approve
from test_report_print import workspace as workspace


def test_draft_package_is_refused(workspace: tuple[TestClient, Report], run_log: RunLog) -> None:
    from incident_report.package import PackageRefused, build

    _, report = workspace
    with pytest.raises(PackageRefused, match="approved"):
        build(report.run_id, 1, log=run_log)


def test_package_is_deterministic_and_matches_print(workspace: tuple[TestClient, Report], run_log: RunLog) -> None:
    from incident_report.package import build

    client, report = workspace
    approve(client, report)
    evidence = cast(FastAPI, client.app).state.evidence
    first = build(report.run_id, 1, log=run_log, evidence=evidence)
    second = build(report.run_id, 1, log=run_log, evidence=evidence)
    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.read("report.html") == client.get(f"/reports/{report.run_id}/print?version=1").content
        view_path = f"views/{report.views[0].sha256}.mp4"
        assert archive.read(view_path) == b"\x00\x00\x00\x18ftypisom" + bytes(range(256))
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version_number"] == 1
        assert manifest["input_hash"] == report.input_hash
        assert manifest["prompt_version"] == "synthetic-print"
        approvals = [
            e for e in run_log.audit.list_entries() if e.run_id == report.run_id and e.action == "report.approved"
        ]
        assert manifest["approval_audit_entry_hash"] == approvals[0].hash
        decisions = json.loads(archive.read("decisions.json"))
        approved_at = run_log.decisions.state(report.run_id, 1).approved_at
        assert approved_at is not None
        assert decisions["approval"]["approved_at"] == approved_at.isoformat()
        assert decisions["lineage"] == []
        assert decisions["overrides"] == {}
        assert archive.namelist() == sorted(archive.namelist())
    builds = [e for e in run_log.audit.list_entries() if e.run_id == report.run_id and e.action == "package.built"]
    assert len(builds) == 2
    import hashlib

    assert all(e.details["manifest_sha256"] == hashlib.sha256(archive_manifest(first)).hexdigest() for e in builds)
    assert run_log.audit.verify().valid


def archive_manifest(content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return archive.read("manifest.json")


def test_package_appendix_uses_only_imports_attached_at_approval(
    workspace: tuple[TestClient, Report], run_log: RunLog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg

    from evidence_import_fixtures import synthetic_bundle
    from incident_report.package import build
    from incident_report.reconstruction.config import ReconstructionSettings
    from incident_report.reconstruction.store import Workspace

    client, report = workspace
    import hashlib

    from incident_report.domain import ReportView, Role
    from incident_report.facts import DriverObservation
    from test_review_workspace_r2 import store_report

    interior_bytes = b"\x00\x00\x00\x18ftypisom-interior"
    interior_key = hashlib.sha256(interior_bytes).hexdigest()
    cast(FastAPI, client.app).state.evidence.put(io.BytesIO(interior_bytes), sha256=interior_key)
    assert report.facts is not None
    interior = ReportView(
        filename="interior.mp4",
        label="Synthetic interior",
        role=Role.INTERIOR,
        acquisition_method=None,
        sha256=interior_key,
    )
    facts = report.facts.model_copy(
        update={
            "driver_observations": [
                DriverObservation(
                    kind="looking_at_phone",
                    subject=report.facts.parties[0].description,
                    view_id=interior_key,
                    start_seconds=1.0,
                    end_seconds=3.0,
                    confidence=1.0,
                )
            ]
        }
    )
    report = store_report(run_log, report.model_copy(update={"views": (*report.views, interior), "facts": facts}))
    root = tmp_path / "exports"

    def bind(documents: dict[str, list[dict[str, Any]]], envelope: dict[str, Any]) -> None:
        old = envelope["sources"][0]["original_video_sha256"]

        def replace(value: Any) -> Any:
            if isinstance(value, dict):
                for key, item in value.items():
                    value[key] = replace(item)
            elif isinstance(value, list):
                return [replace(item) for item in value]
            elif value == old:
                return report.views[0].sha256
            return value

        replace(documents)
        replace(envelope)

    synthetic_bundle(root / "one", transform=bind)
    envelope_path = root / "one/bundle.json"
    settings = ReconstructionSettings(True, root, True)
    with psycopg.connect(run_log.database_url) as conn:
        attached = Workspace(conn, settings).attach(report.run_id, "one", "synthetic reviewer")
        # Captured IDs must win even when a transaction timestamp is later than approval's start.
        conn.execute(
            "INSERT INTO view_time_alignments(incident_id,view_sha256,reference_view_sha256,offset_seconds,uncertainty_seconds,clock_reliability,reviewer,reason,created_at) VALUES (%s,%s,%s,1,0.5,'reviewer_estimated','<script>reviewer()</script>','synthetic',now()+interval '1 day')",
            (report.run_id, interior_key, report.views[0].sha256),
        )
    approve(client, report)
    approval = next(
        e for e in run_log.audit.list_entries() if e.run_id == report.run_id and e.action == "report.approved"
    )
    assert approval.details["reconstruction_snapshot"]["link_ids"] == [str(attached[0].link_id)]
    monkeypatch.setenv("INCIDENTDESK_RECONSTRUCTION", "true")
    monkeypatch.setenv("INCIDENTDESK_DRIVER_CONTEXT", "true")
    content = build(report.run_id, 1, log=run_log, evidence=cast(FastAPI, client.app).state.evidence)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        prefix = f"reconstruction/{attached[0].import_id}/"
        from incident_report.evidence_import.models import ImportDecision
        from incident_report.evidence_import.persistence import load_import

        with psycopg.connect(run_log.database_url) as conn:
            stored = load_import(attached[0].import_id, conn)
        assert isinstance(stored, ImportDecision)
        assert {name.removeprefix(prefix) for name in archive.namelist() if name.startswith(prefix)} == set(
            stored.raw_artifacts
        )
        for name, raw in stored.raw_artifacts.items():
            assert archive.read(prefix + name) == raw
        assert archive.read(prefix + "bundle.json") == envelope_path.read_bytes()
        appendix = archive.read("reconstruction/review_aid.html").decode()
        assert "Reconstruction proposal / review aid, not part of the approved findings" in appendix
        assert "Reconstruction cross-check" in appendix
        assert "Interior driver timeline" in appendix
        assert "uncertainty \u00b10.5" in appendix
        assert "&lt;script&gt;reviewer()&lt;/script&gt;" in appendix
        assert "<script>reviewer()" not in appendix
        assert archive.read("report.html") == client.get(f"/reports/{report.run_id}/print?version=1").content
    # Later mappings/reviewer text must not change the approved-version snapshot.
    with psycopg.connect(run_log.database_url) as conn:
        conn.execute(
            "INSERT INTO view_time_alignments(incident_id,view_sha256,reference_view_sha256,offset_seconds,uncertainty_seconds,reviewer,reason) VALUES (%s,%s,%s,0,0,'later','<script>later()</script>')",
            (report.run_id, report.views[0].sha256, report.views[0].sha256),
        )
    assert build(report.run_id, 1, log=run_log, evidence=cast(FastAPI, client.app).state.evidence) == content
    monkeypatch.setenv("INCIDENTDESK_RECONSTRUCTION", "false")
    without = build(report.run_id, 1, log=run_log, evidence=cast(FastAPI, client.app).state.evidence)
    with zipfile.ZipFile(io.BytesIO(without)) as archive:
        assert not any(name.startswith("reconstruction/") for name in archive.namelist())


@pytest.mark.parametrize(
    "text",
    [
        "/home/alice/evidence/clip.mp4",
        "http://localhost:8000/private",
        "http://127.0.0.1/private",
        "http://127.1/private",
        "http://2130706433/private",
        "https://example.com/C:/Users/private",
        "https://example.com/source?file=/home/alice/private",
        "file:///tmp/private",
        r"\\server\share\private",
    ],
)
def test_package_refuses_machine_references_in_report_text(
    workspace: tuple[TestClient, Report], run_log: RunLog, text: str
) -> None:
    from incident_report.package import PackageRefused, build
    from test_review_workspace_r2 import store_report

    client, report = workspace
    report = store_report(run_log, report.model_copy(update={"summary": "Synthetic " + text}))
    approve(client, report)
    with pytest.raises(PackageRefused, match="machine|path|host"):
        build(report.run_id, 1, log=run_log, evidence=cast(FastAPI, client.app).state.evidence)
    assert not any(e.action == "package.built" and e.run_id == report.run_id for e in run_log.audit.list_entries())


@pytest.mark.parametrize("name", ["evil.html", "evil.svg", "evil.js", "evil.exe"])
def test_raw_active_artifact_formats_are_refused(name: str) -> None:
    from incident_report.package import PackageRefused, assemble

    with pytest.raises(PackageRefused, match="active|artifact"):
        assemble({f"reconstruction/import/{name}": b'<script src="https://example.org/x.js"></script>'})


def test_build_refuses_missing_and_corrupted_evidence_without_a_build_audit(
    workspace: tuple[TestClient, Report], run_log: RunLog, tmp_path: Path
) -> None:
    from incident_report.package import PackageRefused, build
    from incident_report.storage.local import LocalEvidenceStore

    client, report = workspace
    approve(client, report)
    with pytest.raises(PackageRefused, match="Missing") as missing:
        build(report.run_id, 1, log=run_log, evidence=LocalEvidenceStore(tmp_path / "empty"))
    assert report.views[0].label and report.views[0].label in str(missing.value)
    store = cast(FastAPI, client.app).state.evidence
    with store.get(report.views[0].sha256) as stream:
        original = Path(stream.name)
    original.chmod(0o600)
    original.write_bytes(b"corrupted")
    with pytest.raises(PackageRefused, match="mismatch") as mismatch:
        build(report.run_id, 1, log=run_log, evidence=store)
    assert report.views[0].label in str(mismatch.value)
    assert not any(e.action == "package.built" and e.run_id == report.run_id for e in run_log.audit.list_entries())


def test_build_cli_writes_the_same_approved_package(
    workspace: tuple[TestClient, Report], run_log: RunLog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import sys

    from incident_report.package import build

    client, report = workspace
    approve(client, report)
    monkeypatch.setenv("DATABASE_URL", run_log.database_url)
    monkeypatch.setenv("INCIDENT_EVIDENCE_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("INCIDENTDESK_RECONSTRUCTION", "false")
    destination = tmp_path / "cli.zip"
    command = [sys.executable, "-m", "incident_report.package", "build", str(report.run_id), "1", str(destination)]
    result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == build(
        report.run_id, 1, log=run_log, evidence=cast(FastAPI, client.app).state.evidence
    )
