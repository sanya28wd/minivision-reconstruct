"""Agreement signal across multiple analysis runs (RPT-23, spec 001, ADR 0004, ADR 0006).

Compares typed facts (parties, events, candidate collisions, and per-party Fault Status from
rules.evaluate_fault) across N runs:
- N/N (e.g. 3/3) agree -> trusted
- Strict majority (e.g. 2/3) agree -> flagged (amber "Check" badge in Report)
- Split (no strict majority, e.g. 1/1/1 or tie) -> Cannot Determine
- Explicit unknown is an outcome; omitted items never count as agreement.
- N = 1 skips agreement entirely (status "not measured", never "trusted").

Timestamp tolerance for cited facts:
Citing timestamps within the same 1.0 second count as agreeing.
State this tolerance as a project assumption in the result, it's not a legal figure (ADR 0006).
"""

from __future__ import annotations

import enum
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from incident_report.facts import OverviewFacts, PartyFact
from incident_report.rules.consistency import ConsistencyIssue
from incident_report.rules.models import FaultStatus, Finding, PartyFault, PartyViolation

# ADR 0006: Thresholds and parameters cite the research or measurement they come from,
# or are labelled "project assumption, to be validated".
TIMESTAMP_TOLERANCE_SECONDS: float = 1.0  # project assumption, to be validated (ADR 0006)
MAX_AGREEMENT_RUNS: int = 10  # project assumption, to be validated (ADR 0006)
_ABSENT_SENTINEL = "__absent__"


def is_absent(value: Any) -> bool:
    """The two missing-item encodings used by the retained research votes."""
    return value is None or value == _ABSENT_SENTINEL


def extract_party_typed_attributes(
    party: PartyFact | Any,
) -> tuple[str | None, str | None, str | None]:
    """Extract normalized (color, make, body_type) strictly from typed attributes.

    No text parsing or word lists (RPT-23 R2).
    """
    if isinstance(party, PartyFact):
        color = " ".join(party.color.split()).casefold() if party.color else None
        make_model = " ".join(party.make_model.split()).casefold() if party.make_model else None
        body_type = " ".join(party.body_type.split()).casefold() if party.body_type else None
        return (color, make_model, body_type)
    return (None, None, None)


def party_canonical_key(party: PartyFact | Any) -> str:
    """Generate a canonical key for a party.

    If typed attributes (color, make/model, body_type) are present,
    returns party:<color>:<make>:<body_type>.
    If missing, falls back to the exact description, normalised only by
    casefold() and collapsing whitespace: party:desc:<normalised> (RPT-23 R2 round 2).
    """
    if isinstance(party, str) and party.startswith("party:"):
        return party

    c, m, b = extract_party_typed_attributes(party)
    if c or m or b:
        return f"party:{c or 'unknown'}:{m or 'unknown'}:{b or 'unknown'}"

    # Fallback to exact description, normalised by casefold() and collapsing whitespace
    if isinstance(party, PartyFact):
        desc = party.description
    elif isinstance(party, str):
        desc = party
    elif hasattr(party, "description"):
        desc = party.description
    elif hasattr(party, "party_description"):
        desc = party.party_description
    else:
        desc = str(party)

    normalised = " ".join(desc.split()).casefold() if desc else ""
    if normalised:
        return f"party:desc:{normalised}"
    return f"party:unmatched:{id(party)}"


class AgreementStatus(enum.StrEnum):
    """Agreement outcome per field and overall."""

    TRUSTED = "trusted"
    FLAGGED = "flagged"
    CANNOT_DETERMINE = "cannot_determine"
    NOT_MEASURED = "not_measured"


