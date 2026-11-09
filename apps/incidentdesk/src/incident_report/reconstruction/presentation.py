"""Read-only web context; never mutates Report or its cited storyline."""

from dataclasses import dataclass, replace
from typing import Any

from incident_report.domain import Report, Role

from .alignment import RELIABILITIES, combined_groups
from .alignment import history as alignment_history
from .config import ReconstructionSettings, native_tools_enabled
from .cross_check import Comparison, compare_view
from .driver_context import driver_panels
from .mapping import Mapping, TrackOption, mapping_history, track_options
from .paths import list_bundles
from .store import Attachment, Workspace
from .timeline import MISSING_LABELS, TimelineGroup, timeline_groups
from .tools import native_links, tool_links


@dataclass(frozen=True)
class NativeSelection:
    description: str
    tools: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ViewPanel:
    groups: tuple[TimelineGroup, ...]
    notices: tuple[str, ...]
    links: tuple[Attachment, ...]
    tool_selections: tuple[str, ...] = ()
    tracks: tuple[TrackOption, ...] = ()
    stale_mappings: tuple[Mapping, ...] = ()
    comparisons: tuple[Comparison, ...] = ()
    native_selections: tuple[NativeSelection, ...] = ()


def context(workspace: Workspace, report: Report, settings: ReconstructionSettings) -> dict[str, Any]:
    if not settings.enabled:
        return {}
    current = workspace.current(report.run_id)
    native = native_tools_enabled()
    panels = {}
    for view in report.views:
        links = tuple(link for link in current if link.view_sha256 == view.sha256)
        groups = list(timeline_groups(None, "", view.sha256, report.storyline))
        notices = []
        if view.role == Role.INTERIOR:
            if links:
                notices.append("reconstruction export attached to an interior view is ignored (road views only)")
            links = ()
        for previous in workspace.history(report.run_id):
            if (
                view.role != Role.INTERIOR
                and previous.view_sha256 == view.sha256
                and previous.superseded_by is not None
            ):
                notices.append(f"Imported revision {previous.import_id}: {workspace.attachment_note(previous)}")
        selections = []
        native_selections = []
        tracks: list[TrackOption] = []
        comparisons: list[Comparison] = []
        history = mapping_history(workspace, report.run_id)
        for link in links:
            decision = workspace.decision(link.import_id)
            if decision.envelope:
                source = next(s for s in decision.envelope.sources if s.view_id == link.source_view_id)
                selections.append(
                    f"Case: {link.producer_case_id}; tracking run: {source.tracking_run}; tracks revision: {source.track_revision}; calibration: {source.calibration_revision}; reconstruction run: {source.reconstruction_run}; incident run: {source.incident_run}; review revision: {source.review_revision}"
                )
                if native:
                    native_selections.append(NativeSelection(selections[-1], native_links(report.run_id, link.link_id)))
            options = track_options(workspace, report.run_id, link)
            tracks.extend(options)
            latest = {
                m.track_ref: m
                for m in history
                if not m.stale and m.import_id == link.import_id and m.view_sha256 == view.sha256
            }
            valid_refs = {o.track_ref for o in options}
            party_codes = {code for o in options for code, _ in o.parties}
            mappings = {
                ref: m.party_code
                for ref, m in latest.items()
                if ref in valid_refs and m.action == "map" and m.party_code in party_codes and m.party_code is not None
            }
            comparisons.extend(compare_view(decision, link.source_view_id, view.sha256, report.facts, mappings))
            labels = {o.track_ref: o.label for o in options}
            for group in timeline_groups(decision, link.source_view_id, view.sha256, ()):
                groups.append(
                    replace(
                        group,
                        rows=tuple(
                            replace(
                                row,
                                text=row.text
                                + (
                                    " — "
                                    + "; ".join(
                                        labels.get(ref, "Unmapped reconstruction track (unknown)")
                                        for ref in row.track_refs
                                    )
                                    if row.track_refs
                                    else ""
                                ),
                            )
                            for row in group.rows
                        ),
                    )
                )
            for code in decision.missing_inputs:
                notices.append("Reconstruction could not assess this view: " + MISSING_LABELS.get(code, code))
            if decision.provenance_state != "verified":
                notices.append("Reconstruction provenance is incomplete; review the imported evidence before use.")
        groups.sort(
            key=lambda group: min(
                (r.source_time for r in group.rows if r.source_time is not None), default=float("inf")
            )
        )
        stale = (
            tuple(m for m in mapping_history(workspace, report.run_id) if m.stale and m.view_sha256 == view.sha256)
            if view.role != Role.INTERIOR
            else ()
        )
        panels[view.sha256] = ViewPanel(
            tuple(groups),
            tuple(dict.fromkeys(notices)),
            links,
            tuple(selections),
            tuple(tracks),
            stale,
            tuple(comparisons),
            tuple(native_selections),
        )
    alignments = alignment_history(workspace, report.run_id)
    return {
        "driver_context": driver_panels(report, panels, alignments) if settings.driver_context_enabled else {},
        "reconstruction": panels,
        "reconstruction_disagreements": {
            view: tuple(
                dict.fromkeys(
                    r.item
                    for r in panel.comparisons
                    if r.state in {"disagree_time", "disagree_parties", "disagree_motion", "tracker_miss", "model_miss"}
                )
            )
            for view, panel in panels.items()
        },
        "reconstruction_tools": tool_links(),
        "reconstruction_alignments": alignments,
        "reconstruction_clocks": RELIABILITIES,
        "reconstruction_combined": combined_groups(panels, alignments),
        "reconstruction_bundles": list_bundles(settings.evidence_exports_root)
        if settings.evidence_exports_root
        else (),
    }
