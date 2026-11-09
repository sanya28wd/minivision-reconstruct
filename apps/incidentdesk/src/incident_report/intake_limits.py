"""All intake caps and deadlines are project assumption, to be validated (ADR 0006)."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields

from incident_report.config import ConfigError


@dataclass(frozen=True)
class IntakeLimits:
    # Each default below is a project assumption, to be validated; no provider/legal limit is claimed.
    max_upload_bytes: int = 512 * 1024 * 1024
    max_total_upload_bytes: int = 1024 * 1024 * 1024
    max_files: int = 10
    max_fields: int = 20
    max_duration_seconds: float = 3600
    upload_timeout_seconds: float = 120
    hash_timeout_seconds: float = 120
    store_timeout_seconds: float = 120
    probe_timeout_seconds: float = 30
    copy_timeout_seconds: float = 120
    max_probe_bytes: int = 4 * 1024 * 1024
    max_form_bytes: int = 16 * 1024
    stage_workers: int = 4
    staging_ttl_seconds: float = 900

    def __post_init__(self) -> None:
        # Developer Files expire after 48h (docs/research/PERF08_EARLY_UPLOAD.md).
        # Keep the local lease strictly shorter; defaults remain project assumptions.
        if self.staging_ttl_seconds >= 48 * 60 * 60:
            raise ConfigError("INCIDENT_STAGING_TTL_SECONDS must be less than Files retention (48 hours).")
        for field in fields(self):
            value = getattr(self, field.name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigError(f"INCIDENT_{field.name.upper()} must be positive and finite.")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> IntakeLimits:
        env = os.environ if env is None else env
        values: dict[str, int | float] = {}
        for field in fields(cls):
            name = "INCIDENT_" + field.name.upper()
            if name in env:
                try:
                    values[field.name] = (
                        int(env[name])
                        if field.name.endswith("bytes") or field.name in ("stage_workers", "max_files", "max_fields")
                        else float(env[name])
                    )
                except ValueError:
                    raise ConfigError(f"{name} must be a positive number.") from None
        return cls(**values)  # type: ignore[arg-type]  # field parser selects int for byte caps
