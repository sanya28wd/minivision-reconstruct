"""The "What happened" storyline and the one-line summary, built by code from cited facts only.

No model prose is used: every sentence is a cited event, or a plain sentence code writes from a typed collision
observation, and carries its view and timestamp (ADR 0006). Party names are already display names by the time
facts are stored. The 0.25 s duplicate window is a project assumption, not a sourced figure.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from incident_report.facts import CONTACT_KINDS, CitedFact, CollisionObservation, ObservationNote, OverviewFacts

if TYPE_CHECKING:
    from incident_report.domain import Report

# Project assumption (no source): an event this close in time to a contact, naming the same parties, restates it.
DUPLICATE_SECONDS = 0.25
# The fixed strings the live Gemini adapter used as its summary before the code-built line existed.
EXTRACTED_PLACEHOLDER = "Overview facts extracted from one view."
REPAIRED_PLACEHOLDER = "Overview facts repaired."
PLACEHOLDER_SUMMARIES = frozenset({EXTRACTED_PLACEHOLDER, REPAIRED_PLACEHOLDER})
NO_COLLISION_SUMMARY = "No collision found in the reviewed footage."
CANNOT_DETERMINE_SUMMARY = "Cannot Determine: the reviewed footage does not show whether a collision occurred."

_AFTER_SENTENCES = {
    "person_injured_visible": "{s} is visibly hurt",
    "person_dead_visible": "{s} is clearly shown or stated to be dead",
    "no_injury_visible": "{s} is seen moving normally and unharmed",
    "vehicle_stopped_at_scene": "{s} stops at the scene",
    "vehicle_driven_away": "{s} is driven away",
    "vehicle_left_scene": "{s} leaves the scene without stopping",
}


class StorylineItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["event", "note", "contact", "after"]
    text: str = Field(min_length=1)
    view_id: str = Field(min_length=1)
    timestamp_seconds: float = Field(ge=0)

    def citation(self) -> CitedFact:
        return CitedFact(
            description=self.text, view_id=self.view_id, timestamp_seconds=self.timestamp_seconds, confidence=1.0
        )


def contact_phrase(c: CollisionObservation) -> str:
    """The contact as a plain clause, without time or visibility note."""
    if c.kind == "vehicle_contact_vehicle":
        return f"{c.subject} and {c.other_party} make contact"
    if c.kind == "vehicle_struck_pedestrian":
        return f"{c.subject} strikes {c.other_party}"
    if c.kind == "vehicle_overturned":
        return f"{c.subject} overturns"
    return f"{c.subject} touches {c.other_party}"


def _contact_sentence(c: CollisionObservation) -> str:
    note = " (touch not clearly visible)" if c.visibility == "possible_obscured" else ""
    return contact_phrase(c) + note


def _contact_parties(c: CollisionObservation) -> set[str]:
    return {name for name in (c.subject, c.other_party) if name}


def _note_text(note: ObservationNote, names: dict[tuple[str, str], str]) -> str:
    """A note always starts with who it is about: the party's display name, "Scene", or its code if it has no name."""
    if note.party_code == "scene":
        return f"Scene: {note.note}"
    name = names.get((note.view_id, note.party_code)) or (note.subject if note.subject != "scene" else "")
    return f"{name or note.party_code}: {note.note}"


