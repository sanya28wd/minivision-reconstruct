"""Deployment environment and evidence storage settings (RPT-03, ADR 0011)."""

from __future__ import annotations

from pathlib import Path

import pytest

import incident_report
from incident_report.config import ConfigError, Environment, Settings
from incident_report.storage import LocalEvidenceStore, StorageConfigError, create_evidence_store

BASE = {"DATABASE_URL": "postgresql:///x"}


def test_the_environment_defaults_to_dev_and_names_one_of_three() -> None:
    assert Settings.from_env(BASE).environment is Environment.DEV
    assert [e.value for e in Environment] == ["dev", "staging", "production"]
    for name in ("dev", "staging", "production"):
        assert Settings.from_env({**BASE, "INCIDENT_ENV": name}).environment.value == name


def test_an_unknown_environment_name_is_refused_with_the_allowed_names() -> None:
    with pytest.raises(ConfigError, match=r"INCIDENT_ENV.*dev, staging, production"):
        Settings.from_env({**BASE, "INCIDENT_ENV": "prod"})


def test_no_evidence_directory_means_no_evidence_store() -> None:
    settings = Settings.from_env(BASE)

    assert settings.evidence_dir is None
    assert create_evidence_store(settings) is None


def test_a_local_evidence_directory_gives_a_local_store(tmp_path: Path) -> None:
    settings = Settings.from_env({**BASE, "INCIDENT_EVIDENCE_DIR": str(tmp_path / "ev")})

    assert settings.evidence_dir == tmp_path / "ev"
    assert isinstance(create_evidence_store(settings), LocalEvidenceStore)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_the_local_backend_is_refused_in_staging_and_production(environment: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"local.*Cloud Run.*in-memory"):
        Settings.from_env({**BASE, "INCIDENT_ENV": environment, "INCIDENT_EVIDENCE_DIR": str(tmp_path)})


def test_asking_for_the_cloud_storage_backend_says_it_is_not_built_yet() -> None:
    with pytest.raises(ConfigError, match="Cloud Storage backend is not available yet"):
        Settings.from_env({**BASE, "INCIDENT_ENV": "staging", "INCIDENT_STORAGE_BACKEND": "gcs"})


def test_an_unknown_storage_backend_is_refused() -> None:
    with pytest.raises(ConfigError, match="INCIDENT_STORAGE_BACKEND"):
        Settings.from_env({**BASE, "INCIDENT_STORAGE_BACKEND": "s3"})


def test_an_evidence_directory_inside_the_package_is_refused_when_the_store_is_made() -> None:
    package_dir = Path(incident_report.__file__).resolve().parent
    settings = Settings.from_env({**BASE, "INCIDENT_EVIDENCE_DIR": str(package_dir / "evidence")})

    with pytest.raises(StorageConfigError):
        create_evidence_store(settings)
