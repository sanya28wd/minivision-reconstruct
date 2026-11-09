"""Reconstruction settings do not initialize AI or network clients."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def native_tools_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    value = values.get("INCIDENTDESK_NATIVE_TOOLS", "false").strip().lower()
    if value not in {"false", "0", "true", "1", ""}:
        raise ValueError("INCIDENTDESK_NATIVE_TOOLS must be true or false")
    return value in {"true", "1"}


@dataclass(frozen=True)
class ReconstructionSettings:
    enabled: bool = False
    evidence_exports_root: Path | None = None
    driver_context_enabled: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ReconstructionSettings":
        values = os.environ if env is None else env
        flag = values.get("INCIDENTDESK_RECONSTRUCTION", "").lower().strip()
        if flag not in ("", "0", "false", "1", "true"):
            raise ValueError("INCIDENTDESK_RECONSTRUCTION must be true or false")
        root = values.get("INCIDENTDESK_EVIDENCE_EXPORTS_ROOT", "").strip()
        driver_flag = values.get("INCIDENTDESK_DRIVER_CONTEXT", "").lower().strip()
        if driver_flag not in ("", "0", "false", "1", "true"):
            raise ValueError("INCIDENTDESK_DRIVER_CONTEXT must be true or false")
        return cls(flag in ("1", "true"), Path(root) if root else None, driver_flag in ("1", "true"))
