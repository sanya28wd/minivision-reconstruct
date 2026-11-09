"""Final report checks (spec 001 step 8). Preserve evidence; never repair a failing report."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from incident_report.facts import OverviewFacts, validate_citations
from incident_report.fines import FinesTable, Referral
from incident_report.rules.adapter import situation_from_overview
from incident_report.rules.decree_law_14_2024 import ALL_RULES, RULE_ART_6_MAIN_ROAD
from incident_report.rules.driver import DriverAssessment
from incident_report.rules.drowsiness import (
    DROWSINESS_ARTICLE,
    DROWSINESS_LEGAL_SOURCE,
    DROWSINESS_RULE_ID,
    DROWSINESS_SOURCE_URL,
)
from incident_report.rules.models import Finding, PartyFault, PartyViolation, Violation
from incident_report.rules.violations import RULE_ID_TO_FINE_KEY, map_violation

if TYPE_CHECKING:
    from incident_report.domain import Report


@dataclass(frozen=True)
class ProjectObservationRule:
    """Exact metadata for a non-legal observation finding, never a penalty."""

    legal_source: str
    article: str
    source_url: str


PROJECT_OBSERVATION_RULES: Mapping[str, ProjectObservationRule] = MappingProxyType(
    {
        DROWSINESS_RULE_ID: ProjectObservationRule(
            legal_source=DROWSINESS_LEGAL_SOURCE,
            article=DROWSINESS_ARTICLE,
            source_url=DROWSINESS_SOURCE_URL,
        ),
    }
)


_RAW_ID = re.compile(
    r"\b(?:vehicle|car|truck|van|bus|suv|actor|party|object|entity|participant)(?:[ _-]?\d+|[_-][a-z]| (?-i:[A-Z]))\b"
    r"|\b[vcp]\d+\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_INJURY = re.compile(r"\b(?:injur\w*|fatal\w*|dead|died|death|killed)\b", re.IGNORECASE)
_INJURY_ABSTENTION = re.compile(
    r"\s*(?:injury grade|injury status|injuries|injury)\s*"
    r"(?::\s*cannot determine|cannot be determined)"
    r"(?: from (?:this|the) (?:view|footage|video))?\s*",
    re.IGNORECASE,
)
_LEGAL_FIGURE = re.compile(
    r"\b(?:AED\s*[\d,]+|[\d,]+\s*(?:AED|dirhams?|black points?|impound days?)|"
    r"(?:article|art\.|item)\s*\d+)",
    re.IGNORECASE,
)


def _text(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Walk content fields, excluding citation keys and machine metadata."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            if key not in {"view_id", "rule_id", "source_url", "research_reference", "party_code"}:
                yield from _text(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for i, child in enumerate(value):
            yield from _text(child, f"{path}[{i}]")


def validate_report(report: Report) -> Report:
    """Run final checks before logging or rendering, retaining every offending value."""
    results: dict[str, tuple[str, ...]] = {}
    content = {
        "summary": report.summary,
        "facts": report.facts.model_dump() if report.facts else None,
        "dense_facts": [f.model_dump() for f in report.dense_facts],
        "parties": [p.model_dump() for p in report.parties],
        "violations": [v.model_dump() for v in report.violations],
        "driver_assessments": [a.model_dump() for a in report.driver_assessments],
    }
    # overview-10 display names ("P1 silver sedan") carry the checklist code on purpose. Only that leading code
    # of each built name is exempt; the model words after it (party_type_other, colour_other) and any other code
    # left in the text (an unknown "P7", a bare "P1") are still scanned.
    codes = sorted(
        {(row.display_name, row.party_code) for row in report.facts.party_checklist} if report.facts else (),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )

    def without_display_names(text: str) -> str:
        for name, code in codes:
            if name.startswith(code + " "):
                text = text.replace(name, name[len(code) :].lstrip())
        return text

    results["raw_ids"] = tuple(
        f"{path}: raw party ID in report text"
        for path, text in _text(content, "report")
        if _RAW_ID.search(without_display_names(text))
    )
    citation_errors: list[str] = []
    injury_errors: list[str] = []
    for i, facts in enumerate(([report.facts] if report.facts else []) + list(report.dense_facts)):
        checked = validate_citations(facts, report.views)
        citation_errors.extend(f"facts[{i}]: {error}" for error in checked.errors)
        injury_errors.extend(error for error in checked.errors if "collision_observation" in error)
    conclusions: list[PartyFault | Finding | PartyViolation | DriverAssessment] = [
        *report.parties,
        *report.violations,
        *report.driver_assessments,
        *(f for p in report.parties for f in p.findings),
    ]
    final_citations = OverviewFacts(
        parties=[],
        events=[c for result in conclusions for c in result.cited_facts],
        candidate_collisions=[],
        behaviour_observations=[],
        location_conditions=[],
    )
    citation_errors.extend(f"conclusions: {e}" for e in validate_citations(final_citations, report.views).errors)
    # Summary has no citation fields. Require the named person's typed visible observation,
    # with a valid citation, rather than allowing impact severity to imply injury.
    narratives = [
        (path, text)
        for path, text in _text(content, "report")
        if path == "report.summary" or path.endswith((".description", ".reason", ".agreement_detail"))
    ]
    for path, text in narratives:
        for sentence in re.split(r"[.!?\n]", text):
            if not _INJURY.search(sentence) or _INJURY_ABSTENTION.fullmatch(sentence):
                continue
            kind = "person_injured_visible"
            if re.search(r"\b(?:no injur\w*|uninjured)\b", sentence, re.IGNORECASE):
                kind = "no_injury_visible"
            elif re.search(r"\b(?:fatal\w*|dead|died|death|killed)\b", sentence, re.IGNORECASE):
                kind = "person_dead_visible"
            grades = set(re.findall(r"\b(?:serious|moderate|minor)\b", sentence.casefold()))
            supported = report.facts is not None and any(
                o.kind == kind
                and o.subject.casefold() in sentence.casefold()
                and (not grades or grades == {o.injury_grade})
                for o in report.facts.collision_observations
            )
            if not supported or any("collision_observation" in e for e in citation_errors):
                injury_errors.append(
                    f"{path}: injury or death language has no matching named, cited collision observation"
                )
    results["citations"] = tuple(citation_errors)
    results["injury_citations"] = tuple(injury_errors)
    table = FinesTable.default()
    party_types = (
        {p.description: p.party_type.value for p in situation_from_overview(report.facts).parties}
        if report.facts is not None
        else {}
    )
    legal_errors: list[str] = []
    fine_errors: list[str] = []
    referral_errors: list[str] = []
    # The AI extracts facts, never penalties or legal conclusions (ADR 0007). A prose
    # figure has no typed link to a schedule row, even if its number happens to match.
    for path, text in _text(content, "report"):
        # These fields are checked against their exact table/rule metadata below;
        # reasons have a separate check allowing only their own sourced article.
        if path.endswith(
            (".article", ".legal_source", ".name_en", ".name_ar", ".notes", ".reason", ".agreement_detail")
        ):
            continue
        if _LEGAL_FIGURE.search(text):
            legal_errors.append(f"{path}: legal figure outside a sourced rule or FinesTable row")
            if not report.violations:
                fine_errors.append(f"{path}: legal figure without a Violation")
    for i, violation in enumerate(report.violations):
        prefix = f"violation[{i}]"
        if not violation.cited_facts:
            fine_errors.append(f"{prefix}: fine without a cited Violation")
        key = RULE_ID_TO_FINE_KEY.get(violation.rule_id)
        referral = next((r for r in table.referrals if r.key == key), None)
        if isinstance(referral, Referral) and any(
            value is not None for value in (violation.aed, violation.black_points, violation.impound_days)
        ):
            referral_errors.append(f"{prefix}: Referral cannot carry AED, black points or impound days")
        try:
            expected = map_violation(
                Violation(
                    rule_id=violation.rule_id,
                    party_description=violation.party_description,
                    cited_facts=violation.cited_facts,
                ),
                fines_table=table,
                party_type=party_types.get(violation.party_description),
            )
        except (KeyError, ValueError):
            legal_errors.append(f"{prefix}: no sourced FinesTable mapping for this Violation")
            continue
        for field in (
            "item_number",
            "source_url",
            "aed",
            "black_points",
            "impound_days",
            "status",
            "name_en",
            "name_ar",
            "research_reference",
            "notes",
        ):
            if getattr(violation, field) != getattr(expected, field):
                legal_errors.append(f"{prefix}.{field}: does not match the mapped FinesTable row")
    # Duty articles are not penalty schedule items. Check their existing official
    # Rule metadata; schedule-based Contributing Factors must match the table row.
    rules = {r.rule_id: r for r in (*ALL_RULES, RULE_ART_6_MAIN_ROAD)}
    for i, result in enumerate(conclusions):
        for field in ("reason", "agreement_detail"):
            prose = getattr(result, field, None)
            if not prose:
                continue
            # A Cannot Determine party may quote its own cited Contributing Factors
            # without carrying a fault rule_id. Their metadata is validated below too.
            rule_ids = [getattr(result, "rule_id", ""), *(f.rule_id for f in getattr(result, "findings", ()))]
            for rule_id in rule_ids:
                rule = rules.get(rule_id)
                if rule:
                    prose = re.sub(r"(?<!\w)" + re.escape(rule.article) + r"(?!(?:[\w(/-]|\.\d))", "", prose)
            if _LEGAL_FIGURE.search(prose):
                legal_errors.append(f"conclusion[{i}].{field}: legal figure outside its sourced legal metadata")
    for i, party in enumerate(report.parties):
        rule_conclusions: list[tuple[str, PartyFault | Finding]] = [
            (f"party[{i}]", party),
            *((f"party[{i}].finding[{j}]", f) for j, f in enumerate(party.findings)),
        ]
        for path, conclusion in rule_conclusions:
            if conclusion.rule_id is None and conclusion.article is None and conclusion.legal_source is None:
                continue
            project_rule = PROJECT_OBSERVATION_RULES.get(conclusion.rule_id or "")
            if isinstance(conclusion, Finding) and project_rule is not None:
                metadata_matches = (conclusion.legal_source, conclusion.article, conclusion.source_url) == (
                    project_rule.legal_source,
                    project_rule.article,
                    project_rule.source_url,
                )
                has_penalty = any(
                    getattr(conclusion, field, None) is not None for field in ("aed", "black_points", "impound_days")
                )
                if not metadata_matches or has_penalty:
                    legal_errors.append(f"{path}: project observation metadata mismatch or forbidden penalty")
                # Citation validation above applies to these findings unchanged.
                continue
            rule = rules.get(conclusion.rule_id or "")
            if rule and (conclusion.article, conclusion.legal_source) == (rule.article, rule.legal_source):
                if hasattr(conclusion, "source_url") and conclusion.source_url != rule.source_url:
                    legal_errors.append(f"{path}: rule source URL does not match its official source")
                continue
            key = RULE_ID_TO_FINE_KEY.get(conclusion.rule_id or "")
            row = next((r for r in table.fines if r.key == key), None)
            if not (
                row
                and conclusion.article == f"Schedule item {row.item_number}"
                and conclusion.legal_source == "MR 178/2017 as amended"
                and getattr(conclusion, "source_url", None) == row.source_url
            ):
                legal_errors.append(f"{path}: article or item has no matching sourced rule or FinesTable row")
    results["fine_requires_violation"] = tuple(fine_errors)
    results["referral_without_fine"] = tuple(referral_errors)
    results["legal_sources"] = tuple(legal_errors)
    errors = tuple(f"{guard}: {reason}" for guard, reasons in results.items() for reason in reasons)
    return report.model_copy(
        update={
            "guardrail_results": results,
            "is_valid": report.is_valid and not errors,
            "validation_errors": tuple(dict.fromkeys((*report.validation_errors, *errors))),
        }
    )
