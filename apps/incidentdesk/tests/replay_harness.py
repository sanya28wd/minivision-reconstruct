"""Replay test harness runner and Report assertion logic (RPT-16, spec 001).

Runs incident video clips through Seam 1 (analyse_incident) using recorded
AI responses, asserting exclusively on the resulting Report data object.
Zero live API calls.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from incident_report.ai import ReplayAIConnection
from incident_report.domain import Origin, Report, Role, Run, View
from incident_report.entry import PROMPT_VERSION, analyse_incident
from incident_report.facts import DEFAULT_MIN_CONFIDENCE, is_raw_id
from incident_report.hashing import sha256_hex
from incident_report.runlog import RunLog
from synthetic_clips import get_synthetic_clip_by_sha256

DEFAULT_CASES_DIR = Path(__file__).parent / "replay_cases"

ALLOWED_EXPECTED_KEYS: frozenset[str] = frozenset(
    {
        "case_id",
        "provenance",
        "summary",
        "is_valid",
        "repair_outcome",
        "validation_errors",
        "validation_error_patterns",
        "parties",
        "events",
        "candidate_collisions",
        "overall_fault",
        "_comment_rpt15_overall_fault",
    }
)

ALLOWED_PARTY_KEYS: frozenset[str] = frozenset(
    {
        "description",
        "timestamp_seconds",
        "confidence",
        "color",
        "make_model",
        "body_type",
        "fault_status",
        "_comment_rpt15_fault",
    }
)

ALLOWED_EVENT_KEYS: frozenset[str] = frozenset(
    {
        "description",
        "timestamp_seconds",
        "confidence",
    }
)

ALLOWED_COLLISION_KEYS: frozenset[str] = frozenset(
    {
        "description",
        "timestamp_seconds",
        "confidence",
    }
)


@dataclass(frozen=True)
class ViewReference:
    filename: str
    role: Role
    sha256: str
    label: str | None = None
    acquisition_method: str | None = None


@dataclass(frozen=True)
class CaseDefinition:
    case_id: str
    case_dir: Path
    description: str
    prompt_version: str
    origin: Origin
    views: tuple[ViewReference, ...]
    expected: dict[str, Any]


def discover_replay_cases(cases_dir: Path | None = None) -> list[CaseDefinition]:
    """Discover all replay cases in cases_dir (one directory per case)."""
    root = Path(cases_dir) if cases_dir is not None else DEFAULT_CASES_DIR
    if not root.is_dir():
        return []

    cases: list[CaseDefinition] = []
    for item in sorted(root.iterdir()):
        if not item.is_dir():
            continue
        clip_ref_path = item / "clip_reference.json"
        expected_path = item / "expected.json"
        if not clip_ref_path.is_file() or not expected_path.is_file():
            continue

        ref_data = json.loads(clip_ref_path.read_text(encoding="utf-8"))
        expected_data = json.loads(expected_path.read_text(encoding="utf-8"))

        views: list[ViewReference] = []
        for v in ref_data.get("views", []):
            views.append(
                ViewReference(
                    filename=v["filename"],
                    role=Role(v.get("role", "exterior")),
                    sha256=v["sha256"],
                    label=v.get("label"),
                    acquisition_method=v.get("acquisition_method"),
                )
            )

        cases.append(
            CaseDefinition(
                case_id=ref_data.get("case_id", item.name),
                case_dir=item,
                description=ref_data.get("description", ""),
                prompt_version=ref_data.get("prompt_version", PROMPT_VERSION),
                origin=Origin(ref_data.get("origin", "replay")),
                views=tuple(views),
                expected=expected_data,
            )
        )
    return cases


def load_case_views(case: CaseDefinition) -> list[View]:
    """Load View streams for each view in the case, verifying content hashes."""
    views: list[View] = []
    clips_dir = os.environ.get("INCIDENT_REPLAY_CLIPS_DIR")

    for v_ref in case.views:
        raw_bytes: bytes | None = None

        # 1. Look in case folder
        local_path = case.case_dir / v_ref.filename
        if local_path.is_file():
            raw_bytes = local_path.read_bytes()

        # 2. Look in external clips directory
        if raw_bytes is None and clips_dir:
            cand = Path(clips_dir) / v_ref.filename
            if not cand.is_file():
                cand = Path(clips_dir) / f"{v_ref.sha256}.mp4"
            if cand.is_file():
                raw_bytes = cand.read_bytes()

        # 3. Known synthetic clips
        if raw_bytes is None:
            raw_bytes = get_synthetic_clip_by_sha256(v_ref.sha256)

        if raw_bytes is None:
            raise FileNotFoundError(
                f"Video clip for SHA-256 {v_ref.sha256} ({v_ref.filename}) in case {case.case_id} "
                "was not found in the case folder, INCIDENT_REPLAY_CLIPS_DIR, or synthetic clip registry."
            )

        digest = sha256_hex(raw_bytes)
        if digest != v_ref.sha256:
            raise ValueError(
                f"Clip {v_ref.filename} in case {case.case_id} has digest {digest}, expected {v_ref.sha256}."
            )

        views.append(
            View(
                stream=io.BytesIO(raw_bytes),
                filename=v_ref.filename,
                role=v_ref.role,
                label=v_ref.label,
                acquisition_method=v_ref.acquisition_method,
            )
        )

    return views


def run_replay_case(case: CaseDefinition, run_log: RunLog) -> Report:
    """Run one replay case through Seam 1 (analyse_incident) with ReplayAIConnection."""
    views = load_case_views(case)
    ai = ReplayAIConnection(case.case_dir)
    return analyse_incident(
        views=views,
        origin=case.origin,
        ai=ai,
        run_log=run_log,
        prompt_version=case.prompt_version,
    )


def assert_report_matches_expected(
    report: Report,
    expected: dict[str, Any],
    run: Run | None = None,
) -> None:
    """Assert exclusively on the Report data object (and Run repair_outcome if present) against expected fields."""
    unknown = set(expected.keys()) - ALLOWED_EXPECTED_KEYS
    if unknown:
        raise ValueError(f"Unknown keys in expected: {sorted(unknown)}")

    # 1. Validity & Provenance
    if "is_valid" in expected:
        assert report.is_valid is expected["is_valid"], (
            f"Expected is_valid={expected['is_valid']}, got {report.is_valid}. Errors: {report.validation_errors}"
        )

    if "provenance" in expected:
        assert report.provenance == expected["provenance"], (
            f"Expected provenance={expected['provenance']}, got {report.provenance}"
        )

    # 2. Repair outcome
    if "repair_outcome" in expected:
        exp_repair = expected["repair_outcome"]
        actual_repair = getattr(report, "repair_outcome", None) if run is None else run.repair_outcome
        assert actual_repair == exp_repair, f"Expected repair_outcome={exp_repair!r}, got {actual_repair!r}"

    # 3. Summary
    if "summary" in expected and expected["summary"] is not None:
        assert expected["summary"] in report.summary, (
            f"Expected summary substring {expected['summary']!r} not in {report.summary!r}"
        )

    # 4. Validation errors
    if "validation_errors" in expected:
        assert list(report.validation_errors) == list(expected["validation_errors"]), (
            f"Expected validation_errors={expected['validation_errors']!r}, got {list(report.validation_errors)!r}"
        )
    elif expected.get("is_valid") is True:
        assert report.validation_errors == (), (
            f"Expected empty validation_errors for valid report, got {report.validation_errors}"
        )

    if expected.get("is_valid") is False:
        assert len(report.validation_errors) > 0, "Expected validation_errors on invalid report, got none"
        for pattern in expected.get("validation_error_patterns", []):
            assert any(pattern in err for err in report.validation_errors), (
                f"Pattern {pattern!r} not found in validation_errors: {report.validation_errors}"
            )

    view_hashes = {v.sha256 for v in report.views}

    # 5. Parties
    exp_parties = expected.get("parties")
    if exp_parties is not None:
        assert report.facts is not None, "Expected facts with parties on Report, but report.facts is None"
        actual_parties = report.facts.parties
        assert len(actual_parties) == len(exp_parties), (
            f"Party count mismatch: expected {len(exp_parties)}, got {len(actual_parties)}"
        )
        for act_party, exp_party in zip(actual_parties, exp_parties, strict=True):
            party_unknown = set(exp_party.keys()) - ALLOWED_PARTY_KEYS
            if party_unknown:
                raise ValueError(f"Unknown keys in party: {sorted(party_unknown)}")
            # Guardrail: never raw IDs
            assert not is_raw_id(act_party.description), (
                f"Party description {act_party.description!r} is a raw ID (AGENTS.md, ADR 0004)"
            )
            assert act_party.description == exp_party["description"]
            assert act_party.view_id in view_hashes, (
                f"Party view_id {act_party.view_id} not in report views {view_hashes}"
            )
            assert act_party.timestamp_seconds == exp_party["timestamp_seconds"]
            assert act_party.confidence == exp_party["confidence"]
            assert act_party.confidence >= DEFAULT_MIN_CONFIDENCE, (
                f"Confidence {act_party.confidence} below threshold {DEFAULT_MIN_CONFIDENCE} (ADR 0006)"
            )
            if "color" in exp_party and exp_party["color"] is not None:
                assert act_party.color == exp_party["color"]
            if "make_model" in exp_party and exp_party["make_model"] is not None:
                assert act_party.make_model == exp_party["make_model"]
            if "body_type" in exp_party and exp_party["body_type"] is not None:
                assert act_party.body_type == exp_party["body_type"]

            # Reserved for RPT-15: pink/green/white fault status per party
            if exp_party.get("fault_status") is not None:
                assert getattr(act_party, "fault_status", None) == exp_party["fault_status"]

    # 6. Events
    exp_events = expected.get("events")
    if exp_events is not None:
        assert report.facts is not None, "Expected facts with events on Report, but report.facts is None"
        actual_events = report.facts.events
        assert len(actual_events) == len(exp_events), (
            f"Event count mismatch: expected {len(exp_events)}, got {len(actual_events)}"
        )
        for act_event, exp_event in zip(actual_events, exp_events, strict=True):
            event_unknown = set(exp_event.keys()) - ALLOWED_EVENT_KEYS
            if event_unknown:
                raise ValueError(f"Unknown keys in event: {sorted(event_unknown)}")
            assert act_event.description == exp_event["description"]
            assert act_event.view_id in view_hashes, (
                f"Event view_id {act_event.view_id} not in report views {view_hashes}"
            )
            assert act_event.timestamp_seconds == exp_event["timestamp_seconds"]
            assert act_event.confidence == exp_event["confidence"]
            assert act_event.confidence >= DEFAULT_MIN_CONFIDENCE

    # 7. Candidate collisions
    exp_collisions = expected.get("candidate_collisions")
    if exp_collisions is not None:
        assert report.facts is not None, "Expected facts with candidate_collisions, but report.facts is None"
        actual_collisions = report.facts.candidate_collisions
        assert len(actual_collisions) == len(exp_collisions), (
            f"Collision count mismatch: expected {len(exp_collisions)}, got {len(actual_collisions)}"
        )
        for act_collision, exp_collision in zip(actual_collisions, exp_collisions, strict=True):
            collision_unknown = set(exp_collision.keys()) - ALLOWED_COLLISION_KEYS
            if collision_unknown:
                raise ValueError(f"Unknown keys in collision: {sorted(collision_unknown)}")
            assert act_collision.description == exp_collision["description"]
            assert act_collision.view_id in view_hashes, (
                f"Collision view_id {act_collision.view_id} not in report views {view_hashes}"
            )
            assert act_collision.timestamp_seconds == exp_collision["timestamp_seconds"]
            assert act_collision.confidence == exp_collision["confidence"]
            assert act_collision.confidence >= DEFAULT_MIN_CONFIDENCE

    # 8. Overall fault status (Reserved for RPT-15)
    if expected.get("overall_fault") is not None:
        assert getattr(report, "overall_fault", None) == expected["overall_fault"]