def build_storyline(
    facts: OverviewFacts | None, view_order: Sequence[str] = (), *, include_notes: bool = False
) -> tuple[StorylineItem, ...]:
    """Events and typed collision observations merged by time, one clock per view, duplicates dropped."""
    if facts is None:
        return ()
    contacts = [c for c in facts.collision_observations if c.kind in CONTACT_KINDS]
    items: list[StorylineItem] = []
    for c in facts.collision_observations:
        if c.kind in CONTACT_KINDS:
            items.append(
                StorylineItem(
                    kind="contact", text=_contact_sentence(c), view_id=c.view_id, timestamp_seconds=c.timestamp_seconds
                )
            )
        else:
            items.append(
                StorylineItem(
                    kind="after",
                    text=_AFTER_SENTENCES.get(c.kind, "{s}: " + c.kind.replace("_", " ")).format(s=c.subject),
                    view_id=c.view_id,
                    timestamp_seconds=c.timestamp_seconds,
                )
            )
    if not facts.collision_observations:
        # Records before the typed observations existed: their candidate collisions are the contacts.
        for fact in facts.candidate_collisions:
            items.append(
                StorylineItem(
                    kind="contact",
                    text=fact.description,
                    view_id=fact.view_id,
                    timestamp_seconds=fact.timestamp_seconds,
                )
            )
    # Events first, then observation notes (including scene notes): both are cited model text, dropped when they
    # restate a contact or an item already kept.
    names = {(row.view_id, row.party_code): row.display_name for row in facts.party_checklist}
    sources = [("event", e.description, e.view_id, e.timestamp_seconds, e.description) for e in facts.events] + [
        ("note", n.note, n.view_id, n.timestamp_seconds, _note_text(n, names)) for n in facts.observation_notes
    ]
    if not include_notes:
        sources = [src for src in sources if src[0] != "note"]
    kept: list[StorylineItem] = []
    seen: list[tuple[str, float, str]] = []
    for kind, text, view_id, seconds, shown in sources:
        if any(
            c.view_id == view_id
            and abs(c.timestamp_seconds - seconds) <= DUPLICATE_SECONDS
            and _contact_parties(c)
            and all(name in text for name in _contact_parties(c))
            for c in contacts
        ):
            continue
        if any(v == view_id and abs(t - seconds) <= DUPLICATE_SECONDS and raw == text for v, t, raw in seen):
            continue
        seen.append((view_id, seconds, text))
        kept.append(StorylineItem(kind=kind, text=shown, view_id=view_id, timestamp_seconds=seconds))  # type: ignore[arg-type]
    items.extend(kept)
    rank = {view: index for index, view in enumerate(view_order)}
    kind_rank = {"note": 0, "event": 1, "contact": 2, "after": 3}
    items.sort(key=lambda i: (rank.get(i.view_id, len(rank)), i.view_id, i.timestamp_seconds, kind_rank[i.kind]))
    return tuple(items)


def first_contact(facts: OverviewFacts | None, view_order: Sequence[str] = ()) -> StorylineItem | None:
    """The earliest contact, as a storyline item (the text is the contact clause only)."""
    if facts is None:
        return None
    rank = {view: index for index, view in enumerate(view_order)}

    def key(view_id: str, t: float) -> tuple[int, str, float]:
        return (rank.get(view_id, len(rank)), view_id, t)

    contacts = [c for c in facts.collision_observations if c.kind in CONTACT_KINDS]
    if contacts:
        c = min(contacts, key=lambda c: key(c.view_id, c.timestamp_seconds))
        return StorylineItem(
            kind="contact", text=contact_phrase(c), view_id=c.view_id, timestamp_seconds=c.timestamp_seconds
        )
    if facts.candidate_collisions:
        f = min(facts.candidate_collisions, key=lambda f: key(f.view_id, f.timestamp_seconds))
        return StorylineItem(
            kind="contact", text=f.description, view_id=f.view_id, timestamp_seconds=f.timestamp_seconds
        )
    return None


def build_summary(report: Report) -> str:
    """One line from facts already present: status, the parties of the first contact, and its time."""
    status = report.incident_status
    if status == "Cannot Determine":
        return CANNOT_DETERMINE_SUMMARY
    if status == "Collision":
        first = first_contact(report.facts, [v.sha256 for v in report.views])
        if first is not None:
            return f"Collision: {first.text} at {first.timestamp_seconds:.1f} s."
        return "Collision found in the reviewed footage."
    return NO_COLLISION_SUMMARY
