"""Streamlit incident panel: candidates with their signal checklist, PET, contribution evidence, and reviewer decisions."""

from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import streamlit as st

from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.incident.models import ContributionOutcome, IncidentManifest, ReviewDecision, ReviewedAssessment, ReviewedEvent
from minivision_reconstruct.incident.review import (
    IncidentRun, export_reviewed_view, effective_view, load_incident_run, load_review, review_revisions, save_review_revision, with_decision,
)
from minivision_reconstruct.movement.models import MovementManifest
from minivision_reconstruct.movement.precision import display_time, display_value
from minivision_reconstruct.movement.ui import _request_seek
from minivision_reconstruct.reconstruction.models import ReconstructionError
from minivision_reconstruct.reconstruction.storage import read_record


ACTIONS = {"Confirm": "confirm", "Reject": "reject", "Modify": "modify"}
PENDING_KEY = "incident-review-pending"


def incident_runs(case_root: Path, reconstruction_run: str) -> tuple[Path, ...]:
    """Incident runs whose movement run was derived from the selected reconstruction run."""
    root = case_root / "analysis" / "incident"
    if not root.is_dir():
        return ()
    result: list[Path] = []
    for path in sorted(root.iterdir()):
        if not (path / "manifest.json").is_file():
            continue
        movement = case_root / "analysis" / "movement" / read_record(path / "manifest.json", IncidentManifest).movement_run / "manifest.json"
        if movement.is_file() and read_record(movement, MovementManifest).reconstruction_run == reconstruction_run:
            result.append(path)
    return tuple(result)


@st.cache_data(show_spinner="Verifying incident records…")
def _load_run(run_directory: str, manifest_sha256: str) -> IncidentRun:
    """Hash-verified load keyed by the manifest hash, so changed files are never shown."""
    return load_incident_run(Path(run_directory))


def render_incident_panel(case_root: Path, reconstruction_run: str, frame_period: float, reviewer: str) -> None:
    st.subheader("Incident analysis")
    runs = incident_runs(case_root, reconstruction_run)
    if not runs:
        st.info("No Week 8 incident run for this reconstruction. Create one with scripts/analyze_incident.py.")
        return
    run_directory = st.selectbox("Incident run", runs, format_func=lambda path: path.name, key=f"incident-run-{case_root.name}-{reconstruction_run}")
    run = _load_run(str(run_directory), sha256_file(run_directory / "manifest.json"))
    revisions = review_revisions(run_directory)
    revision_key = f"incident-revision-{run.manifest.case_id}-{run.manifest.run_id}"
    if (pending := st.session_state.pop(PENDING_KEY, None)) in revisions:
        st.session_state[revision_key] = pending
    revision = st.selectbox("Review revision", revisions, index=len(revisions) - 1, key=revision_key)
    view = effective_view(run_directory, revision)
    unit = "m" if run.manifest.unit == "m" else "relative units"
    st.caption(f"Config {run.manifest.config_id} · {run.manifest.pipeline_version} · unit: {unit}. Every item is a system proposal until a reviewer decides. "
               + " ".join(run.manifest.limitations))
    candidates_tab, conflicts_tab = st.tabs(("Candidates", "PET conflicts"))
    with conflicts_tab:
        st.dataframe([{
            "conflict": item.conflict_id, "first → second": f"#{item.track_first} → #{item.track_second}",
            "PET": display_time(item.pet_seconds, item.pet_window, frame_period) if item.pet_seconds >= 0 else f"{item.pet_seconds:.2f} s (both inside)",
            "window (s)": f"{item.pet_window[0]:.2f} to {item.pet_window[1]:.2f}", "radius": f"{item.radius:.2f} {unit}",
            "frames": f"{item.first_exit_frame} → {item.second_enter_frame}", "confidence": item.confidence,
        } for item in run.conflicts], hide_index=True, width="stretch")
        st.caption("Post-encroachment time from source-frame times and positions only; no speed or time-to-collision is calculated.")
    with candidates_tab:
        if not view.events:
            st.info("No collision, near-miss, or single-vehicle candidate was proposed for this run.")
            return
        assessments = {item.proposal.event_id: item for item in view.assessments}
        st.dataframe([{
            "candidate": item.proposal.event_type.replace("_", " "), "road users": ", ".join(f"#{track}" for track in item.proposal.track_ids),
            "time": display_time(item.proposal.peak_time, item.proposal.time_window, frame_period),
            "reviewed time (s)": f"{item.peak_time:.2f}" if item.peak_time != item.proposal.peak_time else "",
            "type": item.proposal.predicted_collision_type.replace("_", " "), "confidence": item.proposal.confidence, "status": item.status,
            "contribution": (assessments[item.proposal.event_id].outcome if item.proposal.event_id in assessments else "not assessed"),
        } for item in view.events], hide_index=True, width="stretch")
        # Select by ID: the widget would otherwise hand back the object from before the last saved decision.
        by_id = {item.proposal.event_id: item for item in view.events}
        chosen = by_id[st.selectbox("Incident candidate", tuple(by_id), key=f"incident-candidate-{run.manifest.run_id}",
                                    format_func=lambda event_id: f"{by_id[event_id].proposal.event_type.replace('_', ' ')} · "
                                    f"{', '.join(f'#{track}' for track in by_id[event_id].proposal.track_ids)} · {by_id[event_id].proposal.peak_time:.2f} s")]
        _candidate_detail(chosen, frame_period, unit)
        assessment = assessments.get(chosen.proposal.event_id)
        if assessment is not None:
            _assessment_detail(assessment)
        _review_form(run, revision, revisions, chosen, assessment, reviewer)
        if st.button("Export reviewed view", key=f"incident-export-{run.manifest.run_id}"):
            try:
                st.success(f"Exported {export_reviewed_view(run_directory, revision)}")
            except ReconstructionError as error:
                st.error(str(error))


