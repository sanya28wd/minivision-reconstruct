"""Seam 1: the incident entry point.

Given an incident's views and an Origin it returns the finished Report data object and writes
one Run row. The web route and the future benchmark harness call only this function.
"""

from __future__ import annotations

import concurrent.futures
import os
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from contextvars import copy_context
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from threading import Event
from typing import Any, cast

from incident_report.agreement import (
    AgreementResult,
    AgreementSettings,
    AgreementStatus,
    FieldAgreement,
    agree_no_collision_violations,
    apply_contributing_agreement,
    compute_agreement,
    evaluate_majority,
    is_absent,
    party_canonical_key,
)
from incident_report.ai import AIConnection, AIConnectionError, AIRequest, AIViewRef
from incident_report.ai.dense import (
    DENSE_PROMPT_VERSION,
    CandidateWindow,
    DenseSettings,
    calculate_overview_collision_recall,
    close_up_confirmed_pairs,
    compute_windows,
    consolidate_facts,
    dense_confirmed_pairs,
    settle_collisions,
    union_windows,
)
from incident_report.ai.motion import detect_motion, select_windows
from incident_report.ai.overview11 import PROMPT_VERSION_11
from incident_report.config import ProcessingSettings
from incident_report.domain import (
    Attribution,
    DenseWindowRecord,
    InvalidIncident,
    MotionWindowSelection,
    Origin,
    Report,
    ReportView,
    Run,
    RunStatus,
    View,
)
from incident_report.facts import (
    ChecklistParty,
    CitedFact,
    CollisionObservation,
    DriverObservation,
    DrowsinessObservation,
    IncidentAssessment,
    ObservationNote,
    OverviewFacts,
    PartyFact,
    StructuredFact,
    TextReading,
    validate_citations,
)
from incident_report.hashing import incident_input_hash
from incident_report.intake import configured_store, receive_view
from incident_report.intake_limits import IntakeLimits
from incident_report.model_calls import ModelCall
from incident_report.rules import (
    FAULT_STATUS_COLORS,
    FaultStatus,
    PartyFault,
    PartyViolation,
    evaluate_fault,
    violations_from_faults,
)
from incident_report.rules.adapter import party_identities, situation_from_overview
from incident_report.rules.consistency import ConsistencyIssue, check_consistency
from incident_report.rules.engine import apply_consistency_issues
from incident_report.rules.referrals import evaluate_referrals, replace_fines_with_referrals
from incident_report.runlog import RunLog
from incident_report.storage import EvidenceStore
from incident_report.timings import StageTimings, capture_timings, timed_call

# Legacy synthetic recordings retain their prompt key. The live overview caller explicitly
# selects ai.gemini.PROMPT_VERSION; the web app stays replay-only until later intake/UI tickets.
PROMPT_VERSION = "skeleton-1"


def confirmed_pairs_for(
    prompt_version: str, facts: OverviewFacts, dense_facts: Sequence[OverviewFacts]
) -> frozenset[frozenset[str]]:
    """The pairs the close-up check confirmed. overview-11 never runs dense-2, so its own observations seen at a
    close-up rate (ADR 0008) play that part; older versions keep the dense-2 determinations unchanged."""
    if prompt_version == PROMPT_VERSION_11:
        return close_up_confirmed_pairs(facts)
    return dense_confirmed_pairs(dense_facts)


def _vote_stage_pairs(prompt_version: str, facts: OverviewFacts) -> dict[str, frozenset[frozenset[str]]]:
    """The close-up confirmation for the overview-stage evaluations that feed the agreement vote. Only overview-11 has
    one there (it has no dense-2 to confirm later, and the vote must see the same fault the final evaluation gives);
    older versions are called exactly as before."""
    if prompt_version == PROMPT_VERSION_11:
        return {"dense_confirmed_pairs": close_up_confirmed_pairs(facts)}
    return {}


def _run_with_timeout[T](func: Callable[[], T], timeout_seconds: float, stage_name: str) -> T:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(copy_context().run, func)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(f"The {stage_name} stage timed out.") from None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _run_ordered[T](functions: Sequence[Callable[[], T]], timeout: float, stage: str, enabled: bool) -> list[T]:
    """Submit in input order, bound each wait from submission, never join timed-out workers.

    Python 3.13 concurrent.futures: shutdown(wait=False) cannot stop running threads.
    Report file scopes reject subsequent requests and clean late uploads instead.
    """
    if not enabled or len(functions) < 2:
        return [_run_with_timeout(func, timeout, stage) for func in functions]
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(functions))
    stopped = Event()

    def invoke(func: Callable[[], T]) -> T:
        if stopped.is_set():
            raise concurrent.futures.CancelledError()
        try:
            return func()
        except Exception:
            stopped.set()
            raise

    futures = [(executor.submit(copy_context().run, invoke, func), time.monotonic() + timeout) for func in functions]
    try:
        results: list[T] = []
        error: Exception | None = None
        for future, deadline in futures:
            try:
                results.append(future.result(timeout=max(0.0, deadline - time.monotonic())))
            except concurrent.futures.TimeoutError:
                raise
            except Exception as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error
        return results
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"The {stage} stage timed out.") from None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _stage_snapshot(
    prefix: str, timings: StageTimings, started: float | None, finished: float | None
) -> dict[str, float]:
    values = timings.snapshot()
    result = {
        f"{prefix}.{name}": values.get(name, 0.0)
        for name in (
            "upload",
            "processing_wait",
            "generate",
            "parse",
            "validate",
            "repair",
            "recovery.generate",
            "recovery.repair",
            "retry_backoff",
            "retry_request",
        )
    }
    if started is not None:
        result[f"{prefix}.total"] = ((finished if finished is not None else time.perf_counter()) - started) * 1000
        result[f"{prefix}.incomplete"] = float(finished is None)
    return result


@dataclass(frozen=True)
class _OverviewRunResult:
    facts: OverviewFacts | None
    calls: tuple[ModelCall, ...]
    provenance: str
    summary: str
    is_valid: bool
    validation_errors: tuple[str, ...]
    repair_attempts: int
    repair_outcome: str | None
    overview_latency_ms: float
    validation_latency_ms: float
    repair_latency_ms: float


