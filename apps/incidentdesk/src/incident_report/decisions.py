"""Officer decisions on a Report: Override, Approve, lock and versions (RPT-38, spec 001, ADR 0006).

The AI's Report is never rewritten. Decisions live beside it in three append-only tables (migration 0016):

- report_overrides: one row per Override, with the AI's original finding copied beside it by the server.
- report_approvals: one row per Approve. A version with a row here is locked (read-only).
- report_versions: one row per version >= 2. Version 1 is implicit, so a Report with no decisions needs no rows.

A change after approval makes a new version that branches from the approved one. Earlier versions are never
touched: a version's effective Overrides are those made in it plus those it inherits from its parents.

Officer free text stays in the report_overrides table. It is never sent to a model, and the audit log holds a
SHA-256 fingerprint of it, not the text. All of it is escaped when rendered (templates use autoescape).

The override reasons below are plain user-interface reasons. They are not RTA or Dubai Police categories.
"""

from __future__ import annotations

import enum
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from incident_report.audit import AuditLog, canonical_json
from incident_report.domain import Attribution, Report, Run, RunStatus
from incident_report.rules.models import FaultStatus

# project assumption, to be validated: a generous bound that keeps pages and audit rows small.
MAX_REASON_TEXT_CHARS = 1000

# Arbitrary constant key (first half of a two-part advisory lock; the run id supplies the second half) so decisions
# on one Report are applied one at a time: an Override can never slip into a version that is being approved.
_DECISION_LOCK_KEY: int = 7_450_016


class OverrideReason(enum.StrEnum):
    """Why an officer overrode a finding. Plain UI reasons, not RTA categories (ADR 0004, ADR 0006)."""

    VIDEO_SHOWS_OTHERWISE = "video_shows_otherwise"
    WRONG_PARTY = "wrong_party"
    WRONG_TIMESTAMP = "wrong_timestamp"
    OTHER = "other"


OVERRIDE_REASON_LABELS: dict[OverrideReason, str] = {
    OverrideReason.VIDEO_SHOWS_OTHERWISE: "Video shows otherwise",
    OverrideReason.WRONG_PARTY: "Wrong party",
    OverrideReason.WRONG_TIMESTAMP: "Wrong timestamp",
    OverrideReason.OTHER: "Other",
}

# Values an officer may choose. Parties use the existing Fault Status words (ADR 0004). A violation can only be
# disputed or left undetermined: adding one would need a fine row from the official table (ADR 0006).
# White needs F1 and typed supporting facts; the override form cannot record these.
PARTY_VALUES: tuple[str, ...] = tuple(status.value for status in FaultStatus if status != FaultStatus.WHITE)
VIOLATION_NOT_ESTABLISHED = "Not established"
VIOLATION_VALUES: tuple[str, ...] = (VIOLATION_NOT_ESTABLISHED, FaultStatus.CANNOT_DETERMINE.value)


