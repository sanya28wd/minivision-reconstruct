"""RPT-25: every Collision with its Dubai Police / MOI classification, derived by code (ADR 0007).

The model reports only typed, cited observations (`CollisionObservation`). This module derives:

- Accident Type: Collision between vehicles / Run-over / Rollover / Other (GLOSSARY.md "Accident Type",
  spec 001 "Domain rules and data"). Mapping from the observed form:
  vehicle_contact_vehicle -> Collision between vehicles; vehicle_struck_pedestrian -> Run-over;
  vehicle_overturned -> Rollover; contact_other -> Other. The only repo support for "Run-over" meaning a
  vehicle striking a pedestrian is docs/research/UAE_ROAD_STRUCTURE_AND_PRIORITY.md line 144 (Dubai Police,
  320 run-over accidents among pedestrian-related figures). No source covers cyclists or riders, so they fall
  under contact_other -> Other, never Run-over.
- Accident Outcome: Simple / Injury / Fatal (GLOSSARY.md "Accident Outcome"). Fatal needs a cited visible
  death; Injury needs a cited visible injury; Simple needs cited unharmed people and a vehicle visibly driven away. Anything else
  is Cannot Determine, never Simple. Impact force is never an input (ADR 0006; owner ruling 2026-10-16).
- Injury Grade: Serious / Moderate / Minor (GLOSSARY.md "Injury Grade") only when the cited observation
  carries it; otherwise Cannot Determine. No rule here maps injury signs to a grade: none is sourced.
- Cause: the repository holds no sourced MOI cause list (GLOSSARY.md gives two examples only;
  docs/research/UAE_ROAD_STRUCTURE_AND_PRIORITY.md open question 13 records that the 160-category list was
  not obtained). A Cause is therefore always Cannot Determine, with that reason, until the list is sourced.

Collisions come from the facts' candidate collisions, in order: views in first-appearance order (each view
has its own clock, ADR 0003), then by timestamp. Each observation attaches to the nearest candidate
collision on the same view (ties go to the earlier one); no distance threshold is introduced. An
observation on a view with no listed collision is returned as unattached, never dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from incident_report.facts import CitedFact, CollisionObservation, OverviewFacts

AccidentType = Literal["collision_between_vehicles", "run_over", "rollover", "other", "cannot_determine"]
AccidentOutcome = Literal["simple", "injury", "fatal", "cannot_determine"]
InjuryGrade = Literal["serious", "moderate", "minor", "cannot_determine"]

_TYPE_BY_KIND: dict[str, AccidentType] = {
    "vehicle_contact_vehicle": "collision_between_vehicles",
    "vehicle_struck_pedestrian": "run_over",
    "vehicle_overturned": "rollover",
    "contact_other": "other",
}


CAUSE_REASON = (
    "There is no sourced MOI cause list in this repository (GLOSSARY.md gives two examples only), "
    "so no Cause is assigned: Cannot Determine."
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InjuryRecord(_Frozen):
    """One visibly injured person. The citation is required: no citation, no injury."""

    subject: str
    grade: InjuryGrade
    cited: CitedFact


class CollisionRecord(_Frozen):
    """One Collision of the incident with its derived classification and the citations behind it."""

    number: int
    collision: CitedFact
    accident_type: AccidentType
    type_reason: str
    type_cited: tuple[CitedFact, ...]
    accident_outcome: AccidentOutcome
    outcome_reason: str
    outcome_cited: tuple[CitedFact, ...]
    injuries: tuple[InjuryRecord, ...]
    cause: Literal["cannot_determine"]
    cause_reason: str

    @model_validator(mode="after")
    def _decided_labels_are_cited(self) -> Self:
        if self.accident_type != "cannot_determine" and not self.type_cited:
            raise ValueError("A decided Accident Type must cite at least one observation (ADR 0006)")
        if self.accident_outcome != "cannot_determine" and not self.outcome_cited:
            raise ValueError("A decided Accident Outcome must cite at least one observation (ADR 0006)")
        if self.injuries and self.accident_outcome not in {"injury", "fatal"}:
            raise ValueError("Injured persons imply an Injury or Fatal outcome")
        return self


class CollisionSummary(_Frozen):
    records: tuple[CollisionRecord, ...]
    # Observations on a view that lists no Collision: shown for review, never silently dropped.
    unattached: tuple[CitedFact, ...]


def _grade(value: str) -> InjuryGrade:
    """Only the three official grades pass through; anything else (including unknown) is Cannot Determine."""
    if value == "serious":
        return "serious"
    if value == "moderate":
        return "moderate"
    if value == "minor":
        return "minor"
    return "cannot_determine"


def _ordered_collisions(candidates: Sequence[CitedFact]) -> list[CitedFact]:
    view_order: dict[str, int] = {}
    for candidate in candidates:
        view_order.setdefault(candidate.view_id, len(view_order))
    return sorted(candidates, key=lambda c: (view_order[c.view_id], c.timestamp_seconds))


def _type_of(observations: Sequence[CollisionObservation]) -> tuple[AccidentType, str, tuple[CitedFact, ...]]:
    forms = [o for o in observations if o.kind in _TYPE_BY_KIND]
    if not forms:
        return (
            "cannot_determine",
            "No cited observation shows how the contact happened; Cannot Determine.",
            (),
        )
    kinds = {_TYPE_BY_KIND[o.kind] for o in forms}
    cited = tuple(o.citation() for o in forms)
    if len(kinds) > 1:
        names = ", ".join(sorted(kinds))
        return (
            "cannot_determine",
            f"Several forms are cited on this collision ({names}); one official Accident Type is not chosen.",
            cited,
        )
    (only,) = kinds
    return only, "Derived from the cited observed form of contact.", cited


def _outcome_of(
    observations: Sequence[CollisionObservation],
) -> tuple[AccidentOutcome, str, tuple[CitedFact, ...], tuple[InjuryRecord, ...]]:
    dead = [o for o in observations if o.kind == "person_dead_visible"]
    hurt = [o for o in observations if o.kind == "person_injured_visible"]
    unharmed = [o for o in observations if o.kind == "no_injury_visible"]
    injuries = tuple(
        InjuryRecord(
            subject=o.subject,
            grade=_grade(o.injury_grade),
            cited=o.citation(),
        )
        for o in hurt
    )
    if dead:
        return "fatal", "A person is cited as visibly dead.", tuple(o.citation() for o in dead + hurt), injuries
    if hurt:
        return "injury", "A person is cited as visibly injured.", tuple(o.citation() for o in hurt), injuries
    driven = [o for o in observations if o.kind == "vehicle_driven_away"]
    contacts = [o for o in observations if o.kind in _TYPE_BY_KIND]
    involved = {o.subject for o in contacts}
    involved.update(
        o.other_party for o in contacts if o.kind in {"vehicle_contact_vehicle", "vehicle_struck_pedestrian"}
    )
    vehicles = {o.subject for o in contacts}
    vehicles.update(o.other_party for o in contacts if o.kind == "vehicle_contact_vehicle")
    if involved and involved <= {o.subject for o in unharmed} and vehicles <= {o.subject for o in driven}:
        return (
            "simple",
            "People are cited as visibly unharmed and no visible injury or death is cited. "
            "All involved vehicles are cited as visibly driven away after contact.",
            tuple(o.citation() for o in unharmed + driven),
            (),
        )
    return (
        "cannot_determine",
        "Injury/death or both unharmed people and a vehicle driven away are not established; impact force is never used. Cannot Determine.",
        (),
        (),
    )


def classify_collisions(facts: OverviewFacts) -> CollisionSummary:
    """List every Collision in order and derive its classification from cited observations only."""
    ordered = _ordered_collisions(facts.candidate_collisions)
    attached: list[list[CollisionObservation]] = [[] for _ in ordered]
    unattached: list[CitedFact] = []
    observations: Sequence[CollisionObservation] = getattr(facts, "collision_observations", ())
    for observation in sorted(observations, key=lambda o: (o.view_id, o.timestamp_seconds)):
        same_view = [i for i, c in enumerate(ordered) if c.view_id == observation.view_id]
        if not same_view:
            unattached.append(observation.citation())
            continue
        nearest = min(same_view, key=lambda i: (abs(ordered[i].timestamp_seconds - observation.timestamp_seconds), i))
        attached[nearest].append(observation)
    records = []
    for number, (collision, observations) in enumerate(zip(ordered, attached, strict=True), start=1):
        accident_type, type_reason, type_cited = _type_of(observations)
        outcome_observations = [
            o
            for o in observations
            if o.kind not in {"vehicle_driven_away", "no_injury_visible"}
            or o.timestamp_seconds >= collision.timestamp_seconds
        ]
        outcome, outcome_reason, outcome_cited, injuries = _outcome_of(outcome_observations)
        records.append(
            CollisionRecord(
                number=number,
                collision=collision,
                accident_type=accident_type,
                type_reason=type_reason,
                type_cited=type_cited,
                accident_outcome=outcome,
                outcome_reason=outcome_reason,
                outcome_cited=outcome_cited,
                injuries=injuries,
                cause="cannot_determine",
                cause_reason=CAUSE_REASON,
            )
        )
    return CollisionSummary(records=tuple(records), unattached=tuple(unattached))