def _run_overview_stage(
    ai: AIConnection,
    report_views: Sequence[ReportView],
    input_hash: str,
    prompt_version: str,
    *,
    on_calls: Callable[[Sequence[ModelCall]], None] | None = None,
) -> _OverviewRunResult:
    """Run overview analysis stage (single-view or ADR 0003 per-view loop) with citation check & repair."""
    multi_view = len(report_views) > 1 and prompt_version != "skeleton-1"
    provenance: str
    summary: str
    t_overview_start = time.perf_counter()

    if multi_view:
        # ADR 0003: Each view is analysed independently before any comparison.
        all_calls: list[ModelCall] = []
        all_parties: list[PartyFact] = []
        all_events: list[CitedFact] = []
        all_collisions: list[CitedFact] = []
        all_behaviours: list[CitedFact] = []
        all_locations: list[CitedFact] = []
        all_structured: list[StructuredFact] = []
        all_text_readings: list[TextReading] = []
        all_driver_observations: list[DriverObservation] = []
        all_drowsiness_observations: list[DrowsinessObservation] = []
        all_collision_observations: list[CollisionObservation] = []
        all_checklist: list[ChecklistParty] = []
        all_notes: list[ObservationNote] = []
        all_assessments: list[IncidentAssessment] = []
        summaries: list[str] = []
        provenances: set[str] = set()
        total_repair_attempts = 0
        repair_outcomes: list[str] = []
        per_view_validation_errors: list[str] = []
        has_facts = False
        repair_ms = 0.0

        for v in report_views:
            v_ref = AIViewRef(v.role, v.sha256, v.duration_seconds)
            v_req = AIRequest(
                input_hash=incident_input_hash([(v.role, v.sha256)]),
                prompt_version=prompt_version,
                views=(v_ref,),
            )
            v_resp = ai.analyse(v_req)
            all_calls.extend(v_resp.calls)
            if on_calls is not None:
                on_calls(v_resp.calls)
            if v_resp.summary:
                summaries.append(v_resp.summary)
            if v_resp.provenance:
                provenances.add(v_resp.provenance)
            v_facts = v_resp.facts
            if v_facts is not None:
                has_facts = True
                v_durations = {v.sha256: v.duration_seconds} if v.duration_seconds is not None else {}
                v_val = timed_call("validate", validate_citations, v_facts, [v], view_durations=v_durations)
                if not v_val.is_valid:
                    if hasattr(ai, "repair") and callable(ai.repair):
                        total_repair_attempts += 1
                        t_repair_start = time.perf_counter()
                        try:
                            v_repaired = timed_call("repair", ai.repair, v_req, v_resp, v_val.errors)
                            new_repair_calls = [c for c in v_repaired.calls if c not in all_calls]
                            all_calls.extend(new_repair_calls)
                            if on_calls is not None:
                                on_calls(new_repair_calls)
                            if v_repaired.facts is not None:
                                v_recheck = timed_call(
                                    "validate", validate_citations, v_repaired.facts, [v], view_durations=v_durations
                                )
                                if v_recheck.is_valid:
                                    v_facts = v_repaired.facts
                                    repair_outcomes.append("repaired")
                                else:
                                    v_facts = v_repaired.facts
                                    repair_outcomes.append("failed")
                                    per_view_validation_errors.extend(v_recheck.errors)
                            else:
                                repair_outcomes.append("failed")
                                per_view_validation_errors.extend(v_val.errors)
                        except Exception:
                            repair_outcomes.append("failed")
                            per_view_validation_errors.extend(v_val.errors)
                        repair_ms += (time.perf_counter() - t_repair_start) * 1000
                    else:
                        per_view_validation_errors.extend(v_val.errors)
                all_parties.extend(v_facts.parties)
                all_events.extend(v_facts.events)
                all_collisions.extend(v_facts.candidate_collisions)
                all_behaviours.extend(v_facts.behaviour_observations)
                all_locations.extend(v_facts.location_conditions)
                all_structured.extend(v_facts.structured_facts)
                all_text_readings.extend(v_facts.text_readings)
                all_driver_observations.extend(v_facts.driver_observations)
                all_drowsiness_observations.extend(v_facts.drowsiness_observations)
                all_collision_observations.extend(v_facts.collision_observations)
                all_checklist.extend(v_facts.party_checklist)
                all_notes.extend(v_facts.observation_notes)
                all_assessments.extend(v_facts.incident_assessments)

        repair_attempts = total_repair_attempts
        repair_outcome = (
            "repaired"
            if repair_outcomes and all(o == "repaired" for o in repair_outcomes)
            else ("failed" if repair_outcomes else None)
        )
        facts = (
            OverviewFacts(
                parties=all_parties,
                events=all_events,
                candidate_collisions=all_collisions,
                behaviour_observations=all_behaviours,
                location_conditions=all_locations,
                structured_facts=all_structured,
                text_readings=all_text_readings,
                driver_observations=all_driver_observations,
                drowsiness_observations=all_drowsiness_observations,
                collision_observations=all_collision_observations,
                party_checklist=all_checklist,
                observation_notes=all_notes,
                incident_assessments=all_assessments,
            )
            if has_facts
            else None
        )
        provenance = ", ".join(sorted(provenances)) if provenances else "unknown"
        summary = " ".join(summaries)
        overview_latency_ms = (time.perf_counter() - t_overview_start) * 1000

        t_val_start = time.perf_counter()
        is_valid = True
        validation_errors: tuple[str, ...] = ()
        if facts is not None:
            view_durations = {v.sha256: v.duration_seconds for v in report_views if v.duration_seconds is not None}
            val_res = timed_call("validate", validate_citations, facts, report_views, view_durations=view_durations)
            combined_errors = tuple(per_view_validation_errors) + tuple(
                e for e in val_res.errors if e not in per_view_validation_errors
            )
            is_valid = val_res.is_valid and not per_view_validation_errors
            validation_errors = combined_errors
        validation_latency_ms = (time.perf_counter() - t_val_start) * 1000

        return _OverviewRunResult(
            facts=facts,
            calls=tuple(all_calls),
            provenance=provenance,
            summary=summary,
            is_valid=is_valid,
            validation_errors=validation_errors,
            repair_attempts=repair_attempts,
            repair_outcome=repair_outcome,
            overview_latency_ms=overview_latency_ms,
            validation_latency_ms=validation_latency_ms,
            repair_latency_ms=repair_ms,
        )

    else:
        req = AIRequest(
            input_hash=input_hash,
            prompt_version=prompt_version,
            views=tuple(AIViewRef(v.role, v.sha256, v.duration_seconds) for v in report_views),
        )
        response = ai.analyse(req)
        calls_list = list(response.calls)
        if on_calls is not None:
            on_calls(response.calls)
        facts = response.facts
        provenance = response.provenance
        summary = response.summary
        overview_latency_ms = (time.perf_counter() - t_overview_start) * 1000

        t_val_start = time.perf_counter()
        is_valid = True
        validation_errors = ()
        repair_attempts = 0
        repair_outcome = None
        repair_ms = 0.0

        if facts is not None:
            view_durations = {v.sha256: v.duration_seconds for v in report_views if v.duration_seconds is not None}
            val_res = timed_call("validate", validate_citations, facts, report_views, view_durations=view_durations)
            if not val_res.is_valid:
                repair_attempts = 1
                t_repair_start = time.perf_counter()
                if hasattr(ai, "repair") and callable(ai.repair):
                    try:
                        repaired_response = timed_call("repair", ai.repair, req, response, val_res.errors)
                        new_repair_calls = [c for c in repaired_response.calls if c not in calls_list]
                        calls_list.extend(new_repair_calls)
                        if on_calls is not None:
                            on_calls(new_repair_calls)
                        if repaired_response.facts is not None:
                            recheck = timed_call(
                                "validate",
                                validate_citations,
                                repaired_response.facts,
                                report_views,
                                view_durations=view_durations,
                            )
                            if recheck.is_valid:
                                facts = repaired_response.facts
                                repair_outcome = "repaired"
                                is_valid = True
                                validation_errors = ()
                            else:
                                facts = repaired_response.facts
                                repair_outcome = "failed"
                                is_valid = False
                                validation_errors = recheck.errors
                        else:
                            repair_outcome = "failed"
                            is_valid = False
                            validation_errors = ("Repaired output contained no facts",)
                    except Exception:
                        repair_outcome = "failed"
                        is_valid = False
                        validation_errors = val_res.errors
                else:
                    repair_outcome = "failed"
                    is_valid = False
                    validation_errors = val_res.errors
                repair_ms = (time.perf_counter() - t_repair_start) * 1000

        validation_latency_ms = (time.perf_counter() - t_val_start) * 1000
        return _OverviewRunResult(
            facts=facts,
            calls=tuple(calls_list),
            provenance=provenance,
            summary=summary,
            is_valid=is_valid,
            validation_errors=validation_errors,
            repair_attempts=repair_attempts,
            repair_outcome=repair_outcome,
            overview_latency_ms=overview_latency_ms,
            validation_latency_ms=validation_latency_ms,
            repair_latency_ms=repair_ms,
        )


