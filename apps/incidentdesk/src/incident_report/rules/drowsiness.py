"""RPT-32 observation assessments; no legal duty, fault decision or fine mapping."""

from collections import defaultdict

from pydantic import BaseModel, ConfigDict

from incident_report.facts import CitedFact, OverviewFacts
from incident_report.rules.models import Finding

# Source: docs/research/COMPARABLE_SYSTEMS.md section 1, "Thresholds" (explicitly
# secondhand/configurable; no primary numeric citation supplied there). The RPT-32
# prompt authorizes these only as a project assumption, to be validated.
MICROSLEEP_SECONDS = 1.5
SEVERE_SECONDS = 3.0
DROWSINESS_RULE_ID = "driver_drowsiness"
DROWSINESS_LEGAL_SOURCE = "Project observation rule; not a legal duty or offence"
DROWSINESS_ARTICLE = "RPT-32 (no fine)"
DROWSINESS_SOURCE_URL = "docs/research/COMPARABLE_SYSTEMS.md#1-fleet-video-telematics-and-driver-monitoring"
ASSUMPTION = "project assumption, to be validated; COMPARABLE_SYSTEMS.md section 1 (secondhand thresholds)"


class DrowsinessAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    party_description: str
    label: str
    duration_seconds: float | None
    causal_role: str | None
    reason: str
    cited_facts: tuple[CitedFact, ...]


def assess_drowsiness(facts: OverviewFacts) -> tuple[DrowsinessAssessment, ...]:
    results = []
    for observation in facts.drowsiness_observations:
        duration = None
        label = "Cannot Determine"
        if observation.kind == "eye_closure":
            if observation.start_seconds is not None and observation.end_seconds is not None:
                duration = observation.end_seconds - observation.start_seconds
                label = "Eye closure below microsleep threshold"
                if duration > SEVERE_SECONDS:
                    label = "Severe eye closure"
                elif duration > MICROSLEEP_SECONDS:
                    label = "Microsleep"
        elif observation.kind != "lane_drift":
            label = observation.kind.replace("_", " ").capitalize()
        cited = observation.citations()
        causal_role: str | None = (
            None
            if label == "Eye closure below microsleep threshold"
            else "Cannot Determine"
            if label == "Cannot Determine"
            else "Contributing Factor"
        )
        # RPT-32 timing-link policy is a project assumption, to be validated; it
        # describes a possible physical cause, never a legal fault determination.
        # No comparison of clocks across views and no generic candidate-only cause.
        contacts = [
            c
            for c in facts.collision_observations
            if c.subject == observation.subject
            and c.view_id == observation.view_id
            and c.kind
            in {"vehicle_contact_vehicle", "vehicle_struck_pedestrian", "vehicle_overturned", "contact_other"}
            and observation.start_seconds is not None
            and observation.end_seconds is not None
            and observation.start_seconds < c.timestamp_seconds < observation.end_seconds
            and any(
                k.view_id == c.view_id and k.timestamp_seconds == c.timestamp_seconds
                for k in facts.candidate_collisions
            )
        ]
        if label in {"Microsleep", "Severe eye closure"} and contacts:
            causal_role = "Cause"
            cited = (
                *cited,
                *(c.citation() for c in contacts),
                *(
                    k
                    for k in facts.candidate_collisions
                    if any(k.view_id == c.view_id and k.timestamp_seconds == c.timestamp_seconds for c in contacts)
                ),
            )
        reason = (
            f"Drowsiness cue: {observation.kind}; {label}; "
            f"eye-closure duration: {f'{duration:g}s' if duration is not None else 'Cannot Determine'}. "
            f"Microsleep >{MICROSLEEP_SECONDS:g}s, severe >{SEVERE_SECONDS:g}s ({ASSUMPTION}). "
            + (
                "Possible cause: continuous eye closure spans a cited same-view contact "
                "(RPT-32 timing-link project assumption, to be validated); legal fault unchanged. "
                if causal_role == "Cause"
                else f"{causal_role}; causal role Cannot Determine. "
            )
            + "Lane drift alone does not establish fatigue. "
            "No drowsiness fine."
        )
        results.append(
            DrowsinessAssessment(
                party_description=observation.subject,
                label=label,
                duration_seconds=duration,
                causal_role=causal_role,
                reason=reason,
                cited_facts=cited,
            )
        )
    return tuple(results)


def drowsiness_findings(facts: OverviewFacts) -> tuple[Finding, ...]:
    # Agreement groups by party + rule id. Aggregate every supported cue and its
    # citations before that boundary, so a later yawn cannot overwrite eye closure.
    grouped: dict[str, list[DrowsinessAssessment]] = defaultdict(list)
    for assessment in assess_drowsiness(facts):
        if assessment.causal_role in {"Contributing Factor", "Cause"}:
            grouped[assessment.party_description].append(assessment)
    return tuple(
        Finding(
            party_description=subject,
            rule_id=DROWSINESS_RULE_ID,
            legal_source=DROWSINESS_LEGAL_SOURCE,
            article=DROWSINESS_ARTICLE,
            source_url=DROWSINESS_SOURCE_URL,
            reason=" ".join(a.reason for a in assessments),
            classification="Finding" if any(a.causal_role == "Cause" for a in assessments) else "Contributing Factor",
            cited_facts=tuple(dict.fromkeys(c for a in assessments for c in a.cited_facts)),
        )
        for subject, assessments in grouped.items()
    )