def _candidate_detail(item: ReviewedEvent, frame_period: float, unit: str) -> None:
    event = item.proposal
    st.markdown(f"**{event.event_type.replace('_', ' ').capitalize()}** · status `{item.status}` · confidence {event.confidence} · "
                f"predicted type {event.predicted_collision_type.replace('_', ' ')}")
    st.markdown(f"Proposed impact {display_time(event.peak_time, event.time_window, frame_period)} at frame {event.peak_frame}; "
                f"location ({display_value(event.location[0], max(event.location_uncertainty, 0.01), '')}, "
                f"{display_value(event.location[1], max(event.location_uncertainty, 0.01), unit)}).")
    if item.decision is not None:
        st.info(f"Reviewer {item.decision.reviewer} chose **{item.decision.action}** ({item.decision.decided_at}): {item.decision.note or 'no note'}")
    st.markdown("**Signals** (proximity is required; image box overlap is supporting only)")
    for signal in event.signals:
        cited = f" · events {', '.join(signal.supporting_event_ids)}" if signal.supporting_event_ids else ""
        st.markdown(f"- ✓ *{signal.kind}* · `{signal.name}` · frames {', '.join(map(str, signal.source_frames))}{cited}  \n  {signal.description}")
    phases = event.phases
    st.caption(f"Phases (s): pre-event {phases.pre_event[0]:.2f}–{phases.pre_event[1]:.2f} · event {phases.event[0]:.2f}–{phases.event[1]:.2f} · "
               f"post-event {phases.post_event[0]:.2f}–{phases.post_event[1]:.2f}")
    if st.button("Go to impact frame", key=f"incident-seek-{event.event_id}"):
        _request_seek(round(item.peak_time / frame_period) if item.peak_time != event.peak_time else event.peak_frame,
                      event.track_ids[0] if len(event.track_ids) == 1 else None)


