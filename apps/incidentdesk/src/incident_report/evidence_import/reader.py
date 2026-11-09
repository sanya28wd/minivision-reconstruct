"""Bounded local directory reader: no execution or external calls."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from .issues import Issues
from .models import (
    IMPORTER_VERSION,
    MAX_BUNDLE_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_RECORD_BYTES,
    MAX_RECORDS,
    DerivedOmission,
    Envelope,
    ImportDecision,
)


class InvalidBundle(ValueError):
    """Untrusted bundle cannot be interpreted."""


def safe_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or ":" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise InvalidBundle("unsafe relative artifact path")
    return path


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise InvalidBundle("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise InvalidBundle("non-finite JSON number")


def parse_record(content: bytes) -> dict[str, Any]:
    if len(content) > MAX_RECORD_BYTES:
        raise InvalidBundle("oversized record (project assumption)")
    value = json.loads(content, object_pairs_hook=_pairs, parse_constant=_constant)
    if not isinstance(value, dict):
        raise InvalidBundle("JSON record must be an object")
    if "schema_version" in value and value["schema_version"] != "1.0":
        raise InvalidBundle("unknown producer schema version")
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if "\x00" in item:
                raise InvalidBundle("JSON text is not representable in the local audit store")
            item.encode("utf-8", errors="strict")
        if isinstance(item, float) and not math.isfinite(item):
            raise InvalidBundle("non-finite JSON number")
        if isinstance(item, dict):
            if "schema_version" in item and item["schema_version"] != "1.0":
                raise InvalidBundle("unknown nested producer schema version")
            pending.extend(item.values())
            pending.extend(item.keys())
        elif isinstance(item, list):
            pending.extend(item)
    return value


def read_files(directory: Path, files: dict[str, bytes]) -> None:
    # Refuse UNC/device roots before even stat(): an OS filesystem probe may send SMB traffic.
    if str(directory).replace("\\", "/").startswith("//"):
        raise InvalidBundle("network/device input is forbidden")
    directory = directory.absolute()
    if os.name == "nt":
        import ctypes

        if ctypes.windll.kernel32.GetDriveTypeW(directory.anchor) != 3:
            raise InvalidBundle("input must be on a fixed local drive")
    for parent in (directory, *directory.parents):
        if parent.is_symlink() or parent.is_junction():
            raise InvalidBundle("linked bundle root or ancestor")
    if not directory.is_dir():
        raise InvalidBundle("input must be a local directory, not a link or archive")
    total = 0
    visited = 0
    for path in directory.rglob("*"):
        visited += 1
        if visited > MAX_FILES * 2:
            raise InvalidBundle("oversized directory count (project assumption)")
        if path.is_symlink() or path.is_junction():
            raise InvalidBundle("symlink or junction in bundle")
        if path.is_dir():
            continue
        if not stat.S_ISREG(path.stat().st_mode):
            raise InvalidBundle("non-regular artifact")
        name = path.relative_to(directory).as_posix()
        safe_path(name)
        if len(files) >= MAX_FILES or path.stat().st_size > MAX_FILE_BYTES:
            raise InvalidBundle("oversized file count or file (project assumption)")
        before = path.stat(follow_symlinks=False)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise InvalidBundle("artifact changed while opening")
            content = stream.read(MAX_FILE_BYTES + 1)
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(directory.resolve()):
            raise InvalidBundle("artifact changed containment")
        total += len(content)
        if len(content) > MAX_FILE_BYTES or total > MAX_BUNDLE_BYTES:
            raise InvalidBundle("oversized bundle (project assumption)")
        files[name] = content


def inspect_bundle(directory: Path) -> ImportDecision:
    """Return a decision even on rejection; original artifact bytes remain untouched."""
    raw: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    # An unreadable/oversized attempt has no complete content identity: never deduplicate it as another attempt.
    identity = hashlib.sha256(IMPORTER_VERSION.encode() + uuid.uuid4().bytes).hexdigest()
    read_complete = False
    try:
        read_files(directory, raw)
        read_complete = True
        hashes = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(raw.items())}
        identity = hashlib.sha256(json.dumps([IMPORTER_VERSION, hashes], sort_keys=True).encode()).hexdigest()
        envelope = Envelope.model_validate(parse_record(raw["bundle.json"]))
        issues = Issues()
        for name, digest in envelope.artifacts.items():
            safe_path(name)
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise InvalidBundle("invalid artifact digest")
            if name not in raw:
                issues.emit("upstream_artifacts_missing", f"missing artifact: {name}")
            elif hashes[name] != digest:
                raise InvalidBundle(f"hash mismatch: {name}")
        if set(raw) - {"bundle.json"} - envelope.artifacts.keys():
            raise InvalidBundle("unlisted artifact")
        parsed: dict[str, list[dict[str, Any]]] = {}
        count = 0
        for name, content in raw.items():
            if name.endswith((".json", ".jsonl")) and name != "bundle.json":
                lines = io.BytesIO(content) if name.endswith(".jsonl") else [content]
                records = []
                for line in lines:
                    if not line.strip():
                        continue
                    count += 1
                    if count > MAX_RECORDS:
                        raise InvalidBundle("oversized record count (project assumption)")
                    records.append(parse_record(line))
                parsed[name] = records
        from .normalize import normalize

        omissions: list[DerivedOmission] = []
        normalized = normalize(envelope, parsed, hashes, issues, omissions)
        for omission in omissions:
            expected = envelope.artifacts.get(omission.artifact_path)
            if expected is not None and expected != omission.sha256:
                raise InvalidBundle("conflicting omitted-derived digests")
            issues.remove_message(f"missing artifact: {omission.artifact_path}")
        provenance_issues = [code for code in issues.codes if code not in ("no_incident_candidates", "needs_review")]

        return ImportDecision(
            read_complete=read_complete,
            importer_version=IMPORTER_VERSION,
            import_id=identity,
            not_supplied_derived=tuple(omissions),
            provenance_state="incomplete_provenance" if provenance_issues else "verified",
            issues=tuple(issues),
            issue_codes=issues.codes,
            records=normalized,
            structurally_accepted_records=tuple(record.record_id for record in normalized if not record.needs_review)
            if not provenance_issues
            else (),
            quarantined_records=tuple(
                record.record_id for record in normalized if provenance_issues or record.needs_review
            ),
            source_hashes=hashes,
            envelope=envelope,
            raw_artifacts=raw,
        )
    except (InvalidBundle, ValidationError, ValueError, OSError, KeyError, TypeError, RecursionError) as exc:
        hashes = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(raw.items())}
        return ImportDecision(
            read_complete=read_complete,
            importer_version=IMPORTER_VERSION,
            import_id=identity,
            provenance_state="rejected",
            issues=(f"{type(exc).__name__}: {exc}",),
            issue_codes=("bundle_rejected",),
            source_hashes=hashes,
            raw_artifacts=raw,
        )
