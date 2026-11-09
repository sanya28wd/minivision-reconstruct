"""Reconstruction appendix frozen at approval; never part of report findings."""

from datetime import datetime
from typing import Any

import psycopg

from incident_report.domain import Report, Role
from incident_report.evidence_import.models import ImportDecision
from incident_report.evidence_import.persistence import load_import
from incident_report.evidence_import.reader import safe_path
from incident_report.reconstruction.alignment import Alignment
from incident_report.reconstruction.config import ReconstructionSettings
from incident_report.reconstruction.cross_check import Comparison, compare_view
from incident_report.reconstruction.database import local_database
from incident_report.reconstruction.driver_context import driver_panels
from incident_report.reconstruction.presentation import ViewPanel
from incident_report.reconstruction.timeline import timeline_groups
from incident_report.web.printing import embedded_css
from incident_report.web.templating import build_environment

LABEL = "Reconstruction proposal / review aid, not part of the approved findings"


def appendix(
    report: Report,
    approved_at: datetime,
    database_url: str,
    settings: ReconstructionSettings,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, bytes]:
    if not settings.enabled:
        return {}
    files: dict[str, bytes] = {}
    panels: dict[str, ViewPanel] = {}
    with psycopg.connect(local_database(database_url)) as conn:
        # All presentation records are append-only. Exclude links, supersessions,
        # mappings and alignments made after this version was approved.
        links = conn.execute(
            "SELECT l.import_id,l.view_sha256,l.source_view_id,l.link_id FROM incident_evidence_imports l WHERE l.incident_id=%s AND l.attached_at<=%s AND NOT EXISTS (SELECT 1 FROM incident_evidence_supersessions s WHERE s.old_link_id=l.link_id AND s.created_at<=%s) ORDER BY l.sequence_number",
            (report.run_id, approved_at, approved_at),
        ).fetchall()
        if snapshot is not None:
            # Snapshot IDs in the immutable approval event are authoritative even if
            # an in-flight transaction later commits an older created_at timestamp.
            links = conn.execute(
                "SELECT import_id,view_sha256,source_view_id,link_id FROM incident_evidence_imports WHERE incident_id=%s AND link_id=ANY(%s::uuid[]) ORDER BY sequence_number",
                (report.run_id, snapshot["link_ids"]),
            ).fetchall()
        for view in report.views:
            if view.role == Role.INTERIOR:
                continue
            groups = list(timeline_groups(None, "", view.sha256, report.storyline))
            comparisons: list[Comparison] = []
            for import_id, key, source, _link_id in links:
                if key != view.sha256:
                    continue
                safe_path(import_id)
                decision = load_import(import_id, conn)
                if not isinstance(decision, ImportDecision) or decision.provenance_state == "rejected":
                    raise ValueError("Attached reconstruction snapshot is unavailable.")
                for name, content in decision.raw_artifacts.items():
                    safe_path(name)
                    files[f"reconstruction/{import_id}/{name}"] = content
                mappings: dict[str, str] = {}
                valid_codes = (
                    {p.party_code for p in report.facts.parties if p.view_id == key and p.party_code}
                    if report.facts
                    else set()
                )
                if snapshot is None:
                    rows = conn.execute(
                        "SELECT track_ref,party_code,action,mapping_id FROM track_party_mappings WHERE incident_id=%s AND import_id=%s AND view_sha256=%s AND created_at<=%s ORDER BY mapping_id",
                        (report.run_id, import_id, key, approved_at),
                    )
                else:
                    rows = conn.execute(
                        "SELECT track_ref,party_code,action,mapping_id FROM track_party_mappings WHERE incident_id=%s AND import_id=%s AND view_sha256=%s AND mapping_id=ANY(%s::bigint[]) ORDER BY mapping_id",
                        (report.run_id, import_id, key, snapshot["mapping_ids"]),
                    )
                for ref, code, action, _mapping_id in rows:
                    mappings.pop(ref, None)
                    if action == "map" and code in valid_codes:
                        mappings[ref] = code
                comparisons.extend(compare_view(decision, source, key, report.facts, mappings))
                groups.extend(timeline_groups(decision, source, key, ()))
            panels[view.sha256] = ViewPanel(tuple(groups), (), (), comparisons=tuple(comparisons))
        if snapshot is None:
            alignment_rows = conn.execute(
                "SELECT view_sha256,reference_view_sha256,offset_seconds,uncertainty_seconds,clock_reliability,reviewer,reason FROM view_time_alignments WHERE incident_id=%s AND created_at<=%s ORDER BY alignment_id",
                (report.run_id, approved_at),
            )
        else:
            alignment_rows = conn.execute(
                "SELECT view_sha256,reference_view_sha256,offset_seconds,uncertainty_seconds,clock_reliability,reviewer,reason FROM view_time_alignments WHERE incident_id=%s AND alignment_id=ANY(%s::bigint[]) ORDER BY alignment_id",
                (report.run_id, snapshot["alignment_ids"]),
            )
        alignments = tuple(Alignment(*row) for row in alignment_rows)
    if not files:
        return {}
    context: dict[str, Any] = {
        "report": report,
        "reconstruction": panels,
        "driver_context": driver_panels(report, panels, alignments) if settings.driver_context_enabled else {},
        "proposal_label": LABEL,
        "print_view": True,
        "embedded_css": embedded_css(),
    }
    files["reconstruction/review_aid.html"] = (
        build_environment().get_template("package_appendix.html").render(**context).encode()
    )
    return files