def _append_cited_facts_to_events(
    facts: OverviewFacts,
    citations: Sequence[CitedFact],
) -> OverviewFacts:
    """Append citations to facts.events, de-duplicating across all fact groups (RPT-23 R3).

    Checks facts.events, facts.candidate_collisions, and facts.parties so a fact
    already present in any fact group is not duplicated into events.
    """
    if not citations:
        return facts

    all_existing_facts: list[CitedFact] = list(facts.events) + list(facts.candidate_collisions) + list(facts.parties)
    new_events = list(facts.events)

    for cf in citations:
        already_present = any(
            f.view_id == cf.view_id
            and abs(f.timestamp_seconds - cf.timestamp_seconds) < 0.01
            and f.description == cf.description
            for f in all_existing_facts
        )
        if not already_present:
            new_events.append(cf)
            all_existing_facts.append(cf)

    return facts.model_copy(update={"events": new_events})


def _selection_with_agreement(
    existing: MotionWindowSelection | None,
    windows: Sequence[CandidateWindow],
    dropped_agreement: Sequence[CandidateWindow],
) -> MotionWindowSelection:
    """Record the dense windows and cap drops, with run provenance; motion maps stay empty without motion."""

    def records(items: Sequence[CandidateWindow]) -> tuple[DenseWindowRecord, ...]:
        return tuple(DenseWindowRecord.model_validate(w, from_attributes=True) for w in items)

    return MotionWindowSelection(
        selected=records(windows),
        dropped=(existing.dropped if existing else ()) + records(dropped_agreement),
        candidates=existing.candidates if existing else {},
        cuts=existing.cuts if existing else {},
        camera_shake=existing.camera_shake if existing else {},
        detector_ms=existing.detector_ms if existing else {},
    )


def _flag_agreement_confirmed_collisions(
    agreement: AgreementResult,
    extras: Sequence[tuple[int, CitedFact]],
    windows: Sequence[CandidateWindow],
    n_runs: int,
) -> AgreementResult:
    """Mark each contact only non-primary runs saw, and dense confirmed, as FLAGGED with a plain note."""
    if not extras:
        return agreement
    fields = dict(agreement.fields)
    for index, collision in extras:
        window = windows[index]
        runs = tuple(r + 1 for r in window.proposed_by_runs)
        who = f"run {runs[0]}" if len(runs) == 1 else "runs " + ", ".join(str(r) for r in runs)
        note = (
            "Confirmed by the close-up check."
            if n_runs == 1
            else f"Seen by {who} of {n_runs}, confirmed by the close-up check."
        )
        old_key = next(
            (
                key
                for key, field in fields.items()
                if key.startswith("collision:@")
                and field.values
                and is_absent(field.values[0])
                and window.start_seconds <= float(key[len("collision:@") : -1]) <= window.end_seconds
            ),
            None,
        )
        old = fields.pop(old_key) if old_key else None
        key = f"collision:@{collision.timestamp_seconds:.1f}s"
        fields[key] = FieldAgreement(
            field_name=key,
            status=AgreementStatus.FLAGGED,
            agreed_value=collision.description,
            runs_total=n_runs,
            runs_agreeing=old.runs_agreeing if old else len(runs),
            details=note,
            values=old.values if old else [],
        )
    status = agreement.status
    if status in (AgreementStatus.TRUSTED, AgreementStatus.NOT_MEASURED):
        status = AgreementStatus.FLAGGED
    return agreement.model_copy(update={"fields": fields, "status": status})


