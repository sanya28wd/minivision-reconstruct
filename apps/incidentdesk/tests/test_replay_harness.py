"""Replay test harness test suite (RPT-16, spec 001).

Asserts on Report data objects using recorded AI responses with zero live calls.
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path
from typing import Any

import pytest

from incident_report.ai.google_video import GoogleVideoTransport
from incident_report.domain import Origin, Report, ReportView, Role
from incident_report.entry import PROMPT_VERSION
from incident_report.facts import OverviewFacts, PartyFact
from incident_report.runlog import RunLog
from replay_harness import (
    CaseDefinition,
    ViewReference,
    assert_report_matches_expected,
    discover_replay_cases,
    load_case_views,
    run_replay_case,
)
from synthetic_clips import CONTACT_CLIP_SHA256


@pytest.fixture(autouse=True)
def guard_no_live_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforce that no live Gemini transport or external network socket can be opened."""

    # 1. Block construction of the live Gemini transport
    def forbidden_transport(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Live Gemini transport construction is forbidden in replay harness.")

    monkeypatch.setattr(GoogleVideoTransport, "__init__", forbidden_transport)

    # 2. Block external network sockets, allowing only loopback/localhost for PostgreSQL
    original_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost"):
            original_connect(self, address)
            return
        raise RuntimeError(f"Network socket connection to {address} is forbidden in replay harness.")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def test_guard_blocks_live_gemini_transport_construction() -> None:
    with pytest.raises(RuntimeError, match="Live Gemini transport construction is forbidden"):
        GoogleVideoTransport(None)


def test_guard_blocks_external_socket_connection() -> None:
    with pytest.raises(RuntimeError, match="Network socket connection .* is forbidden"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("8.8.8.8", 53))


def test_discover_replay_cases() -> None:
    cases = discover_replay_cases()
    assert len(cases) >= 5
    case_ids = [c.case_id for c in cases]
    assert "case_01_two_party_contact" in case_ids
    assert "case_02_no_contact" in case_ids
    assert "case_03_citation_error_repair_fails" in case_ids
    assert "case_04_citation_error_repaired" in case_ids
    assert "case_05_walking_skeleton" in case_ids


@pytest.mark.parametrize("case", discover_replay_cases(), ids=lambda c: c.case_id)
def test_replay_case_runs_through_seam_1_and_matches_report(case: CaseDefinition, run_log: RunLog) -> None:
    report = run_replay_case(case, run_log)
    assert isinstance(report, Report)
    run = run_log.get(report.run_id)
    assert_report_matches_expected(report, case.expected, run=run)


def test_case_with_missing_clip_raises_filenotfound(tmp_path: Path) -> None:
    case = CaseDefinition(
        case_id="missing_clip",
        case_dir=tmp_path,
        description="case with unknown clip",
        prompt_version=PROMPT_VERSION,
        origin=Origin.REPLAY,
        views=(ViewReference(filename="unknown.mp4", role=Role.EXTERIOR, sha256="aa" * 32),),
        expected={},
    )
    with pytest.raises(FileNotFoundError, match="not found in the case folder"):
        load_case_views(case)


def test_case_with_tampered_hash_raises_value_error(tmp_path: Path) -> None:
    clip_file = tmp_path / "tampered.mp4"
    clip_file.write_bytes(b"tampered content")
    case = CaseDefinition(
        case_id="tampered_clip",
        case_dir=tmp_path,
        description="case with tampered clip",
        prompt_version=PROMPT_VERSION,
        origin=Origin.REPLAY,
        views=(ViewReference(filename="tampered.mp4", role=Role.EXTERIOR, sha256="bb" * 32),),
        expected={},
    )
    with pytest.raises(ValueError, match="expected"):
        load_case_views(case)


def test_assertion_detects_summary_mismatch() -> None:
    report = Report(
        run_id=uuid.uuid4(),
        input_hash="11" * 32,
        prompt_version="overview-1",
        provenance="recorded-gemini-api",
        summary="Actual summary.",
        views=(),
        facts=None,
        is_valid=True,
    )
    with pytest.raises(AssertionError, match="Expected summary substring"):
        assert_report_matches_expected(report, {"summary": "Completely different expected summary."})


def test_assertion_detects_raw_id_in_parties() -> None:
    view_hash = CONTACT_CLIP_SHA256
    view = ReportView(filename="front.mp4", role=Role.EXTERIOR, label=None, acquisition_method=None, sha256=view_hash)
    # Construct a report that bypassed party fact raw ID check (e.g. from an internal bug)
    party = object.__new__(PartyFact)
    object.__setattr__(party, "description", "Vehicle 1")
    object.__setattr__(party, "view_id", view_hash)
    object.__setattr__(party, "timestamp_seconds", 0.5)
    object.__setattr__(party, "confidence", 0.9)
    object.__setattr__(party, "color", None)
    object.__setattr__(party, "make_model", None)
    object.__setattr__(party, "body_type", None)

    facts = object.__new__(OverviewFacts)
    object.__setattr__(facts, "parties", [party])
    object.__setattr__(facts, "events", [])
    object.__setattr__(facts, "candidate_collisions", [])
    object.__setattr__(facts, "structured_facts", [])

    report = Report(
        run_id=uuid.uuid4(),
        input_hash="11" * 32,
        prompt_version="overview-1",
        provenance="recorded-gemini-api",
        summary="Summary.",
        views=(view,),
        facts=facts,
        is_valid=True,
    )
    with pytest.raises(AssertionError, match="raw ID"):
        assert_report_matches_expected(
            report,
            {"parties": [{"description": "Vehicle 1", "timestamp_seconds": 0.5, "confidence": 0.9}]},
        )


def test_assertion_detects_low_confidence() -> None:
    view_hash = CONTACT_CLIP_SHA256
    view = ReportView(filename="front.mp4", role=Role.EXTERIOR, label=None, acquisition_method=None, sha256=view_hash)
    party = PartyFact(
        description="red Toyota sedan",
        view_id=view_hash,
        timestamp_seconds=0.5,
        confidence=0.3,  # Below 0.5
    )
    report = Report(
        run_id=uuid.uuid4(),
        input_hash="11" * 32,
        prompt_version="overview-1",
        provenance="recorded-gemini-api",
        summary="Summary.",
        views=(view,),
        facts=OverviewFacts(
            behaviour_observations=[],
            location_conditions=[],
            parties=[party],
            events=[],
            candidate_collisions=[],
        ),
        is_valid=True,
    )
    with pytest.raises(AssertionError, match="below threshold"):
        assert_report_matches_expected(
            report,
            {"parties": [{"description": "red Toyota sedan", "timestamp_seconds": 0.5, "confidence": 0.3}]},
        )


def test_assertion_rejects_unknown_expected_keys() -> None:
    report = Report(
        run_id=uuid.uuid4(),
        input_hash="11" * 32,
        prompt_version="overview-1",
        provenance="synthetic-fixture",
        summary="Summary.",
        views=(),
        facts=None,
        is_valid=True,
    )
    with pytest.raises(ValueError, match="Unknown keys in expected.*unrecognised_key"):
        assert_report_matches_expected(report, {"unrecognised_key": "some_value"})


def test_assertion_detects_repair_outcome_mismatch() -> None:
    from datetime import UTC, datetime

    from incident_report.domain import Origin, Run, RunStatus

    run_id = uuid.uuid4()
    report = Report(
        run_id=run_id,
        input_hash="11" * 32,
        prompt_version="overview-1",
        provenance="synthetic-fixture",
        summary="Summary.",
        views=(),
        facts=None,
        is_valid=True,
    )
    run = Run(
        run_id=run_id,
        origin=Origin.REPLAY,
        created_at=datetime.now(UTC),
        input_hash="11" * 32,
        report=report,
        status=RunStatus.SUCCEEDED,
        total_latency_ms=10.0,
        repair_outcome="repaired",
    )
    with pytest.raises(AssertionError, match="Expected repair_outcome='failed', got 'repaired'"):
        assert_report_matches_expected(report, {"repair_outcome": "failed"}, run=run)


def test_assertion_detects_validation_errors_mismatch() -> None:
    report = Report(
        run_id=uuid.uuid4(),
        input_hash="11" * 32,
        prompt_version="overview-1",
        provenance="synthetic-fixture",
        summary="Summary.",
        views=(),
        facts=None,
        is_valid=False,
        validation_errors=("error 1",),
    )
    with pytest.raises(AssertionError, match="Expected validation_errors"):
        assert_report_matches_expected(report, {"validation_errors": ["error 2"]})