def _assessment_detail(item: ReviewedAssessment) -> None:
    assessment = item.proposal
    contributors = ", ".join(f"#{track}" for track in item.contributing_track_ids) or "none"
    st.markdown(f"**Contribution assessment** · `{item.outcome}` · contributors {contributors} · status `{item.status}` · confidence {assessment.confidence}")
    st.caption(assessment.disclaimer + " A track ID identifies a visible road user, not a driver or legal party.")
    for title, entries in (("Evidence for", assessment.supporting_evidence), ("Evidence against", assessment.counterevidence)):
        if entries:
            st.markdown(f"*{title}*")
            for entry in entries:
                st.markdown(f"- #{entry.track_id} · `{entry.rule}` · {entry.seconds_before_peak:.2f} s before the candidate · "
                            f"events {', '.join(entry.event_ids)} · frames {', '.join(map(str, entry.source_frames))}  \n  {entry.description}")
    for fact in assessment.unresolved_facts:
        st.warning(f"{'Decisive u' if fact.decisive else 'U'}nresolved: {fact.description}")


def _review_form(run: IncidentRun, revision: str, revisions: tuple[str, ...], chosen: ReviewedEvent, assessment: ReviewedAssessment | None, reviewer: str) -> None:
    st.markdown("**Reviewer decision**")
    if revision != revisions[-1]:
        st.info("You are viewing an earlier review revision. Select the latest revision to record a decision.")
        return
    key = f"incident-review-{run.manifest.run_id}-{chosen.proposal.event_id}"
    targets = ("Candidate event", "Contribution assessment") if assessment is not None else ("Candidate event",)
    target = st.radio("Decision applies to", targets, horizontal=True, key=f"{key}-target")
    action = ACTIONS[st.radio("Decision", tuple(ACTIONS), horizontal=True, key=f"{key}-action")]
    changes: dict[str, object] = {}
    if action == "modify" and target == "Candidate event":
        columns = st.columns(3)
        peak = columns[0].number_input("Corrected impact time (s)", min_value=0.0, value=float(chosen.peak_time), step=0.04, format="%.2f", key=f"{key}-peak")
        x = columns[1].number_input("Corrected location x", value=float(chosen.location[0]), format="%.2f", key=f"{key}-x")
        y = columns[2].number_input("Corrected location y", value=float(chosen.location[1]), format="%.2f", key=f"{key}-y")
        changes = {"modified_peak_time": None if abs(peak - chosen.proposal.peak_time) < 1e-9 else peak,
                   "modified_location": None if (x, y) == tuple(chosen.proposal.location) else (x, y)}
    elif action == "modify" and assessment is not None:
        outcomes = get_args(ContributionOutcome)
        outcome = st.selectbox("Corrected outcome", outcomes, index=outcomes.index(assessment.outcome), key=f"{key}-outcome")
        contributors = st.multiselect("Contributing road users", assessment.proposal.involved_track_ids, default=list(assessment.contributing_track_ids),
                                      format_func=lambda track: f"#{track}", key=f"{key}-contributors")
        changes = {"modified_outcome": outcome, "modified_contributing_track_ids": tuple(sorted(contributors))}
    note = st.text_area("Reviewer note (required to reject or modify)", key=f"{key}-note", max_chars=2000)
    if not st.button("Save review decision", type="primary", key=f"{key}-save"):
        return
    target_kind = "event" if target == "Candidate event" else "assessment"
    try:
        decision = ReviewDecision(
            target_kind=target_kind, target_id=chosen.proposal.event_id if target_kind == "event" else assessment.proposal.assessment_id,  # type: ignore[union-attr]
            action=action, note=note, modified_peak_time=changes.get("modified_peak_time"), modified_location=changes.get("modified_location"),
            modified_outcome=changes.get("modified_outcome"), modified_contributing_track_ids=changes.get("modified_contributing_track_ids"),
            reviewer=reviewer.strip() or "unnamed", decided_at=datetime.now(UTC).isoformat(),
        )
        saved = save_review_revision(run.directory, with_decision(load_review(run.directory, revision).decisions, decision), reviewer)
    except (ReconstructionError, ValueError) as error:
        st.error(str(error))
        return
    st.session_state[PENDING_KEY] = saved.name
    st.toast(f"Saved {saved.name}")
    st.rerun()