@dataclass(frozen=True)
class AgreementSettings:
    """Configuration for multi-run analysis agreement."""

    n_runs: int = 1
    # Timestamp tolerance for fact matching across runs: project assumption, to be validated (ADR 0006)
    timestamp_tolerance_seconds: float = TIMESTAMP_TOLERANCE_SECONDS
    max_runs: int = MAX_AGREEMENT_RUNS  # project assumption, to be validated (ADR 0006)

    def __post_init__(self) -> None:
        if self.n_runs < 1:
            raise ValueError("n_runs must be at least 1")
        if self.n_runs > self.max_runs:
            raise ValueError(f"n_runs cannot exceed {self.max_runs} (project assumption, ADR 0006)")
        if self.timestamp_tolerance_seconds < 0:
            raise ValueError("timestamp_tolerance_seconds must be nonnegative")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgreementSettings:
        from incident_report.config import ConfigError

        env = os.environ if env is None else env
        raw_n = env.get("INCIDENT_AGREEMENT_RUNS")
        if raw_n is not None and raw_n.strip():
            try:
                n_runs = int(raw_n.strip())
            except ValueError:
                raise ConfigError("INCIDENT_AGREEMENT_RUNS must be a positive integer.") from None
            if n_runs < 1 or n_runs > MAX_AGREEMENT_RUNS:
                raise ConfigError(
                    f"INCIDENT_AGREEMENT_RUNS must be between 1 and {MAX_AGREEMENT_RUNS} (project assumption, ADR 0006)."
                )
        else:
            n_runs = 1
        raw_tol = env.get("INCIDENT_AGREEMENT_TOLERANCE_SECONDS")
        if raw_tol is not None and raw_tol.strip():
            try:
                tol = float(raw_tol.strip())
            except ValueError:
                raise ConfigError("INCIDENT_AGREEMENT_TOLERANCE_SECONDS must be a number.") from None
            if tol < 0:
                raise ConfigError("INCIDENT_AGREEMENT_TOLERANCE_SECONDS must be nonnegative.")
        else:
            tol = TIMESTAMP_TOLERANCE_SECONDS
        return cls(n_runs=n_runs, timestamp_tolerance_seconds=tol)


class FieldAgreement(BaseModel):
    """Agreement evaluation for a single typed fact or finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str
    status: AgreementStatus
    agreed_value: Any | None = None
    runs_total: int
    runs_agreeing: int
    details: str | None = None
    values: list[Any] = Field(default_factory=list)  # default-ok: observed values across runs


class AgreementResult(BaseModel):
    """Complete agreement outcome across all evaluated fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_runs: int
    status: AgreementStatus
    fields: dict[str, FieldAgreement]
    total_latency_ms: float | None = None
    model_call_count: int = 0


def evaluate_majority(
    outcomes: Sequence[Any],
    *,
    field_name: str,
    n_runs: int,
) -> FieldAgreement:
    """Evaluate agreement across N runs for a single field.

    - N = 1 -> NOT_MEASURED (never trusted)
    - N/N agree -> TRUSTED
    - Strict majority (> N / 2) -> FLAGGED
    - No majority (tie or split) -> CANNOT_DETERMINE
    Missing items never vote; explicit unknown remains a distinct outcome.
    """
    if n_runs == 1:
        val = outcomes[0] if outcomes and outcomes[0] != _ABSENT_SENTINEL else None
        return FieldAgreement(
            field_name=field_name,
            status=AgreementStatus.NOT_MEASURED,
            agreed_value=val,
            runs_total=1,
            runs_agreeing=int(bool(outcomes) and outcomes[0] != _ABSENT_SENTINEL),
            details="Single run; agreement not measured.",
            values=list(outcomes),
        )

    # Pad with absent sentinel if fewer outcomes than n_runs
    full_outcomes = list(outcomes)
    while len(full_outcomes) < n_runs:
        full_outcomes.append(_ABSENT_SENTINEL)

    present = [value for value in full_outcomes if value != _ABSENT_SENTINEL]
    counts = Counter(present)
    top_val, top_count = counts.most_common(1)[0] if counts else (None, 0)

    if top_count == n_runs:
        status = AgreementStatus.TRUSTED
        agreed_value = top_val
        details = f"{n_runs} of {n_runs} runs agree unanimously."
    elif top_count > n_runs / 2:
        status = AgreementStatus.FLAGGED
        agreed_value = top_val
        details = f"{top_count} of {n_runs} runs agree (majority)."
    else:
        status = AgreementStatus.CANNOT_DETERMINE
        if "fault" in field_name.lower():
            if full_outcomes and isinstance(full_outcomes[0], tuple):
                agreed_value = (FaultStatus.CANNOT_DETERMINE.value, None)
            else:
                agreed_value = FaultStatus.CANNOT_DETERMINE.value
        else:
            agreed_value = None
        if len(present) <= n_runs / 2:
            item = "party" if field_name.startswith("fault") else "finding"
            details = (
                f"{top_count} of {n_runs} analyses found this {item} with the same value; "
                f"seen in only {len(present)} of {n_runs} analyses; Cannot Determine."
            )
        else:
            details = f"Runs split with no majority ({top_count} of {n_runs}); Cannot Determine."

    return FieldAgreement(
        field_name=field_name,
        status=status,
        agreed_value=agreed_value,
        runs_total=n_runs,
        runs_agreeing=top_count,
        details=details,
        values=full_outcomes,
    )


