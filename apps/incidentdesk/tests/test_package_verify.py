"""MIG-05 C: verification consumes actual builder ZIPs and refuses tampering safely."""

import io
import json
import stat
import zipfile
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from incident_report.domain import Report
from incident_report.runlog import RunLog
from test_package_parity import approve
from test_report_print import workspace as workspace


@pytest.fixture
def built(workspace: tuple[TestClient, Report], run_log: RunLog) -> bytes:
    from incident_report.package import build

    client, report = workspace
    approve(client, report)
    return build(report.run_id, 1, log=run_log, evidence=cast(FastAPI, client.app).state.evidence)


def test_verifier_cli_accepts_actual_builder_output(
    built: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from incident_report.package import main

    path = tmp_path / "approved.zip"
    path.write_bytes(built)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["verify", str(path)]) == 0
    assert "verified" in capsys.readouterr().out.lower()


@pytest.mark.parametrize(
    "change",
    [
        "tamper",
        "size",
        "add",
        "remove",
        "traversal",
        "absolute",
        "windows",
        "symlink",
        "duplicate",
        "manifest_duplicate",
        "format",
        "bad_json",
    ],
)
def test_verifier_rejects_tampering_and_unsafe_members(
    built: bytes, tmp_path: Path, capsys: pytest.CaptureFixture[str], change: str
) -> None:
    from incident_report.package import main

    with zipfile.ZipFile(io.BytesIO(built)) as source:
        files = {name: source.read(name) for name in source.namelist()}
    extra = None
    if change == "tamper":
        files["report.html"] = b"!" + files["report.html"][1:]
    elif change == "size":
        files["report.html"] += b"!"
    elif change == "add":
        files["extra.txt"] = b"extra"
    elif change == "remove":
        del files["report.html"]
    elif change in {"traversal", "absolute", "windows"}:
        files[{"traversal": "../escape", "absolute": "/escape", "windows": "C:/escape"}[change]] = b"escape"
    elif change == "symlink":
        extra = zipfile.ZipInfo("link")
        extra.create_system = 3
        extra.external_attr = (stat.S_IFLNK | 0o777) << 16
    elif change == "duplicate":
        extra = zipfile.ZipInfo("report.html")
    elif change in {"manifest_duplicate", "format"}:
        manifest = json.loads(files["manifest.json"])
        if change == "format":
            manifest["format_version"] = 999
        else:
            manifest["files"].append(manifest["files"][0])
        files["manifest.json"] = json.dumps(manifest).encode()
    else:
        files["manifest.json"] = b"{broken"
    path = tmp_path / "changed.zip"
    with zipfile.ZipFile(path, "w") as target:
        for name, content in files.items():
            target.writestr(name, content)
        if extra is not None:
            if change == "duplicate":
                with pytest.warns(UserWarning, match="Duplicate name"):
                    target.writestr(extra, b"../elsewhere")
            else:
                target.writestr(extra, b"../elsewhere")
    assert main(["verify", str(path)]) == 1
    assert capsys.readouterr().err.strip()
    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.parametrize("character", ["?", "*", "<", ">", '"', "|"])
def test_verifier_refuses_windows_invalid_names_even_when_fully_inventoried(
    built: bytes, tmp_path: Path, character: str
) -> None:
    import hashlib

    from incident_report.package import main

    with zipfile.ZipFile(io.BytesIO(built)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    for name in (f"reconstruction/import/a{character}.json", "reconstruction/import/a_.json"):
        files[name] = b"{}"
        manifest["files"].append({"path": name, "sha256": hashlib.sha256(b"{}").hexdigest(), "bytes": 2})
    files["manifest.json"] = json.dumps(manifest).encode()
    path = tmp_path / "invalid-name.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    assert main(["verify", str(path)]) == 1


def test_builder_refuses_oversize_manifest_before_build_audit(
    workspace: tuple[TestClient, Report], run_log: RunLog
) -> None:
    from incident_report.package import PackageRefused, build
    from incident_report.package_format import MAX_MANIFEST_BYTES
    from test_review_workspace_r2 import store_report

    client, report = workspace
    report = store_report(
        run_log, report.model_copy(update={"prompt_version": "synthetic-" + "x" * MAX_MANIFEST_BYTES})
    )
    approve(client, report)
    with pytest.raises(PackageRefused, match="manifest is too large"):
        build(report.run_id, 1, log=run_log, evidence=cast(FastAPI, client.app).state.evidence)
    assert not any(e.action == "package.built" and e.run_id == report.run_id for e in run_log.audit.list_entries())
