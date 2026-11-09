"""Map structured observations into Situation without deriving legal conclusions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from incident_report.facts import SCENE_FIELDS, CitedFact, OverviewFacts, PartyFact, StructuredFact
from incident_report.rules.situation import Contradiction, Observed, PartySituation, Situation

type PartyKey = tuple[str, str, str]


@dataclass(frozen=True)
class PartyIdentityMap:
    """Existing adapter reconciliation, retaining keys separately from labels."""

    sources: dict[PartyKey, PartyFact]
    view_keys: dict[tuple[str, str], PartyKey]
    description_keys: dict[str, set[PartyKey]]

    def key(self, description: str, view_id: str) -> PartyKey:
        if description == "scene" or not description:
            return (description, "", "")
        if (view_id, description) in self.view_keys:
            return self.view_keys[(view_id, description)]
        keys = self.description_keys.get(description, set())
        return next(iter(keys)) if len(keys) == 1 else (description, view_id, "")


def _identity_key(party: PartyFact) -> str:
    """The agreement key, except that an overview-10 checklist code is only meaningful inside its own view.

    A party with a code but without both a colour and a body type cannot be matched to another view's party, so
    it keeps a view-local key instead of the description fallback (which would carry the code across views).
    """
    from incident_report.agreement import party_canonical_key

    if party.party_code is not None and not (party.color and party.body_type):
        return f"party:code:{party.view_id}:{party.party_code}"
    return party_canonical_key(party)


def party_identities(facts: OverviewFacts) -> PartyIdentityMap:
    """Share the adapter's identity decisions with consistency/fine readers."""
    # A key shared by distinct parties in one view cannot establish cross-view identity.
    names_by_view_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    for party in facts.parties:
        names_by_view_key[(party.view_id, _identity_key(party))].add(party.description)
    ambiguous = {key for (_, key), names in names_by_view_key.items() if len(names) > 1}
    sources: dict[tuple[str, str, str], PartyFact] = {}
    view_keys = {}
    description_keys: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for party in facts.parties:
        canonical = _identity_key(party)
        key = (canonical, party.view_id, party.description) if canonical in ambiguous else (canonical, "", "")
        sources.setdefault(key, party)
        view_keys[(party.view_id, party.description)] = key
        description_keys[party.description].add(key)

    return PartyIdentityMap(sources, view_keys, description_keys)


def situation_party_keys(situation: Situation) -> tuple[PartyKey | None, ...]:
    """Recover reconciled keys from Situation's retained party sources."""
    sources = [p.source for p in situation.parties if isinstance(p.source, PartyFact)]
    identities = party_identities(
        OverviewFacts(
            parties=sources, events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
        )
    )
    return tuple(
        identities.key(p.source.description, p.source.view_id) if isinstance(p.source, PartyFact) else None
        for p in situation.parties
    )


def situation_from_overview(facts: OverviewFacts) -> Situation:
    """Reconcile observations conservatively, retaining every source citation (ADR 0003)."""
    identities = party_identities(facts)
    sources = identities.sources

    groups: dict[tuple[tuple[str, str, str], str, tuple[str, str, str]], list[StructuredFact]] = defaultdict(list)
    for fact in facts.structured_facts:
        groups[
            (identities.key(fact.subject, fact.view_id), fact.field, identities.key(fact.other_party, fact.view_id))
        ].append(fact)
    scene: dict[str, Any] = {}
    parties: dict[tuple[str, str, str], dict[str, Any]] = {key: {} for key in sources}
    contradictions: list[Contradiction] = []
    for (subject, field, other), records in groups.items():
        # These markers say the view could not observe a reading; retain their citations.
        known = {r.value for r in records if r.value not in {"unknown", "not_visible", "not_in_view"}}
        value = next(iter(known)) if len(known) == 1 else "unknown"
        known_records = [r for r in records if r.value in known]
        if any(a.view_id != b.view_id and a.value != b.value for a in known_records for b in known_records):
            contradictions.append(
                Contradiction(
                    subject=sources[subject].description if subject in sources else subject[0],
                    field=field,
                    other_party=sources[other].description if other in sources else other[0],
                    readings=tuple(records),
                )
            )
        typed_value: str | int = int(value) if field == "posted_speed_limit_kmh" and value.isdigit() else value
        observed = Observed[str | int](
            value=typed_value,
            evidence=tuple(r.citation() for r in records),
            value_evidence=tuple(r.citation() for r in records if r.value == value),
        )
        if field in SCENE_FIELDS:
            scene[field] = observed.model_dump()
        elif field == "relative_position_of_other":
            parties[subject].setdefault(field, {})[sources[other].description] = observed.model_dump()
        else:
            parties[subject][field] = observed.model_dump()
    # overview-10 checklist answers (one row per party and view). Cited to the movement moment; unknown stays
    # unknown and needs no citation. A party merged across views keeps the first row.
    rows = {(r.view_id, r.display_name): r for r in facts.party_checklist}
    for key, party in sources.items():
        row = rows.get((party.view_id, party.description))
        if row is None:
            continue
        for field in ("road_class_on_approach", "road_class_basis", "roundabout_exit_position"):
            value = getattr(row, field)
            cited = CitedFact(
                description=f"{row.display_name}: {field} = {value}",
                view_id=row.view_id,
                timestamp_seconds=row.movement_seen_at_seconds,
                confidence=row.confidence,
            )
            parties[key][field] = Observed[str](
                value=value, evidence=(cited,) if value != "unknown" else (), value_evidence=None
            ).model_dump()
    # overview-2 has no P11/P12 extraction: Situation keeps their explicit unknown defaults.
    return Situation(
        parties=tuple(
            PartySituation(description=p.description, source=p, **parties[key]) for key, p in sources.items()
        ),
        collisions=tuple(facts.candidate_collisions),
        collision_observations=tuple(
            o.model_copy(
                update={
                    "subject": sources[identities.key(o.subject, o.view_id)].description
                    if identities.key(o.subject, o.view_id) in sources
                    else o.subject,
                    "other_party": sources[identities.key(o.other_party, o.view_id)].description
                    if identities.key(o.other_party, o.view_id) in sources
                    else o.other_party,
                }
            )
            for o in facts.collision_observations
        ),
        incident_assessments=tuple(facts.incident_assessments),
        contradictions=tuple(contradictions),
        **scene,
    )