def compare_parties(
    facts_list: Sequence[OverviewFacts],
    tolerance: float = TIMESTAMP_TOLERANCE_SECONDS,
) -> dict[str, FieldAgreement]:
    """Compare parties identified across runs.

    Parties match by typed attributes (color, make/model, body_type).
    """
    n_runs = len(facts_list)
    if n_runs == 0:
        return {}

    party_keys: dict[str, list[int]] = {}
    party_display_names: dict[str, str] = {}
    party_instances: dict[str, dict[int, PartyFact]] = {}

    for run_idx, facts in enumerate(facts_list):
        for p in facts.parties:
            key = party_canonical_key(p)
            if key not in party_keys:
                party_keys[key] = []
                party_display_names[key] = p.description
                party_instances[key] = {}
            if run_idx not in party_keys[key]:
                party_keys[key].append(run_idx)
                party_instances[key][run_idx] = p

    results: dict[str, FieldAgreement] = {}
    for key, matching_runs in party_keys.items():
        k = len(matching_runs)
        field_name = key
        display_name = party_display_names[key]
        if n_runs == 1:
            status = AgreementStatus.NOT_MEASURED
            details = "Single run; agreement not measured."
        elif k == n_runs:
            status = AgreementStatus.TRUSTED
            details = f"{n_runs} of {n_runs} runs agree unanimously on party."
        elif k > n_runs / 2:
            status = AgreementStatus.FLAGGED
            details = f"{k} of {n_runs} runs agree on party (majority)."
        else:
            status = AgreementStatus.CANNOT_DETERMINE
            details = f"Runs split on party presence ({k} of {n_runs}); Cannot Determine."

        agreement = FieldAgreement(
            field_name=field_name,
            status=status,
            agreed_value=display_name if k > n_runs / 2 or n_runs == 1 else None,
            runs_total=n_runs,
            runs_agreeing=k,
            details=details,
            values=[
                party_instances[key][r].description if r in matching_runs else _ABSENT_SENTINEL for r in range(n_runs)
            ],
        )
        results[field_name] = agreement
    return results


def compare_events(
    facts_list: Sequence[OverviewFacts],
    tolerance: float = TIMESTAMP_TOLERANCE_SECONDS,
) -> dict[str, FieldAgreement]:
    """Compare timeline events across runs.

    Events match by timestamp within tolerance (no prose text matching).
    """
    n_runs = len(facts_list)
    if n_runs == 0:
        return {}

    clusters: list[dict[str, Any]] = []
    for run_idx, facts in enumerate(facts_list):
        for ev in facts.events:
            matched = None
            for cl in clusters:
                if run_idx not in cl["runs"] and abs(cl["timestamp"] - ev.timestamp_seconds) <= tolerance:
                    matched = cl
                    break
            if matched is not None:
                matched["runs"][run_idx] = ev.description
            else:
                clusters.append(
                    {
                        "timestamp": ev.timestamp_seconds,
                        "display": ev.description,
                        "runs": {run_idx: ev.description},
                    }
                )

    results: dict[str, FieldAgreement] = {}
    for cl in clusters:
        k = len(cl["runs"])
        field_name = f"event:@{cl['timestamp']:.1f}s"
        if n_runs == 1:
            status = AgreementStatus.NOT_MEASURED
            details = "Single run; agreement not measured."
        elif k == n_runs:
            status = AgreementStatus.TRUSTED
            details = f"{n_runs} of {n_runs} runs agree unanimously on event."
        elif k > n_runs / 2:
            status = AgreementStatus.FLAGGED
            details = f"{k} of {n_runs} runs agree on event (majority)."
        else:
            status = AgreementStatus.CANNOT_DETERMINE
            details = f"Runs split on event ({k} of {n_runs}); Cannot Determine."

        results[field_name] = FieldAgreement(
            field_name=field_name,
            status=status,
            agreed_value=cl["display"] if k > n_runs / 2 or n_runs == 1 else None,
            runs_total=n_runs,
            runs_agreeing=k,
            details=details,
            values=[cl["runs"].get(r, _ABSENT_SENTINEL) for r in range(n_runs)],
        )
    return results


