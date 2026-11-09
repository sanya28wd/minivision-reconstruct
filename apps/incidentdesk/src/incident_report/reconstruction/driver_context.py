"""Read-only interior intervals; never an input to driver or collision rules."""

from collections.abc import Mapping
from dataclasses import dataclass

from incident_report.domain import Report, Role
from incident_report.facts import CONTACT_KINDS, DriverObservation
from incident_report.rules.driver import DriverAssessment

from .alignment import Alignment, Panel


@dataclass(frozen=True)
class ClockLink:
    offset: float
    uncertainty: float
    reviewers: tuple[str, ...]


@dataclass(frozen=True)
class ContactTime:
    label: str
    time: float
    source_frame: int | None = None
    review_status: str = ""


@dataclass(frozen=True)
class DriverTiming:
    observation: DriverObservation
    start: float
    end: float
    contact: ContactTime
    start_difference: float
    end_difference: float
    relation: str


@dataclass(frozen=True)
class RoadContext:
    view_id: str
    label: str
    alignment: ClockLink | None
    timings: tuple[DriverTiming, ...]


@dataclass(frozen=True)
class DriverPanel:
    observations: tuple[DriverObservation, ...]
    assessments: tuple[DriverAssessment, ...]
    roads: tuple[RoadContext, ...]


def _clock_link(interior: str, road: str, latest: Mapping[str, Alignment]) -> ClockLink | None:
    a, b = latest.get(interior), latest.get(road)
    if a is not None and a.clock_reliability != "unknown" and a.reference == road:
        return ClockLink(a.offset, a.uncertainty, (a.reviewer,))
    if b is not None and b.clock_reliability != "unknown" and b.reference == interior:
        return ClockLink(-b.offset, b.uncertainty, (b.reviewer,))
    if (
        a is not None
        and b is not None
        and a.clock_reliability != "unknown"
        and b.clock_reliability != "unknown"
        and a.reference == b.reference
    ):
        return ClockLink(a.offset - b.offset, a.uncertainty + b.uncertainty, (a.reviewer, b.reviewer))
    return None


def _contacts(report: Report, road: str, panels: Mapping[str, Panel]) -> tuple[ContactTime, ...]:
    contacts = []
    if report.facts:
        for assessment in report.facts.incident_assessments:
            if assessment.view_id == road:
                for label, time in (
                    ("Video analysis contact interval start", assessment.contact_interval_start_seconds),
                    ("Video analysis contact interval end", assessment.contact_interval_end_seconds),
                ):
                    if time is not None:
                        qualifier = (
                            " (contact possible, obscured)"
                            if assessment.contact_state == "contact_possible_obscured"
                            else ""
                        )
                        contacts.append(ContactTime(label + qualifier, time))
        for contact in report.facts.collision_observations:
            if contact.view_id == road and contact.kind in CONTACT_KINDS:
                qualifier = " (vehicle overturned)" if contact.kind == "vehicle_overturned" else ""
                contacts.append(ContactTime("Video analysis contact timestamp" + qualifier, contact.timestamp_seconds))
    panel = panels.get(road)
    if panel:
        for group in panel.groups:
            for row in group.rows:
                if (
                    row.event_type == "collision_candidate"
                    and not row.rejected
                    and not row.needs_review
                    and not group.order_unresolved
                    and row.source_time is not None
                ):
                    contacts.append(
                        ContactTime(
                            "Reconstruction candidate peak",
                            row.source_time,
                            row.source_frame,
                            row.review_status,
                        )
                    )
    return tuple(contacts)


def _timing(observation: DriverObservation, contact: ContactTime, link: ClockLink) -> DriverTiming:
    start, end = observation.start_seconds + link.offset, observation.end_seconds + link.offset
    t, u = contact.time, link.uncertainty
    relation = ""
    if start + u <= t <= end - u:
        relation = "overlaps contact time"
    elif start - u <= t <= end + u:
        relation = f"possible overlap (within ±{u:g} s)"
    return DriverTiming(observation, start, end, contact, start - t, end - t, relation)


def driver_panels(
    report: Report, panels: Mapping[str, Panel], alignments: tuple[Alignment, ...]
) -> dict[str, DriverPanel]:
    latest = {a.view: a for a in alignments}
    result = {}
    for view in report.views:
        if view.role != Role.INTERIOR:
            continue
        observations = (
            tuple(o for o in report.facts.driver_observations if o.view_id == view.sha256) if report.facts else ()
        )
        roads = []
        for road in report.views:
            if road.role == Role.INTERIOR:
                continue
            link = _clock_link(view.sha256, road.sha256, latest)
            roads.append(
                RoadContext(
                    road.sha256,
                    road.label or road.filename,
                    link,
                    tuple(_timing(o, c, link) for o in observations for c in _contacts(report, road.sha256, panels))
                    if link
                    else (),
                )
            )
        result[view.sha256] = DriverPanel(
            observations,
            tuple(a for a in report.driver_assessments if any(c.view_id == view.sha256 for c in a.cited_facts)),
            tuple(roads),
        )
    return result
