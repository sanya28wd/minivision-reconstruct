"""Read-only streaming ZIP verifier; never extracts or needs application configuration."""

import hashlib
import io
import json
import re
import stat
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from incident_report.package_format import (
    MAX_MANIFEST_BYTES,
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_MEMBERS,
    VerificationRefused,
)
from incident_report.web.package_safety import member_path


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise VerificationRefused("Manifest has duplicate JSON keys.")
        result[key] = value
    return result


def _manifest(value: Any) -> dict[str, Any]:
    required = {
        "format_version",
        "run_id",
        "version_number",
        "approved_at",
        "input_hash",
        "prompt_version",
        "app_commit",
        "approval_audit_entry_hash",
        "export_options",
        "files",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or type(value["format_version"]) is not int
        or value["format_version"] != 1
    ):
        raise VerificationRefused("Unsupported or invalid package manifest format.")
    if type(value["version_number"]) is not int or value["version_number"] < 1:
        raise VerificationRefused("Invalid report version in manifest.")
    for field in ("input_hash", "approval_audit_entry_hash"):
        if not isinstance(value[field], str) or re.fullmatch(r"[0-9a-f]{64}", value[field]) is None:
            raise VerificationRefused("Invalid hash in manifest metadata.")
    for field in ("run_id", "approved_at", "prompt_version", "app_commit"):
        if not isinstance(value[field], str) or not value[field]:
            raise VerificationRefused("Invalid manifest metadata.")
    try:
        uuid.UUID(value["run_id"])
        approved = datetime.fromisoformat(value["approved_at"])
        if approved.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError:
        raise VerificationRefused("Invalid run or approval timestamp in manifest.") from None
    if value["app_commit"] != "unknown" and re.fullmatch(r"[0-9a-f]{40}", value["app_commit"]) is None:
        raise VerificationRefused("Invalid app commit in manifest.")
    options = value["export_options"]
    if (
        not isinstance(options, dict)
        or set(options) != {"reconstruction", "driver_context"}
        or any(type(v) is not bool for v in options.values())
    ):
        raise VerificationRefused("Invalid export options in manifest.")
    if not isinstance(value["files"], list):
        raise VerificationRefused("Invalid manifest file inventory.")
    for record in value["files"]:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "bytes"}:
            raise VerificationRefused("Invalid manifest file record.")
        if (
            not isinstance(record["path"], str)
            or not isinstance(record["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None
            or type(record["bytes"]) is not int
            or record["bytes"] < 0
        ):
            raise VerificationRefused("Invalid manifest file hash, path or size.")
        member_path(record["path"])
    return value


def verify(source: Path | bytes) -> dict[str, Any]:
    try:
        return _verify(source)
    except VerificationRefused:
        raise
    except (OSError, ValueError, KeyError, RuntimeError, NotImplementedError, zipfile.BadZipFile, EOFError):
        raise VerificationRefused("Package is unreadable, corrupt or has an unsafe path.") from None


_END_RECORD_BYTES = 22  # An end-of-central-directory record with an empty comment.


def _tail(source: Path | bytes) -> bytes:
    if isinstance(source, bytes):
        return source[-_END_RECORD_BYTES:]
    with source.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(handle.tell() - _END_RECORD_BYTES, 0))
        return handle.read(_END_RECORD_BYTES)


def _verify(source: Path | bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_PACKAGE_MEMBERS:
            raise VerificationRefused("Package has too many members.")
        if sum(info.file_size for info in infos) > MAX_PACKAGE_BYTES:
            raise VerificationRefused("Package total size exceeds the verifier limit.")
        # Our builder writes one plain archive: no comment, nothing before the first member or after the end.
        tail = _tail(source)
        if (
            archive.comment
            or tail[:4] != b"PK\x05\x06"
            or tail[-2:] != b"\x00\x00"
            or min((info.header_offset for info in infos), default=0) != 0
        ):
            raise VerificationRefused("Package has a comment or bytes outside the archive.")
        names = [info.filename for info in infos]
        if len(names) != len(set(name.casefold() for name in names)):
            raise VerificationRefused("Package contains duplicate or colliding members.")
        for info in infos:
            member_path(info.filename)
            mode = stat.S_IFMT(info.external_attr >> 16)
            if info.compress_type != zipfile.ZIP_STORED:
                raise VerificationRefused("Only stored (uncompressed) members are accepted.")
            if mode not in {0, stat.S_IFREG} or info.is_dir() or info.flag_bits & 1:
                raise VerificationRefused("Links, directories, special or encrypted members are refused.")
        if "manifest.json" not in names:
            raise VerificationRefused("Package manifest is missing.")
        # Project engineering bound: metadata stays small; video payloads are streamed separately.
        if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
            raise VerificationRefused("Package manifest is too large.")
        manifest = _manifest(json.loads(archive.read("manifest.json"), object_pairs_hook=_object))
        records = {record["path"]: record for record in manifest["files"]}
        if len(records) != len(manifest["files"]) or "manifest.json" in records:
            raise VerificationRefused("Manifest inventory has duplicates or inventories itself.")
        if set(records) != set(names) - {"manifest.json"}:
            raise VerificationRefused("Manifest inventory does not match package files (missing or extra).")
        if not {"report.html", "decisions.json", "README.txt"} <= set(records):
            raise VerificationRefused("Required report payload is missing.")
        for name, record in records.items():
            if archive.getinfo(name).file_size != record["bytes"]:
                raise VerificationRefused(f"Byte size mismatch: {name}")
            digest = hashlib.sha256()
            size = 0
            with archive.open(name) as stream:
                while chunk := stream.read(128 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if size != record["bytes"] or digest.hexdigest() != record["sha256"]:
                raise VerificationRefused(f"SHA-256 or byte size mismatch: {name}")
        return manifest
