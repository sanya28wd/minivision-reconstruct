"""Tamper-evident audit trail (RPT-14, spec 001).

Append-only, hash-chained audit log in PostgreSQL. Each row stores the previous row's hash
plus its own hash over a canonical serialization of its fields.
Database triggers block UPDATE and DELETE on audit rows.
A verify command walks the chain and reports the first broken row.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from audit_test_helpers import unsafe_test_only_truncate
from incident_report.audit import (
    GENESIS_HASH,
    AuditEntry,
    AuditLog,
    canonical_json,
    canonical_timestamp,
    compute_entry_hash,
    main,
)


def test_canonical_json_and_timestamp_are_deterministic() -> None:
    dt = datetime(2026, 10, 15, 14, 30, 0, 123456, tzinfo=UTC)
    assert canonical_timestamp(dt) == "2026-10-15T14:30:00.123456Z"

    payload = {"z": 1, "a": "text", "m": None}
    assert canonical_json(payload) == '{"a":"text","m":null,"z":1}'


def test_compute_entry_hash_matches_sha256() -> None:
    dt = datetime(2026, 10, 15, 12, 0, 0, 0, tzinfo=UTC)
    evt_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    expected_payload: dict[str, Any] = {
        "action": "test.action",
        "actor": "system",
        "details": {},
        "event_id": str(evt_id),
        "event_type": "test",
        "link_id": None,
        "occurred_at": "2026-10-15T12:00:00.000000Z",
        "prev_hash": GENESIS_HASH,
        "request_id": None,
        "run_id": None,
        "sequence_number": 1,
        "session_id": None,
        "status_code": 200,
        "target_id": None,
        "target_type": None,
    }
    raw = canonical_json(expected_payload)
    expected_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    h = compute_entry_hash(
        sequence_number=1,
        event_id=evt_id,
        occurred_at=dt,
        event_type="test",
        actor="system",
        session_id=None,
        link_id=None,
        request_id=None,
        run_id=None,
        action="test.action",
        status_code=200,
        target_type=None,
        target_id=None,
        details={},
        prev_hash=GENESIS_HASH,
    )
    assert h == expected_hash


def test_forbidden_detail_keys_are_rejected() -> None:
    audit = AuditLog("postgresql:///unused")
    for key in ("secret", "token", "password", "url", "header", "body", "filename", "cookie"):
        with pytest.raises(ValueError, match="Forbidden detail key"):
            audit.append(
                event_type="test",
                action="test.action",
                details={key: "value"},
            )


def test_append_creates_hash_chain_in_database(database_url: str) -> None:
    audit = AuditLog(database_url)
    unsafe_test_only_truncate(database_url, "audit_log")

    e1 = audit.append(
        event_type="access",
        action="GET /",
        actor="anonymous",
        status_code=200,
    )
    assert e1.sequence_number == 1
    assert e1.prev_hash == GENESIS_HASH
    assert e1.hash != GENESIS_HASH

    e2 = audit.append(
        event_type="admin",
        action="POST /admin/links",
        actor="admin",
        status_code=201,
        target_type="link",
        target_id="test-link-1",
    )
    assert e2.sequence_number == 2
    assert e2.prev_hash == e1.hash

    result = audit.verify()
    assert result.valid
    assert result.total_entries == 2
    assert result.first_broken_sequence is None


def test_database_triggers_block_update_and_delete(database_url: str) -> None:
    audit = AuditLog(database_url)
    unsafe_test_only_truncate(database_url, "audit_log")

    entry = audit.append(
        event_type="test",
        action="immutable.test",
        actor="system",
    )

    with psycopg.connect(database_url) as conn:
        with pytest.raises(psycopg.Error, match="immutable: UPDATE operations are forbidden"):
            conn.execute(
                "UPDATE audit_log SET action = 'tampered' WHERE sequence_number = %s",
                (entry.sequence_number,),
            )
        conn.rollback()

        with pytest.raises(psycopg.Error, match="immutable: DELETE operations are forbidden"):
            conn.execute(
                "DELETE FROM audit_log WHERE sequence_number = %s",
                (entry.sequence_number,),
            )


def test_truncate_audit_log_fails_and_keeps_every_row(database_url: str) -> None:
    audit = AuditLog(database_url)
    unsafe_test_only_truncate(database_url, "audit_log")
    audit.append(event_type="test", action="event-1")
    audit.append(event_type="test", action="event-2")

    for statement in (
        "TRUNCATE audit_log",
        "TRUNCATE TABLE audit_log RESTART IDENTITY",
        "TRUNCATE audit_log CASCADE",
        "TRUNCATE audit_log, runs",
    ):
        with psycopg.connect(database_url) as conn:
            with pytest.raises(psycopg.Error, match="immutable: TRUNCATE operations are forbidden"):
                conn.execute(statement)

    result = audit.verify()
    assert result.valid
    assert result.total_entries == 2


def test_test_only_truncate_helper_really_skips_the_guard_and_only_inside_its_transaction(
    database_url: str,
) -> None:
    audit = AuditLog(database_url)
    unsafe_test_only_truncate(database_url, "audit_log")
    audit.append(event_type="test", action="event-1")

    unsafe_test_only_truncate(database_url, "audit_log")
    assert audit.list_entries() == []

    # The bypass was SET LOCAL: the guard is back for the very next statement.
    audit.append(event_type="test", action="event-after")
    with psycopg.connect(database_url) as conn:
        with pytest.raises(psycopg.Error, match="TRUNCATE operations are forbidden"):
            conn.execute("TRUNCATE audit_log")
    assert len(audit.list_entries()) == 1


def test_production_code_never_uses_the_audit_bypass() -> None:
    src = Path(__file__).resolve().parent.parent / "src"
    forbidden = (
        "session_replication_role",
        "disable trigger",
        "disable_trigger",
        "unsafe_test_only",
        "audit_test_helpers",
        "truncate",
        "drop trigger",
        "alter table audit_log",
    )
    offenders: list[str] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".sql"}:
            continue
        # Migrations legitimately create and drop the guards; they are not application code paths.
        if path.parent.name == "migrations":
            continue
        text = path.read_text(encoding="utf-8").lower()
        offenders.extend(f"{path.relative_to(src)}: {word}" for word in forbidden if word in text)
    assert offenders == []

    public = {name for name in dir(AuditLog) if not name.startswith("_")}
    assert not {name for name in public if any(w in name for w in ("truncate", "wipe", "reset", "clear", "delete"))}


def test_verify_detects_manually_altered_row(database_url: str) -> None:
    audit = AuditLog(database_url)
    unsafe_test_only_truncate(database_url, "audit_log")

    audit.append(event_type="test", action="event-1")
    e2 = audit.append(event_type="test", action="event-2")
    audit.append(event_type="test", action="event-3")

    assert audit.verify().valid

    # Superuser alters row 2 by temporarily disabling the immutability trigger
    with psycopg.connect(database_url) as conn:
        conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete")
        conn.execute("UPDATE audit_log SET action = 'tampered-action' WHERE sequence_number = 2")
        conn.execute("ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_no_update_delete")

    result = audit.verify()
    assert not result.valid
    assert result.first_broken_sequence == 2
    assert result.first_broken_event_id == e2.event_id
    assert "hash mismatch" in str(result.reason)


def test_verify_detects_deleted_row(database_url: str) -> None:
    audit = AuditLog(database_url)
    unsafe_test_only_truncate(database_url, "audit_log")

    audit.append(event_type="test", action="event-1")
    audit.append(event_type="test", action="event-2")
    audit.append(event_type="test", action="event-3")

    # Superuser deletes row 2
    with psycopg.connect(database_url) as conn:
        conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete")
        conn.execute("DELETE FROM audit_log WHERE sequence_number = 2")
        conn.execute("ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_no_update_delete")

    result = audit.verify()
    assert not result.valid
    assert result.first_broken_sequence == 3  # Gap detected at row 3


def test_concurrent_appends_serialize_without_forking_chain(database_url: str) -> None:
    audit = AuditLog(database_url)
    unsafe_test_only_truncate(database_url, "audit_log")

    num_threads = 8
    appends_per_thread = 5
    total_events = num_threads * appends_per_thread

    def worker(worker_id: int) -> list[AuditEntry]:
        results: list[AuditEntry] = []
        for i in range(appends_per_thread):
            entry = audit.append(
                event_type="test",
                action=f"concurrent.worker.{worker_id}.{i}",
                actor="system",
                details={"worker_id": worker_id, "step": i},
            )
            results.append(entry)
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, w) for w in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    result = audit.verify()
    assert result.valid
    assert result.total_entries == total_events

    entries = audit.list_entries()
    assert len(entries) == total_events
    seqs = [e.sequence_number for e in entries]
    assert seqs == list(range(1, total_events + 1))


def test_cli_verify_command(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = AuditLog(database_url)
    unsafe_test_only_truncate(database_url, "audit_log")

    audit.append(event_type="test", action="event-1")
    audit.append(event_type="test", action="event-2")

    monkeypatch.setenv("DATABASE_URL", database_url)
    assert main(["verify"]) == 0
    assert "Audit chain valid: 2 entries verified." in capsys.readouterr().out

    # Tamper with row
    with psycopg.connect(database_url) as conn:
        conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete")
        conn.execute("UPDATE audit_log SET prev_hash = 'bad-hash' WHERE sequence_number = 2")
        conn.execute("ALTER TABLE audit_log ENABLE ALWAYS TRIGGER audit_log_no_update_delete")

    assert main(["verify"]) == 1
    err = capsys.readouterr().err
    assert "BROKEN at sequence 2" in err


def test_upload_and_run_events_exist_after_an_upload(database_url: str, recordings_dir: Path, tmp_path: Path) -> None:
    from conftest import write_recording
    from incident_report.ai.replay import ReplayAIConnection
    from incident_report.domain import Origin, Role, View
    from incident_report.entry import PROMPT_VERSION, analyse_incident
    from incident_report.hashing import incident_input_hash, sha256_hex
    from incident_report.runlog import RunLog
    from incident_report.storage import LocalEvidenceStore
    from video_fixtures import synthetic_video

    content = synthetic_video(b"audit-upload-test")
    content_hash = sha256_hex(content)
    write_recording(
        recordings_dir,
        incident_input_hash([(Role.EXTERIOR, content_hash)]),
        PROMPT_VERSION,
        "Audit upload summary.",
    )

    run_log = RunLog(database_url)
    unsafe_test_only_truncate(database_url, "audit_log", "runs")

    import io

    evidence = LocalEvidenceStore(tmp_path / "evidence")
    view = View(
        stream=io.BytesIO(content),
        filename="car_front.mp4",
        role=Role.EXTERIOR,
        label="Front view",
    )

    report = analyse_incident(
        [view],
        Origin.APP,
        ai=ReplayAIConnection(recordings_dir),
        run_log=run_log,
        evidence=evidence,
    )

    audit = run_log.audit
    entries = audit.list_entries()

    # Upload event exists
    upload_entries = [e for e in entries if e.event_type == "upload"]
    assert len(upload_entries) == 1
    assert upload_entries[0].run_id == report.run_id
    assert upload_entries[0].details["sha256"] == content_hash
    assert upload_entries[0].details["role"] == "exterior"
    assert "filename" not in upload_entries[0].details

    # Run event exists
    run_entries = [e for e in entries if e.event_type == "run"]
    assert len(run_entries) == 1
    assert run_entries[0].run_id == report.run_id
    assert run_entries[0].action == "run.succeeded"
    assert run_entries[0].details["status"] == "succeeded"

    # Entire chain verifies
    assert audit.verify().valid


def test_run_audit_event_records_the_repair_outcome(database_url: str) -> None:
    from incident_report.domain import Origin, Run, RunStatus
    from incident_report.runlog import RunLog

    unsafe_test_only_truncate(database_url, "audit_log", "runs")
    run_log = RunLog(database_url)
    run = Run(
        run_id=uuid.uuid4(),
        origin=Origin.APP,
        created_at=datetime.now(UTC),
        input_hash="22" * 32,
        report=None,
        status=RunStatus.FAILED,
        total_latency_ms=10.0,
        error="facts failed validation after repair",
        repair_attempts=1,
        repair_outcome="failed",
    )
    run_log.record(run)

    (entry,) = [e for e in run_log.audit.list_entries() if e.event_type == "run"]
    assert entry.run_id == run.run_id
    assert entry.action == "run.failed"
    assert entry.details["repair_attempts"] == 1
    assert entry.details["repair_outcome"] == "failed"
    assert run_log.get(run.run_id).repair_outcome == "failed"
    assert run_log.audit.verify().valid
