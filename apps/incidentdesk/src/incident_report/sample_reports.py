"""Sample reports (RPT-40): ready-made Reports built from recorded model output, with no AI call.

`samples/` holds, for each sample, the recorded `ModelCall`s of one real smoke run on a research clip
(written by scripts/export_sample_recordings.py) and a manifest. This module replays those calls through
the normal incident entry point, so rules, fines and citations are computed by the current code and are
never copied from the old run. Nothing here is a model response written by hand: a recording that no
longer replays (for example after a prompt or schema change) is dropped with a warning, never patched.

The clips' licence is unconfirmed (dataset/manifest.json), so the pages say "research footage" only.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict, Field

from incident_report.agreement import AgreementSettings
from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import GeminiSettings, overview_request_hash, parse_overview
from incident_report.ai.interface import AIRequest, AIResponse
from incident_report.ai.overview_replay import ReplayOverviewAIConnection
from incident_report.config import ProcessingSettings
from incident_report.domain import InvalidIncident, Origin, Report, ReportView, Role, Run
from incident_report.entry import analyse_incident
from incident_report.model_calls import ModelCall
from incident_report.runlog import RunLog
from incident_report.storage.interface import StoredObject

logger = logging.getLogger(__name__)

# Shown at the top of every sample page and in its title. No word "public": the licence is unverified.
BANNER = "Sample report from research footage — not a real case."

DEFAULT_SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"
SAMPLES_DIR_ENV = "INCIDENT_SAMPLES_DIR"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _ManifestView(_Frozen):
    filename: str
    role: Role
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int
    mime_type: str
    duration_seconds: float


class _ManifestEntry(_Frozen):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    title: str
    source_clip: str
    licence: str
    source_run_id: uuid.UUID
    recording: str
    prompt_version: str
    view: _ManifestView


class _Manifest(_Frozen):
    schema_version: int
    samples: tuple[_ManifestEntry, ...]


class _Recording(_Frozen):
    calls: tuple[ModelCall, ...]


@dataclass(frozen=True)
class SampleReport:
    sample_id: str
    title: str
    source_clip: str
    licence: str
    report: Report


def samples_dir_from_env() -> Path:
    configured = os.environ.get(SAMPLES_DIR_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_SAMPLES_DIR


class _SampleReplay(ReplayOverviewAIConnection):
    """Replays recorded calls, including the recorded repair call the live connection made.

    The live connection (ai/gemini.py) answers an unparseable first answer with one repair call whose
    prompt version is "<v>-repair". Replay mirrors that: `analyse` only sees the calls of the request's
    own prompt version, and when that answer does not parse it falls back to the recorded repair call.
    The repair call is never taken as the first answer.
    """

    def analyse(self, request: AIRequest) -> AIResponse:
        own = tuple(c for c in self.calls if c.prompt_version == request.prompt_version)
        try:
            return ReplayOverviewAIConnection(self.settings, own).analyse(request)
        except AIConnectionError as exc:
            if request.prompt_version == "overview-1":
                raise
            repaired = self.repair(request, AIResponse("Invalid overview facts.", "recorded-gemini-api"), [str(exc)])
            if repaired.facts is None:
                raise
            return repaired

    def repair(self, request: AIRequest, response: AIResponse, errors: Sequence[str]) -> AIResponse:
        key = overview_request_hash(request, self.settings)
        repair_version = request.prompt_version + "-repair"
        for call in reversed(self.calls):
            if call.prompt_version == repair_version and call.request_hash == key and call.raw_response is not None:
                view = request.views[0]
                try:
                    facts = parse_overview(
                        call.raw_response, view.sha256, prompt_version=request.prompt_version, view_role=view.role.value
                    )
                except AIConnectionError:
                    break
                return AIResponse("Overview facts repaired.", "recorded-gemini-api", facts, calls=response.calls)
        return AIResponse("Overview facts repair failed.", "recorded-gemini-api", None, calls=response.calls)


class _NoRunLog(RunLog):
    """A Run log that stores nothing: a sample is not an incident anyone filed."""

    def __init__(self) -> None:  # deliberately no database
        pass

    def record(self, run: Run) -> None:
        return None

    def update_step(self, run_id: uuid.UUID, current_step: str) -> None:
        return None


class _NoEvidence:
    """The entry point asks for an evidence store; samples carry no video, so nothing is ever stored."""

    def stage(self) -> AbstractContextManager[BinaryIO]:
        raise AIConnectionError("Samples hold no video.")

    def put(self, stream: BinaryIO, *, sha256: str, deadline: float | None = None) -> StoredObject:
        raise AIConnectionError("Samples hold no video.")

    def get(self, sha256: str) -> BinaryIO:
        raise AIConnectionError("Samples hold no video.")

    def exists(self, sha256: str) -> bool:
        return False


def _build(entry: _ManifestEntry, samples_dir: Path) -> SampleReport:
    recording_path = (samples_dir / entry.recording).resolve()
    if not recording_path.is_relative_to(samples_dir.resolve()):
        raise ValueError("A recording must live inside the samples folder.")
    if entry.source_clip != entry.view.filename:
        raise ValueError("The source clip name must match the view's file name.")
    calls = _Recording.model_validate_json(recording_path.read_text(encoding="utf-8")).calls
    if not calls:
        raise ValueError("A recording holds no model calls.")
    settings = GeminiSettings(model=calls[0].model, fps=calls[0].fps)
    view = ReportView(
        filename=entry.view.filename,
        role=entry.view.role,
        label=None,
        acquisition_method=None,
        sha256=entry.view.sha256,
        size_bytes=entry.view.size_bytes,
        mime_type=entry.view.mime_type,
        duration_seconds=entry.view.duration_seconds,
    )
    report = analyse_incident(
        (view,),
        Origin.REPLAY,
        ai=_SampleReplay(settings, calls),
        run_log=_NoRunLog(),
        evidence=_NoEvidence(),
        prompt_version=entry.prompt_version,
        agreement_settings=AgreementSettings(n_runs=1),
        processing_settings=ProcessingSettings(),
        run_id=entry.source_run_id,
    )
    if not report.is_valid:
        raise ValueError("The recording no longer produces a valid report: " + "; ".join(report.validation_errors))
    return SampleReport(entry.id, entry.title, entry.source_clip, entry.licence, report)


def load_samples(samples_dir: Path | None = None) -> tuple[SampleReport, ...]:
    """Build every sample that still replays. Never calls a model; skips (and logs) any that cannot."""
    directory = samples_dir if samples_dir is not None else samples_dir_from_env()
    manifest_path = directory / "manifest.json"
    try:
        manifest = _Manifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        logger.warning("No sample reports: %s does not exist.", manifest_path)
        return ()
    except (OSError, ValueError):
        logger.warning("No sample reports: %s is unreadable.", manifest_path)
        return ()
    built: list[SampleReport] = []
    for entry in manifest.samples:
        try:
            built.append(_build(entry, directory))
        except (OSError, ValueError, AIConnectionError, InvalidIncident, TimeoutError) as exc:
            logger.warning("Sample %s dropped, its recording no longer replays: %s", entry.id, exc)
    return tuple(built)
