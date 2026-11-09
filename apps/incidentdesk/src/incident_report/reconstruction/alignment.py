"""Display-only camera alignment; source citations always remain native."""

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .store import Workspace, WorkspaceRefused
from .timeline import TimelineGroup, TimelineRow

RELIABILITIES = ("unknown", "reviewer_estimated", "shared_clock_documented")
UNKNOWN_ORDER = "order across cameras not determined"


@dataclass(frozen=True)
class Alignment:
    view: str
    reference: str
    offset: float
    uncertainty: float
    clock_reliability: str
    reviewer: str
    reason: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.offset) or not math.isfinite(self.uncertainty) or self.uncertainty < 0:
            raise WorkspaceRefused("Alignment requires a finite offset and nonnegative uncertainty")
        if self.clock_reliability not in RELIABILITIES:
            raise WorkspaceRefused("Choose a documented clock reliability")
        if not self.reviewer.strip() or not self.reason.strip() or len(self.reviewer) > 200 or len(self.reason) > 4000:
            raise WorkspaceRefused("Record reviewer and reason for the alignment")


def compare(a_time: float, a: Alignment | None, b_time: float, b: Alignment | None) -> str:
    if (
        a is None
        or b is None
        or a.reference != b.reference
        or a.clock_reliability == "unknown"
        or b.clock_reliability == "unknown"
        or not math.isfinite(a_time)
        or not math.isfinite(b_time)
    ):
        return UNKNOWN_ORDER
    difference = (b_time + b.offset) - (a_time + a.offset)
    uncertainty = a.uncertainty + b.uncertainty
    if difference > uncertainty:
        return "before"
    if -difference > uncertainty:
        return "after"
    return UNKNOWN_ORDER


def history(workspace: Workspace, incident_id: uuid.UUID) -> tuple[Alignment, ...]:
    workspace._enabled()
    rows = workspace.connection.execute(
        "SELECT view_sha256,reference_view_sha256,offset_seconds,uncertainty_seconds,clock_reliability,reviewer,reason FROM view_time_alignments WHERE incident_id=%s ORDER BY alignment_id",
        (incident_id,),
    )
    return tuple(Alignment(*row) for row in rows)


def record_alignment(workspace: Workspace, incident_id: uuid.UUID, alignment: Alignment) -> None:
    workspace._enabled()
    with workspace.connection.transaction():
        incident = workspace._incident(incident_id)
        views = {v["sha256"] for v in incident["views"]}
        if alignment.view not in views or alignment.reference not in views:
            raise WorkspaceRefused("Choose views from this incident")
        if alignment.view == alignment.reference and alignment.offset != 0:
            raise WorkspaceRefused("A reference view has zero offset to itself")
        workspace.connection.execute(
            "INSERT INTO view_time_alignments(incident_id,view_sha256,reference_view_sha256,offset_seconds,uncertainty_seconds,clock_reliability,reviewer,reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                incident_id,
                alignment.view,
                alignment.reference,
                alignment.offset,
                alignment.uncertainty,
                alignment.clock_reliability,
                alignment.reviewer.strip(),
                alignment.reason.strip(),
            ),
        )


class Panel(Protocol):
    @property
    def groups(self) -> tuple[TimelineGroup, ...]: ...


@dataclass(frozen=True)
class AlignedRow:
    row: TimelineRow
    aligned_time: float
    uncertainty: float


@dataclass(frozen=True)
class AlignedGroup:
    reference: str
    rows: tuple[AlignedRow, ...]
    order_unresolved: bool


def _aligned_groups(reference: str, rows: list[AlignedRow]) -> tuple[AlignedGroup, ...]:
    # Offset uncertainty compares cameras, not two moments on the same native clock.
    native_order = tuple(sorted(rows, key=lambda r: r.aligned_time))
    if len({r.row.view_id for r in rows}) == 1:
        return tuple(AlignedGroup(reference, (row,), False) for row in native_order)
    return (AlignedGroup(reference, native_order, True),)


def combined_groups(panels: Mapping[str, Panel], alignments: tuple[Alignment, ...]) -> tuple[AlignedGroup, ...]:
    latest = {a.view: a for a in alignments}
    references: dict[str, list[AlignedRow]] = {}
    for view, panel in panels.items():
        alignment = latest.get(view)
        if alignment is None or alignment.clock_reliability == "unknown":
            continue
        for group in panel.groups:
            if group.order_unresolved:
                continue
            for row in group.rows:
                if row.source_time is not None and not row.rejected:
                    references.setdefault(alignment.reference, []).append(
                        AlignedRow(row, row.source_time + alignment.offset, alignment.uncertainty)
                    )
    result: list[AlignedGroup] = []
    for reference, rows in sorted(references.items()):
        if len({r.row.view_id for r in rows}) < 2:
            continue
        rows.sort(key=lambda r: r.aligned_time - r.uncertainty)
        pending: list[AlignedRow] = []

        upper = float("-inf")
        for aligned in rows:
            lower = aligned.aligned_time - aligned.uncertainty
            if pending and lower > upper:
                result.extend(_aligned_groups(reference, pending))
                pending = []
            pending.append(aligned)
            upper = max(upper, aligned.aligned_time + aligned.uncertainty)
        if pending:
            result.extend(_aligned_groups(reference, pending))
    return tuple(result)