def analyse_incident(
    views: Sequence[View | ReportView],
    origin: Origin,
    *,
    ai: AIConnection,
    run_log: RunLog,
    dense_ai: Any | None = None,
    prompt_version: str = PROMPT_VERSION,
    dense_prompt_version: str = DENSE_PROMPT_VERSION,
    dense_settings: DenseSettings | None = None,
    ground_truth_collisions: Sequence[float | CitedFact] | None = None,
    evidence: EvidenceStore | None = None,
    limits: IntakeLimits | None = None,
    attribution: Attribution | None = None,
    request_id: uuid.UUID | None = None,
    agreement_settings: AgreementSettings | None = None,
    officer_question: str | None = None,
    run_id: uuid.UUID | None = None,
    on_step: Callable[[str], None] | None = None,
    processing_settings: ProcessingSettings | None = None,
) -> Report:
    """Analyse one incident and log the Run.

    Raises InvalidIncident (no Run written) when there are no views. Once the views are
    fingerprinted, every outcome writes exactly one Run row: status "succeeded" with the final
    Report, or status "failed" with a plain error and no report (never partial output). The
    exception is then re-raised. The logged latency covers fingerprinting through Report
    assembly, not the log write itself.
    """
    if not views:
        raise InvalidIncident("An incident needs at least one view.")

    run_id = run_id if run_id is not None else uuid.uuid4()
    timeouts = processing_settings if processing_settings is not None else ProcessingSettings.from_env()
    created_at = datetime.now(UTC)
    started = time.perf_counter()
    stage_latencies_ms: dict[str, float] = {}

    if on_step:
        on_step("uploading")

    store = evidence if evidence is not None else configured_store()
    # RPT-09's anonymous session (request.state.attribution) is the intake record's session.
    session = str(attribution.session_id) if attribution else str(uuid.uuid4())
    intake_limits = limits if limits is not None else IntakeLimits.from_env()
    t_intake_start = time.perf_counter()
    report_views: tuple[ReportView, ...]
    if all(isinstance(v, ReportView) for v in views):
        report_views = tuple(v for v in views if isinstance(v, ReportView))
    else:
        accepted: list[ReportView] = []
        try:
            for number, view in enumerate(views):
                if isinstance(view, ReportView):
                    accepted.append(view)
                    continue
                accepted_view = receive_view(
                    view,
                    store,
                    session_id=session,
                    limits=intake_limits,
                    record=partial(run_log.record_intake_view, run_id, number),
                )
                accepted.append(accepted_view)
                run_log.record_upload_audit(
                    run_id=run_id,
                    view_number=number,
                    view=accepted_view,
                    attribution=attribution,
                    request_id=request_id,
                )
        except InvalidIncident as exc:
            exc.intake_id = run_id
            raise
        report_views = tuple(accepted)
    input_hash = incident_input_hash((v.role, v.sha256) for v in report_views)
    stage_latencies_ms["intake"] = (time.perf_counter() - t_intake_start) * 1000

    calls_accumulator: list[ModelCall] = []
    repair_attempts = 0
    repair_outcome: str | None = None
    motion_selection: MotionWindowSelection | None = None

    def log(status: RunStatus, report: Report | None, error: str | None, current_step: str | None = None) -> None:
        calls = tuple(calls_accumulator)
        run_log.record(
            Run(
                run_id=run_id,
                origin=origin,
                created_at=created_at,
                input_hash=input_hash,
                report=report,
                status=status,
                total_latency_ms=(time.perf_counter() - started) * 1000,
                error=error,
                calls=calls,
                retry_count=sum(call.retry_index > 0 for call in calls),
                repair_attempts=repair_attempts,
                repair_outcome=repair_outcome,
                attribution=attribution,
                request_id=request_id,
                current_step=current_step,
                motion_window_selection=motion_selection,
                stage_timings_ms=dict(stage_latencies_ms),
            )
        )

    file_scopes = ExitStack()
    try:
        seen_transports: set[int] = set()
        for connection in (ai, dense_ai, getattr(ai, "dense_connection", None)):
            transport = getattr(connection, "transport", None)
            scope = getattr(transport, "report_scope", None)
            if callable(scope) and id(transport) not in seen_transports:
                file_scopes.enter_context(scope())
                seen_transports.add(id(transport))
        if on_step:
            on_step("fingerprinting")
        run_log.update_step(run_id, "fingerprinting")

        if officer_question is not None and officer_question.strip():
            run_log.record_officer_question(run_id, officer_question.strip())

        eff_agreement = agreement_settings if agreement_settings is not None else AgreementSettings.from_env()
        n_runs = eff_agreement.n_runs

        log(RunStatus.PROCESSING, None, None, current_step="fingerprinting")

        if on_step:
            on_step("overview")
        run_log.update_step(run_id, "overview")

        overview_calls: list[list[ModelCall]] = [[] for _ in range(n_runs)]
        overview_timings = [StageTimings() for _ in range(n_runs)]
        overview_started: list[float | None] = [None] * n_runs
        overview_finished: list[float | None] = [None] * n_runs

        def overview_run(index: int) -> _OverviewRunResult:
            with capture_timings(overview_timings[index]):
                overview_started[index] = time.perf_counter()
                try:
                    return _run_overview_stage(
                        ai, report_views, input_hash, prompt_version, on_calls=overview_calls[index].extend
                    )
                except AIConnectionError as exc:
                    for call in exc.calls:
                        if call not in overview_calls[index]:
                            overview_calls[index].append(call)
                    raise
                finally:
                    overview_finished[index] = time.perf_counter()

        t_overviews = time.perf_counter()
        try:
            overview_runs = _run_ordered(
                [partial(overview_run, index) for index in range(n_runs)],
                timeouts.overview_timeout_seconds,
                "overview",
                timeouts.concurrent_analysis,
            )
        finally:
            for index, bucket in enumerate(overview_calls):
                calls_accumulator.extend(tuple(bucket))
                stage_latencies_ms.update(
                    _stage_snapshot(
                        f"overview.run.{index}",
                        overview_timings[index],
                        overview_started[index],
                        overview_finished[index],
                    )
                )
            stage_latencies_ms["overviews_wall"] = (time.perf_counter() - t_overviews) * 1000
        primary_overview = overview_runs[0]
        for index, result in enumerate(overview_runs):
            stage_latencies_ms[f"overview.run.{index}.validation"] = result.validation_latency_ms
            stage_latencies_ms[f"overview.run.{index}.citation_repair"] = result.repair_latency_ms
        facts = primary_overview.facts
        provenance = primary_overview.provenance
        summary = primary_overview.summary
        is_valid = primary_overview.is_valid
        validation_errors = primary_overview.validation_errors
        repair_attempts = primary_overview.repair_attempts
        repair_outcome = primary_overview.repair_outcome

        stage_latencies_ms["overview"] = primary_overview.overview_latency_ms
        stage_latencies_ms["validation"] = primary_overview.validation_latency_ms
        if primary_overview.repair_attempts:
            stage_latencies_ms["repair"] = primary_overview.repair_latency_ms

        overview_consistency_issues = tuple(
            check_consistency(
                run.facts, {v.sha256: v.duration_seconds for v in report_views if v.duration_seconds is not None}
            )
            if run.facts is not None
            else ()
            for run in overview_runs
        )

        primary_overview_faults = (
            evaluate_fault(
                primary_overview.facts,
                **_vote_stage_pairs(prompt_version, primary_overview.facts),
                consistency_issues=overview_consistency_issues[0],
            )
            if primary_overview.is_valid and primary_overview.facts is not None
            else ()
        )

        if on_step:
            on_step("agreement")
        run_log.update_step(run_id, "agreement")

        t_agreement_start = time.perf_counter()
        additional_runs_facts: list[OverviewFacts] = []
        additional_runs_faults: list[Sequence[PartyFault]] = []

        if n_runs > 1:
            for run_index, sub_run in enumerate(overview_runs[1:], start=1):
                if sub_run.facts is not None:
                    additional_runs_facts.append(sub_run.facts)
                    if sub_run.is_valid:
                        sub_faults = evaluate_fault(
                            sub_run.facts,
                            **_vote_stage_pairs(prompt_version, sub_run.facts),
                            consistency_issues=overview_consistency_issues[run_index],
                        )
                    else:
                        sub_faults = ()
                    additional_runs_faults.append(sub_faults)
                else:
                    additional_runs_facts.append(
                        OverviewFacts(
                            behaviour_observations=[],
                            location_conditions=[],
                            parties=[],
                            events=[],
                            candidate_collisions=[],
                        )
                    )
                    additional_runs_faults.append(())

        # Preserve every requested position, including a missing primary analysis.
        # The close-up helper also relies on values[0] continuing to mean the primary.
        primary_vote_facts = (
            primary_overview.facts
            if primary_overview.facts is not None
            else OverviewFacts(
                behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=[]
            )
        )
        all_overview_facts = [primary_vote_facts] + additional_runs_facts
        all_overview_faults = [primary_overview_faults] + additional_runs_faults
        all_overview_issues = overview_consistency_issues

        agreement_res = compute_agreement(
            all_overview_facts,
            all_overview_faults,
            eff_agreement,
            call_count=len(calls_accumulator),
            consistency_issues=all_overview_issues,
        )
        agreement_compute_ms = (time.perf_counter() - t_agreement_start) * 1000
        stage_latencies_ms["agreement_compute"] = agreement_compute_ms
        # Preserve the historical extra-run work metric; overviews_wall measures elapsed time.
        agreement_ms = agreement_compute_ms + sum(
            stage_latencies_ms[f"overview.run.{index}.total"] for index in range(1, n_runs)
        )
        stage_latencies_ms["agreement"] = agreement_ms
        agreement_res = agreement_res.model_copy(update={"total_latency_ms": agreement_ms})

        overview_candidates = list(facts.candidate_collisions) if facts is not None else []
        dense_facts_list: list[OverviewFacts] = []
        motion_enabled = os.environ.get("INCIDENT_MOTION_WINDOWS") == "1"

        # Dense collision pass: runs only on candidate windows (ADR 0008).
        if on_step:
            on_step("collision check")
        run_log.update_step(run_id, "collision check")

        t_dense_start = time.perf_counter()

        dense_calls_buckets: list[list[ModelCall]] = []
        dense_timings: list[StageTimings] = []
        dense_started: list[float | None] = []
        dense_finished: list[float | None] = []
        overview_for_dense = facts
        dense_windows_used: list[CandidateWindow] = []
        # Overview runs that produced valid facts other than the primary (run 0): their contacts get
        # a window too (RPT-36 T5). Contacts they share with the primary are covered by its windows.
        agreement_candidates = [
            (index, list(run.facts.candidate_collisions))
            for index, run in enumerate(overview_runs)
            if index > 0 and run.is_valid and run.facts is not None and run.facts.candidate_collisions
        ]

        def _do_dense() -> tuple[OverviewFacts | None, list[OverviewFacts], MotionWindowSelection | None]:
            local_facts = overview_for_dense
            local_dense_facts: list[OverviewFacts] = []
            local_motion_selection = None
            effective_dense_ai = dense_ai
            if effective_dense_ai is None:
                if hasattr(ai, "analyse_window"):
                    effective_dense_ai = ai
                elif hasattr(ai, "dense_connection"):
                    effective_dense_ai = ai.dense_connection
            if (
                local_facts is not None
                and (local_facts.candidate_collisions or motion_enabled or agreement_candidates)
                and effective_dense_ai is not None
            ):
                view_durations = {v.sha256: v.duration_seconds for v in report_views if v.duration_seconds is not None}
                eff_dense_settings = dense_settings if dense_settings is not None else DenseSettings.from_env()
                windows = compute_windows(
                    local_facts.candidate_collisions, eff_dense_settings, view_durations=view_durations
                )
                if motion_enabled:
                    detections = {}
                    detector_ms = {}
                    for view in report_views:
                        detector_start = time.perf_counter()
                        with store.get(view.sha256) as media:
                            detections[view.sha256] = detect_motion(media, stage=store.stage)
                        detector_ms[view.sha256] = (time.perf_counter() - detector_start) * 1000
                    candidates = {key: detection.candidates for key, detection in detections.items()}
                    windows, dropped = select_windows(windows, candidates, eff_dense_settings, view_durations)
                    local_motion_selection = MotionWindowSelection(
                        selected=tuple(DenseWindowRecord.model_validate(w, from_attributes=True) for w in windows),
                        dropped=tuple(DenseWindowRecord.model_validate(w, from_attributes=True) for w in dropped),
                        candidates=candidates,
                        cuts={key: detection.cuts for key, detection in detections.items()},
                        camera_shake={key: detection.camera_shake for key, detection in detections.items()},
                        detector_ms=detector_ms,
                    )
                dropped_agreement: tuple[CandidateWindow, ...] = ()
                if agreement_candidates:
                    windows, dropped_agreement = union_windows(
                        windows, agreement_candidates, eff_dense_settings, view_durations
                    )
                if (
                    local_motion_selection is not None
                    or dropped_agreement
                    or any(w.source == "agreement_run" for w in windows)
                ):
                    local_motion_selection = _selection_with_agreement(
                        local_motion_selection, windows, dropped_agreement
                    )
                dense_windows_used.extend(windows)
                dense_req = AIRequest(
                    input_hash=input_hash,
                    prompt_version=dense_prompt_version,
                    views=tuple(AIViewRef(v.role, v.sha256, v.duration_seconds) for v in report_views),
                )
                dense_calls_buckets.extend([] for _ in windows)
                dense_timings.extend(StageTimings() for _ in windows)
                dense_started.extend(None for _ in windows)
                dense_finished.extend(None for _ in windows)
                overview_party_descriptions = tuple(p.description for p in local_facts.parties)

                def dense_window(index: int) -> OverviewFacts:
                    with capture_timings(dense_timings[index]):
                        dense_started[index] = time.perf_counter()
                        try:
                            window = replace(windows[index], overview_party_descriptions=overview_party_descriptions)
                            result, calls = effective_dense_ai.analyse_window(dense_req, window)
                            dense_calls_buckets[index].extend(calls)
                            return cast(OverviewFacts, result)
                        except AIConnectionError as exc:
                            dense_calls_buckets[index].extend(exc.calls)
                            raise
                        finally:
                            dense_finished[index] = time.perf_counter()

                local_dense_facts = _run_ordered(
                    [partial(dense_window, index) for index in range(len(windows))],
                    timeouts.collision_timeout_seconds,
                    "collision check",
                    timeouts.concurrent_analysis,
                )
                if local_dense_facts:
                    local_facts = consolidate_facts(
                        local_facts,
                        local_dense_facts,
                        agreement_only=frozenset(i for i, w in enumerate(windows) if w.source == "agreement_run"),
                    )
            return local_facts, local_dense_facts, local_motion_selection

        if prompt_version == PROMPT_VERSION_11:
            # overview-11 never runs dense-2: its recovery request plays that role (spec 003 ticket 8). Recorded as
            # skipped, not as 0 ms of work.
            motion_selection = None
            stage_latencies_ms["dense_skipped"] = 1.0
        else:
            try:
                facts, dense_facts_list, motion_selection = _run_with_timeout(
                    _do_dense, timeouts.collision_timeout_seconds, "collision check"
                )
            finally:
                for index, bucket in enumerate(tuple(dense_calls_buckets)):
                    calls_accumulator.extend(tuple(bucket))
                    if index < len(dense_timings) and index < len(dense_started) and index < len(dense_finished):
                        stage_latencies_ms.update(
                            _stage_snapshot(
                                f"dense.window.{index}",
                                dense_timings[index],
                                dense_started[index],
                                dense_finished[index],
                            )
                        )
                stage_latencies_ms["dense"] = (time.perf_counter() - t_dense_start) * 1000

        if overview_for_dense is not None and dense_facts_list:
            _, agreement_extras = settle_collisions(
                overview_for_dense,
                dense_facts_list,
                frozenset(i for i, w in enumerate(dense_windows_used) if w.source == "agreement_run"),
            )
            agreement_res = _flag_agreement_confirmed_collisions(
                agreement_res, agreement_extras, dense_windows_used, n_runs
            )

        # Post-dense validation: Validate consolidated facts (ADR 0005, ADR 0006).
        # Any bad fact from dense pass (or combined facts) must mark the report invalid.
        if on_step:
            on_step("checks")
        run_log.update_step(run_id, "checks")

        t_val_post = time.perf_counter()

        def _do_checks() -> None:
            nonlocal is_valid, validation_errors
            if facts is not None:
                view_durations = {v.sha256: v.duration_seconds for v in report_views if v.duration_seconds is not None}
                # Validate the raw dense evidence too, including observations kept out of
                # rule-facing facts because their subjects have no exact overview match.
                for dense_fact in dense_facts_list:
                    dense_validation = validate_citations(dense_fact, report_views, view_durations=view_durations)
                    if not dense_validation.is_valid:
                        is_valid = False
                        validation_errors = dense_validation.errors
                        return
                val_res = timed_call("validate", validate_citations, facts, report_views, view_durations=view_durations)
                if not val_res.is_valid:
                    is_valid = False
                    validation_errors = val_res.errors
                elif not is_valid:
                    # Overview was invalid and repair did not succeed
                    is_valid = False
                else:
                    is_valid = True
                    validation_errors = ()

        _run_with_timeout(_do_checks, timeouts.checks_timeout_seconds, "checks")
        stage_latencies_ms["checks"] = (time.perf_counter() - t_val_post) * 1000
        stage_latencies_ms["validation"] = stage_latencies_ms.get("validation", 0.0) + stage_latencies_ms["checks"]

        # Overview collision recall metric: empty when ground truth is not provided
        overview_collision_recall = None
        if ground_truth_collisions is not None and overview_candidates:
            overview_collision_recall = calculate_overview_collision_recall(
                overview_candidates, ground_truth_collisions
            )

        if on_step:
            on_step("rules")
        run_log.update_step(run_id, "rules")

        t_rules_start = time.perf_counter()
        party_faults: tuple[PartyFault, ...] = ()
        violations: tuple[PartyViolation, ...] = ()

        consistency_issues: tuple[ConsistencyIssue, ...] = ()

        def _do_rules() -> None:
            nonlocal facts, party_faults, violations, consistency_issues
            if facts is not None:
                consistency_issues = check_consistency(
                    facts, {v.sha256: v.duration_seconds for v in report_views if v.duration_seconds is not None}
                )
            if facts is not None and is_valid:
                party_faults = evaluate_fault(
                    facts,
                    dense_confirmed_pairs=confirmed_pairs_for(prompt_version, facts, dense_facts_list),
                    consistency_issues=consistency_issues,
                )

            # Agreement may supply a decided result from a plain two-party overview.
            # Keep the final selected-pair guard authoritative without changing votes.
            guarded_pair_faults = {
                pf.party_description: pf
                for pf in party_faults
                if pf.pair_selection is not None
                and (
                    not pf.pair_selection.corroborated
                    or pf.party_description not in {pf.pair_selection.subject, pf.pair_selection.other_party}
                )
            }
            updated_parties: list[PartyFault] = []
            for pf in party_faults:
                p_fact = (
                    next((p for p in facts.parties if p.description == pf.party_description), None) if facts else None
                )
                p_key = party_canonical_key(p_fact) if p_fact else party_canonical_key(pf.party_description)
                field_name = f"fault_status:{p_key}"
                fa = agreement_res.fields.get(field_name)
                if fa is None:
                    fa = agreement_res.fields.get(f"fault_status:{pf.party_description}")
                if fa is None and n_runs > 1:
                    fa = evaluate_majority([], field_name=field_name, n_runs=n_runs)
                if fa is None or n_runs == 1:
                    updated_parties.append(
                        pf.model_copy(
                            update={
                                "agreement_status": "not_measured" if n_runs == 1 else "trusted",
                                "agreement_detail": fa.details if fa else None,
                            }
                        )
                    )
                elif fa.status == AgreementStatus.CANNOT_DETERMINE:
                    # Split -> Cannot Determine (ADR 0004, RPT-23)
                    updated_parties.append(
                        pf.model_copy(
                            update={
                                "status": FaultStatus.CANNOT_DETERMINE,
                                "color": "grey",
                                "rule_id": None,
                                "legal_source": None,
                                "article": None,
                                "cited_facts": (),
                                "reason": (
                                    fa.details
                                    if sum(not is_absent(v) for v in fa.values) <= fa.runs_total / 2
                                    else f"Runs disagreed on fault status ({fa.details}); Cannot Determine (RPT-23)."
                                ),
                                "agreement_status": "cannot_determine",
                                "agreement_detail": fa.details,
                            }
                        )
                    )
                elif fa.status == AgreementStatus.TRUSTED:
                    if isinstance(fa.agreed_value, (tuple, list)) and len(fa.agreed_value) == 2:
                        agreed_status_val, agreed_rule_id = fa.agreed_value
                        agreed_status = (
                            FaultStatus(agreed_status_val)
                            if agreed_status_val in [f.value for f in FaultStatus]
                            else None
                        )
                    elif isinstance(fa.agreed_value, str) and fa.agreed_value in [f.value for f in FaultStatus]:
                        agreed_status = FaultStatus(fa.agreed_value)
                        agreed_rule_id = None
                    else:
                        agreed_status = None
                        agreed_rule_id = None

                    if (pf.status, pf.rule_id) != (agreed_status, agreed_rule_id):
                        # R1: The vote uses overview-stage faults, but the final status and fine come
                        # from the dense-consolidated facts. If the final (status, rule_id) differs
                        # from the voted value, downgrade: status -> Cannot Determine (no fine) with
                        # a reason naming the mismatch.
                        voted_desc = f"{agreed_status.value if agreed_status else 'Unknown'}" + (
                            f" ({agreed_rule_id})" if agreed_rule_id else ""
                        )
                        final_desc = f"{pf.status.value}" + (f" ({pf.rule_id})" if pf.rule_id else "")
                        updated_parties.append(
                            pf.model_copy(
                                update={
                                    "status": FaultStatus.CANNOT_DETERMINE,
                                    "color": "grey",
                                    "rule_id": None,
                                    "legal_source": None,
                                    "article": None,
                                    "cited_facts": (),
                                    "reason": (
                                        f"Dense-consolidated fault {final_desc} differs from voted overview "
                                        f"agreement {voted_desc}; Cannot Determine (RPT-23 R1)."
                                    ),
                                    "agreement_status": "cannot_determine",
                                    "agreement_detail": f"Final evaluation differs from voted agreement ({fa.details}).",
                                }
                            )
                        )
                    else:
                        updated_parties.append(
                            pf.model_copy(
                                update={
                                    "agreement_status": "trusted",
                                    "agreement_detail": fa.details,
                                }
                            )
                        )
                elif fa.status == AgreementStatus.FLAGGED:
                    # Majority agreed -> flagged (amber Check badge in report)
                    if isinstance(fa.agreed_value, (tuple, list)) and len(fa.agreed_value) == 2:
                        agreed_status_val, agreed_rule_id = fa.agreed_value
                        agreed_status = (
                            FaultStatus(agreed_status_val)
                            if agreed_status_val in [f.value for f in FaultStatus]
                            else pf.status
                        )
                    else:
                        agreed_status = (
                            FaultStatus(fa.agreed_value)
                            if fa.agreed_value in [f.value for f in FaultStatus]
                            else pf.status
                        )
                        agreed_rule_id = pf.rule_id

                    matching_pf = next(
                        (
                            p
                            for run_idx, faults in enumerate(all_overview_faults)
                            for p in faults
                            if (
                                party_canonical_key(
                                    next(
                                        (
                                            pfct
                                            for pfct in all_overview_facts[run_idx].parties
                                            if pfct.description == p.party_description
                                        ),
                                        None,
                                    )
                                    if run_idx < len(all_overview_facts)
                                    else p.party_description
                                )
                                == p_key
                            )
                            and p.status == agreed_status
                            and (agreed_rule_id is None or p.rule_id == agreed_rule_id)
                            and p.cited_facts
                        ),
                        None,
                    )

                    if agreed_status != FaultStatus.CANNOT_DETERMINE and (
                        not matching_pf or not matching_pf.cited_facts
                    ):
                        # ADR 0006 guard: a decided status must never have empty cited_facts.
                        updated_parties.append(
                            pf.model_copy(
                                update={
                                    "status": FaultStatus.CANNOT_DETERMINE,
                                    "color": "grey",
                                    "rule_id": None,
                                    "legal_source": None,
                                    "article": None,
                                    "cited_facts": (),
                                    "reason": "Fault status cannot be decided: agreeing majority provided no cited facts (ADR 0006).",
                                    "agreement_status": "flagged",
                                    "agreement_detail": fa.details,
                                }
                            )
                        )
                    elif agreed_status != FaultStatus.CANNOT_DETERMINE:
                        expected_color = FAULT_STATUS_COLORS.get(agreed_status, "white")
                        cited = matching_pf.cited_facts if matching_pf else pf.cited_facts
                        reason = matching_pf.reason if matching_pf else pf.reason
                        rule_id = agreed_rule_id or (matching_pf.rule_id if matching_pf else pf.rule_id)
                        legal_source = matching_pf.legal_source if matching_pf else pf.legal_source
                        article = matching_pf.article if matching_pf else pf.article

                        if facts is not None and matching_pf is not None:
                            facts = _append_cited_facts_to_events(facts, matching_pf.cited_facts)

                        updated_parties.append(
                            pf.model_copy(
                                update={
                                    "status": agreed_status,
                                    "color": expected_color,
                                    "cited_facts": cited,
                                    "reason": reason,
                                    "rule_id": rule_id,
                                    "legal_source": legal_source,
                                    "article": article,
                                    "agreement_status": "flagged",
                                    "agreement_detail": fa.details,
                                }
                            )
                        )
                    else:
                        keep_reason = pf.status == FaultStatus.CANNOT_DETERMINE and bool(pf.reason)
                        updated_parties.append(
                            pf.model_copy(
                                update={
                                    "status": FaultStatus.CANNOT_DETERMINE,
                                    "color": "grey",
                                    "rule_id": None,
                                    "legal_source": None,
                                    "article": None,
                                    "cited_facts": pf.cited_facts if keep_reason else (),
                                    "reason": (
                                        f"Runs agreed on Cannot Determine ({fa.details}). Reason: {pf.reason}"
                                        if keep_reason
                                        else f"Runs agreed on Cannot Determine ({fa.details})."
                                    ),
                                    "agreement_status": "flagged",
                                    "agreement_detail": fa.details,
                                }
                            )
                        )
            updated_parties = [
                guarded_pair_faults[pf.party_description].model_copy(
                    update={"agreement_status": pf.agreement_status, "agreement_detail": pf.agreement_detail}
                )
                if pf.party_description in guarded_pair_faults
                else pf
                for pf in updated_parties
            ]
            party_faults = apply_consistency_issues(
                apply_contributing_agreement(updated_parties, all_overview_faults),
                consistency_issues,
                party_keys=tuple(party_identities(facts).sources) if facts is not None else (),
            )

            # Collision fines follow agreement-checked faults (RPT-17 x RPT-23).
            # Standalone typed breaches (red crossing without collision, phone, seatbelt) have their
            # own agreement vote (RPT-26/30) and are added with or without a collision.
            if facts is not None and is_valid:
                standalone, _ = agree_no_collision_violations(
                    all_overview_facts, all_overview_faults, consistency_issues=all_overview_issues
                )
                from incident_report.rules.violations import filter_consistent_standalone_violations

                standalone = filter_consistent_standalone_violations(
                    standalone, consistency_issues, facts=facts, source_facts=all_overview_facts
                )
                violations = (
                    violations_from_faults(
                        party_faults,
                        None,
                        None,
                        {p.description: p.party_type.value for p in situation_from_overview(facts).parties},
                    )
                    + standalone
                    if facts.candidate_collisions
                    else standalone
                )
                referrals = evaluate_referrals(facts, party_faults)
                violations = replace_fines_with_referrals(violations, referrals, party_faults)
                # Majority evidence may come from another valid run; persist those citations (RPT-23 R3).
                facts = _append_cited_facts_to_events(facts, [c for v in violations for c in v.cited_facts])

        _run_with_timeout(_do_rules, timeouts.rules_timeout_seconds, "rules")
        stage_latencies_ms["rules"] = (time.perf_counter() - t_rules_start) * 1000

        from incident_report.rules.driver import classify_driver_observations

        driver_assessments = []
        if facts is not None and is_valid:
            for i, run_facts in enumerate([facts] + additional_runs_facts):
                if i > 0 and not additional_runs_faults[i - 1]:
                    continue
                for assessment in classify_driver_observations(run_facts):
                    label = (
                        (
                            f"Overview run {i + 1} of {n_runs}; classification agreement not measured. "
                            "Fines use the separate repeated-run breach vote. "
                        )
                        if n_runs > 1
                        else ""
                    )
                    driver_assessments.append(assessment.model_copy(update={"reason": label + assessment.reason}))
        report = Report(
            run_id=run_id,
            input_hash=input_hash,
            prompt_version=prompt_version,
            provenance=provenance,
            summary=summary,
            views=report_views,
            facts=facts,
            parties=party_faults,
            violations=violations,
            is_valid=is_valid,
            validation_errors=validation_errors,
            stage_latencies_ms=stage_latencies_ms,
            overview_collision_recall=overview_collision_recall,
            dense_facts=tuple(dense_facts_list),
            consistency_issues=consistency_issues,
            overview_consistency_issues=overview_consistency_issues,
            agreement=agreement_res,
            driver_assessments=tuple(driver_assessments),
        )
        from incident_report.guardrails import validate_report
        from incident_report.storyline import PLACEHOLDER_SUMMARIES, build_summary

        if provenance == "gemini-api" and summary in PLACEHOLDER_SUMMARIES:
            # The live adapter's fixed placeholder becomes a code-built line from cited facts (no model prose).
            report = report.model_copy(update={"summary": build_summary(report)})
        report = validate_report(report)
    except AIConnectionError as exc:
        if exc.calls:
            for c in exc.calls:
                if c not in calls_accumulator:
                    calls_accumulator.append(c)
        log(RunStatus.FAILED, None, str(exc), current_step=None)
        raise
    except TimeoutError as exc:
        log(RunStatus.FAILED, None, str(exc), current_step=None)
        raise
    except Exception as exc:
        log(RunStatus.FAILED, None, f"Unexpected error ({type(exc).__name__}).", current_step=None)
        raise
    finally:
        file_scopes.close()

    if on_step:
        on_step("done")
    run_log.update_step(run_id, "done")
    log(RunStatus.SUCCEEDED, report, None, current_step="done")
    return report
