"""Validate the producer graph and retain originals beside proposal-only normalized records."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any

from .issues import Issues
from .mapping import payload
from .models import TIMESTAMP_TOLERANCE_SECONDS, DerivedOmission, Envelope, NormalizedRecord, SourceBinding
from .reader import InvalidBundle, safe_path

STATUSES = {"PROPOSED_BY_SYSTEM", "CONFIRMED_BY_REVIEWER", "REJECTED_BY_REVIEWER", "MODIFIED_BY_REVIEWER"}
STATES = {
    "manual",
    "observed",
    "lost",
    "recovered",
    "interpolated",
    "estimated",
    "reviewer",
    "tentative",
    "confirmed",
    "deleted",
    "unavailable",
}


def identity(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def normalize(
    envelope: Envelope,
    parsed: dict[str, list[dict[str, Any]]],
    hashes: dict[str, str],
    issues: Issues,
    omissions: list[DerivedOmission],
) -> tuple[NormalizedRecord, ...]:
    """Hash closure includes every supplied manifest, even older reviewed revisions."""
    if not envelope.sources:
        issues.emit("upstream_artifacts_missing", "missing source bindings")
    if len({source.view_id for source in envelope.sources}) != len(envelope.sources):
        raise InvalidBundle("duplicate view binding")
    for field in (
        "original_video_sha256",
        "reconstruction_manifest",
        "movement_manifest",
        "incident_manifest",
        "reviewed_manifest",
    ):
        bindings = [getattr(source, field) for source in envelope.sources]
        if len(bindings) != len(set(bindings)):
            raise InvalidBundle(f"duplicate source binding: {field}")
    by_hash = {digest: name for name, digest in hashes.items()}
    for source in envelope.sources:
        by_hash[source.original_video_sha256] = "original-video-binding-only"
    for path, documents in parsed.items():
        for document in documents:
            _closure(path, document, hashes, by_hash, issues, omissions)
    records: list[NormalizedRecord] = []
    for source in envelope.sources:
        records.extend(_source(envelope, source, parsed, hashes, issues))
    ids = [record.record_id for record in records]
    if len(ids) != len(set(ids)):
        raise InvalidBundle("duplicate normalized record identity")
    return tuple(records)


def _closure(
    path: str,
    document: dict[str, Any],
    hashes: dict[str, str],
    by_hash: dict[str, str],
    issues: Issues,
    omissions: list[DerivedOmission],
) -> None:
    for artifact in document.get("artifacts", []):
        if not isinstance(artifact["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"]):
            raise InvalidBundle("invalid artifact digest")
        child = safe_path(artifact["filename"])
        target = (PurePosixPath(path).parent / child).as_posix()
        if target not in hashes:
            derived = (child.name == "replay.mp4" and "tracking_run" in document) or (
                child.name == "trajectories.csv" and "reconstruction_run" in document
            )
            if derived and len(child.parts) == 1:
                omissions.append(
                    DerivedOmission(artifact_path=target, sha256=artifact["sha256"], manifest_sha256=hashes[path])
                )
            else:
                issues.emit("upstream_artifacts_missing", f"missing upstream artifact: {target}")
        elif hashes[target] != artifact["sha256"]:
            raise InvalidBundle(f"producer artifact hash mismatch: {target}")
    for key, value in document.items():
        # Original video is bound by digest, intentionally not copied into the JSON evidence package.
        if key.endswith("_sha256") and value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise InvalidBundle("invalid producer digest")
            if key == "source_video_sha256":
                continue
            if key == "revision_sha256":
                target = str(PurePosixPath(path).parent / "revision.json")
                if target not in hashes:
                    issues.emit("upstream_artifacts_missing", f"missing upstream revision: {target}")
                elif hashes[target] != value:
                    raise InvalidBundle("revision sidecar hash mismatch")
                continue
            if value not in by_hash:
                issues.emit("upstream_artifacts_missing", f"missing upstream hash: {path}:{key}")
    if document.get("rule_source") is not None:
        raise InvalidBundle("producer rule_source must remain null")


def _source(
    envelope: Envelope,
    source: SourceBinding,
    parsed: dict[str, list[dict[str, Any]]],
    hashes: dict[str, str],
    issues: Issues,
) -> list[NormalizedRecord]:
    records: list[NormalizedRecord] = []
    chain_hashes: list[str] = []
    movement: dict[str, Any] = {}
    producer_case_id = source.producer_case_id or envelope.case_id
    prefix = (envelope.producer, envelope.case_id, source.view_id, source.tracking_run, source.track_revision)

    def track(track_id: int) -> str:
        if type(track_id) is not int or track_id < 1:
            raise InvalidBundle("invalid track ID")
        return identity(*prefix, track_id)

    def load(path: str) -> list[dict[str, Any]]:
        safe_path(path)
        if path not in parsed:
            issues.emit("upstream_artifacts_missing", f"missing upstream JSON: {path}")
        return parsed.get(path, [])

    def manifest(path: str, run: str) -> dict[str, Any]:
        documents = load(path)
        if len(documents) != 1:
            issues.emit("upstream_artifacts_missing", f"missing or ambiguous manifest: {path}")
            return {}
        result = documents[0]
        if (
            result.get("case_id") != producer_case_id
            or result.get("run_id") != run
            or result.get("schema_version") != "1.0"
        ):
            raise InvalidBundle("manifest identity or schema mismatch")
        return result

    movement_events = {
        event["event_id"]: event
        for event in parsed.get(str(PurePosixPath(source.movement_manifest).parent / "events.jsonl"), [])
    }

    def add(kind: Any, path: str, original: dict[str, Any], token: object, **values: Any) -> None:
        mapped = payload(
            kind,
            original,
            source,
            envelope.producer,
            envelope.case_id,
            movement,
            track,
            {
                entry["event_id"]: entry["order_unresolved_with"]
                for doc in parsed.get(str(PurePosixPath(source.movement_manifest).parent / "sequence.json"), [])
                for entry in doc["entries"]
            },
            parsed.get(str(PurePosixPath(source.incident_manifest).parent / "assessments.jsonl"), []),
            chain_hashes,
            {
                r["source_frame"]: r["source_time"]
                for r in parsed.get(
                    str(PurePosixPath(source.reconstruction_manifest).parent / "source_frames.jsonl"), []
                )
            },
            movement_events,
        )
        if mapped.kind == "sequence" and (mapped.peak_time is None or mapped.covering_frames is None):
            values["needs_review"] = True
            if mapped.time_window == (None, None):
                problem = "invalid source-time interval"
            elif mapped.covering_frames is None:
                problem = "window contains no source frame"
            else:
                problem = "has unresolved source time"
            issues.emit("needs_review", f"needs_review: sequence {mapped.event_id} {problem}")
        records.append(
            NormalizedRecord(
                record_id=identity(*prefix, path, token),
                kind=kind,
                view_id=source.view_id,
                artifact_path=path,
                data=mapped,
                original=original,
                **values,
            )
        )

    reconstruction = manifest(source.reconstruction_manifest, source.reconstruction_run)
    movement = manifest(source.movement_manifest, source.movement_run)
    incident = manifest(source.incident_manifest, source.incident_run)
    reviewed = manifest(source.reviewed_manifest, source.incident_run)
    checks = (
        (reconstruction, "source_video_sha256", source.original_video_sha256),
        (reconstruction, "tracking_run", source.tracking_run),
        (reconstruction, "track_revision", source.track_revision),
        (reconstruction, "calibration_revision", source.calibration_revision),
        (movement, "reconstruction_run", source.reconstruction_run),
        (movement, "source_reconstruction_manifest_sha256", hashes.get(source.reconstruction_manifest)),
        (incident, "movement_run", source.movement_run),
        (incident, "source_movement_manifest_sha256", hashes.get(source.movement_manifest)),
        (reviewed, "source_incident_manifest_sha256", hashes.get(source.incident_manifest)),
        (reviewed, "review_revision", source.review_revision),
    )
    for document, key, expected in checks:
        if expected is None:
            issues.emit("upstream_artifacts_missing", f"upstream binding unavailable: {key}")
            continue
        if document and document.get(key) != expected:
            raise InvalidBundle(f"source binding mismatch: {key}")
    calibration_digest = reconstruction.get("source_calibration_sha256")
    calibration_paths = [path for path, digest in hashes.items() if digest == calibration_digest]
    if not calibration_paths:
        issues.emit("calibration_unavailable", "missing_input: calibration unavailable")
    else:
        calibration_rows = load(calibration_paths[0])
        if len(calibration_rows) != 1:
            issues.emit("calibration_unavailable", "missing_input: calibration unavailable")
        else:
            calibration = calibration_rows[0]
            if (
                calibration.get("case_id"),
                calibration.get("source_video_sha256"),
                calibration.get("calibration_id"),
            ) != (producer_case_id, source.original_video_sha256, source.calibration_revision):
                raise InvalidBundle("calibration source binding mismatch")
            if calibration.get("validation_passed") is not True:
                issues.emit("calibration_failed", "missing_input: failed calibration")
            if movement and (
                calibration.get("mode") != movement.get("calibration_mode")
                or calibration.get("world_unit") != movement.get("unit")
            ):
                raise InvalidBundle("calibration geometry units mismatch")
    rec_root = PurePosixPath(source.reconstruction_manifest).parent
    mov_root = PurePosixPath(source.movement_manifest).parent
    inc_root = PurePosixPath(source.incident_manifest).parent
    rev_root = PurePosixPath(source.reviewed_manifest).parent
    for transformation in source.transformations:
        if transformation != {
            "operation": "producer reconstruction replay",
            "mapping": str(rec_root / "replay_frames.jsonl"),
        }:
            raise InvalidBundle(
                "unsupported source transformation; cropping/scaling/rotation require a reviewed mapping"
            )
    for scene in load(str(rec_root / "scene.json")):
        if movement and scene["display_unit"] != movement["unit"]:
            issues.emit("validation_issue", "scene display unit mismatch")
    timestamps = load(str(rec_root / "source_frames.jsonl"))
    time_by_frame: dict[int, dict[str, Any]] = {}
    frame_by_time: dict[float, int] = {}
    for row in timestamps:
        frame, time = row["source_frame"], row["source_time"]
        if (
            type(frame) is not int
            or frame < 0
            or not isinstance(time, (float, int))
            or time < 0
            or row["timestamp_source"] not in {"presentation_timestamp", "frame_rate_fallback"}
        ):
            raise InvalidBundle("invalid source timestamp")
        if frame in time_by_frame or time in frame_by_time:
            raise InvalidBundle("ambiguous source timestamp mapping")
        time_by_frame[frame] = row
        frame_by_time[time] = frame
    if not timestamps:
        issues.emit("source_timestamps_unavailable", "missing_input: no source timestamps")
    if reconstruction and (
        reconstruction.get("source_frame_count") != len(time_by_frame)
        or any(frame != index for index, frame in enumerate(sorted(time_by_frame)))
    ):
        issues.emit("source_timestamps_incomplete", "missing_input: incomplete zero-based source timestamp table")

    def citation(frame: Any, time: Any, *, overlay: bool = False) -> dict[str, Any]:
        if frame is not None and (type(frame) is not int or frame < 0):
            raise InvalidBundle("citation frame must be a zero-based integer")
        if type(time) not in (int, float) or time < 0:
            raise InvalidBundle("citation time must be a nonnegative source timestamp")
        row = time_by_frame.get(frame) if frame is not None else None
        if row is None or abs(row["source_time"] - time) > TIMESTAMP_TOLERANCE_SECONDS:
            if not overlay:
                issues.emit("validation_issue", "source frame/time unresolved")
            return {"needs_review": True}
        return {
            "source_frame": frame,
            "source_time": row["source_time"],
            "timestamp_provenance": row["timestamp_source"],
        }

    add("source_binding", source.reconstruction_manifest, source.model_dump(mode="json"), "binding")
    for row in timestamps:
        add(
            "source_binding",
            str(rec_root / "source_frames.jsonl"),
            row,
            row["source_frame"],
            **citation(row["source_frame"], row["source_time"]),
        )
    for name in ("observations.jsonl", "replay_frames.jsonl"):
        path = str(rec_root / name)
        for index, row in enumerate(load(path)):
            cite = citation(row["source_frame"], row["source_time"])
            if name == "observations.jsonl":
                if row["state"] not in STATES:
                    raise InvalidBundle("unknown track state")
                add("track_reference", path, row, index, track_refs=(track(row["track_id"]),), **cite)
            else:
                add("source_binding", path, row, index, **cite)
    if movement and (movement.get("unit"), movement.get("calibration_mode")) not in {
        ("m", "metric"),
        ("relative_units", "non_metric"),
    }:
        raise InvalidBundle("calibration units mismatch")
    for name in ("smoothed.jsonl", "movement_observations.jsonl"):
        path = str(mov_root / name)
        for index, row in enumerate(load(path)):
            if movement and row["unit"] != movement.get("unit"):
                raise InvalidBundle("mixed geometry units")
            add(
                "geometry",
                path,
                row,
                index,
                track_refs=(track(row["track_id"]),),
                **citation(row["source_frame"], row.get("source_time", row.get("timestamp_seconds"))),
            )
    sequence_path = str(mov_root / "sequence.json")
    movement_event_path = str(mov_root / "events.jsonl")
    for event in load(movement_event_path):
        if event["status"] not in STATUSES or (movement and event["unit"] != movement["unit"]):
            raise InvalidBundle("invalid movement event status or units")
        for boundary in ("start", "end"):
            citation(event[f"{boundary}_frame"], event[f"{boundary}_time"])
        add(
            "event_proposal",
            movement_event_path,
            event,
            event["event_id"],
            track_refs=tuple(track(t) for t in event["track_ids"]),
            producer_status=event["status"],
            **citation(event["peak_frame"], event["peak_time"]),
        )
    for document in load(sequence_path):
        for row in document["entries"]:
            add("sequence", sequence_path, row, row["event_id"], track_refs=tuple(track(t) for t in row["track_ids"]))

    originals: dict[str, dict[str, Any]] = {}
    original_assessments: dict[str, dict[str, Any]] = {}
    for row in load(str(inc_root / "events.jsonl")):
        if row["event_id"] in originals:
            raise InvalidBundle("duplicate event ID")
        originals[row["event_id"]] = row
    for row in load(str(inc_root / "assessments.jsonl")):
        if row["assessment_id"] in original_assessments or row["rule_source"] is not None:
            raise InvalidBundle("invalid assessment")
        original_assessments[row["assessment_id"]] = row
    if not originals:
        issues.emit("no_incident_candidates", "missing_input: no incident candidates")

    decisions: dict[tuple[str, str], dict[str, Any]] = {}
    revision_hash = reviewed.get("review_revision_sha256")
    expected_revision = source.review_revision
    visited: set[str] = set()
    latest_loaded = False
    while revision_hash:
        if revision_hash in visited:
            raise InvalidBundle("cyclic review chain")
        visited.add(revision_hash)
        chain_hashes.append(revision_hash)
        candidates = [p for p, h in hashes.items() if h == revision_hash]
        if not candidates:
            issues.emit("upstream_artifacts_missing", "missing upstream review revision")
            break
        revision_path = candidates[0]
        revision_docs = load(revision_path)
        if len(revision_docs) != 1:
            raise InvalidBundle("invalid review revision")
        revision = revision_docs[0]
        if revision["revision_id"] != expected_revision:
            raise InvalidBundle("review revision binding mismatch")
        if not latest_loaded:
            latest_loaded = True
            for decision in revision["document"]["decisions"]:
                decision_key = (decision["target_kind"], decision["target_id"])
                if decision_key in decisions:
                    raise InvalidBundle("duplicate review decision")
                decisions[decision_key] = decision
        previous = revision["previous_sha256"]
        if previous == hashes.get(source.incident_manifest):
            if expected_revision != "revision-001":
                raise InvalidBundle("review chain skips preceding revisions")
            break
        if expected_revision == "revision-001" and source.incident_manifest not in hashes:
            issues.emit("upstream_artifacts_missing", "review chain root incident manifest unavailable")
            break
        revision_hash = previous
        # IDs are producer revision-NNN; verify backwards order rather than trusting arbitrary linked JSON.
        number = int(expected_revision.removeprefix("revision-"))
        if number <= 1:
            raise InvalidBundle("review chain does not terminate at incident manifest")
        expected_revision = f"revision-{number - 1:03d}"
    if source.review_revision == "initial" and revision_hash is not None:
        raise InvalidBundle("initial review cannot name a revision")
    if source.review_revision != "initial" and reviewed and not revision_hash:
        issues.emit("upstream_artifacts_missing", "missing upstream review revision")
    review_available = source.review_revision == "initial" or latest_loaded

    def check_frames(proposal: dict[str, Any]) -> None:
        for field in ("start_frame", "peak_frame", "end_frame"):
            if field in proposal:
                citation(proposal[field], proposal[field.replace("frame", "time")])
        for collection in ("signals", "supporting_evidence", "counterevidence"):
            for signal in proposal.get(collection, []):
                for frame in signal["source_frames"]:
                    if frame not in time_by_frame:
                        issues.emit("validation_issue", "supporting evidence frame unresolved")

    for proposal in (*originals.values(), *original_assessments.values()):
        check_frames(proposal)

    statuses: dict[str, str] = {}
    seen_events: set[str] = set()
    path = str(rev_root / "reviewed_events.jsonl")
    for row in load(path):
        proposal = row["proposal"]
        event_id = proposal["event_id"]
        if event_id in seen_events or (event_id in originals and originals[event_id] != proposal):
            raise InvalidBundle("reviewed event does not preserve original proposal")
        if event_id not in originals:
            issues.emit("upstream_artifacts_missing", "missing original event proposal")
        seen_events.add(event_id)
        decision = decisions.get(("event", event_id)) if review_available else row["decision"]
        expected_status = _review_status(decision)
        if row["status"] not in STATUSES or (
            review_available and (row["status"] != expected_status or row["decision"] != decision)
        ):
            raise InvalidBundle("event review overlay mismatch")
        statuses[event_id] = row["status"]
        if movement and proposal["unit"] != movement.get("unit"):
            raise InvalidBundle("event units mismatch")
        refs = tuple(track(t) for t in proposal["track_ids"])
        add(
            "event_proposal",
            path,
            proposal,
            event_id,
            track_refs=refs,
            producer_status=row["status"],
            **citation(proposal["peak_frame"], proposal["peak_time"]),
        )
        effective_time = decision.get("modified_peak_time") if decision else None
        effective_time = proposal["peak_time"] if effective_time is None else effective_time
        effective_location = decision.get("modified_location") if decision else None
        effective_location = proposal["location"] if effective_location is None else effective_location
        if row["peak_time"] != effective_time or row["location"] != effective_location:
            raise InvalidBundle("effective event value inconsistent with review")
        if decision:
            matches = [f for t, f in frame_by_time.items() if abs(t - effective_time) <= TIMESTAMP_TOLERANCE_SECONDS]
            frame = matches[0] if len(matches) == 1 else None
            if frame is None and decision.get("modified_peak_time") is not None:
                issues.emit(
                    "needs_review",
                    f"needs_review: event {event_id} corrected time {effective_time!r} unresolved in source timestamp table",
                )
            add(
                "review_overlay",
                path,
                decision,
                event_id + "-review",
                effective={
                    "peak_time": effective_time,
                    "location": effective_location,
                    "reviewer_authenticated": False,
                },
                track_refs=refs,
                producer_status=row["status"],
                **citation(frame, effective_time, overlay=True),
            )
    if seen_events != originals.keys():
        issues.emit("upstream_artifacts_missing", "missing reviewed events")
    for event_id in originals.keys() - seen_events:
        proposal = originals[event_id]
        add(
            "event_proposal",
            str(inc_root / "events.jsonl"),
            proposal,
            event_id,
            track_refs=tuple(track(t) for t in proposal["track_ids"]),
            needs_review=True,
        )
    seen_assessments: set[str] = set()
    path = str(rev_root / "reviewed_assessments.jsonl")
    for row in load(path):
        proposal = row["proposal"]
        assessment_id = proposal["assessment_id"]
        if assessment_id in seen_assessments or (
            assessment_id in original_assessments and original_assessments[assessment_id] != proposal
        ):
            raise InvalidBundle("reviewed assessment does not preserve original proposal")
        if assessment_id not in original_assessments:
            issues.emit("upstream_artifacts_missing", "missing original assessment proposal")
        seen_assessments.add(assessment_id)
        decision = decisions.get(("assessment", assessment_id)) if review_available else row["decision"]
        parent = statuses.get(proposal["event_id"])
        if row["status"] not in STATUSES or (
            review_available
            and (
                row["status"] != _review_status(decision)
                or row["decision"] != decision
                or (parent is not None and row["event_status"] != parent)
            )
        ):
            raise InvalidBundle("assessment or parent status mismatch")
        if parent is None:
            issues.emit("upstream_artifacts_missing", "assessment parent event missing")
        effective = {"outcome": row["outcome"], "contributing_track_ids": row["contributing_track_ids"]}
        for key, modification in (
            ("outcome", "modified_outcome"),
            ("contributing_track_ids", "modified_contributing_track_ids"),
        ):
            expected = decision.get(modification) if decision else None
            if effective[key] != (proposal[key] if expected is None else expected):
                raise InvalidBundle("assessment effective value mismatch")
        add(
            "assessment",
            path,
            proposal,
            assessment_id,
            effective=effective,
            producer_status=row["status"],
            parent_status=parent,
            track_refs=tuple(track(t) for t in proposal["involved_track_ids"]),
            needs_review=True,
        )
        if decision:
            add(
                "review_overlay",
                path,
                decision,
                assessment_id + "-review",
                effective=effective,
                producer_status=row["status"],
                parent_status=parent,
                needs_review=True,
            )
    if seen_assessments != original_assessments.keys():
        issues.emit("upstream_artifacts_missing", "missing reviewed assessments")
    for assessment_id in original_assessments.keys() - seen_assessments:
        proposal = original_assessments[assessment_id]
        add(
            "assessment",
            str(inc_root / "assessments.jsonl"),
            proposal,
            assessment_id,
            track_refs=tuple(track(t) for t in proposal["involved_track_ids"]),
            needs_review=True,
        )
    return records


def _review_status(decision: dict[str, Any] | None) -> str:
    if decision is None:
        return "PROPOSED_BY_SYSTEM"
    action = decision["action"]
    if action not in {"confirm", "reject", "modify"}:
        raise InvalidBundle("unknown review action")
    return {"confirm": "CONFIRMED_BY_REVIEWER", "reject": "REJECTED_BY_REVIEWER", "modify": "MODIFIED_BY_REVIEWER"}[
        action
    ]
