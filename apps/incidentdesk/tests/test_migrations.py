"""Reversible migrations (RPT-03): every migration has an up and a down; up -> down -> up is clean.

The shipped migrations are proven on a scratch database. The step/target mechanics are proven with small
synthetic migration sets in a temporary folder, so they do not depend on how many real migrations exist.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

import incident_report.db as db
from incident_report.db import MigrationError, load_migrations, migrate, rollback
from pgscratch import schema_snapshot, scratch_database, user_tables


def _write(folder: Path, name: str, sql_text: str) -> None:
    (folder / name).write_text(sql_text, encoding="utf-8")


@pytest.fixture
def three_migrations(tmp_path: Path) -> Path:
    folder = tmp_path / "migrations"
    folder.mkdir()
    _write(folder, "0001_create_a.sql", "CREATE TABLE a (id int PRIMARY KEY);")
    _write(folder, "0001_create_a.down.sql", "DROP TABLE a;")
    _write(
        folder,
        "0002_create_b.sql",
        "CREATE TABLE b (id int PRIMARY KEY, a_id int REFERENCES a (id)); CREATE INDEX b_a_idx ON b (a_id);",
    )
    _write(folder, "0002_create_b.down.sql", "DROP TABLE b;")
    _write(folder, "0003_add_a_note.sql", "ALTER TABLE a ADD COLUMN note text NOT NULL DEFAULT '';")
    _write(folder, "0003_add_a_note.down.sql", "ALTER TABLE a DROP COLUMN note;")
    return folder


def _applied(url: str) -> list[str]:
    with psycopg.connect(url) as conn:
        if conn.execute("SELECT to_regclass('schema_migrations')").fetchone() == (None,):
            return []  # a failed first migrate rolls the bookkeeping table back too
        return [r[0] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]


# --- the shipped migrations -------------------------------------------------------------------------


def test_every_shipped_migration_has_a_down() -> None:
    migrations = load_migrations()

    assert [m.name for m in migrations][:1] == ["0001_create_runs.sql"]
    assert all(m.down_sql.strip() for m in migrations)


def test_shipped_migrations_go_up_down_and_up_again_to_an_identical_schema() -> None:
    with scratch_database() as url:
        with psycopg.connect(url) as conn:
            applied = migrate(conn)
        assert applied[0] == "0001_create_runs.sql"
        first = schema_snapshot(url)
        assert any(line.startswith("column: public.runs run_id") for line in first)  # the 0001 table is covered

        with psycopg.connect(url) as conn:
            rolled_back = rollback(conn, to="base")
        assert rolled_back == list(reversed(applied))
        assert user_tables(url) == ["public.schema_migrations"]  # the runs table is gone, nothing else left
        assert schema_snapshot(url) == []

        with psycopg.connect(url) as conn:
            assert migrate(conn) == applied
        assert schema_snapshot(url) == first


def test_the_runs_table_works_again_after_a_round_trip() -> None:
    with scratch_database() as url:
        with psycopg.connect(url) as conn:
            migrate(conn)
            rollback(conn, to="base")
            migrate(conn)
            conn.execute(
                "INSERT INTO runs (run_id, origin, created_at, input_hash, report, status, total_latency_ms)"
                " VALUES (gen_random_uuid(), 'app', now(), %s, '{}'::jsonb, 'succeeded', 1)",
                ("ab" * 32,),
            )
            assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)


# --- steps and targets (synthetic migrations) -------------------------------------------------------


def test_migrate_applies_in_order_and_a_second_run_applies_nothing(three_migrations: Path) -> None:
    with scratch_database() as url, psycopg.connect(url) as conn:
        assert migrate(conn, source=three_migrations) == [
            "0001_create_a.sql",
            "0002_create_b.sql",
            "0003_add_a_note.sql",
        ]
        assert migrate(conn, source=three_migrations) == []


def test_rollback_one_step_undoes_only_the_latest(three_migrations: Path) -> None:
    with scratch_database() as url:
        with psycopg.connect(url) as conn:
            migrate(conn, source=three_migrations)
            assert rollback(conn, steps=1, source=three_migrations) == ["0003_add_a_note.sql"]
        assert _applied(url) == ["0001_create_a.sql", "0002_create_b.sql"]
        assert not any("a note" in line for line in schema_snapshot(url))


def test_rollback_n_steps_undoes_the_latest_n_newest_first(three_migrations: Path) -> None:
    with scratch_database() as url:
        with psycopg.connect(url) as conn:
            migrate(conn, source=three_migrations)
            assert rollback(conn, steps=2, source=three_migrations) == ["0003_add_a_note.sql", "0002_create_b.sql"]
        assert _applied(url) == ["0001_create_a.sql"]
        assert user_tables(url) == ["public.a", "public.schema_migrations"]


def test_rollback_to_a_target_keeps_the_target_applied(three_migrations: Path) -> None:
    with scratch_database() as url:
        with psycopg.connect(url) as conn:
            migrate(conn, source=three_migrations)
            assert rollback(conn, to="0001", source=three_migrations) == ["0003_add_a_note.sql", "0002_create_b.sql"]
        assert _applied(url) == ["0001_create_a.sql"]


def test_rollback_to_base_undoes_everything(three_migrations: Path) -> None:
    with scratch_database() as url:
        with psycopg.connect(url) as conn:
            migrate(conn, source=three_migrations)
            rollback(conn, to="base", source=three_migrations)
        assert _applied(url) == []
        assert user_tables(url) == ["public.schema_migrations"]


def test_up_down_up_gives_the_same_schema_for_every_depth(three_migrations: Path) -> None:
    with scratch_database() as url:
        with psycopg.connect(url) as conn:
            migrate(conn, source=three_migrations)
        full = schema_snapshot(url)
        for steps in (1, 2, 3):
            with psycopg.connect(url) as conn:
                rollback(conn, steps=steps, source=three_migrations)
                migrate(conn, source=three_migrations)
            assert schema_snapshot(url) == full


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"steps": 0}, "at least 1"),
        ({"steps": 4}, "only 3"),
        ({"to": "0009"}, "not an applied migration"),
        ({}, "exactly one"),
        ({"steps": 1, "to": "base"}, "exactly one"),
    ],
)
def test_rollback_refuses_unclear_requests_and_changes_nothing(
    three_migrations: Path, kwargs: dict[str, object], message: str
) -> None:
    with scratch_database() as url:
        with psycopg.connect(url) as conn:
            migrate(conn, source=three_migrations)
            with pytest.raises(MigrationError, match=message):
                rollback(conn, source=three_migrations, **kwargs)  # type: ignore[arg-type]
        assert len(_applied(url)) == 3


def test_a_migration_that_fails_leaves_the_whole_batch_unapplied(three_migrations: Path) -> None:
    _write(three_migrations, "0002_create_b.sql", "CREATE TABLE b (id int); SELECT this_is_not_sql;")
    with scratch_database() as url:
        with psycopg.connect(url) as conn, pytest.raises(psycopg.Error):
            migrate(conn, source=three_migrations)
        assert _applied(url) == []
        assert user_tables(url) == []  # not even table a from the good first migration


def test_a_down_that_fails_keeps_the_database_as_it_was(three_migrations: Path) -> None:
    _write(three_migrations, "0003_add_a_note.down.sql", "ALTER TABLE a DROP COLUMN note; SELECT this_is_not_sql;")
    with scratch_database() as url:
        with psycopg.connect(url) as conn:
            migrate(conn, source=three_migrations)
            with pytest.raises(psycopg.Error):
                rollback(conn, steps=1, source=three_migrations)
        assert len(_applied(url)) == 3
        assert any("a note" in line for line in schema_snapshot(url))


# --- the migration files themselves -----------------------------------------------------------------


def test_a_migration_without_a_down_is_refused(three_migrations: Path) -> None:
    (three_migrations / "0002_create_b.down.sql").unlink()

    with pytest.raises(MigrationError, match=r"0002_create_b\.sql.*down"):
        load_migrations(three_migrations)


def test_a_down_without_an_up_is_refused(three_migrations: Path) -> None:
    _write(three_migrations, "0004_orphan.down.sql", "SELECT 1;")

    with pytest.raises(MigrationError, match="0004_orphan"):
        load_migrations(three_migrations)


def test_an_empty_down_is_refused(three_migrations: Path) -> None:
    _write(three_migrations, "0003_add_a_note.down.sql", "  \n")

    with pytest.raises(MigrationError, match="empty"):
        load_migrations(three_migrations)


@pytest.mark.parametrize("bad", ["create_c.sql", "1_create_c.sql", "0004_Create-C.sql"])
def test_a_migration_with_a_badly_formed_name_is_refused(three_migrations: Path, bad: str) -> None:
    _write(three_migrations, bad, "SELECT 1;")

    with pytest.raises(MigrationError, match="name"):
        load_migrations(three_migrations)


def test_two_migrations_with_the_same_number_are_refused(three_migrations: Path) -> None:
    _write(three_migrations, "0003_other.sql", "SELECT 1;")
    _write(three_migrations, "0003_other.down.sql", "SELECT 1;")

    with pytest.raises(MigrationError, match="0003"):
        load_migrations(three_migrations)


def test_rolling_back_a_migration_whose_files_are_gone_is_refused(three_migrations: Path) -> None:
    with scratch_database() as url, psycopg.connect(url) as conn:
        migrate(conn, source=three_migrations)
        (three_migrations / "0003_add_a_note.sql").unlink()
        (three_migrations / "0003_add_a_note.down.sql").unlink()
        with pytest.raises(MigrationError, match="0003_add_a_note.sql"):
            rollback(conn, steps=1, source=three_migrations)


# --- the command line -------------------------------------------------------------------------------


def test_cli_migrate_status_and_rollback(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    shipped = [m.name for m in load_migrations()]  # the expectation follows the migrations folder
    with scratch_database() as url:
        monkeypatch.setenv("DATABASE_URL", url)

        assert db.main(["migrate"]) == 0
        assert "applied: " + ", ".join(shipped) in capsys.readouterr().out

        assert db.main(["status"]) == 0
        assert capsys.readouterr().out.splitlines()[1:] == [f"[applied] {name}" for name in shipped]

        assert db.main(["rollback", "--steps", str(len(shipped))]) == 2  # destructive: needs --yes
        assert "--yes" in capsys.readouterr().err
        assert _applied(url) == shipped

        assert db.main(["rollback", "--steps", str(len(shipped)), "--yes"]) == 0
        assert "rolled back: " + ", ".join(reversed(shipped)) in capsys.readouterr().out
        assert _applied(url) == []

        assert db.main(["status"]) == 0
        assert capsys.readouterr().out.splitlines()[1:] == [f"[pending] {name}" for name in shipped]


def test_cli_rollback_needs_exactly_one_of_steps_or_to(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql:///never_connected")

    assert db.main(["rollback", "--yes"]) == 2
    assert "--steps or --to" in capsys.readouterr().err
    assert db.main(["rollback", "--steps", "1", "--to", "base", "--yes"]) == 2
