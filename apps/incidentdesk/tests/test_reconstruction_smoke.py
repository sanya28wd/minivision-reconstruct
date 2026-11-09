"""The smoke helper must refuse test/remote databases before any side effects."""

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "integration/smoke/seed_smoke_incident.py"


@pytest.mark.parametrize(
    "database",
    [
        "postgresql://outside.example/incidentdesk_dev",
        "postgresql://localhost/incident_test_deadbeef",
        "postgresql://localhost/postgres",
        "host=outside.example hostaddr=127.0.0.1 dbname=incidentdesk_dev",
    ],
)
def test_smoke_refuses_remote_and_non_dev_databases(database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    monkeypatch.setenv("INCIDENT_ENV", "dev")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("must refuse before connecting")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    validate = cast(Callable[[str], str], runpy.run_path(str(SCRIPT))["development_database"])
    with pytest.raises(ValueError):
        validate(database)


@pytest.mark.parametrize(
    "test_url",
    [
        "postgresql://localhost:5433/incidentdesk_dev",
        "postgresql://[::1]:5433/incidentdesk_dev",
        "host=localhost port=5433",
    ],
)
def test_smoke_refuses_configured_test_database_even_with_local_alias(
    test_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INCIDENT_ENV", "dev")
    monkeypatch.setenv("TEST_DATABASE_URL", test_url)
    monkeypatch.setenv("PGDATABASE", "incidentdesk_dev")
    validate = cast(Callable[[str], str], runpy.run_path(str(SCRIPT))["development_database"])
    with pytest.raises(ValueError, match="test database"):
        validate("postgresql://127.0.0.1:5433/incidentdesk_dev")


def test_smoke_only_allows_development_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENT_ENV", "production")
    validate = cast(Callable[[str], str], runpy.run_path(str(SCRIPT))["development_database"])
    with pytest.raises(ValueError, match="development"):
        validate("postgresql://localhost/incidentdesk_dev")


def test_smoke_wrong_video_refuses_before_database_or_file_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import psycopg

    monkeypatch.setenv("INCIDENT_ENV", "dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost:5433/incidentdesk_dev")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    exports = tmp_path / "exports"
    evidence = tmp_path / "evidence"
    exports.mkdir()
    evidence.mkdir()
    video = tmp_path / "wrong.avi"
    video.write_bytes(b"synthetic wrong video")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("wrong video must refuse before connecting")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    main = cast(Callable[[list[str]], int], runpy.run_path(str(SCRIPT))["main"])
    with pytest.raises(SystemExit) as error:
        main(["--video", str(video), "--exports-root", str(exports), "--evidence-root", str(evidence)])
    assert error.value.code == 2
    assert "Smoke seed refused" in capsys.readouterr().err
    assert list(exports.iterdir()) == [] and list(evidence.iterdir()) == []
