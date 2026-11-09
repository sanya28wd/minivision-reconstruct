"""Approved report packages; original evidence is hash checked before any export."""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import uuid
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg

from incident_report.audit import canonical_json
from incident_report.decisions import OverrideRecord, ReportState
from incident_report.domain import Attribution, Report, RunStatus
from incident_report.package_format import MAX_MANIFEST_BYTES, PackageRefused, VerificationRefused
from incident_report.reconstruction.config import ReconstructionSettings
from incident_report.runlog import RunLog
from incident_report.storage import EvidenceStore, StorageError
from incident_report.storage.interface import validate_sha256_key
from incident_report.web.package_safety import UnsafePayload, member_path, validate_payload
from incident_report.web.package_verification import verify as verify


def _original_views(report: Report, evidence: EvidenceStore | None) -> dict[str, bytes]:
    files = {}
    for view in report.views:
        label = view.label or view.filename
        if evidence is None:
            raise PackageRefused(f"Missing evidence for view {label}.")
        try:
            key = validate_sha256_key(view.sha256)
            with evidence.get(key) as stream:
                content = stream.read()
        except (StorageError, OSError, ValueError):
            raise PackageRefused(f"Missing evidence for view {label}.") from None
        if hashlib.sha256(content).hexdigest() != key:
            raise PackageRefused(f"Evidence hash mismatch for view {label}.")
        extension = PurePosixPath(view.filename).suffix.lower()
        if extension not in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}:
            extension = ".bin"
        files[f"views/{key}{extension}"] = content
    return files


def _utc_iso(value: datetime) -> str:
    """Pin an export timestamp to UTC so the bytes never depend on the database session TimeZone."""
    return value.astimezone(UTC).isoformat()


def _override_data(record: OverrideRecord) -> dict[str, Any]:
    data: dict[str, Any] = record.model_dump(mode="json")
    data["created_at"] = _utc_iso(record.created_at)
    return data


def _decision_data(log: RunLog, state: ReportState) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    with psycopg.connect(log.database_url) as conn:
        approval = conn.execute(
            "SELECT approved_at,overrides_digest,session_id,link_id,request_id FROM report_approvals WHERE run_id=%s AND version_number=%s",
            (state.run_id, state.version_number),
        ).fetchone()
        lineage = conn.execute(
            "SELECT version_number,parent_version,created_at FROM report_versions WHERE run_id=%s AND version_number<=%s ORDER BY version_number",
            (state.run_id, state.version_number),
        ).fetchall()
        anchor = conn.execute(
            "SELECT hash,details FROM audit_log WHERE run_id=%s AND action='report.approved' AND target_id=%s ORDER BY sequence_number LIMIT 1",
            (state.run_id, f"{state.run_id}:{state.version_number}"),
        ).fetchone()
    if approval is None or anchor is None:
        raise PackageRefused("The approved version has no approval audit record.")
    data = {
        "run_id": str(state.run_id),
        "version_number": state.version_number,
        "overrides": {key: _override_data(record) for key, record in state.overrides.items()},
        "approval": dict(
            zip(
                ("approved_at", "overrides_digest", "session_id", "link_id", "request_id"),
                (
                    _utc_iso(value) if isinstance(value, datetime) else str(value) if value is not None else None
                    for value in approval
                ),
                strict=True,
            )
        ),
        "lineage": [
            {"version_number": number, "parent_version": parent, "created_at": _utc_iso(created)}
            for number, parent, created in lineage
        ],
    }
    return data, str(anchor[0]), anchor[1].get("reconstruction_snapshot")


