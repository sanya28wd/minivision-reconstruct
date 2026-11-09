"""Domain types shared by both seams. Terms follow GLOSSARY.md (View, Report, Run, Origin)."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from incident_report.agreement import AgreementResult, AgreementStatus
from incident_report.facts import CitedFact, OverviewFacts
from incident_report.model_calls import ModelCall
from incident_report.rules.collisions import CollisionSummary, classify_collisions
from incident_report.rules.consistency import ConsistencyIssue
from incident_report.rules.driver import DriverAssessment
from incident_report.rules.models import Finding, IncidentViolationsSummary, PartyFault, PartyViolation
from incident_report.rules.referrals import CriminalReferral, evaluate_referrals
from incident_report.rules.situation import Contradiction
from incident_report.storyline import StorylineItem


class Origin(enum.StrEnum):
    """Where a Run came from (spec 001, Seam 1). Every Run is logged with one."""

    APP = "app"
    BENCHMARK = "benchmark"
    REPLAY = "replay"


class Role(enum.StrEnum):
    """Which kind of View a file is (GLOSSARY: Interior View, Exterior View)."""

    INTERIOR = "interior"
    EXTERIOR = "exterior"


class RunStatus(enum.StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PROCESSING = "processing"


@dataclass
class View:
    """One uploaded file going into seam 1: the byte stream plus what the officer told us.

    The entry point reads `stream` once in chunks, hashing to externally configured
    disk-backed staging, then verifies and stores an untouched original (ADR 0011).
    """

    stream: BinaryIO
    filename: str
    role: Role
    label: str | None = None
    acquisition_method: str | None = None
    duration_seconds: float | None = None


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceCopy(_Frozen):
    """A working or derived object and the original it came from."""

    sha256: str
    parent_sha256: str


class ReportView(_Frozen):
    """A view as the Report describes it. Carries no bytes, only its fingerprint."""

    filename: str
    role: Role
    label: str | None
    acquisition_method: str | None
    sha256: str
    uploaded_at: datetime | None = None
    session_id: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    ffprobe_stdout: str | None = None
    ffprobe_stderr: str | None = None
    ffprobe_stdout_object: EvidenceCopy | None = None
    ffprobe_stderr_object: EvidenceCopy | None = None
    duration_seconds: float | None = None
    working_copy: EvidenceCopy | None = None
    validation_error: str | None = None


class DenseWindowRecord(_Frozen):
    view_id: str
    candidate_timestamp: float
    start_seconds: float
    end_seconds: float
    candidate_description: str
    source: Literal["overview", "motion", "both", "agreement_run"]
    proposed_by_runs: tuple[int, ...] = ()  # default-ok: records stored before RPT-36 T5 have no run provenance


class MotionWindowSelection(_Frozen):
    selected: tuple[DenseWindowRecord, ...]
    dropped: tuple[DenseWindowRecord, ...]
    candidates: dict[str, tuple[float, ...]]
    cuts: dict[str, tuple[float, ...]]
    camera_shake: dict[str, tuple[float, ...]]
    detector_ms: dict[str, float]


class Report(_Frozen):
    """The single data object the page (and later the PDF and scoring) is rendered from."""

    run_id: uuid.UUID
    input_hash: str
    prompt_version: str
    # Where the AI answer came from, copied from the recording (e.g. "synthetic" for the
    # hand-written fixture), so a placeholder is never mistaken for a real analysis.
    provenance: str
    summary: str
    views: tuple[ReportView, ...]
    facts: OverviewFacts | None = None
    parties: tuple[PartyFault, ...] = ()
    violations: tuple[PartyViolation, ...] = ()
    is_valid: bool = True
    validation_errors: tuple[str, ...] = ()
    guardrail_results: dict[str, tuple[str, ...]] = Field(
        default_factory=dict
    )  # default-ok: legacy stored Reports predate RPT-33; newly generated Reports always run the checks
    stage_latencies_ms: dict[str, float] = Field(default_factory=dict)
    overview_collision_recall: float | None = None
    dense_facts: tuple[OverviewFacts, ...] = ()
    consistency_issues: tuple[ConsistencyIssue, ...] = ()  # default-ok: legacy Reports predate spec 003 checks
    overview_consistency_issues: tuple[
        tuple[ConsistencyIssue, ...], ...
    ] = ()  # default-ok: legacy Reports predate per-analysis issue storage
    agreement: AgreementResult | None = None
    driver_assessments: tuple[
        DriverAssessment, ...
    ] = ()  # default-ok: pre-overview-4 reports have no driver assessment

    @property
    def referrals(self) -> tuple[CriminalReferral, ...]:
        return evaluate_referrals(self.facts, self.parties) if self.is_valid and self.facts is not None else ()

    @property
    def storyline(self) -> tuple[StorylineItem, ...]:
        """The cited "What happened" list, built by code from events and typed collision observations."""
        from incident_report.storyline import build_storyline

        return build_storyline(self.facts, [v.sha256 for v in self.views], include_notes=True)

    @property
    def summary_citation(self) -> StorylineItem | None:
        """The first contact the code-built summary names, so the summary links to its view and time."""
        from incident_report.storyline import first_contact

        return (
            first_contact(self.facts, [v.sha256 for v in self.views]) if self.incident_status == "Collision" else None
        )

    @property
    def collision_summary(self) -> CollisionSummary:
        """Every Collision with its code-derived Dubai/MOI classification (RPT-25, ADR 0007). Not persisted."""
        return (
            classify_collisions(self.facts) if self.facts is not None else CollisionSummary(records=(), unattached=())
        )

    def flagged_collision_note(self, collision: CitedFact) -> str | None:
        """The plain note for a Collision only some overview runs saw and the close-up check confirmed (RPT-36 T5)."""
        field = self.agreement.fields.get(f"collision:@{collision.timestamp_seconds:.1f}s") if self.agreement else None
        if field and field.status == AgreementStatus.FLAGGED and field.details and "close-up check" in field.details:
            return field.details
        return None

    @property
    def contradictions(self) -> tuple[Contradiction, ...]:
        """Use the same reconciliation as the rules, including persisted source citations."""
        from incident_report.rules.adapter import situation_from_overview

        return situation_from_overview(self.facts).contradictions if self.facts is not None else ()

    @property
    def incident_status(self) -> Literal["No Incident", "No collision", "Collision", "Cannot Determine"]:
        """The report label is independent of the model's narrative and Fault Status."""
        if not self.is_valid or self.facts is None:
            return "Cannot Determine"
        if self.facts.candidate_collisions or any(f.candidate_collisions for f in self.dense_facts):
            return "Collision"
        # overview-10 (spec 003 1b D): an assessment that says a touch happened, or cannot be judged, on a view
        # with no candidate contact must never read as No Incident / No collision.
        if any(
            a.contact_state in {"contact_observed", "contact_possible_obscured", "cannot_assess"}
            and not any(c.view_id == a.view_id for c in self.facts.candidate_collisions)
            for a in self.facts.incident_assessments
        ):
            return "Cannot Determine"
        # overview-11: the higher-detail re-check of a negative first answer failed, so "no collision" was never
        # confirmed (spec 003 ticket 8, card section 4).
        if any(
            a.recovery == "failed" and {"negative_state", "no_typed_contact"} & set(a.recovery_triggers)
            for a in self.facts.incident_assessments
        ):
            return "Cannot Determine"
        if self.violations:
            return "No collision"
        votes = (
            {key: field for key, field in self.agreement.fields.items() if key.startswith("violation:")}
            if self.agreement
            else {}
        )
        if self.agreement and self.agreement.status == AgreementStatus.CANNOT_DETERMINE:
            return "Cannot Determine"
        if votes:
            if any(field.agreed_value is not False for field in votes.values()):
                return "Cannot Determine"
        else:
            from incident_report.rules.adapter import situation_from_overview
            from incident_report.rules.violations import signal_crossing_readings

            if any(
                reading.is_red is not False
                for reading in signal_crossing_readings(
                    situation_from_overview(self.facts), consistency_issues=self.consistency_issues
                )
            ):
                return "Cannot Determine"
        return "No Incident"

    @property
    def contributing_factors(self) -> tuple[Finding, ...]:
        """Only sourced rule Findings, never prose observations or a new Fault Status."""
        if self.incident_status != "Collision":
            return ()
        return tuple(
            finding
            for party in self.parties
            for finding in party.findings
            if finding.classification == "Contributing Factor"
        )

    @property
    def violations_summary(self) -> IncidentViolationsSummary:
        from incident_report.rules.violations import summarize_violations

        return summarize_violations(self.violations)

    @property
    def total_fines_aed(self) -> int:
        return sum(v.aed for v in self.violations if v.aed is not None)

    @property
    def total_black_points(self) -> int:
        return sum(v.black_points for v in self.violations if v.black_points is not None)