class DecisionRefused(Exception):
    """A decision the system will not record. `status_code` is the HTTP status the web layer answers with."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class FindingRef:
    """One finding an officer can override. The key is chosen by the server from the stored Report."""

    key: str
    kind: Literal["party", "violation"]
    label: str
    ai_original: dict[str, Any]
    allowed_values: tuple[str, ...]
    ai_value: str


def findings_of(report: Report) -> dict[str, FindingRef]:
    """Every overridable finding of a Report, in page order, keyed `party:<n>` / `violation:<n>`."""
    found: dict[str, FindingRef] = {}
    for index, party in enumerate(report.parties):
        found[f"party:{index}"] = FindingRef(
            key=f"party:{index}",
            kind="party",
            label=party.party_description,
            ai_original={
                "party_description": party.party_description,
                "fault_status": party.status.value,
                "reason": party.reason,
                "rule": f"{party.article} ({party.legal_source})" if party.article and party.legal_source else None,
            },
            allowed_values=PARTY_VALUES,
            ai_value=party.status.value,
        )
    for index, violation in enumerate(report.violations):
        found[f"violation:{index}"] = FindingRef(
            key=f"violation:{index}",
            kind="violation",
            label=f"{violation.party_description}: item {violation.item_number}, {violation.name_en}",
            ai_original={
                "party_description": violation.party_description,
                "item_number": violation.item_number,
                "name_en": violation.name_en,
                "aed": violation.aed,
                "black_points": violation.black_points,
                "impound_days": violation.impound_days,
                "status": violation.status,
            },
            allowed_values=VIOLATION_VALUES,
            ai_value="Established",
        )
    return found


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OverrideRecord(_Frozen):
    override_id: uuid.UUID
    version_number: int
    finding_key: str
    ai_original: dict[str, Any]
    new_value: str
    reason_code: OverrideReason
    reason_text: str
    created_at: datetime

    @property
    def reason_label(self) -> str:
        return OVERRIDE_REASON_LABELS[self.reason_code]


class VersionInfo(_Frozen):
    version_number: int
    created_at: datetime | None
    approved_at: datetime | None

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None


class ReportState(_Frozen):
    """What the officer sees for one version: its effective Overrides and whether it is locked."""

    run_id: uuid.UUID
    version_number: int
    versions: tuple[VersionInfo, ...]
    overrides: dict[str, OverrideRecord]

    @property
    def current(self) -> VersionInfo:
        return next(v for v in self.versions if v.version_number == self.version_number)

    @property
    def is_approved(self) -> bool:
        return self.current.is_approved

    @property
    def approved_at(self) -> datetime | None:
        return self.current.approved_at

    @property
    def latest_version(self) -> int:
        return self.versions[-1].version_number

    @property
    def is_latest(self) -> bool:
        return self.version_number == self.latest_version

    @property
    def can_edit(self) -> bool:
        """Only the newest version, and only while it is not approved."""
        return self.is_latest and not self.is_approved


def _has_control_chars(text: str) -> bool:
    # NUL and other control characters are refused (tab, CR and LF are fine); PostgreSQL text cannot hold NUL.
    return any((ord(ch) < 32 and ord(ch) not in (9, 10, 13)) or ord(ch) == 127 for ch in text)


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()


class DecisionStore:
    def __init__(self, database_url: str, audit: AuditLog) -> None:
        self._url = database_url
        self._audit = audit

    # --- reading ---------------------------------------------------------------------------------------

    def state(self, run_id: uuid.UUID, version: int | None = None) -> ReportState:
        with psycopg.connect(self._url) as conn:
            return self._state(conn, run_id, version)

    def has_approved_version(self, run_id: uuid.UUID) -> bool:
        with psycopg.connect(self._url) as conn:
            row = conn.execute("SELECT 1 FROM report_approvals WHERE run_id = %s LIMIT 1", (run_id,)).fetchone()
        return row is not None

    def _state(self, conn: psycopg.Connection[Any], run_id: uuid.UUID, version: int | None) -> ReportState:
        created = {
            number: when
            for number, when in conn.execute(
                "SELECT version_number, created_at FROM report_versions WHERE run_id = %s", (run_id,)
            ).fetchall()
        }
        approved = {
            number: when
            for number, when in conn.execute(
                "SELECT version_number, approved_at FROM report_approvals WHERE run_id = %s", (run_id,)
            ).fetchall()
        }
        numbers = sorted({1, *created})
        versions = tuple(
            VersionInfo(version_number=n, created_at=created.get(n), approved_at=approved.get(n)) for n in numbers
        )
        target = numbers[-1] if version is None else version
        if target not in numbers:
            raise DecisionRefused(404, "No such version of this report.")
        rows = conn.execute(
            "SELECT override_id, version_number, finding_key, ai_original, new_value, reason_code, reason_text, "
            "created_at FROM report_overrides WHERE run_id = %s AND version_number <= %s ORDER BY override_seq",
            (run_id, target),
        ).fetchall()
        effective: dict[str, OverrideRecord] = {}
        for row in rows:  # oldest first, so a later Override of the same finding replaces an earlier one
            record = OverrideRecord(
                override_id=row[0],
                version_number=row[1],
                finding_key=row[2],
                ai_original=row[3],
                new_value=row[4],
                reason_code=OverrideReason(row[5]),
                reason_text=row[6],
                created_at=row[7],
            )
            effective[record.finding_key] = record
        return ReportState(run_id=run_id, version_number=target, versions=versions, overrides=effective)

    # --- writing ---------------------------------------------------------------------------------------

    def add_override(
        self,
        run: Run,
        *,
        finding_key: str,
        new_value: str,
        reason_code: str,
        reason_text: str,
        attribution: Attribution,
        request_id: uuid.UUID | None,
        new_version: bool = False,
    ) -> OverrideRecord:
        """Record one Override. On an approved version this is refused unless `new_version` starts version N+1."""
        report = _decidable_report(run)
        finding = findings_of(report).get(finding_key)
        if finding is None:
            raise DecisionRefused(422, "Choose a finding from this report to override.")
        if new_value not in finding.allowed_values or new_value == finding.ai_value:
            raise DecisionRefused(422, "Choose a different value from the list.")
        try:
            reason = OverrideReason(reason_code)
        except ValueError:
            raise DecisionRefused(422, "Choose a reason for the override from the list.") from None
        text = reason_text.strip()
        if _has_control_chars(text):
            raise DecisionRefused(422, "The explanation contains characters that are not allowed.")
        if not text:
            raise DecisionRefused(422, "Write a short explanation of the override.")
        if len(text) > MAX_REASON_TEXT_CHARS:
            raise DecisionRefused(422, f"The explanation must be at most {MAX_REASON_TEXT_CHARS} characters.")

        with psycopg.connect(self._url) as conn:
            with conn.transaction():
                self._lock(conn, run.run_id)
                state = self._state(conn, run.run_id, None)
                version = state.version_number
                if state.is_approved:
                    if not new_version:
                        raise DecisionRefused(
                            409, "This report is approved and read-only. Start a new version to change it."
                        )
                    version = self._insert_version(conn, run, state, attribution, request_id)
                override_id = uuid.uuid4()
                row = conn.execute(
                    "INSERT INTO report_overrides (override_id, run_id, version_number, finding_key, ai_original, "
                    "new_value, reason_code, reason_text, session_id, link_id, request_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING created_at",
                    (
                        override_id,
                        run.run_id,
                        version,
                        finding_key,
                        Jsonb(finding.ai_original),
                        new_value,
                        reason.value,
                        text,
                        attribution.session_id,
                        attribution.link_id,
                        request_id,
                    ),
                ).fetchone()
                if row is None:
                    raise RuntimeError("INSERT ... RETURNING gave no row")
                self._audit.append(
                    conn=conn,
                    event_type="override",
                    action="override.created",
                    actor="visitor",
                    session_id=attribution.session_id,
                    link_id=attribution.link_id,
                    request_id=request_id,
                    run_id=run.run_id,
                    target_type="report_version",
                    target_id=f"{run.run_id}:{version}",
                    details={
                        "version_number": version,
                        "finding_key": finding_key,
                        "new_value": new_value,
                        "reason_code": reason.value,
                        "override_id": str(override_id),
                        "content_sha256": _fingerprint(finding_key, new_value, reason.value, text),
                    },
                )
        return OverrideRecord(
            override_id=override_id,
            version_number=version,
            finding_key=finding_key,
            ai_original=finding.ai_original,
            new_value=new_value,
            reason_code=reason,
            reason_text=text,
            created_at=row[0],
        )

    def approve(self, run: Run, *, attribution: Attribution, request_id: uuid.UUID | None) -> ReportState:
        """Lock the newest version. Its Overrides, the input hash and the version number are fingerprinted."""
        _decidable_report(run)
        with psycopg.connect(self._url) as conn:
            with conn.transaction():
                self._lock(conn, run.run_id)
                state = self._state(conn, run.run_id, None)
                if state.is_approved:
                    raise DecisionRefused(409, "This version is already approved.")
                # Workspace writers lock this same row. Capture immutable record IDs,
                # rather than relying on transaction-start timestamps for later exports.
                conn.execute("SELECT run_id FROM runs WHERE run_id=%s FOR UPDATE", (run.run_id,))
                reconstruction_snapshot = {
                    "link_ids": [
                        str(row[0])
                        for row in conn.execute(
                            "SELECT l.link_id FROM incident_evidence_imports l WHERE l.incident_id=%s AND NOT EXISTS (SELECT 1 FROM incident_evidence_supersessions s WHERE s.old_link_id=l.link_id) ORDER BY l.sequence_number",
                            (run.run_id,),
                        )
                    ],
                    "mapping_ids": [
                        row[0]
                        for row in conn.execute(
                            "SELECT mapping_id FROM track_party_mappings WHERE incident_id=%s ORDER BY mapping_id",
                            (run.run_id,),
                        )
                    ],
                    "alignment_ids": [
                        row[0]
                        for row in conn.execute(
                            "SELECT alignment_id FROM view_time_alignments WHERE incident_id=%s ORDER BY alignment_id",
                            (run.run_id,),
                        )
                    ],
                }
                digest = _fingerprint(
                    run.input_hash,
                    str(state.version_number),
                    *(
                        _fingerprint(
                            key, record.new_value, record.reason_code.value, record.reason_text, str(record.override_id)
                        )
                        for key, record in sorted(state.overrides.items())
                    ),
                )
                conn.execute(
                    "INSERT INTO report_approvals (run_id, version_number, overrides_digest, session_id, link_id, "
                    "request_id) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        run.run_id,
                        state.version_number,
                        digest,
                        attribution.session_id,
                        attribution.link_id,
                        request_id,
                    ),
                )
                self._audit.append(
                    conn=conn,
                    event_type="approval",
                    action="report.approved",
                    actor="visitor",
                    session_id=attribution.session_id,
                    link_id=attribution.link_id,
                    request_id=request_id,
                    run_id=run.run_id,
                    target_type="report_version",
                    target_id=f"{run.run_id}:{state.version_number}",
                    details={
                        "version_number": state.version_number,
                        "override_count": len(state.overrides),
                        "overrides_digest": digest,
                        "input_hash": run.input_hash,
                        "reconstruction_snapshot": reconstruction_snapshot,
                    },
                )
                return self._state(conn, run.run_id, None)

    def create_version(self, run: Run, *, attribution: Attribution, request_id: uuid.UUID | None) -> ReportState:
        """Start version N+1 from the approved newest version. It begins with the same Overrides."""
        _decidable_report(run)
        with psycopg.connect(self._url) as conn:
            with conn.transaction():
                self._lock(conn, run.run_id)
                state = self._state(conn, run.run_id, None)
                if not state.is_approved:
                    raise DecisionRefused(
                        409, "Approve the current version before starting a new one. It can still be changed."
                    )
                self._insert_version(conn, run, state, attribution, request_id)
                return self._state(conn, run.run_id, None)

    def _lock(self, conn: psycopg.Connection[Any], run_id: uuid.UUID) -> None:
        conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_DECISION_LOCK_KEY, str(run_id)))

    def _insert_version(
        self,
        conn: psycopg.Connection[Any],
        run: Run,
        state: ReportState,
        attribution: Attribution,
        request_id: uuid.UUID | None,
    ) -> int:
        number = state.latest_version + 1
        conn.execute(
            "INSERT INTO report_versions (run_id, version_number, parent_version, session_id, link_id, request_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (run.run_id, number, state.latest_version, attribution.session_id, attribution.link_id, request_id),
        )
        self._audit.append(
            conn=conn,
            event_type="version",
            action="version.created",
            actor="visitor",
            session_id=attribution.session_id,
            link_id=attribution.link_id,
            request_id=request_id,
            run_id=run.run_id,
            target_type="report_version",
            target_id=f"{run.run_id}:{number}",
            details={"version_number": number, "parent_version": state.latest_version},
        )
        return number


def _decidable_report(run: Run) -> Report:
    """Only a finished, valid Report can be decided on. An invalid one must not become an official finding."""
    if run.status is not RunStatus.SUCCEEDED or run.report is None:
        raise DecisionRefused(404, "No report with that id.")
    if not run.report.is_valid:
        raise DecisionRefused(409, "This report is marked invalid, so it cannot be overridden or approved.")
    return run.report
