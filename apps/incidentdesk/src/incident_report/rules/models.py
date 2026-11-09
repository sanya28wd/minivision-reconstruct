"""Domain models for the fault rule engine (RPT-15, spec 001 Seam 3, ADR 0004, ADR 0006).

Terms and statuses strictly follow GLOSSARY.md:
- Fault Status: At Fault (pink), Not at Fault (green), White (white), Cannot Determine (grey; report-owned).
- No liability percentages or invented scales.
- Every PartyFault links to the cited facts and legal rule that produced it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from incident_report.facts import CitedFact, is_raw_id


class FaultStatus(enum.StrEnum):
    """Police form states plus report-owned abstention (GLOSSARY.md, ADR 0004)."""

    AT_FAULT = "At Fault"
    NOT_AT_FAULT = "Not at Fault"
    WHITE = "White"
    CANNOT_DETERMINE = "Cannot Determine"

    @property
    def display_label(self) -> str:
        """The evidence abstention label belongs to this report, not the police scheme."""
        if self == FaultStatus.CANNOT_DETERMINE:
            return "Not determined by this report"
        return self.value


FAULT_STATUS_COLORS: dict[FaultStatus, str] = {
    FaultStatus.AT_FAULT: "pink",
    FaultStatus.NOT_AT_FAULT: "green",
    FaultStatus.WHITE: "white",
    FaultStatus.CANNOT_DETERMINE: "grey",
}


@dataclass(frozen=True)
class Rule:
    """Representation of an official legal rule as data."""

    rule_id: str
    legal_source: str
    article: str
    title: str
    description: str
    source_url: str

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.rule_id, self.legal_source, self.article, self.source_url)):
            raise ValueError("A rule requires a legal source, article and source URL (ADR 0006)")


class Finding(BaseModel):
    """A cited duty breach; a Finding need not be a sole-fault determination."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    party_description: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    legal_source: str = Field(min_length=1)
    article: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    classification: Literal["Finding", "Contributing Factor"]
    agreement_status: str | None = None  # default-ok: agreement is not measured for legacy Findings
    agreement_detail: str | None = None  # default-ok: optional repeated-run evidence detail
    cited_facts: tuple[CitedFact, ...] = Field(min_length=1)


class PairSelection(BaseModel):
    """Which two parties were assessed, chosen from typed contact observations (RPT-36 T2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    other_party: str = Field(min_length=1)
    collision: CitedFact  # the candidate collision nearest in time to the pair's contacts, same view
    observations: tuple[CitedFact, ...] = Field(min_length=1)  # the typed pair contacts it was chosen from
    corroborated: bool = False  # default-ok: legacy selections have no recorded close-up confirmation
    reason: str = Field(min_length=1)


class PartyFault(BaseModel):
    """Fault determination for one Party in an incident."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    party_description: str = Field(min_length=1)
    status: FaultStatus
    color: str  # "pink" | "green" | "white" | "grey"
    rule_id: str | None = None
    legal_source: str | None = None
    article: str | None = None
    reason: str = Field(min_length=1)
    cited_facts: tuple[CitedFact, ...]  # required (ADR 0006); a decided status must cite at least one fact
    agreement_status: str | None = None  # default-ok: optional agreement details
    agreement_detail: str | None = None  # default-ok: optional agreement details

    findings: tuple[Finding, ...] = ()  # default-ok: no breach Finding until proved by a rule
    pair_selection: PairSelection | None = None  # default-ok: set only when a pair was picked from several parties

    @model_validator(mode="before")
    @classmethod
    def _read_legacy_abstention_color(cls, data: Any) -> Any:
        """Old stored reports used white for abstention; their meaning stays unchanged."""
        if (
            isinstance(data, dict)
            and data.get("status") == FaultStatus.CANNOT_DETERMINE
            and data.get("color") == "white"
        ):
            return {**data, "color": "grey"}
        return data

    @property
    def display_label(self) -> str:
        """Keep the serialized state stable while clearly labelling report abstention."""
        return self.status.display_label

    @model_validator(mode="after")
    def _validate_party_and_color(self) -> Self:
        if is_raw_id(self.party_description):
            raise ValueError(
                f"party_description {self.party_description!r} is a raw ID; must be colour + make/model + body type."
            )
        if self.status != FaultStatus.CANNOT_DETERMINE and not self.cited_facts:
            raise ValueError(f"{self.status.value!r} must cite at least one fact (view and timestamp), ADR 0006.")
        if self.status == FaultStatus.WHITE and (self.rule_id != "F1" or not self.legal_source or not self.article):
            raise ValueError("White requires the cited F1 handbook rule (ADR 0004, RPT-56).")
        expected_color = FAULT_STATUS_COLORS.get(self.status)
        if expected_color is not None and self.color != expected_color:
            raise ValueError(
                f"Color {self.color!r} does not match status {self.status.value!r} (expected {expected_color!r})."
            )
        return self


