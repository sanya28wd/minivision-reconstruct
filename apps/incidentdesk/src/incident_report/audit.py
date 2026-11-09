"""Tamper-evident audit trail (RPT-14, spec 001).

Append-only, hash-chained audit log in PostgreSQL. Each row stores the previous row's
hash plus its own hash over a canonical serialization of its fields. Database triggers
block UPDATE and DELETE on audit rows. A verify command walks the chain and reports the
first broken row.

Canonical serialization contract:
- Keys are sorted lexicographically.
- No whitespace separators ("," and ":").
- UTC ISO 8601 timestamps with microsecond precision and 'Z' suffix (YYYY-MM-DDTHH:MM:SS.ffffffZ).
- Hash algorithm: SHA-256.
- Genesis row (sequence_number = 1) prev_hash: 64 zeros.
- Prohibited fields: URLs, headers, bodies, filenames, cookies, secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from incident_report.config import Settings

GENESIS_HASH: str = "0" * 64

# Arbitrary constant key so concurrent audit appends serialize without forking the chain.
_AUDIT_LOCK_KEY: int = 7_450_014

FORBIDDEN_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        "url",
        "urls",
        "header",
        "headers",
        "body",
        "bodies",
        "filename",
        "filenames",
        "cookie",
        "cookies",
        "secret",
        "secrets",
        "token",
        "tokens",
        "password",
        "passwords",
        "authorization",
    }
)


def canonical_timestamp(dt: datetime) -> str:
    """Format UTC datetime with 6-digit microsecond precision and 'Z' suffix."""
    utc_dt = dt.astimezone(UTC)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(data: Any) -> str:
    """Deterministic JSON serialization with sorted keys and no extraneous whitespace."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_entry_hash(
    *,
    sequence_number: int,
    event_id: uuid.UUID,
    occurred_at: datetime,
    event_type: str,
    actor: str,
    session_id: uuid.UUID | None,
    link_id: uuid.UUID | None,
    request_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    action: str,
    status_code: int | None,
    target_type: str | None,
    target_id: str | None,
    details: dict[str, Any],
    prev_hash: str,
) -> str:
    """Compute the SHA-256 digest over the canonical representation of an audit row."""
    payload = {
        "action": action,
        "actor": actor,
        "details": details,
        "event_id": str(event_id),
        "event_type": event_type,
        "link_id": str(link_id) if link_id else None,
        "occurred_at": canonical_timestamp(occurred_at),
        "prev_hash": prev_hash,
        "request_id": str(request_id) if request_id else None,
        "run_id": str(run_id) if run_id else None,
        "sequence_number": sequence_number,
        "session_id": str(session_id) if session_id else None,
        "status_code": status_code,
        "target_id": str(target_id) if target_id else None,
        "target_type": target_type,
    }
    serialized = canonical_json(payload)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    sequence_number: int
    event_id: uuid.UUID
    occurred_at: datetime
    event_type: str
    actor: str
    session_id: uuid.UUID | None
    link_id: uuid.UUID | None
    request_id: uuid.UUID | None
    run_id: uuid.UUID | None
    action: str
    status_code: int | None
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    total_entries: int
    first_broken_sequence: int | None = None
    first_broken_event_id: uuid.UUID | None = None
    reason: str | None = None