def build(
    run_id: uuid.UUID,
    version: int,
    *,
    log: RunLog | None = None,
    evidence: EvidenceStore | None = None,
    attribution: Attribution | None = None,
    request_id: uuid.UUID | None = None,
) -> bytes:
    if log is None:
        from incident_report.config import Settings
        from incident_report.storage import create_evidence_store

        app_settings = Settings.from_env()
        log = RunLog(app_settings.database_url)
        evidence = evidence or create_evidence_store(app_settings)
    try:
        run = log.get(run_id)
    except KeyError:
        raise PackageRefused("No report with that id.") from None
    if run.status is not RunStatus.SUCCEEDED or run.report is None or not run.report.is_valid:
        raise PackageRefused("A valid finished report is required.")
    state = log.decisions.state(run_id, version)
    if not state.is_approved:
        raise PackageRefused("Only approved versions can be packaged; this version is a draft.")
    from incident_report.web.printing import render_print

    files = _original_views(run.report, evidence)
    decisions, anchor, snapshot = _decision_data(log, state)
    files["report.html"] = render_print(run.report, state)
    files["decisions.json"] = canonical_json(decisions).encode()
    files["README.txt"] = README.encode()
    from incident_report.web.package_reconstruction import appendix

    if state.approved_at is None:
        raise PackageRefused("Only approved versions can be packaged.")
    try:
        settings = ReconstructionSettings.from_env()
        files.update(appendix(run.report, state.approved_at, log.database_url, settings, snapshot))
    except (ValueError, OSError, psycopg.Error):
        raise PackageRefused("Reconstruction snapshot is unavailable; ask the owner to check it.") from None
    manifest = {
        "format_version": 1,
        "run_id": str(run_id),
        "version_number": version,
        "approved_at": _utc_iso(state.approved_at),
        "input_hash": run.report.input_hash,
        "prompt_version": run.report.prompt_version,
        "app_commit": "unknown",
        "export_options": {
            "reconstruction": settings.enabled,
            "driver_context": settings.enabled and settings.driver_context_enabled,
        },
        "approval_audit_entry_hash": anchor,
        "files": [
            {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for path, data in sorted(files.items())
        ],
    }
    manifest_bytes = canonical_json(manifest).encode()
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise PackageRefused("Package manifest is too large to verify portably.")
    files["manifest.json"] = manifest_bytes
    result = assemble(files, evidence_root=_evidence_root(evidence))
    try:
        verify(result)
    except VerificationRefused:
        raise PackageRefused("Package inventory exceeds verifier limits or is not portable.") from None
    log.audit.append(
        event_type="export",
        action="package.built",
        actor="visitor" if attribution else "system",
        session_id=attribution.session_id if attribution else None,
        link_id=attribution.link_id if attribution else None,
        request_id=request_id,
        run_id=run_id,
        target_type="report_version",
        target_id=f"{run_id}:{version}",
        details={"version_number": version, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()},
    )
    return result


README = """Incident Video Report evidence package
This package preserves one approved report version, its original videos and decisions.
Open report.html offline. CSS and fonts are embedded. Legal source links are citations;
opening a citation is optional and requires a connection. PDF uses browser print.

Verify without extracting: python -m incident_report.package verify <zip>
Use an installed IncidentDesk Python environment; no database or model is needed to verify.
manifest.json inventories every payload file (SHA-256 and bytes), excluding itself.
The approval audit-entry hash anchors the approved version. Each build is logged separately
with the manifest hash and builder attribution. Hashes detect alteration relative to the
manifest; this is not a digital signature or proof that a package came from an authority.

Any reconstruction material is a proposal / review aid, not part of approved findings.
It retains imported source bytes and the review context available at approval time.
Export options in the manifest describe the current reconstruction/driver feature selection;
changing those options intentionally changes the appendix, never the approved report.
App commit is unknown when the original report did not record that provenance.
Do not treat producer reviews or contribution outcomes as officer approval or fault.
"""


def _evidence_root(evidence: EvidenceStore | None) -> str | None:
    root = getattr(evidence, "root", None)
    return str(root) if root is not None else None


def assemble(files: dict[str, bytes], *, evidence_root: str | None = None) -> bytes:
    """Pure deterministic ZIP writer: no filesystem, database, clock or model calls."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, data in sorted(files.items()):
            member_path(path)
            if any(ord(char) < 32 or ord(char) == 127 for char in path):
                raise PackageRefused("Unsafe package member path.")
            try:
                validate_payload(path, data, evidence_root=evidence_root)
            except UnsafePayload as exc:
                raise PackageRefused(str(exc)) from None
            entry = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return output.getvalue()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m incident_report.package")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("build", help="build one approved report version locally")
    command.add_argument("run_id", type=uuid.UUID)
    command.add_argument("version", type=int)
    command.add_argument("output", type=Path)
    verification = commands.add_parser("verify", help="verify package paths, inventory, SHA-256 and sizes")
    verification.add_argument("zip", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            verify(args.zip)
            print("Package verified: every payload hash and byte size matches the manifest.")
            return 0
        content = build(args.run_id, args.version)
        args.output.write_bytes(content)
        print("Approved report package built.")
        return 0
    except (PackageRefused, VerificationRefused, OSError) as exc:
        print(
            f"Package refused: {exc}"
            if isinstance(exc, (PackageRefused, VerificationRefused))
            else "Package file is unavailable.",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print("Package could not be built. Check local configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
