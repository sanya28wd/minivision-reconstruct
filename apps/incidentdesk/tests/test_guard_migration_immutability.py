"""The migration-immutability guard: an immutable table must also block TRUNCATE (RPT-14)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import guard_migration_immutability

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "guard_migration_immutability.py"
REAL_MIGRATIONS = Path(__file__).resolve().parent.parent / "src" / "incident_report" / "migrations"

TABLE = "CREATE TABLE ledger (id int PRIMARY KEY);\n"
FUNCTION = "CREATE FUNCTION ledger_stop() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'no'; END; $$ LANGUAGE plpgsql;\n"
ROW_TRIGGER = (
    "CREATE TRIGGER ledger_no_update_delete\nBEFORE UPDATE OR DELETE ON ledger\nFOR EACH ROW\n"
    "EXECUTE FUNCTION ledger_stop();\n"
)
TRUNCATE_TRIGGER = "CREATE TRIGGER ledger_no_truncate\nBEFORE TRUNCATE ON ledger\nFOR EACH STATEMENT\nEXECUTE FUNCTION ledger_stop();\n"


def run_guard(folder: Path) -> int:
    return guard_migration_immutability.main(["--migrations", str(folder)])


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / "0001_ledger.down.sql").write_text("DROP TABLE ledger;\n", encoding="utf-8")
    return tmp_path


def write(folder: Path, name: str, text: str) -> None:
    (folder / name).write_text(text, encoding="utf-8")


def test_the_real_migrations_pass() -> None:
    assert run_guard(REAL_MIGRATIONS) == 0


def test_an_immutable_table_without_a_truncate_trigger_fails(folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(folder, "0001_ledger.sql", TABLE + FUNCTION + ROW_TRIGGER)

    assert run_guard(folder) == 1

    out = capsys.readouterr().out
    assert "ledger" in out
    assert "TRUNCATE" in out


def test_an_immutable_table_with_both_triggers_passes(folder: Path) -> None:
    write(folder, "0001_ledger.sql", TABLE + FUNCTION + ROW_TRIGGER + TRUNCATE_TRIGGER)

    assert run_guard(folder) == 0


def test_the_truncate_trigger_may_arrive_in_a_later_migration(folder: Path) -> None:
    write(folder, "0001_ledger.sql", TABLE + FUNCTION + ROW_TRIGGER)
    write(folder, "0002_ledger_truncate.sql", TRUNCATE_TRIGGER)

    assert run_guard(folder) == 0


@pytest.mark.parametrize("events", ["UPDATE", "DELETE", "UPDATE OR DELETE"])
def test_update_and_delete_triggers_each_need_the_truncate_trigger(folder: Path, events: str) -> None:
    trigger = f"CREATE TRIGGER t BEFORE {events} ON ledger FOR EACH ROW EXECUTE FUNCTION ledger_stop();\n"
    write(folder, "0001_ledger.sql", TABLE + FUNCTION + trigger)

    assert run_guard(folder) == 1


def test_a_row_level_truncate_declaration_does_not_count(folder: Path) -> None:
    # PostgreSQL itself refuses this, but the guard must not accept it as protection either.
    bad = "CREATE TRIGGER t2 BEFORE TRUNCATE ON ledger FOR EACH ROW EXECUTE FUNCTION ledger_stop();\n"
    write(folder, "0001_ledger.sql", TABLE + FUNCTION + ROW_TRIGGER + bad)

    assert run_guard(folder) == 1


def test_an_after_truncate_trigger_does_not_count(folder: Path) -> None:
    after = "CREATE TRIGGER t2 AFTER TRUNCATE ON ledger FOR EACH STATEMENT EXECUTE FUNCTION ledger_stop();\n"
    write(folder, "0001_ledger.sql", TABLE + FUNCTION + ROW_TRIGGER + after)

    assert run_guard(folder) == 1


def test_a_truncate_trigger_on_another_table_does_not_count(folder: Path) -> None:
    other = "CREATE TRIGGER t2 BEFORE TRUNCATE ON other FOR EACH STATEMENT EXECUTE FUNCTION ledger_stop();\n"
    write(folder, "0001_ledger.sql", TABLE + FUNCTION + ROW_TRIGGER + other)

    assert run_guard(folder) == 1


def test_a_commented_out_truncate_trigger_does_not_count(folder: Path) -> None:
    commented = (
        "-- CREATE TRIGGER ledger_no_truncate BEFORE TRUNCATE ON ledger FOR EACH STATEMENT EXECUTE FUNCTION f();\n"
    )
    write(folder, "0001_ledger.sql", TABLE + FUNCTION + ROW_TRIGGER + commented)

    assert run_guard(folder) == 1


def test_dropping_the_truncate_trigger_later_is_caught(folder: Path) -> None:
    write(folder, "0001_ledger.sql", TABLE + FUNCTION + ROW_TRIGGER + TRUNCATE_TRIGGER)
    write(folder, "0002_loosen.sql", "DROP TRIGGER ledger_no_truncate ON ledger;\n")

    assert run_guard(folder) == 1


def test_down_migrations_are_not_read(folder: Path) -> None:
    write(folder, "0001_ledger.sql", TABLE)
    write(folder, "0001_ledger.down.sql", FUNCTION + ROW_TRIGGER)

    assert run_guard(folder) == 0


def test_a_table_with_no_blocking_trigger_needs_nothing(folder: Path) -> None:
    other = "CREATE TRIGGER t AFTER INSERT ON ledger FOR EACH ROW EXECUTE FUNCTION ledger_stop();\n"
    write(folder, "0001_ledger.sql", TABLE + FUNCTION + other)

    assert run_guard(folder) == 0


def test_no_migrations_is_an_error_not_a_pass(tmp_path: Path) -> None:
    assert run_guard(tmp_path) == 2


def test_the_command_line_accepts_the_common_base_flag() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--migrations", str(REAL_MIGRATIONS), "--base", "fresh-start"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
