"""Recorded/replay adapter: returns saved responses keyed by input hash + prompt version."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from incident_report.ai.errors import AIConnectionError, RecordingInvalid, RecordingNotFound
from incident_report.ai.gemini import parse_overview
from incident_report.ai.interface import AIRequest, AIResponse
from incident_report.facts import OverviewFacts
from incident_report.model_calls import ModelCall

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_PROMPT_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

DEFAULT_RECORDINGS_DIR = Path(__file__).parent / "recordings"


class ReplayAIConnection:
    """Looks up `<dir>/<input_hash>.<prompt_version>.json`.

    A missing recording raises RecordingNotFound; it never invents an answer. Key parts are
    validated before they touch the filesystem, so a request cannot name a path.
    """

    def __init__(self, recordings_dir: Path = DEFAULT_RECORDINGS_DIR) -> None:
        self._dir = Path(recordings_dir)

    def analyse(self, request: AIRequest) -> AIResponse:
        if not _HASH_RE.fullmatch(request.input_hash):
            raise AIConnectionError("The input hash is not a SHA-256 hex digest.")
        if not _PROMPT_VERSION_RE.fullmatch(request.prompt_version):
            raise AIConnectionError("The prompt version contains characters that are not allowed.")

        path = self._dir / f"{request.input_hash}.{request.prompt_version}.json"
        if not path.is_file():
            raise RecordingNotFound(
                f"No recorded AI response exists for input {request.input_hash[:12]}... and prompt "
                f"version {request.prompt_version!r}. Nothing was made up. Add a recording or use "
                "an input that has one."
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["input_hash"] != request.input_hash or data["prompt_version"] != request.prompt_version:
                raise RecordingInvalid(f"Recording {path.name} does not match its own key.")
            summary = data["response"]["summary"]
            provenance = data["provenance"]
            if not (isinstance(summary, str) and summary and isinstance(provenance, str) and provenance):
                raise ValueError("summary and provenance must be non-empty text")

            facts_val: OverviewFacts | None = None
            resp = data.get("response", {})
            view_id = request.views[0].sha256 if request.views else ""
            if "facts" in resp and resp["facts"] is not None:
                facts_val = parse_overview(
                    json.dumps(resp["facts"]),
                    view_id,
                    prompt_version=request.prompt_version,
                    view_role=request.views[0].role.value if request.views else None,
                )
            elif "raw_response" in resp and resp["raw_response"] is not None:
                facts_val = parse_overview(
                    resp["raw_response"],
                    view_id,
                    prompt_version=request.prompt_version,
                    view_role=request.views[0].role.value if request.views else None,
                )

            calls_val: tuple[ModelCall, ...] = ()
            if "calls" in data and isinstance(data["calls"], list):
                calls_val = tuple(ModelCall.model_validate(c) for c in data["calls"])
        except RecordingInvalid:
            raise
        except (OSError, ValueError, KeyError, TypeError, ValidationError, AIConnectionError) as exc:
            raise RecordingInvalid(f"Recording {path.name} is unreadable or incomplete.") from exc
        return AIResponse(summary=summary, provenance=provenance, facts=facts_val, calls=calls_val)

    def repair(self, request: AIRequest, response: AIResponse, errors: Sequence[str]) -> AIResponse:
        repair_path = self._dir / f"{request.input_hash}.{request.prompt_version}-repair.json"
        data: dict[str, Any] | None = None
        if repair_path.is_file():
            with contextlib.suppress(OSError, ValueError):
                data = json.loads(repair_path.read_text(encoding="utf-8"))
        if data is None:
            main_path = self._dir / f"{request.input_hash}.{request.prompt_version}.json"
            if main_path.is_file():
                with contextlib.suppress(OSError, ValueError):
                    main_data = json.loads(main_path.read_text(encoding="utf-8"))
                    cand = main_data.get("repair_response") or main_data.get("repair")
                    if isinstance(cand, dict):
                        data = cand

        if data is None:
            return AIResponse(
                summary="Repair not recorded.",
                provenance=response.provenance,
                facts=None,
                calls=response.calls,
            )

        resp = data.get("response", data)
        repair_summary = resp.get("summary", "Repaired summary.")
        repair_provenance = data.get("provenance", response.provenance)
        view_id = request.views[0].sha256 if request.views else ""

        facts_val: OverviewFacts | None = None
        if "facts" in resp and resp["facts"] is not None:
            with contextlib.suppress(ValidationError, ValueError, KeyError, TypeError):
                facts_val = parse_overview(
                    json.dumps(resp["facts"]),
                    view_id,
                    prompt_version=request.prompt_version,
                    view_role=request.views[0].role.value if request.views else None,
                )
        elif "raw_response" in resp and resp["raw_response"] is not None:
            with contextlib.suppress(AIConnectionError, ValidationError, ValueError, KeyError, TypeError):
                facts_val = parse_overview(
                    resp["raw_response"],
                    view_id,
                    prompt_version=request.prompt_version,
                    view_role=request.views[0].role.value if request.views else None,
                )

        repair_calls = response.calls
        if "calls" in data and isinstance(data["calls"], list):
            with contextlib.suppress(ValidationError, ValueError, KeyError, TypeError):
                repair_calls = response.calls + tuple(ModelCall.model_validate(c) for c in data["calls"])

        return AIResponse(
            summary=repair_summary,
            provenance=repair_provenance,
            facts=facts_val,
            calls=repair_calls,
        )
