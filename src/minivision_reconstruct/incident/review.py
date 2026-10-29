"""Reviewer decisions on incident proposals: immutable, hash-chained revisions and the effective reviewed view.

The proposals in events.jsonl and assessments.jsonl are never rewritten. Each revision holds the complete set of
decisions in force and is chained by SHA-256 to the previous revision, or to the incident run manifest for the
first one, exactly as scene and marker revisions are. The reviewed view overlays the decisions on the proposals
and embeds each proposal unchanged.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from minivision_reconstruct.evidence.integrity import sha256_file
from minivision_reconstruct.incident.models import (
    ConflictRecord, ContributionAssessment, IncidentError, IncidentEvent, IncidentManifest, IncidentReviewDocument,
    IncidentReviewRevision, ReviewDecision, ReviewedAssessment, ReviewedEvent, ReviewedExportManifest,
)
from minivision_reconstruct.movement.pipeline import _write_jsonl
from minivision_reconstruct.reconstruction.models import ArtifactHash, require_identifier
from minivision_reconstruct.reconstruction.storage import _verified_revision_path, read_record, read_records, revision_ids, verify_hash, write_record


CATEGORY = "reviews"
STATUS = {"confirm": "CONFIRMED_BY_REVIEWER", "reject": "REJECTED_BY_REVIEWER", "modify": "MODIFIED_BY_REVIEWER"}
CONTRIBUTOR_COUNT = {"ONE_CONTRIBUTOR": lambda count: count == 1, "MULTIPLE_CONTRIBUTORS": lambda count: count >= 2,
                     "NO_SUPPORTED_CONTRIBUTOR": lambda count: count == 0, "UNDETERMINED": lambda count: count == 0}


@dataclass(frozen=True)
class IncidentRun:
    directory: Path
    manifest: IncidentManifest
    conflicts: tuple[ConflictRecord, ...]
    events: tuple[IncidentEvent, ...]
    assessments: tuple[ContributionAssessment, ...]


@dataclass(frozen=True)
class ReviewedIncidents:
    revision: str
    events: tuple[ReviewedEvent, ...]
    assessments: tuple[ReviewedAssessment, ...]


def load_incident_run(run_directory: Path) -> IncidentRun:
    """Hash-verified proposals of one incident run."""
    manifest = read_record(run_directory / "manifest.json", IncidentManifest)
    for artifact in manifest.artifacts:
        verify_hash(run_directory / artifact.filename, artifact.sha256)
    return IncidentRun(
        directory=run_directory, manifest=manifest, conflicts=read_records(run_directory / "conflicts.jsonl", ConflictRecord),
        events=read_records(run_directory / "events.jsonl", IncidentEvent),
        assessments=read_records(run_directory / "assessments.jsonl", ContributionAssessment),
    )


def review_revisions(run_directory: Path) -> tuple[str, ...]:
    return revision_ids(run_directory, CATEGORY)


def load_review(run_directory: Path, revision: str) -> IncidentReviewDocument:
    """Decisions in force at a revision; 'initial' means no decisions yet."""
    if revision == "initial":
        return IncidentReviewDocument(schema_version="1.0", decisions=())
    return read_record(_verified_revision_path(run_directory, CATEGORY, revision), IncidentReviewRevision).document


def with_decision(decisions: tuple[ReviewDecision, ...], decision: ReviewDecision) -> tuple[ReviewDecision, ...]:
    """The decisions in force after this one replaces any earlier decision on the same proposal."""
    kept = tuple(item for item in decisions if (item.target_kind, item.target_id) != (decision.target_kind, decision.target_id))
    return (*kept, decision)


def validate_decisions(run: IncidentRun, decisions: tuple[ReviewDecision, ...]) -> None:
    """Every decision names one existing proposal of its kind and stays consistent with it."""
    targets = [(item.target_kind, item.target_id) for item in decisions]
    if len(set(targets)) != len(targets):
        raise IncidentError("A revision may hold only one decision per proposal.")
    events = {event.event_id: event for event in run.events}
    assessments = {item.assessment_id: item for item in run.assessments}
    rejected = {item.target_id for item in decisions if item.target_kind == "event" and item.action == "reject"}
    for decision in decisions:
        if decision.target_kind == "event":
            event = events.get(decision.target_id)
            if event is None:
                raise IncidentError(f"No proposed event '{decision.target_id}' in this run.")
            if decision.modified_peak_time is not None and not event.phases.pre_event[0] <= decision.modified_peak_time <= event.phases.post_event[1]:
                raise IncidentError(f"Corrected impact time {decision.modified_peak_time:.2f} s lies outside the observed span of the involved road users.")
            continue
        assessment = assessments.get(decision.target_id)
        if assessment is None:
            raise IncidentError(f"No contribution assessment '{decision.target_id}' in this run.")
        if assessment.event_id in rejected and decision.action != "reject":
            raise IncidentError("The assessed event is rejected; its assessment can only be rejected too.")
        contributors = decision.modified_contributing_track_ids if decision.modified_contributing_track_ids is not None else assessment.contributing_track_ids
        outcome = decision.modified_outcome or assessment.outcome
        if not set(contributors) <= set(assessment.involved_track_ids):
            raise IncidentError(f"Contributors {contributors} must be among the involved road users {assessment.involved_track_ids}.")
        if not CONTRIBUTOR_COUNT[outcome](len(set(contributors))):
            raise IncidentError(f"Outcome {outcome} does not match {len(set(contributors))} contributing road user(s).")


def save_review_revision(run_directory: Path, decisions: tuple[ReviewDecision, ...], reviewer: str) -> Path:
    """Save the decisions in force as the next immutable revision; the proposals are not touched."""
    if not reviewer.strip():
        raise IncidentError("Enter a reviewer name before saving a review decision.")
    validate_decisions(load_incident_run(run_directory), decisions)
    root = run_directory / CATEGORY
    root.mkdir(exist_ok=True)
    with (root / ".revision.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = review_revisions(run_directory)[1:]
        number = max((int(name.removeprefix("revision-")) for name in existing), default=0) + 1
        revision_id = f"revision-{number:03d}"
        previous = root / existing[-1] / "revision.json" if existing else run_directory / "manifest.json"
        revision = IncidentReviewRevision(
            revision_id=revision_id, reviewer=reviewer.strip(), created_at=datetime.now(UTC).isoformat(), previous_sha256=sha256_file(previous),
            document=IncidentReviewDocument(schema_version="1.0", decisions=decisions),
        )
        destination = root / revision_id
        with TemporaryDirectory(prefix=".revision-", dir=root) as temporary:
            staged = Path(temporary)
            write_record(staged / "revision.json", revision)
            (staged / "sha256.json").write_text(json.dumps({"revision_sha256": sha256_file(staged / "revision.json")}) + "\n")
            staged.rename(destination)
    return destination


def effective_view(run_directory: Path, revision: str) -> ReviewedIncidents:
    """Proposals with the decisions in force at `revision` applied; unreviewed proposals stay PROPOSED_BY_SYSTEM."""
    run = load_incident_run(run_directory)
    decisions = load_review(run_directory, revision).decisions
    validate_decisions(run, decisions)
    chosen = {(item.target_kind, item.target_id): item for item in decisions}
    events: list[ReviewedEvent] = []
    for event in run.events:
        decision = chosen.get(("event", event.event_id))
        events.append(ReviewedEvent(
            proposal=event, status=STATUS[decision.action] if decision else "PROPOSED_BY_SYSTEM", decision=decision,
            peak_time=decision.modified_peak_time if decision and decision.modified_peak_time is not None else event.peak_time,
            location=decision.modified_location if decision and decision.modified_location is not None else event.location,
        ))
    statuses = {item.proposal.event_id: item.status for item in events}
    assessments: list[ReviewedAssessment] = []
    for assessment in run.assessments:
        decision = chosen.get(("assessment", assessment.assessment_id))
        assessments.append(ReviewedAssessment(
            proposal=assessment, status=STATUS[decision.action] if decision else "PROPOSED_BY_SYSTEM",
            event_status=statuses[assessment.event_id], decision=decision,
            outcome=decision.modified_outcome if decision and decision.modified_outcome is not None else assessment.outcome,
            contributing_track_ids=decision.modified_contributing_track_ids if decision and decision.modified_contributing_track_ids is not None
            else assessment.contributing_track_ids,
        ))
    return ReviewedIncidents(revision, tuple(events), tuple(assessments))


def export_reviewed_view(run_directory: Path, revision: str) -> Path:
    """Write the reviewed view once per revision, hash-linked to the run manifest and the revision it applies."""
    require_identifier(revision, "review revision")
    view = effective_view(run_directory, revision)
    manifest = read_record(run_directory / "manifest.json", IncidentManifest)
    root = run_directory / "reviewed"
    root.mkdir(exist_ok=True)
    destination = root / revision
    if destination.exists():
        raise IncidentError(f"Reviewed view '{destination}' already exists; it is immutable.")
    with TemporaryDirectory(prefix=".reviewed-", dir=root) as temporary:
        staged = Path(temporary)
        _write_jsonl(staged / "reviewed_events.jsonl", view.events)
        _write_jsonl(staged / "reviewed_assessments.jsonl", view.assessments)
        write_record(staged / "manifest.json", ReviewedExportManifest(
            schema_version="1.0", case_id=manifest.case_id, run_id=manifest.run_id, review_revision=revision,
            source_incident_manifest_sha256=sha256_file(run_directory / "manifest.json"),
            review_revision_sha256=None if revision == "initial" else sha256_file(run_directory / CATEGORY / revision / "revision.json"),
            created_at=datetime.now(UTC).isoformat(),
            artifacts=tuple(ArtifactHash(filename=path.name, sha256=sha256_file(path)) for path in sorted(staged.iterdir())),
        ))
        staged.rename(destination)
    return destination
