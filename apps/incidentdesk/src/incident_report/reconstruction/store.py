"""Append-only incident links; reconstruction never enters AI or fault inputs."""

import uuid
from dataclasses import dataclass
from typing import Any

from incident_report.evidence_import.models import ImportDecision
from incident_report.evidence_import.persistence import load_import, persist_inspection
from incident_report.evidence_import.reader import inspect_bundle

from .config import ReconstructionSettings
from .paths import bundle_path
from .revisions import is_successor


class WorkspaceRefused(ValueError):
    """A plain workspace boundary refusal."""


@dataclass(frozen=True)
class Attachment:
    link_id: uuid.UUID
    incident_id: uuid.UUID
    import_id: str
    view_sha256: str
    source_view_id: str
    producer_case_id: str
    superseded_by: uuid.UUID | None


class Workspace:
    def __init__(self, connection: Any, settings: ReconstructionSettings):
        self.connection = connection
        self.settings = settings

    def _enabled(self) -> None:
        if not self.settings.enabled:
            raise WorkspaceRefused("Reconstruction workspace is disabled")

    def _incident(self, incident_id: uuid.UUID) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT report FROM runs WHERE run_id=%s AND status=%s FOR UPDATE", (incident_id, "succeeded")
        ).fetchone()
        if row is None or row[0] is None:
            raise WorkspaceRefused("Incident report not found")
        return dict(row[0])

    def decision(self, import_id: str) -> ImportDecision:
        self._enabled()
        decision = load_import(import_id, self.connection)
        if not isinstance(decision, ImportDecision):
            raise WorkspaceRefused("Import needs re-importing with the current contract")
        return decision

    def history(self, incident_id: uuid.UUID) -> tuple[Attachment, ...]:
        self._enabled()
        rows = self.connection.execute(
            "SELECT l.link_id,l.incident_id,l.import_id,l.view_sha256,l.source_view_id,l.producer_case_id,s.new_link_id FROM incident_evidence_imports l LEFT JOIN incident_evidence_supersessions s ON s.old_link_id=l.link_id WHERE l.incident_id=%s ORDER BY l.sequence_number",
            (incident_id,),
        )
        return tuple(Attachment(*row) for row in rows)

    def current(self, incident_id: uuid.UUID) -> tuple[Attachment, ...]:
        return tuple(link for link in self.history(incident_id) if link.superseded_by is None)

    def attachment_note(self, link: Attachment) -> str | None:
        if link.superseded_by is None:
            return None
        successor = next(row for row in self.history(link.incident_id) if row.link_id == link.superseded_by)
        return f"this revision is superseded by {successor.import_id}"

    def attach(self, incident_id: uuid.UUID, relative_bundle: str, reviewer: str) -> tuple[Attachment, ...]:
        self._enabled()
        if not reviewer.strip():
            raise WorkspaceRefused("Record who attached this export")
        root = self.settings.evidence_exports_root
        if root is None:
            raise WorkspaceRefused("Evidence exports root is not configured")
        directory = bundle_path(root, relative_bundle)
        # Inspect once, then validate the exact snapshot before any import write.
        inspected = inspect_bundle(directory)
        with self.connection.transaction():
            incident = self._incident(incident_id)
            interior = {v["sha256"] for v in incident["views"] if v.get("role") == "interior"}
            if inspected.envelope and any(s.original_video_sha256 in interior for s in inspected.envelope.sources):
                raise WorkspaceRefused("reconstruction applies to road views only")
            decision = persist_inspection(inspected, self.connection)
        if decision.provenance_state == "rejected" or decision.envelope is None:
            raise WorkspaceRefused("Rejected exports cannot be attached")
        with self.connection.transaction():
            incident = self._incident(incident_id)
            sources = decision.envelope.sources
            if any(
                v.get("role") == "interior" and v["sha256"] == s.original_video_sha256
                for v in incident["views"]
                for s in sources
            ):
                raise WorkspaceRefused("reconstruction applies to road views only")
            views = {view["sha256"] for view in incident["views"]}
            if not sources or any(s.original_video_sha256 not in views for s in sources):
                raise WorkspaceRefused("this export is for a different video")
            created = []
            for source in sources:
                existing = next(
                    (
                        link
                        for link in self.history(incident_id)
                        if link.import_id == decision.import_id and link.view_sha256 == source.original_video_sha256
                    ),
                    None,
                )
                if existing is not None:
                    created.append(existing)
                    continue
                case = source.producer_case_id or decision.envelope.case_id
                previous = [
                    link
                    for link in self.current(incident_id)
                    if link.view_sha256 == source.original_video_sha256 and link.producer_case_id == case
                ]
                if any(
                    not is_successor(self.decision(link.import_id), decision, source.original_video_sha256)
                    for link in previous
                ):
                    raise WorkspaceRefused("Cannot establish that this export is a newer reconstruction revision")
                identifier = uuid.uuid4()
                self.connection.execute(
                    "INSERT INTO incident_evidence_imports(link_id,incident_id,import_id,view_sha256,source_view_id,producer_case_id,attached_by) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        identifier,
                        incident_id,
                        decision.import_id,
                        source.original_video_sha256,
                        source.view_id,
                        case,
                        reviewer.strip(),
                    ),
                )
                for old in previous:
                    self.connection.execute(
                        "INSERT INTO incident_evidence_supersessions(incident_id,old_link_id,new_link_id) VALUES (%s,%s,%s)",
                        (incident_id, old.link_id, identifier),
                    )
                created.append(
                    Attachment(
                        identifier,
                        incident_id,
                        decision.import_id,
                        source.original_video_sha256,
                        source.view_id,
                        case,
                        None,
                    )
                )
            return tuple(created)
