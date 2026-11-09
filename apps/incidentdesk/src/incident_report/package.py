"""Stable public entry points for approved evidence ZIPs and the command line.

Presentation-specific details live behind the web adapter; analysis never renders exports.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from incident_report.package_format import PackageRefused

if TYPE_CHECKING:
    from incident_report.domain import Attribution
    from incident_report.runlog import RunLog
    from incident_report.storage import EvidenceStore

__all__ = ["PackageRefused", "assemble", "build", "main", "verify"]


def build(
    run_id: uuid.UUID,
    version: int,
    *,
    log: RunLog | None = None,
    evidence: EvidenceStore | None = None,
    attribution: Attribution | None = None,
    request_id: uuid.UUID | None = None,
) -> bytes:
    from incident_report.web.package_export import build as export

    return export(run_id, version, log=log, evidence=evidence, attribution=attribution, request_id=request_id)


def assemble(files: dict[str, bytes]) -> bytes:
    from incident_report.web.package_export import assemble as archive

    return archive(files)


def verify(source: Path | bytes) -> dict[str, Any]:
    from incident_report.web.package_verification import verify as check

    return check(source)


def main(argv: Sequence[str] | None = None) -> int:
    from incident_report.web.package_export import main as command

    return command(argv)


if __name__ == "__main__":
    raise SystemExit(main())
