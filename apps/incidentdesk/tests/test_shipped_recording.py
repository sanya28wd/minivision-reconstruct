"""The one recording that ships in the package: a hand-written fixture labelled synthetic."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_view
from incident_report.ai import ReplayAIConnection
from incident_report.ai.replay import DEFAULT_RECORDINGS_DIR
from incident_report.domain import Origin, Role
from incident_report.entry import analyse_incident
from incident_report.runlog import RunLog
from incident_report.samples import SAMPLE_VIEW_BYTES


def test_the_sample_view_replays_the_shipped_recording(run_log: RunLog) -> None:
    report = analyse_incident(
        [make_view(SAMPLE_VIEW_BYTES, Role.EXTERIOR, filename="sample.mp4")],
        Origin.APP,
        ai=ReplayAIConnection(),
        run_log=run_log,
    )

    assert report.provenance == "synthetic"
    assert "Synthetic placeholder" in report.summary


def test_every_shipped_recording_says_it_is_synthetic() -> None:
    files = list(Path(DEFAULT_RECORDINGS_DIR).glob("*.json"))
    assert files, "expected at least the walking-skeleton fixture"
    for f in files:
        assert json.loads(f.read_text(encoding="utf-8"))["provenance"] == "synthetic", f.name