def compare_collisions(
    facts_list: Sequence[OverviewFacts],
    tolerance: float = TIMESTAMP_TOLERANCE_SECONDS,
) -> dict[str, FieldAgreement]:
    """Compare candidate collision moments across runs.

    Collisions match if their timestamps are within tolerance.
    """
    n_runs = len(facts_list)
    if n_runs == 0:
        return {}

    clusters: list[dict[str, Any]] = []
    for run_idx, facts in enumerate(facts_list):
        for coll in facts.candidate_collisions:
            matched = None
            for cl in clusters:
                if run_idx not in cl["runs"] and abs(cl["timestamp"] - coll.timestamp_seconds) <= tolerance:
                    matched = cl
                    break
            if matched is not None:
                matched["runs"][run_idx] = coll.description
            else:
                clusters.append(
                    {
                        "timestamp": coll.timestamp_seconds,
                        "display": coll.description,
                        "runs": {run_idx: coll.description},
                    }
                )

    results: dict[str, FieldAgreement] = {}
    for cl in clusters:
        k = len(cl["runs"])
        field_name = f"collision:@{cl['timestamp']:.1f}s"
        if n_runs == 1:
            status = AgreementStatus.NOT_MEASURED
            details = "Single run; agreement not measured."
        elif k == n_runs:
            status = AgreementStatus.TRUSTED
            details = f"{n_runs} of {n_runs} runs agree unanimously on collision moment."
        elif k > n_runs / 2:
            status = AgreementStatus.FLAGGED
            details = f"{k} of {n_runs} runs agree on collision moment (majority)."
        else:
            status = AgreementStatus.CANNOT_DETERMINE
            details = f"Runs split on collision moment ({k} of {n_runs}); Cannot Determine."

        results[field_name] = FieldAgreement(
            field_name=field_name,
            status=status,
            agreed_value=cl["display"] if k > n_runs / 2 or n_runs == 1 else None,
            runs_total=n_runs,
            runs_agreeing=k,
            details=details,
            values=[cl["runs"].get(r, _ABSENT_SENTINEL) for r in range(n_runs)],
        )
    return results


def compare_fault(
    faults_list: Sequence[Sequence[PartyFault]],
    facts_list: Sequence[OverviewFacts],
) -> dict[str, FieldAgreement]:
    """Compare per-party Fault Status from rules.evaluate_fault across runs.

    Each run provides tuple[PartyFault, ...].
    Parties are matched by typed attributes.
    Votes are cast on (status, rule_id).
    """
    n_runs = len(faults_list)
    if n_runs == 0:
        return {}

    # Find all canonical party keys across all runs and map to display names
    party_keys: set[str] = set()
    display_names: dict[str, str] = {}
    for run_idx, run_faults in enumerate(faults_list):
        run_facts = facts_list[run_idx] if run_idx < len(facts_list) else None
        for pf in run_faults:
            p_fact = (
                next((p for p in run_facts.parties if p.description == pf.party_description), None)
                if run_facts
                else None
            )
            key = party_canonical_key(p_fact) if p_fact else party_canonical_key(pf.party_description)
            party_keys.add(key)
            display_names.setdefault(key, pf.party_description)
    for facts in facts_list:
        for p in facts.parties:
            key = party_canonical_key(p)
            party_keys.add(key)
            display_names.setdefault(key, p.description)

    results: dict[str, FieldAgreement] = {}
    for p_key in sorted(party_keys):
        outcomes: list[Any] = []
        for run_idx, run_faults in enumerate(faults_list):
            run_facts = facts_list[run_idx] if run_idx < len(facts_list) else None
            matching = [
                pf
                for pf in run_faults
                if (
                    party_canonical_key(
                        next((p for p in run_facts.parties if p.description == pf.party_description), None)
                        if run_facts
                        else pf.party_description
                    )
                    == p_key
                )
            ]
            if matching:
                pf = matching[0]
                outcomes.append((pf.status.value, pf.rule_id))
            else:
                outcomes.append(_ABSENT_SENTINEL)

        if all(is_absent(value) for value in outcomes):
            continue
        field_name = f"fault_status:{p_key}"
        agreement = evaluate_majority(outcomes, field_name=field_name, n_runs=n_runs)
        results[field_name] = agreement
    return results