class Violation(BaseModel):
    """A typed violation input observed for one Party (RPT-17, ADR 0006, ADR 0007)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1)
    party_description: str = Field(min_length=1)
    cited_facts: tuple[CitedFact, ...]

    @property
    def evidence(self) -> tuple[CitedFact, ...]:
        return self.cited_facts

    @model_validator(mode="after")
    def _validate_party_and_citation(self) -> Self:
        if is_raw_id(self.party_description):
            raise ValueError(
                f"party_description {self.party_description!r} is a raw ID; must be colour + make/model + body type."
            )
        if not self.cited_facts:
            raise ValueError("a violation must cite at least one fact (view and timestamp), ADR 0006.")
        return self


class PartyViolation(BaseModel):
    """An official fine schedule row mapped to a specific Party violation (RPT-17)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    party_description: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    item_number: str = Field(min_length=1)
    name_en: str = Field(min_length=1)
    name_ar: str | None = None
    aed: int | None = None
    black_points: int | None = None
    impound_days: int | None = None
    source_url: str = Field(min_length=1)
    status: str = Field(min_length=1)
    cited_facts: tuple[CitedFact, ...]
    notes: str | None = None
    research_reference: str | None = None
    agreement_status: str | None = None  # default-ok: optional agreement details
    agreement_detail: str | None = None  # default-ok: optional agreement details

    @property
    def is_provisional(self) -> bool:
        return self.status == "provisional"

    @property
    def evidence(self) -> tuple[CitedFact, ...]:
        return self.cited_facts

    @model_validator(mode="after")
    def _validate_party_and_citation(self) -> Self:
        if is_raw_id(self.party_description):
            raise ValueError(
                f"party_description {self.party_description!r} is a raw ID; must be colour + make/model + body type."
            )
        if not self.cited_facts:
            raise ValueError("a violation must cite at least one fact (view and timestamp), ADR 0006.")
        return self


@dataclass(frozen=True)
class ViolationTotals:
    """Totals of fines and penalties across a collection of violations (RPT-17)."""

    aed: int
    black_points: int
    court_decided_count: int = 0  # default-ok: count of court-decided violations is zero until counted
    provisional_count: int = 0  # default-ok: count of provisional violations is zero until counted
    flagged_count: int = 0  # default-ok: count of flagged violations is zero until counted


@dataclass(frozen=True)
class PartyViolationsSummary:
    """Violations and totals for one party (RPT-17)."""

    party_description: str
    violations: tuple[PartyViolation, ...]
    totals: ViolationTotals

    @property
    def total_aed(self) -> int:
        return self.totals.aed

    @property
    def total_black_points(self) -> int:
        return self.totals.black_points


@dataclass(frozen=True)
class IncidentViolationsSummary:
    """Violations and totals across all parties in an incident (RPT-17)."""

    parties: tuple[PartyViolationsSummary, ...]
    totals: ViolationTotals

    @property
    def total_aed(self) -> int:
        return self.totals.aed

    @property
    def total_black_points(self) -> int:
        return self.totals.black_points
