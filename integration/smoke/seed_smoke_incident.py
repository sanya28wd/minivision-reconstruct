"""Seed one synthetic local development incident; never runs AI or edits producer cases.

Run with apps/incidentdesk/.venv's Python. The dev DB must already be migrated.
Original video bytes must match the selected contract fixture (no hash substitution).
"""

import argparse
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from incident_report.domain import Origin, Report, ReportView, Role, Run, RunStatus
from incident_report.evidence_import import inspect_bundle
from incident_report.evidence_import.reader import safe_path
from incident_report.facts import CitedFact, OverviewFacts, PartyFact
from incident_report.reconstruction.database import local_database
from incident_report.reconstruction.paths import local_root
from incident_report.runlog import RunLog
from incident_report.storage.local import LocalEvidenceStore

FIXTURES = Path(__file__).resolve().parents[2] / "contracts/evidence-v1/fixtures"


def development_database(value: str) -> str:
    """Refuse before connecting; only the named dev database is writable here."""
    if os.environ.get("INCIDENT_ENV", "dev").strip() not in ("", "dev"):
        raise ValueError("Smoke seeding requires the development environment")
    pinned = local_database(value)
    params = conninfo_to_dict(pinned)
    if params.get("dbname") != "incidentdesk_dev":
        raise ValueError("Smoke seeding only writes the incidentdesk_dev database")
    test_url = os.environ.get("TEST_DATABASE_URL", "")
    if test_url:
        test = conninfo_to_dict(test_url)
        test_name = test.get("dbname") or os.environ.get("PGDATABASE") or test.get("user") or os.environ.get("PGUSER")
        # Conservatively treat equal DB names/ports as the same test target, including
        # IPv4/IPv6 aliases. No DNS or connection is needed to make this refusal.
        if test_name == params["dbname"] and test.get("port", os.environ.get("PGPORT", "5432")) == params.get(
            "port", os.environ.get("PGPORT", "5432")
        ):
            raise ValueError("Smoke seeding refuses the configured test database")
    return pinned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", default="mig02-multi-review", choices=sorted(p.name for p in FIXTURES.glob("mig02-*"))
    )
    parser.add_argument("--video", required=True, type=Path, help="Unchanged original video matching the fixture hash")
    parser.add_argument("--exports-root", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path, help="Same local evidence root as the app")
    args = parser.parse_args(argv)
    try:
        database = development_database(os.environ.get("DATABASE_URL", ""))
        root = local_root(args.exports_root)
        evidence_root = local_root(args.evidence_root)
        local_root(args.video.parent)
        if args.video.is_symlink() or args.video.is_junction():
            raise ValueError("Original video must not be a link")
        decision = inspect_bundle(FIXTURES / args.fixture)
        if decision.provenance_state == "rejected" or decision.envelope is None or len(decision.envelope.sources) != 1:
            raise ValueError("Choose a readable single-view fixture")
        source = decision.envelope.sources[0]
        timestamp = next((r.source_time for r in decision.records if r.source_time is not None), None)
        if timestamp is None:
            raise ValueError("Fixture has no supplied source time for a cited smoke marker")
        with args.video.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != source.original_video_sha256:
            raise ValueError("Original video does not match the fixture SHA-256; nothing was seeded")
        # Check the effective DB before creating files or incident rows; no migration/reset here.
        with psycopg.connect(database) as conn:
            if conn.execute("SELECT current_database()").fetchone() != ("incidentdesk_dev",):
                raise ValueError("Smoke seeding only writes the incidentdesk_dev database")
            conn.execute("SELECT 1 FROM incident_evidence_imports LIMIT 0")
        identifier = uuid.uuid4()
        bundle_name = f"mig03-smoke-{identifier.hex}"
        destination = root / bundle_name
        destination.mkdir()  # Fresh child only; never replace an existing export.
        for name, content in decision.raw_artifacts.items():
            target = destination.joinpath(*safe_path(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as out:
                out.write(content)
        with args.video.open("rb") as stream:
            stored = LocalEvidenceStore(evidence_root).put(stream, sha256=digest)
        report = Report(
            run_id=identifier,
            input_hash=digest,
            prompt_version="mig03-smoke-synthetic",
            provenance="synthetic smoke fixture; not model analysis",
            summary="Synthetic MIG-03 smoke incident. Review UI demonstration only; no fault decision.",
            views=(
                ReportView(
                    filename=source.video_metadata.original_filename,
                    role=Role.EXTERIOR,
                    label="Synthetic smoke camera",
                    acquisition_method=None,
                    sha256=digest,
                    size_bytes=stored.size,
                ),
            ),
            facts=OverviewFacts(
                parties=[
                    PartyFact(
                        description="Synthetic white Toyota sedan",
                        color="white",
                        make_model="Toyota",
                        body_type="sedan",
                        party_code="P1",
                        view_id=digest,
                        timestamp_seconds=timestamp,
                        confidence=1.0,
                    )
                ],
                events=[
                    CitedFact(
                        description="Synthetic smoke navigation marker; not an observed incident finding",
                        view_id=digest,
                        timestamp_seconds=timestamp,
                        confidence=1.0,
                    )
                ],
                candidate_collisions=[],
                behaviour_observations=[],
                location_conditions=[],
            ),
        )
        RunLog(database).record(
            Run(
                run_id=identifier,
                origin=Origin.REPLAY,
                created_at=datetime.now(UTC),
                input_hash=digest,
                report=report,
                status=RunStatus.SUCCEEDED,
                total_latency_ms=0,
            )
        )
        print(
            json.dumps(
                {
                    "run_id": str(identifier),
                    "review_path": f"/reports/{identifier}/review",
                    "bundle": bundle_name,
                    "view_sha256": digest,
                    "producer_case_id": source.producer_case_id or decision.envelope.case_id,
                    "note": "Open an existing valid local share session; attach the named bundle from the review form.",
                    "video_format": args.video.suffix,
                    "browser_note": "Original bytes preserved. AVI playback depends on browser codec support.",
                },
                indent=2,
            )
        )
        return 0
    except (ValueError, OSError):
        parser.exit(
            2,
            "Smoke seed refused: check dev database, local roots and matching fixture video (no credentials logged).\n",
        )
    except psycopg.Error:
        parser.exit(2, "Smoke seed database unavailable or not migrated; no credentials logged.\n")


if __name__ == "__main__":
    raise SystemExit(main())
