"""The run log: one row per Run (spec 001, Run log contract; this ticket's subset)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from incident_report.audit import AuditLog
from incident_report.config import ProcessingSettings
from incident_report.decisions import DecisionStore
from incident_report.domain import Attribution, IncidentRow, ReportView, Run, RunStatus


class RunLog:
    def __init__(self, database_url: str, *, processing_settings: ProcessingSettings | None = None) -> None:
        self._url = database_url
        self._audit = AuditLog(database_url)
        self._decisions = DecisionStore(database_url, self._audit)
        self._processing_settings = processing_settings or ProcessingSettings.from_env()

    @property
    def audit(self) -> AuditLog:
        return self._audit

    @property
    def decisions(self) -> DecisionStore:
        """Overrides, approvals and versions of a Report (RPT-38)."""
        return self._decisions

    def record_upload_audit(
        self,
        run_id: uuid.UUID,
        view_number: int,
        view: ReportView,
        attribution: Attribution | None = None,
        request_id: uuid.UUID | None = None,
    ) -> None:
        actor = "visitor" if attribution else "system"
        self._audit.append(
            event_type="upload",
            action="evidence_intake.receive_view",
            actor=actor,
            session_id=attribution.session_id if attribution else None,
            link_id=attribution.link_id if attribution else None,
            request_id=request_id,
            run_id=run_id,
            status_code=None,
            details={
                "view_number": view_number,
                "role": view.role.value,
                "sha256": view.sha256,
                "duration_seconds": round(view.duration_seconds, 3) if view.duration_seconds is not None else None,
                "size_bytes": view.size_bytes,
            },
        )

    def record_intake_view(self, run_id: uuid.UUID, number: int, view: ReportView) -> None:
        """Record each view independently, including rejected/partially accepted incidents."""
        with psycopg.connect(self._url) as conn:
            conn.execute(
                "INSERT INTO evidence_intake (run_id, view_number, metadata) VALUES (%s, %s, %s) "
                "ON CONFLICT (run_id, view_number) DO UPDATE SET metadata = EXCLUDED.metadata",
                (run_id, number, Jsonb(view.model_dump(mode="json"))),
            )

    def intake_views(self, run_id: uuid.UUID) -> tuple[ReportView, ...]:
        with psycopg.connect(self._url) as conn:
            rows = conn.execute(
                "SELECT metadata FROM evidence_intake WHERE run_id = %s ORDER BY view_number", (run_id,)
            ).fetchall()
        return tuple(ReportView.model_validate(row[0]) for row in rows)

    def record_officer_question(self, run_id: uuid.UUID, question: str) -> None:
        """Store optional officer question separately from AI runs and view analysis."""
        with psycopg.connect(self._url) as conn:
            conn.execute(
                "INSERT INTO officer_questions (run_id, question) VALUES (%s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET question = EXCLUDED.question",
                (run_id, question),
            )

    def get_officer_question(self, run_id: uuid.UUID) -> str | None:
        with psycopg.connect(self._url) as conn:
            row = conn.execute("SELECT question FROM officer_questions WHERE run_id = %s", (run_id,)).fetchone()
        return row[0] if row is not None else None

    @property
    def database_url(self) -> str:
        return self._url

    def record(self, run: Run) -> None:
        report = Jsonb(run.report.model_dump(mode="json")) if run.report else None
        with psycopg.connect(self._url) as conn:
            with conn.transaction():
                cur = conn.execute(
                    "INSERT INTO runs (run_id, origin, created_at, input_hash, report, status,"
                    " total_latency_ms, error, calls, retry_count, repair_attempts, repair_outcome,"
                    " session_id, link_id, request_id, current_step, motion_window_selection, stage_timings_ms)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (run_id) DO UPDATE SET"
                    " report = EXCLUDED.report,"
                    " status = EXCLUDED.status,"
                    " total_latency_ms = EXCLUDED.total_latency_ms,"
                    " error = EXCLUDED.error,"
                    " calls = EXCLUDED.calls,"
                    " retry_count = EXCLUDED.retry_count,"
                    " repair_attempts = EXCLUDED.repair_attempts,"
                    " repair_outcome = EXCLUDED.repair_outcome,"
                    " current_step = EXCLUDED.current_step,"
                    " motion_window_selection = EXCLUDED.motion_window_selection,"
                    " stage_timings_ms = EXCLUDED.stage_timings_ms"
                    " WHERE runs.status = 'processing'",
                    (
                        run.run_id,
                        run.origin.value,
                        run.created_at,
                        run.input_hash,
                        report,
                        run.status.value,
                        run.total_latency_ms,
                        run.error,
                        Jsonb([call.model_dump(mode="json") for call in run.calls]),
                        run.retry_count,
                        run.repair_attempts,
                        run.repair_outcome,
                        run.attribution.session_id if run.attribution else None,
                        run.attribution.link_id if run.attribution else None,
                        run.request_id,
                        run.current_step,
                        Jsonb(run.motion_window_selection.model_dump(mode="json"))
                        if run.motion_window_selection
                        else None,
                        Jsonb(run.stage_timings_ms),
                    ),
                )
                if run.status is not RunStatus.PROCESSING and cur.rowcount > 0:
                    actor = "visitor" if run.attribution else "system"
                    self._audit.append(
                        conn=conn,
                        event_type="run",
                        action=f"run.{run.status.value}",
                        actor=actor,
                        session_id=run.attribution.session_id if run.attribution else None,
                        link_id=run.attribution.link_id if run.attribution else None,
                        request_id=run.request_id,
                        run_id=run.run_id,
                        status_code=None,
                        details={
                            "origin": run.origin.value,
                            "status": run.status.value,
                            "input_hash": run.input_hash,
                            "total_latency_ms": round(run.total_latency_ms, 2),
                            "retry_count": run.retry_count,
                            "repair_attempts": run.repair_attempts,
                            "repair_outcome": run.repair_outcome,
                            "error": run.error,
                        },
                    )

    def update_step(self, run_id: uuid.UUID, current_step: str) -> None:
        """Update current_step on an in-flight run in PostgreSQL."""
        with psycopg.connect(self._url) as conn:
            with conn.transaction():
                conn.execute(
                    "UPDATE runs SET current_step = %s WHERE run_id = %s AND status = 'processing'",
                    (current_step, run_id),
                )

    def _check_stale(self, conn: psycopg.Connection[Any], run: Run) -> Run:
        if run.status is RunStatus.PROCESSING:
            age = (datetime.now(UTC) - run.created_at).total_seconds()
            if age > self._processing_settings.max_processing_seconds:
                error_msg = "Analysis was interrupted."
                with conn.transaction():
                    cur = conn.execute(
                        "UPDATE runs SET status = 'failed', error = %s, current_step = NULL WHERE run_id = %s AND status = 'processing'",
                        (error_msg, run.run_id),
                    )
                    if cur.rowcount > 0:
                        actor = "visitor" if run.attribution else "system"
                        self._audit.append(
                            conn=conn,
                            event_type="run",
                            action="run.failed",
                            actor=actor,
                            session_id=run.attribution.session_id if run.attribution else None,
                            link_id=run.attribution.link_id if run.attribution else None,
                            request_id=run.request_id,
                            run_id=run.run_id,
                            status_code=None,
                            details={
                                "origin": run.origin.value,
                                "status": "failed",
                                "input_hash": run.input_hash,
                                "total_latency_ms": round(age * 1000, 2),
                                "retry_count": run.retry_count,
                                "repair_attempts": run.repair_attempts,
                                "repair_outcome": run.repair_outcome,
                                "error": error_msg,
                            },
                        )
                return run.model_copy(update={"status": RunStatus.FAILED, "error": error_msg, "current_step": None})
        return run

    def get(self, run_id: uuid.UUID) -> Run:
        with psycopg.connect(self._url) as conn:
            row = conn.execute(_SELECT + " WHERE run_id = %s", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"No run with id {run_id}")
            return self._check_stale(conn, _to_run(row))

    def list_runs(self) -> list[Run]:
        with psycopg.connect(self._url) as conn:
            rows = conn.execute(_SELECT + " ORDER BY created_at, run_id").fetchall()
            return [self._check_stale(conn, _to_run(r)) for r in rows]

    def find_in_flight_run(self, input_hash: str) -> Run | None:
        """Find an active (processing) Run for this input hash, checking for staleness."""
        with psycopg.connect(self._url) as conn:
            row = conn.execute(
                _SELECT + " WHERE input_hash = %s AND status = 'processing' ORDER BY created_at DESC LIMIT 1",
                (input_hash,),
            ).fetchone()
            if row is None:
                return None
            run = self._check_stale(conn, _to_run(row))
            return run if run.status is RunStatus.PROCESSING else None

    def list_runs_for_link(self, link_id: uuid.UUID, *, limit: int = 50, offset: int = 0) -> list[IncidentRow]:
        capped_limit = max(1, min(limit, 200))
        capped_offset = max(0, offset)
        with psycopg.connect(self._url) as conn:
            rows = conn.execute(
                _SELECT_INCIDENT_ROWS
                + " WHERE r.link_id = %s ORDER BY r.created_at DESC, r.run_id DESC LIMIT %s OFFSET %s",
                (link_id, capped_limit, capped_offset),
            ).fetchall()
            return [
                IncidentRow(
                    run=self._check_stale(conn, _to_run(r[:18])),
                    view_count=int(r[18]),
                    filenames=r[19],
                    is_approved=bool(r[20]),
                )
                for r in rows
            ]

    def list_all_runs(self, *, limit: int = 50, offset: int = 0) -> list[IncidentRow]:
        capped_limit = max(1, min(limit, 200))
        capped_offset = max(0, offset)
        with psycopg.connect(self._url) as conn:
            rows = conn.execute(
                _SELECT_INCIDENT_ROWS + " ORDER BY r.created_at DESC, r.run_id DESC LIMIT %s OFFSET %s",
                (capped_limit, capped_offset),
            ).fetchall()
            return [
                IncidentRow(
                    run=self._check_stale(conn, _to_run(r[:18])),
                    view_count=int(r[18]),
                    filenames=r[19],
                    is_approved=bool(r[20]),
                )
                for r in rows
            ]


_SELECT = (
    "SELECT run_id, origin, created_at, input_hash, report, status, total_latency_ms, error,"
    " calls, retry_count, repair_attempts, repair_outcome, session_id, link_id, request_id, current_step, motion_window_selection, stage_timings_ms FROM runs"
)

_SELECT_INCIDENT_ROWS = (
    "SELECT r.run_id, r.origin, r.created_at, r.input_hash, r.report, r.status, r.total_latency_ms, r.error,"
    " r.calls, r.retry_count, r.repair_attempts, r.repair_outcome, r.session_id, r.link_id, r.request_id, r.current_step, r.motion_window_selection, r.stage_timings_ms,"
    " COALESCE(v.view_count, 0) AS view_count,"
    " v.filenames,"
    " EXISTS (SELECT 1 FROM report_approvals ap WHERE ap.run_id = r.run_id) AS is_approved"
    " FROM runs r"
    " LEFT JOIN LATERAL ("
    "   SELECT COUNT(*) AS view_count, string_agg(e.metadata->>'filename', ', ') AS filenames"
    "   FROM evidence_intake e WHERE e.run_id = r.run_id"
    " ) v ON TRUE"
)


def _to_run(row: tuple[Any, ...]) -> Run:
    (
        run_id,
        origin,
        created_at,
        input_hash,
        report,
        status,
        latency,
        error,
        calls,
        retry_count,
        repair_attempts,
        repair_outcome,
        session_id,
        link_id,
        request_id,
        current_step,
        motion_window_selection,
        stage_timings_ms,
    ) = row
    return Run.model_validate(
        {
            "run_id": run_id,
            "origin": origin,
            "created_at": created_at,
            "input_hash": input_hash,
            "report": report,
            "status": status,
            "total_latency_ms": latency,
            "error": error,
            "calls": calls,
            "retry_count": retry_count,
            "repair_attempts": repair_attempts,
            "repair_outcome": repair_outcome,
            "attribution": Attribution(session_id=session_id, link_id=link_id) if session_id else None,
            "request_id": request_id,
            "current_step": current_step,
            "motion_window_selection": motion_window_selection,
            "stage_timings_ms": stage_timings_ms,
        }
    )
