"""ADR 0010: PostgreSQL-backed links and anonymous sessions.

Trust boundaries: bearer secrets -> hashes -> current database authorization; stable IDs
are attribution, never credentials. RPT-14 owns the hash-chained audit trail.
Token API/entropy source: https://docs.python.org/3/library/secrets.html#how-many-bytes-should-tokens-use
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from incident_report.audit import AuditLog
from incident_report.domain import Attribution

# Python secrets guidance recommends 32 bytes for typical security tokens (source above).
TOKEN_BYTES = 32


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Session:
    attribution: Attribution
    valid: bool
    blocked: bool


class AccessStore:
    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._audit = AuditLog(database_url)

    def create_link(self) -> dict[str, str]:
        link_id, secret = uuid.uuid4(), secrets.token_urlsafe(TOKEN_BYTES)
        with psycopg.connect(self._url) as conn:
            conn.execute(
                "INSERT INTO share_links (link_id, secret_hash) VALUES (%s, %s)", (link_id, secret_hash(secret))
            )
        return {"link_id": str(link_id), "secret": secret, "url": f"/share#{link_id}.{secret}"}

    def open_link(
        self, link_id: uuid.UUID, secret: str, lifetime: int, cookie: str | None = None
    ) -> tuple[Attribution, str] | None:
        previous = self.session(cookie)
        if previous and previous.blocked:
            return None
        with psycopg.connect(self._url) as conn:
            row = conn.execute(
                "SELECT secret_hash, enabled, generation FROM share_links WHERE link_id = %s FOR SHARE", (link_id,)
            ).fetchone()
            matches = secrets.compare_digest(secret_hash(secret), row[0] if row else "0" * 64)
            if not row or not matches or not row[1]:
                return None
            if previous and previous.valid and previous.attribution.link_id == link_id and cookie:
                return previous.attribution, cookie
            session_id, token = uuid.uuid4(), secrets.token_urlsafe(TOKEN_BYTES)
            conn.execute(
                "INSERT INTO anonymous_sessions (session_id, link_id, secret_hash, generation, expires_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (session_id, link_id, secret_hash(token), row[2], datetime.now(UTC) + timedelta(seconds=lifetime)),
            )
        return Attribution(session_id=session_id, link_id=link_id), f"{session_id}.{token}"

    def session(self, cookie: str | None) -> Session | None:
        if not cookie:
            return None
        try:
            raw_id, token = cookie.split(".", 1)
            session_id = uuid.UUID(raw_id)
        except ValueError:
            return None
        with psycopg.connect(self._url) as conn:
            row = conn.execute(
                "SELECT s.link_id, s.secret_hash, s.blocked, s.expires_at, s.generation = l.generation, l.enabled "
                "FROM anonymous_sessions s JOIN share_links l USING (link_id) WHERE s.session_id = %s",
                (session_id,),
            ).fetchone()
        matches = secrets.compare_digest(secret_hash(token), row[1] if row else "0" * 64)
        if not row or not matches:
            return None
        return Session(
            Attribution(session_id=session_id, link_id=row[0]),
            not row[2] and row[3] > datetime.now(UTC) and row[4] and row[5],
            row[2],
        )

    def list_sessions(self) -> list[dict[str, Any]]:
        with psycopg.connect(self._url, row_factory=dict_row) as conn:
            return conn.execute(
                "SELECT session_id, link_id, blocked, created_at, expires_at FROM anonymous_sessions "
                "ORDER BY created_at, session_id"
            ).fetchall()

    def change_link(self, link_id: uuid.UUID, *, rotate: bool) -> dict[str, str] | None:
        secret = secrets.token_urlsafe(TOKEN_BYTES)
        with psycopg.connect(self._url) as conn:
            if rotate:
                row = conn.execute(
                    "UPDATE share_links SET secret_hash = %s, generation = generation + 1 "
                    "WHERE link_id = %s RETURNING link_id",
                    (secret_hash(secret), link_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "UPDATE share_links SET enabled = false WHERE link_id = %s RETURNING link_id", (link_id,)
                ).fetchone()
        if not row:
            return None
        if rotate:
            return {"link_id": str(link_id), "secret": secret, "url": f"/share#{link_id}.{secret}"}
        return {"link_id": str(link_id)}

    def block_session(self, session_id: uuid.UUID) -> bool:
        with psycopg.connect(self._url) as conn:
            return (
                conn.execute(
                    "UPDATE anonymous_sessions SET blocked = true WHERE session_id = %s RETURNING session_id",
                    (session_id,),
                ).fetchone()
                is not None
            )

    def list_links(self) -> list[dict[str, Any]]:
        with psycopg.connect(self._url, row_factory=dict_row) as conn:
            return conn.execute(
                "SELECT link_id, enabled, generation, created_at FROM share_links ORDER BY created_at, link_id"
            ).fetchall()

    def record_event(
        self,
        *,
        request_id: uuid.UUID,
        actor: str,
        attribution: Attribution | None,
        action: str,
        status_code: int,
        run_id: uuid.UUID | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> None:
        # Deliberate allowlist: never URLs, headers, bodies, filenames, cookies or secrets.
        if run_id is None:
            with psycopg.connect(self._url) as conn:
                row = conn.execute("SELECT run_id FROM runs WHERE request_id = %s", (request_id,)).fetchone()
                run_id = row[0] if row else None
        event_type = "admin" if actor == "admin" else "access"
        self._audit.append(
            event_type=event_type,
            action=action,
            actor=actor,
            session_id=attribution.session_id if attribution else None,
            link_id=attribution.link_id if attribution else None,
            request_id=request_id,
            run_id=run_id,
            status_code=status_code,
            target_type=target_type,
            target_id=target_id,
            details={},
        )

    def list_events(self) -> list[dict[str, Any]]:
        with psycopg.connect(self._url, row_factory=dict_row) as conn:
            return conn.execute(
                "SELECT event_id, request_id, occurred_at, actor, session_id, link_id, action, status_code, run_id "
                "FROM audit_log WHERE event_type IN ('access', 'admin') ORDER BY sequence_number"
            ).fetchall()


class RateLimiter:
    """Bounded fixed windows per endpoint group and peer IP, per instance (not distributed).

    All counts/caps/windows come from AccessSettings: project assumption, to be validated.
    Forwarded headers are deliberately ignored; deployment must configure trusted proxy peers.
    """

    def __init__(self, window: int, max_keys: int) -> None:
        import threading

        self._lock = threading.Lock()
        self._window = window
        self._max_keys = max_keys
        self._buckets: dict[tuple[str, str], tuple[float, int]] = {}

    def allow(self, group: str, peer: str, limit: int) -> bool:
        import time

        now = time.monotonic()
        with self._lock:
            self._buckets = {key: value for key, value in self._buckets.items() if value[0] > now}
            key = (group, peer)
            if key not in self._buckets and len(self._buckets) >= self._max_keys:
                return False
            end, count = self._buckets.get(key, (now + self._window, 0))
            if count >= limit:
                return False
            self._buckets[key] = end, count + 1
            return True
