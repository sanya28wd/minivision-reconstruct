from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from incident_report.ai.replay import DEFAULT_RECORDINGS_DIR
from incident_report.config import ConfigError, Settings
from incident_report.db import migrate


def test_migrating_twice_applies_nothing_the_second_time(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        assert migrate(conn) == []


def test_the_runs_table_refuses_a_succeeded_row_without_a_report(database_url: str) -> None:
    with psycopg.connect(database_url) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO runs (run_id, origin, created_at, input_hash, status, total_latency_ms)"
            " VALUES (gen_random_uuid(), 'app', now(), %s, 'succeeded', 1)",
            ("ab" * 32,),
        )


def test_settings_need_a_database_url() -> None:
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env({})


def test_settings_read_the_environment() -> None:
    s = Settings.from_env({"DATABASE_URL": "postgresql:///x", "INCIDENT_RECORDINGS_DIR": "/tmp/r"})
    assert (s.database_url, s.recordings_dir) == ("postgresql:///x", Path("/tmp/r"))
    assert Settings.from_env({"DATABASE_URL": "postgresql:///x"}).recordings_dir == DEFAULT_RECORDINGS_DIR
