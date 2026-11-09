"""Criminal referrals, never fixed fines (Decree-Law 14/2024 Arts. 38–40).

Sources: UAE_ROAD_STRUCTURE_AND_PRIORITY.md D15/D16; COMPARABLE_SYSTEMS.md
85–87. Require one unambiguous contact on a common view clock. Multiple contacts
on a view cannot yet link injury/death to one event, so abstain there.
"""

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from incident_report.facts import CitedFact, OverviewFacts
from incident_report.fines import FinesTable
from incident_report.rules.adapter import situation_from_overview
from incident_report.rules.core import causal_conflict, evidence, pair_fields
from incident_report.rules.models import FaultStatus, PartyFault, PartyViolation
from incident_report.rules.situation import Observed


class CriminalReferral(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    party_description: str = Field(min_length=1)
    article: Literal["Art. 38", "Art. 39", "Art. 40"]
    reason: str = Field(min_length=1)
    cited_facts: tuple[CitedFact, ...] = Field(min_length=1)
    source_url: Literal["https://uaelegislation.gov.ae/en/legislations/2598"]
    aed: None = None  # default-ok: criminal referrals never have fixed penalties
    black_points: None = None  # default-ok: no administrative penalty
    impound_days: None = None  # default-ok: no administrative penalty

    @field_validator("cited_facts")
    @classmethod
    def _plain_citations(cls, values: tuple[CitedFact, ...]) -> tuple[CitedFact, ...]:
        # PartyFact is a CitedFact subtype; normalize so reload preserves equality.
        return tuple(
            CitedFact(
                description=c.description,
                view_id=c.view_id,
                timestamp_seconds=c.timestamp_seconds,
                confidence=c.confidence,
            )
            for c in values
        )


def evaluate_referrals(facts: OverviewFacts, faults: Sequence[PartyFault]) -> tuple[CriminalReferral, ...]:
    referrals = []
    situation = situation_from_overview(facts)
    for collision in facts.candidate_collisions:
        if sum(c.view_id == collision.view_id for c in facts.candidate_collisions) != 1:
            continue
        observations = [
            o
            for o in facts.collision_observations
            if o.view_id == collision.view_id and o.timestamp_seconds >= collision.timestamp_seconds
        ]
        contacts = [
            o
            for o in facts.collision_observations
            if o.view_id == collision.view_id
            and o.kind in {"vehicle_contact_vehicle", "vehicle_struck_pedestrian", "contact_other"}
            # Owner-approved review F4: same tolerance as entry._append_cited_facts_to_events.
            and abs(o.timestamp_seconds - collision.timestamp_seconds) < 0.01
        ]
        involved = {name for o in contacts for name in (o.subject, o.other_party)}
        injured = [o for o in observations if o.kind == "person_injured_visible" and o.subject in involved]
        for leaving in observations:
            stopped = any(o.kind == "vehicle_stopped_at_scene" and o.subject == leaving.subject for o in observations)
            if leaving.kind == "vehicle_left_scene" and leaving.subject in involved and injured and not stopped:
                referrals.append(
                    CriminalReferral(
                        source_url="https://uaelegislation.gov.ae/en/legislations/2598",
                        party_description=leaving.subject,
                        article="Art. 38",
                        reason="Vehicle involved in the injury collision is cited continuously leaving without stopping after contact.",
                        cited_facts=(collision, *(o.citation() for o in contacts + injured), leaving.citation()),
                    )
                )
        dead = [o for o in observations if o.kind == "person_dead_visible" and o.subject in involved]
        for fault in faults:
            if (
                fault.status == FaultStatus.AT_FAULT
                and fault.party_description in involved
                and dead
                and collision in fault.cited_facts
                and fault.rule_id is not None
                and all(
                    c.view_id == collision.view_id and c.timestamp_seconds <= collision.timestamp_seconds
                    for c in fault.cited_facts
                )
                and any(f.classification == "Finding" and collision in f.cited_facts for f in fault.findings)
            ):
                referrals.append(
                    CriminalReferral(
                        source_url="https://uaelegislation.gov.ae/en/legislations/2598",
                        party_description=fault.party_description,
                        article="Art. 40",
                        reason="Cited causal fault in this collision and a cited visible death of an involved party.",
                        cited_facts=tuple(
                            dict.fromkeys((*fault.cited_facts, *(o.citation() for o in contacts + dead)))
                        ),
                    )
                )
        if len(situation.parties) != 2:
            continue
        for party in situation.parties:
            timed_fields: list[Observed[Any]] = [situation.officer_directing_traffic, situation.conflict_zone_type]
            timed_fields.extend(p.movement for p in situation.parties)
            timed_fields.extend(
                p.approach_control_faced for p in situation.parties if p.party_type.value != "pedestrian"
            )
            timed_fields.extend(f for _, f in pair_fields(situation))
            timed_fields.extend((party.party_type, party.pedestrian_crossing_place))
            required = (party.party_type, party.pedestrian_crossing_place)
            struck = [
                o for o in contacts if o.kind == "vehicle_struck_pedestrian" and o.other_party == party.description
            ]
            if (
                party.party_type.value == "pedestrian"
                and party.movement.value == "crossing_on_foot"
                and party.pedestrian_crossing_place.value == "outside_any_crossing"
                and struck
                and situation.officer_directing_traffic.value == "no"
                and causal_conflict(situation, *required)
                and all(
                    any(
                        c.view_id == collision.view_id and c.timestamp_seconds <= collision.timestamp_seconds
                        for c in field.supporting_evidence
                    )
                    for field in timed_fields
                )
            ):
                referrals.append(
                    CriminalReferral(
                        source_url="https://uaelegislation.gov.ae/en/legislations/2598",
                        party_description=party.description,
                        article="Art. 39",
                        reason="Cited crossing outside a designated place led into the observed collision path.",
                        cited_facts=tuple(
                            dict.fromkeys((*evidence(situation, *required), *(o.citation() for o in struck)))
                        ),
                    )
                )
    return tuple(dict.fromkeys(referrals))


def replace_fines_with_referrals(
    violations: Sequence[PartyViolation],
    referrals: Sequence[CriminalReferral],
    faults: Sequence[PartyFault],
    *,
    fines_table: FinesTable | None = None,
) -> tuple[PartyViolation, ...]:
    """Decide replacements once in the rules stage; never rewrite a stored Report.

    Criminal rows require a Referral for that party and article. Catalogue keys map
    our article identifiers to sourced item numbers; no schedule number is guessed.
    Article 40 also replaces its corresponding causal administrative fault fine.
    """
    table = fines_table if fines_table is not None else FinesTable.default()
    catalogue = {row.key: row for row in table.referrals}
    criminal_rows = {(row.item_number, row.source_url) for row in table.referrals}
    replacements = set()
    for referral in referrals:
        row = catalogue["referral_art_" + referral.article.removeprefix("Art. ")]
        if row.source_url == referral.source_url:
            replacements.add((referral.party_description, row.item_number, row.source_url))
    causal_replacements = {
        (referral.party_description, fault.rule_id)
        for referral in referrals
        if referral.article == "Art. 40"
        for fault in faults
        if fault.party_description == referral.party_description
    }
    remaining = []
    for violation in violations:
        if (violation.item_number, violation.source_url) in criminal_rows:
            if (violation.party_description, violation.item_number, violation.source_url) not in replacements:
                remaining.append(violation)
        elif (violation.party_description, violation.rule_id) not in causal_replacements:
            remaining.append(violation)
    return tuple(remaining)
