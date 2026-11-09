"""Explicit display-only track associations. Latest row wins, history never changes."""

import uuid
from dataclasses import dataclass
from typing import Any

from incident_report.evidence_import.payloads import TrackFields

from .store import Attachment, Workspace, WorkspaceRefused


@dataclass(frozen=True)
class Mapping:
    mapping_id: int
    import_id: str
    track_ref: str
    view_sha256: str
    party_code: str | None
    action: str
    reviewer: str
    reason: str
    stale: bool


@dataclass(frozen=True)
class TrackOption:
    track_ref: str
    track_id: int
    label: str
    action: str
    import_id: str
    parties: tuple[tuple[str, str], ...]
    reconfirm: Mapping | None


def parties_for(incident: dict[str, Any], view: str) -> tuple[tuple[str, str], ...]:
    facts = incident.get("facts") or {}
    parties = [
        (p["party_code"], p["description"])
        for p in facts.get("parties", [])
        if p.get("view_id") == view and p.get("party_code")
    ]
    # An ambiguous party code must never select a party implicitly.
    return tuple((code, description) for code, description in parties if sum(c == code for c, _ in parties) == 1)


def mapping_history(workspace: Workspace, incident_id: uuid.UUID) -> tuple[Mapping, ...]:
    workspace._enabled()
    current = {(link.import_id, link.view_sha256) for link in workspace.current(incident_id)}
    rows = workspace.connection.execute(
        "SELECT mapping_id,import_id,track_ref,view_sha256,party_code,action,reviewer,reason FROM track_party_mappings WHERE incident_id=%s ORDER BY mapping_id",
        (incident_id,),
    )
    return tuple(
        Mapping(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], (row[1], row[3]) not in current)
        for row in rows
    )


def track_options(workspace: Workspace, incident_id: uuid.UUID, link: Attachment) -> tuple[TrackOption, ...]:
    workspace._enabled()
    incident = workspace._incident(incident_id)
    parties = parties_for(incident, link.view_sha256)
    descriptions = dict(parties)
    tracks: dict[str, TrackFields] = {}
    for record in workspace.decision(link.import_id).records:
        if record.view_id == link.source_view_id and isinstance(record.data, TrackFields):
            for ref in record.track_refs:
                tracks.setdefault(ref, record.data)
    history = mapping_history(workspace, incident_id)
    past_tracks = {}
    for import_id in {r.import_id for r in history if r.stale}:
        for record in workspace.decision(import_id).records:
            if isinstance(record.data, TrackFields):
                for ref in record.track_refs:
                    past_tracks[(import_id, ref)] = record.data
    result = []
    for ref, track in sorted(tracks.items()):
        latest = next(
            (
                r
                for r in reversed(history)
                if r.import_id == link.import_id and r.track_ref == ref and r.view_sha256 == link.view_sha256
            ),
            None,
        )

        # A suggestion is never applied: the reviewer must explicitly re-confirm on the new ref.
        def same_track(row: Mapping, track: TrackFields = track) -> bool:
            old = past_tracks.get((row.import_id, row.track_ref))
            return bool(
                old
                and (old.producer, old.case_id, old.tracking_run, old.track_id)
                == (track.producer, track.case_id, track.tracking_run, track.track_id)
            )

        reconfirm = next(
            (r for r in reversed(history) if r.stale and r.view_sha256 == link.view_sha256 and same_track(r)), None
        )
        label = descriptions.get(latest.party_code or "") if latest and latest.action == "map" else None
        result.append(
            TrackOption(
                ref,
                track.track_id,
                label
                or f"Unmapped reconstruction track ({track.class_name or 'unknown'}, tool track identifier {track.track_id})",
                latest.action if latest else "unmapped",
                link.import_id,
                parties,
                reconfirm,
            )
        )
    return tuple(result)


def map_track(
    workspace: Workspace,
    incident_id: uuid.UUID,
    import_id: str,
    view: str,
    track_ref: str,
    action: str,
    party_code: str | None,
    reviewer: str,
    reason: str,
    *,
    reconfirm_id: int | None = None,
) -> None:
    workspace._enabled()
    if action not in {"map", "unmap", "cannot_determine"} or not reviewer.strip() or not reason.strip():
        raise WorkspaceRefused("Choose a mapping action and record reviewer and reason")
    if len(reviewer) > 200 or len(reason) > 4000:
        raise WorkspaceRefused("Reviewer or reason is too long")
    with workspace.connection.transaction():
        workspace._incident(incident_id)  # serialize mapping with attachment/supersession
        link = next(
            (
                candidate
                for candidate in workspace.current(incident_id)
                if candidate.import_id == import_id and candidate.view_sha256 == view
            ),
            None,
        )
        if link is None:
            raise WorkspaceRefused("Choose a track from the current reconstruction revision")
        option = next((o for o in track_options(workspace, incident_id, link) if o.track_ref == track_ref), None)
        if option is None:
            raise WorkspaceRefused("Unknown reconstruction track")
        if reconfirm_id is not None:
            prior = option.reconfirm
            if prior is None or prior.mapping_id != reconfirm_id:
                raise WorkspaceRefused("Prior mapping does not match this track; review it manually")
            action, party_code = prior.action, prior.party_code
        if action == "map" and party_code not in dict(option.parties):
            raise WorkspaceRefused("Choose a party from the same view")
        if action != "map":
            party_code = None
        workspace.connection.execute(
            "INSERT INTO track_party_mappings(incident_id,import_id,track_ref,view_sha256,party_code,action,reviewer,reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (incident_id, import_id, track_ref, view, party_code, action, reviewer.strip(), reason.strip()),
        )