def agree_no_collision_violations(
    facts_list: Sequence[OverviewFacts],
    faults_list: Sequence[Sequence[PartyFault]],
    *,
    consistency_issues: Sequence[tuple[ConsistencyIssue, ...]] = (),
) -> tuple[tuple[PartyViolation, ...], dict[str, FieldAgreement]]:
    """Vote on standalone typed breaches independently of collision Fault Status (RPT-26/30).

    Invalid overview runs have no fault outputs and abstain, just as for fault.
    Use the existing majority policy; retain citations from an agreeing run.
    """
    from incident_report.rules.adapter import situation_from_overview
    from incident_report.rules.models import Violation
    from incident_report.rules.violations import (
        RULE_ID_TO_FINE_KEY,
        NoFixedItem,
        fine_key_for_party,
        map_violation,
        standalone_violation_readings,
    )

    scenes = [
        situation_from_overview(facts) if i < len(faults_list) and faults_list[i] else None
        for i, facts in enumerate(facts_list)
    ]
    observations = [
        standalone_violation_readings(
            facts_list[i], scene, consistency_issues=consistency_issues[i] if i < len(consistency_issues) else ()
        )
        if scene is not None
        else ()
        for i, scene in enumerate(scenes)
    ]

    def _party_key(i: int, description: str) -> str:
        # Typed attributes from that run's PartyFact win; else the exact-description fallback (FIX-RPT23).
        party = (
            next((p for p in facts_list[i].parties if p.description == description), None)
            if i < len(facts_list)
            else None
        )
        return party_canonical_key(party if party is not None else description)

    readings = [
        {(_party_key(i, r.party_description), r.rule_id): r.is_breach for r in run}
        for i, run in enumerate(observations)
    ]
    keys = sorted({key for reading in readings for key in reading})
    fields = {}
    agreed = []
    for key in keys:
        name = f"violation:{key[0]}:{key[1]}"
        vote = evaluate_majority(
            [reading.get(key, _ABSENT_SENTINEL) for reading in readings], field_name=name, n_runs=len(observations)
        )
        fields[name] = vote
        if vote.agreed_value is True and vote.status != AgreementStatus.CANNOT_DETERMINE:
            source_index, reading = next(
                (i, r)
                for i, run in enumerate(observations)
                for r in run
                if _party_key(i, r.party_description) == key[0] and r.rule_id == key[1] and r.is_breach is True
            )
            scene = scenes[source_index]
            if scene is None:
                raise ValueError("An agreed violation must retain its source situation")
            party_type = next(p.party_type.value for p in scene.parties if p.description == reading.party_description)
            if isinstance(fine_key_for_party(key[1], party_type, RULE_ID_TO_FINE_KEY), NoFixedItem):
                continue
            source = map_violation(
                Violation(rule_id=key[1], party_description=reading.party_description, cited_facts=reading.cited_facts),
                party_type=party_type,
            )
            agreed.append(
                source.model_copy(update={"agreement_status": vote.status.value, "agreement_detail": vote.details})
            )
    return tuple(agreed), fields


def agree_contributing_factors(
    faults_list: Sequence[Sequence[PartyFault]],
) -> tuple[tuple[Finding, ...], dict[str, FieldAgreement]]:
    """Retain cited contributing Findings, visibly marking minority/absent support.

    Absence of a proved breach is an abstention, not proof of safe behavior. These
    votes annotate Findings only; they never promote or demote Fault Status.
    """
    indexed = [
        {
            (party_canonical_key(f.party_description), f.rule_id): f
            for p in run
            for f in p.findings
            if f.classification == "Contributing Factor"
        }
        for run in faults_list
    ]
    keys = sorted({key for run in indexed for key in run})
    fields = {}
    findings = []
    for key in keys:
        name = f"contributing_factor:{key[0]}:{key[1]}"
        vote = evaluate_majority(
            [True if key in run else _ABSENT_SENTINEL for run in indexed], field_name=name, n_runs=len(indexed)
        )
        fields[name] = vote
        status = vote.status if vote.agreed_value is True else AgreementStatus.CANNOT_DETERMINE
        support = sum(key in run for run in indexed)
        detail = (
            "Observed in this analysis."
            if len(indexed) == 1
            else f"{support} of {len(indexed)} runs support this contributing finding; missing findings abstain."
        )
        source = next(run[key] for run in indexed if key in run)
        findings.append(source.model_copy(update={"agreement_status": status.value, "agreement_detail": detail}))
    return tuple(findings), fields


