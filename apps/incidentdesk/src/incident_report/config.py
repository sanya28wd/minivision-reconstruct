"""Settings come from environment variables. No defaults for anything secret."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from incident_report.ai.replay import DEFAULT_RECORDINGS_DIR


class ConfigError(RuntimeError):
    """A required setting is missing. The message says which one."""


class Environment(StrEnum):
    """Which deployment this process is (ADR 0011). Each has its own database, bucket and credentials."""

    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass(frozen=True)
class Settings:
    database_url: str
    recordings_dir: Path
    environment: Environment = Environment.DEV
    # Root folder of the LOCAL evidence backend; None means no evidence store is configured.
    evidence_dir: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        database_url = env.get("DATABASE_URL", "").strip()
        if not database_url:
            raise ConfigError("DATABASE_URL is not set. Point it at a PostgreSQL database (see README).")
        recordings = env.get("INCIDENT_RECORDINGS_DIR", "").strip()
        environment = _environment(env.get("INCIDENT_ENV", "").strip())
        evidence = env.get("INCIDENT_EVIDENCE_DIR", "").strip()
        _check_storage_backend(env.get("INCIDENT_STORAGE_BACKEND", "").strip(), bool(evidence), environment)
        return cls(
            database_url=database_url,
            recordings_dir=Path(recordings) if recordings else DEFAULT_RECORDINGS_DIR,
            environment=environment,
            evidence_dir=Path(evidence) if evidence else None,
        )


def _environment(name: str) -> Environment:
    if not name:
        return Environment.DEV
    try:
        return Environment(name)
    except ValueError:
        allowed = ", ".join(e.value for e in Environment)
        raise ConfigError(f"INCIDENT_ENV={name!r} is not one of: {allowed}.") from None


def _check_storage_backend(backend: str, evidence_dir_set: bool, environment: Environment) -> None:
    """Refuse combinations that would lose or misplace evidence (ADR 0011)."""
    if backend not in ("", "local", "gcs"):
        raise ConfigError(f"INCIDENT_STORAGE_BACKEND={backend!r} is not one of: local, gcs.")
    if backend == "gcs":
        raise ConfigError(
            "The Cloud Storage backend is not available yet (PENDING-OWNER: it needs a bucket and credentials "
            "that agents never hold). Use the local backend in dev only."
        )
    if (backend == "local" or evidence_dir_set) and environment is not Environment.DEV:
        raise ConfigError(
            f"The local evidence backend must not be used in {environment.value}: on Cloud Run the container "
            "filesystem is in-memory and does not persist (ADR 0011). Use the Cloud Storage backend."
        )


@dataclass(frozen=True)
class AccessSettings:
    # No default admin credential; unset always refuses admin access. Never include it in repr/logs.
    admin_secret: str | None = field(default=None, repr=False)
    # All limits below: project assumption, to be validated. Per-instance, not shared across workers.
    link_attempts: int = 30
    admin_attempts: int = 30
    window_seconds: int = 60
    session_seconds: int = 86400
    limiter_keys: int = 4096
    hsts_seconds: int = 31536000
    refusal_audit_attempts: int = 10

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AccessSettings:
        env = os.environ if env is None else env
        values = {}
        for name, default in (
            ("link_attempts", 30),
            ("admin_attempts", 30),
            ("window_seconds", 60),
            ("session_seconds", 86400),
            ("limiter_keys", 4096),
            ("hsts_seconds", 31536000),
            ("refusal_audit_attempts", 10),
        ):
            key = "INCIDENT_ACCESS_" + name.upper()
            try:
                value = int(env.get(key, str(default)))
            except ValueError:
                raise ConfigError(f"{key} must be a positive integer.") from None
            if value <= 0:
                raise ConfigError(f"{key} must be a positive integer.")
            values[name] = value
        return cls(admin_secret=env.get("INCIDENT_ADMIN_SECRET") or None, **values)


@dataclass(frozen=True)
class ProcessingSettings:
    """Per-stage timeouts for incident analysis (RPT-21).

    All limits below: project assumption, to be validated (ADR 0006).
    """

    overview_timeout_seconds: float = 60.0
    collision_timeout_seconds: float = 60.0
    rules_timeout_seconds: float = 30.0
    checks_timeout_seconds: float = 30.0
    stale_margin_seconds: float = 60.0
    agreement_runs: int = 1
    concurrent_analysis: bool = True

    @property
    def max_processing_seconds(self) -> float:
        """Maximum expected runtime before a processing run is considered stale/interrupted.

        Project assumption, to be validated: every overview/agreement run gets
        its own overview timeout; the remaining stages run once, plus margin.
        """
        return (
            self.agreement_runs * self.overview_timeout_seconds
            + self.collision_timeout_seconds
            + self.rules_timeout_seconds
            + self.checks_timeout_seconds
            + self.stale_margin_seconds
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProcessingSettings:
        env = os.environ if env is None else env
        values: dict[str, float] = {}
        for name, default in (
            ("overview_timeout_seconds", 60.0),
            ("collision_timeout_seconds", 60.0),
            ("rules_timeout_seconds", 30.0),
            ("checks_timeout_seconds", 30.0),
            ("stale_margin_seconds", 60.0),
        ):
            key = "INCIDENT_" + name.upper()
            try:
                value = float(env.get(key, str(default)))
            except ValueError:
                raise ConfigError(f"{key} must be a positive number.") from None
            if value <= 0:
                raise ConfigError(f"{key} must be a positive number.")
            values[name] = value
        concurrency = env.get("INCIDENT_CONCURRENT_ANALYSIS", "1")
        if concurrency not in {"0", "1"}:
            raise ConfigError("INCIDENT_CONCURRENT_ANALYSIS must be 0 or 1.")
        # Spec 003: officer/benchmark budgets describe one analysis; research repetition
        # remains opt-in through AgreementSettings, never through officer processing settings.
        return cls(agreement_runs=1, concurrent_analysis=concurrency == "1", **values)


@dataclass(frozen=True)
class PhoneUseSettings:
    # COMPARABLE_SYSTEMS.md L21: no vendor defines GPS versus entertainment.
    tapping_count: int = 3  # project assumption, to be validated
    tapping_window_seconds: float = 5.0  # project assumption, to be validated
    continuous_look_seconds: float = 3.0  # project assumption, to be validated

    def __post_init__(self) -> None:
        import math

        if isinstance(self.tapping_count, bool) or not isinstance(self.tapping_count, int) or self.tapping_count < 2:
            raise ConfigError("Phone tapping count must be an integer of at least two.")
        if any(not math.isfinite(v) or v <= 0 for v in (self.tapping_window_seconds, self.continuous_look_seconds)):
            raise ConfigError("Phone duration settings must be finite and positive.")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PhoneUseSettings:
        env = os.environ if env is None else env
        try:
            return cls(
                tapping_count=int(env.get("INCIDENT_PHONE_TAPPING_COUNT", "3")),
                tapping_window_seconds=float(env.get("INCIDENT_PHONE_TAPPING_WINDOW_SECONDS", "5")),
                continuous_look_seconds=float(env.get("INCIDENT_PHONE_CONTINUOUS_LOOK_SECONDS", "3")),
            )
        except ValueError:
            raise ConfigError("Phone settings must contain valid numbers.") from None