class Attribution(_Frozen):
    """Public anonymous IDs, never bearer credentials (ADR 0010)."""

    session_id: uuid.UUID
    link_id: uuid.UUID


class Run(_Frozen):
    """One row of the run log (spec 001, Run log contract; this ticket's subset)."""

    run_id: uuid.UUID
    origin: Origin
    created_at: datetime
    input_hash: str
    report: Report | None
    status: RunStatus
    total_latency_ms: float
    error: str | None = None
    calls: tuple[ModelCall, ...] = ()
    retry_count: int = 0
    repair_attempts: int = 0
    repair_outcome: str | None = None
    attribution: Attribution | None = None
    request_id: uuid.UUID | None = None
    current_step: str | None = None
    stage_timings_ms: dict[str, float] = Field(default_factory=dict)  # default-ok: historical Runs have no timings
    motion_window_selection: MotionWindowSelection | None = None  # default-ok: opt-in experiment

    @property
    def stage_latencies_ms(self) -> dict[str, float]:
        return self.report.stage_latencies_ms if self.report else self.stage_timings_ms

    @property
    def overview_collision_recall(self) -> float | None:
        return self.report.overview_collision_recall if self.report else None

    @property
    def agreement(self) -> AgreementResult | None:
        return self.report.agreement if self.report else None

    @model_validator(mode="after")
    def _report_matches_status(self) -> Self:
        if self.status is RunStatus.SUCCEEDED and (self.report is None or self.error is not None):
            raise ValueError("a succeeded run has a report and no error")
        if self.status is RunStatus.FAILED and (self.report is not None or not self.error):
            raise ValueError("a failed run has an error and no report (never partial output)")
        if self.status is RunStatus.PROCESSING and (self.report is not None or self.error is not None):
            raise ValueError("a processing run has no report and no error")
        return self


@dataclass(frozen=True)
class IncidentRow:
    """One row in the incidents list view (RPT-41)."""

    run: Run
    view_count: int
    filenames: str | None = None
    is_approved: bool = False  # default-ok: RPT-38, true only when some version of this Report was approved

    @property
    def run_id(self) -> uuid.UUID:

        return self.run.run_id

    @property
    def created_at(self) -> datetime:
        return self.run.created_at

    @property
    def status_label(self) -> Literal["Processing", "Ready", "Approved", "Invalid", "Failed"]:
        if self.run.status is RunStatus.PROCESSING:
            return "Processing"
        if self.run.status is RunStatus.FAILED:
            return "Failed"
        if self.run.status is RunStatus.SUCCEEDED:
            if self.run.report is not None and self.run.report.is_valid:
                return "Approved" if self.is_approved else "Ready"
            return "Invalid"
        return "Failed"

    @property
    def is_ready(self) -> bool:
        return self.status_label in ("Ready", "Approved")


class InvalidIncident(ValueError):
    """The request cannot be analysed at all (for example no views). No Run is written."""

    intake_id: uuid.UUID | None = None
