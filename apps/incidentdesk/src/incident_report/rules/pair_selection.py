"""Pick the colliding pair from typed contacts when the structural gate would stop (RPT-36 T2).

Conservative by design: exactly one distinct pair of exactly-named parties, one unambiguous
candidate collision in the same view, no contact by anyone outside the pair. Anything else
returns None and the engine keeps its structural Cannot Determine. No prose, no fuzzy names.
"""

from __future__ import annotations

from incident_report.facts import _PAIR_KINDS, OverviewFacts
from incident_report.rules.models import PairSelection
from incident_report.rules.situation import Situation

NOT_IN_PAIR_REASON = "Not in the collision pair; not assessed"


def select_pair(facts: OverviewFacts, situation: Situation) -> PairSelection | None:
    if len(situation.parties) <= 2 and len(situation.collisions) <= 1:
        return None  # the existing path; byte-identical
    if not situation.collisions:
        return None
    pair_obs = [o for o in facts.collision_observations if o.kind in _PAIR_KINDS]
    pairs = {frozenset((o.subject, o.other_party)) for o in pair_obs}
    if len(pairs) != 1:
        return None
    (pair,) = pairs
    if len(pair) != 2:
        return None
    fact_names = [p.description for p in facts.parties]
    situation_names = [p.description for p in situation.parties]
    if any(fact_names.count(n) != 1 or situation_names.count(n) != 1 for n in pair):
        return None
    # A contact by someone outside the pair would be a second collision: stay structural.
    if any(o.kind == "contact_other" and o.subject not in pair for o in facts.collision_observations):
        return None
    distances = [
        (abs(c.timestamp_seconds - o.timestamp_seconds), c)
        for c in situation.collisions
        for o in pair_obs
        if c.view_id == o.view_id
    ]
    if not distances:
        return None
    best = min(d for d, _ in distances)
    nearest = list(dict.fromkeys(c for d, c in distances if d == best))
    if len(nearest) != 1:
        return None
    first, second = (n for n in situation_names if n in pair)
    cited = tuple(dict.fromkeys(o.citation() for o in pair_obs))
    return PairSelection(
        subject=first,
        other_party=second,
        collision=nearest[0],
        observations=cited,
        reason=(
            f"One distinct pair of parties has a typed contact ({first}; {second}); evaluated as a two-party, "
            f"one-collision situation using the candidate collision nearest in time in the same view."
        ),
    )


def pair_situation(situation: Situation, selection: PairSelection) -> Situation:
    pair = {selection.subject, selection.other_party}
    return situation.model_copy(
        update={
            "parties": tuple(p for p in situation.parties if p.description in pair),
            "collisions": (selection.collision,),
        }
    )


__all__ = ["NOT_IN_PAIR_REASON", "pair_situation", "select_pair"]