class AuditLog:
    """PostgreSQL-backed append-only, hash-chained audit log."""

    def __init__(self, database_url: str) -> None:
        self._url = database_url

    @property
    def database_url(self) -> str:
        return self._url

    def append(
        self,
        *,
        event_type: str,
        action: str,
        actor: str = "system",
        occurred_at: datetime | None = None,
        session_id: uuid.UUID | None = None,
        link_id: uuid.UUID | None = None,
        request_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        status_code: int | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        details: dict[str, Any] | None = None,
        conn: psycopg.Connection | None = None,
    ) -> AuditEntry:
        clean_details = dict(details) if details is not None else {}
        for k in clean_details:
            if k.lower() in FORBIDDEN_DETAIL_KEYS:
                raise ValueError(
                    f"Forbidden detail key {k!r}: audit log never stores URLs, headers, bodies, "
                    f"filenames, cookies or secrets."
                )

        if conn is not None:
            return self._append_with_conn(
                conn,
                event_type=event_type,
                action=action,
                actor=actor,
                occurred_at=occurred_at,
                session_id=session_id,
                link_id=link_id,
                request_id=request_id,
                run_id=run_id,
                status_code=status_code,
                target_type=target_type,
                target_id=target_id,
                details=clean_details,
            )

        with psycopg.connect(self._url) as new_conn:
            with new_conn.transaction():
                return self._append_with_conn(
                    new_conn,
                    event_type=event_type,
                    action=action,
                    actor=actor,
                    occurred_at=occurred_at,
                    session_id=session_id,
                    link_id=link_id,
                    request_id=request_id,
                    run_id=run_id,
                    status_code=status_code,
                    target_type=target_type,
                    target_id=target_id,
                    details=clean_details,
                )

    def _append_with_conn(
        self,
        conn: psycopg.Connection,
        *,
        event_type: str,
        action: str,
        actor: str,
        occurred_at: datetime | None,
        session_id: uuid.UUID | None,
        link_id: uuid.UUID | None,
        request_id: uuid.UUID | None,
        run_id: uuid.UUID | None,
        status_code: int | None,
        target_type: str | None,
        target_id: str | None,
        details: dict[str, Any],
    ) -> AuditEntry:
        # Advisory transaction lock serializes concurrent appends so the chain never forks.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_LOCK_KEY,))

        tip = conn.execute(
            "SELECT sequence_number, hash FROM audit_log ORDER BY sequence_number DESC LIMIT 1 FOR UPDATE"
        ).fetchone()

        if tip is None:
            seq_num = 1
            prev_hash = GENESIS_HASH
        else:
            seq_num = tip[0] + 1
            prev_hash = tip[1]

        event_id = uuid.uuid4()
        timestamp = occurred_at if occurred_at is not None else datetime.now(UTC)

        entry_hash = compute_entry_hash(
            sequence_number=seq_num,
            event_id=event_id,
            occurred_at=timestamp,
            event_type=event_type,
            actor=actor,
            session_id=session_id,
            link_id=link_id,
            request_id=request_id,
            run_id=run_id,
            action=action,
            status_code=status_code,
            target_type=target_type,
            target_id=target_id,
            details=details,
            prev_hash=prev_hash,
        )

        conn.execute(
            "INSERT INTO audit_log ("
            "sequence_number, event_id, occurred_at, event_type, actor, "
            "session_id, link_id, request_id, run_id, action, "
            "status_code, target_type, target_id, details, prev_hash, hash"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                seq_num,
                event_id,
                timestamp,
                event_type,
                actor,
                session_id,
                link_id,
                request_id,
                run_id,
                action,
                status_code,
                target_type,
                target_id,
                Jsonb(details),
                prev_hash,
                entry_hash,
            ),
        )

        return AuditEntry(
            sequence_number=seq_num,
            event_id=event_id,
            occurred_at=timestamp,
            event_type=event_type,
            actor=actor,
            session_id=session_id,
            link_id=link_id,
            request_id=request_id,
            run_id=run_id,
            action=action,
            status_code=status_code,
            target_type=target_type,
            target_id=target_id,
            details=details,
            prev_hash=prev_hash,
            hash=entry_hash,
        )

    def verify(self, conn: psycopg.Connection | None = None) -> VerificationResult:
        if conn is None:
            with psycopg.connect(self._url) as new_conn:
                return self._verify_with_conn(new_conn)
        return self._verify_with_conn(conn)

    def _verify_with_conn(self, conn: psycopg.Connection) -> VerificationResult:
        rows = conn.execute(
            "SELECT sequence_number, event_id, occurred_at, event_type, actor, "
            "session_id, link_id, request_id, run_id, action, status_code, "
            "target_type, target_id, details, prev_hash, hash "
            "FROM audit_log ORDER BY sequence_number ASC"
        ).fetchall()

        expected_prev_hash = GENESIS_HASH
        expected_seq = 1

        for row in rows:
            (
                seq_num,
                evt_id,
                occ_at,
                evt_type,
                actor,
                sess_id,
                lnk_id,
                req_id,
                r_id,
                act,
                stat,
                tgt_t,
                tgt_id,
                det,
                p_hash,
                h,
            ) = row

            if seq_num != expected_seq:
                return VerificationResult(
                    valid=False,
                    total_entries=len(rows),
                    first_broken_sequence=seq_num,
                    first_broken_event_id=evt_id,
                    reason=f"Sequence number mismatch: expected {expected_seq}, got {seq_num}",
                )

            if p_hash != expected_prev_hash:
                return VerificationResult(
                    valid=False,
                    total_entries=len(rows),
                    first_broken_sequence=seq_num,
                    first_broken_event_id=evt_id,
                    reason=f"prev_hash mismatch: expected {expected_prev_hash}, got {p_hash}",
                )

            expected_hash = compute_entry_hash(
                sequence_number=seq_num,
                event_id=evt_id,
                occurred_at=occ_at,
                event_type=evt_type,
                actor=actor,
                session_id=sess_id,
                link_id=lnk_id,
                request_id=req_id,
                run_id=r_id,
                action=act,
                status_code=stat,
                target_type=tgt_t,
                target_id=tgt_id,
                details=det if isinstance(det, dict) else {},
                prev_hash=p_hash,
            )

            if h != expected_hash:
                return VerificationResult(
                    valid=False,
                    total_entries=len(rows),
                    first_broken_sequence=seq_num,
                    first_broken_event_id=evt_id,
                    reason=f"hash mismatch: expected {expected_hash}, got {h}",
                )

            expected_prev_hash = h
            expected_seq += 1

        return VerificationResult(valid=True, total_entries=len(rows))

    def list_entries(self, conn: psycopg.Connection | None = None) -> list[AuditEntry]:
        if conn is None:
            with psycopg.connect(self._url) as new_conn:
                return self._list_entries_with_conn(new_conn)
        return self._list_entries_with_conn(conn)

    def _list_entries_with_conn(self, conn: psycopg.Connection) -> list[AuditEntry]:
        rows = conn.execute(
            "SELECT sequence_number, event_id, occurred_at, event_type, actor, "
            "session_id, link_id, request_id, run_id, action, status_code, "
            "target_type, target_id, details, prev_hash, hash "
            "FROM audit_log ORDER BY sequence_number ASC"
        ).fetchall()
        return [
            AuditEntry(
                sequence_number=r[0],
                event_id=r[1],
                occurred_at=r[2],
                event_type=r[3],
                actor=r[4],
                session_id=r[5],
                link_id=r[6],
                request_id=r[7],
                run_id=r[8],
                action=r[9],
                status_code=r[10],
                target_type=r[11],
                target_id=r[12],
                details=r[13] if isinstance(r[13], dict) else {},
                prev_hash=r[14],
                hash=r[15],
            )
            for r in rows
        ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m incident_report.audit",
        description="Tamper-evident audit log verification (RPT-14).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    verify_cmd = commands.add_parser("verify", help="verify audit log hash chain integrity")
    verify_cmd.add_argument("--database-url", help="PostgreSQL connection URL (defaults to DATABASE_URL)")
    return parser


def main(argv: Sequence[str]) -> int:
    """Command-line entry point for audit verification.

    Returns 0 on unbroken chain, 1 on tampered chain or error, 2 on usage error.
    """
    args = _parser().parse_args(list(argv))
    if args.command == "verify":
        db_url = args.database_url or os.environ.get("DATABASE_URL", "").strip()
        if not db_url:
            try:
                db_url = Settings.from_env().database_url
            except Exception as exc:
                print(f"Error: DATABASE_URL is not set: {exc}", file=sys.stderr)
                return 2
        try:
            audit = AuditLog(db_url)
            result = audit.verify()
            if result.valid:
                print(f"Audit chain valid: {result.total_entries} entries verified.")
                return 0
            print(
                f"Audit chain BROKEN at sequence {result.first_broken_sequence} "
                f"(event_id: {result.first_broken_event_id}): {result.reason}",
                file=sys.stderr,
            )
            return 1
        except Exception as exc:
            print(f"Database error during audit verification: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