def apply_contributing_agreement(
    final_faults: Sequence[PartyFault], overview_faults: Sequence[Sequence[PartyFault]]
) -> tuple[PartyFault, ...]:
    """Annotate contributing Findings without dropping dense-only cited breaches."""
    agreed, _ = agree_contributing_factors(overview_faults)
    result = []
    for party in final_faults:
        candidates = {
            f.rule_id: f
            for f in agreed
            if party_canonical_key(f.party_description) == party_canonical_key(party.party_description)
        }
        findings = []
        for finding in party.findings:
            if finding.classification != "Contributing Factor":
                findings.append(finding)
            elif finding.rule_id in candidates:
                annotated = candidates.pop(finding.rule_id)
                # RPT-32 groups multiple cues under one party/rule. Consolidation
                # can change that group; overview agreement cannot validate new cues.
                from incident_report.rules.drowsiness import DROWSINESS_RULE_ID

                if finding.rule_id == DROWSINESS_RULE_ID and finding.reason != annotated.reason:
                    annotated = finding.model_copy(
                        update={
                            "agreement_status": "not_measured",
                            "agreement_detail": "Drowsiness cues changed after overview consolidation; repeated agreement not measured.",
                        }
                    )
                findings.append(
                    annotated.model_copy(
                        update={"cited_facts": tuple(dict.fromkeys((*finding.cited_facts, *annotated.cited_facts)))}
                    )
                )
            else:
                findings.append(
                    finding.model_copy(
                        update={
                            "agreement_status": "not_measured",
                            "agreement_detail": "Established by the close-up check.",
                        }
                    )
                )
        findings.extend(candidates.values())
        result.append(party.model_copy(update={"findings": tuple(findings)}))
    return tuple(result)


def compute_agreement(
    facts_list: Sequence[OverviewFacts],
    faults_list: Sequence[Sequence[PartyFault]],
    settings: AgreementSettings,
    *,
    latency_ms: float | None = None,
    call_count: int = 0,
    consistency_issues: Sequence[tuple[ConsistencyIssue, ...]] = (),
) -> AgreementResult:
    """Aggregate agreement across all typed facts and fault decisions."""
    n_runs = len(facts_list)
    if n_runs <= 1 or settings.n_runs <= 1:
        return AgreementResult(
            n_runs=max(n_runs, 1),
            status=AgreementStatus.NOT_MEASURED,
            fields={},
            total_latency_ms=latency_ms,
            model_call_count=call_count,
        )

    all_fields: dict[str, FieldAgreement] = {}

    tol = settings.timestamp_tolerance_seconds
    all_fields.update(compare_parties(facts_list, tolerance=tol))
    all_fields.update(compare_events(facts_list, tolerance=tol))
    all_fields.update(compare_collisions(facts_list, tolerance=tol))
    all_fields.update(compare_fault(faults_list, facts_list))
    _, violation_fields = agree_no_collision_violations(facts_list, faults_list, consistency_issues=consistency_issues)
    all_fields.update(violation_fields)
    _, contributing_fields = agree_contributing_factors(faults_list)
    all_fields.update(contributing_fields)

    # Deduplicate alias vs canonical: only check unique FieldAgreement objects
    unique_fault_agreements = list({id(f): f for k, f in all_fields.items() if k.startswith("fault_status:")}.values())
    unique_other_agreements = list(
        {id(f): f for k, f in all_fields.items() if not k.startswith("fault_status:")}.values()
    )

    if not all_fields:
        overall = AgreementStatus.NOT_MEASURED
    elif any(f.status == AgreementStatus.CANNOT_DETERMINE for f in unique_fault_agreements):
        overall = AgreementStatus.CANNOT_DETERMINE
    elif any(f.status == AgreementStatus.FLAGGED for f in unique_fault_agreements):
        overall = AgreementStatus.FLAGGED
    elif any(f.status in (AgreementStatus.FLAGGED, AgreementStatus.CANNOT_DETERMINE) for f in unique_other_agreements):
        overall = AgreementStatus.FLAGGED
    elif all(f.status == AgreementStatus.TRUSTED for f in (unique_fault_agreements + unique_other_agreements)):
        overall = AgreementStatus.TRUSTED
    else:
        overall = AgreementStatus.NOT_MEASURED

    return AgreementResult(
        n_runs=n_runs,
        status=overall,
        fields=all_fields,
        total_latency_ms=latency_ms,
        model_call_count=call_count,
    )
