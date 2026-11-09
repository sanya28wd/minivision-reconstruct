"""Overview-9 (RPT-36 T3, T8): required typed facts, first-touch timing and crossing signal state."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from conftest import make_view
from incident_report.agreement import AgreementSettings
from incident_report.ai import AIRequest, AIViewRef
from incident_report.ai.gemini import (
    GeminiAIConnection,
    GeminiSettings,
    PublicResearchVideo,
    overview_request_hash,
    prompt_for_version,
)
from incident_report.ai.google_video import GoogleVideoTransport
from incident_report.ai.overview_replay import ReplayOverviewAIConnection
from incident_report.domain import Origin, Role
from incident_report.entry import analyse_incident
from incident_report.facts import OVERVIEW_7_SCHEMA
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.model_calls import ModelCall
from incident_report.runlog import RunLog
from test_google_video_transport import FakeClient
from test_live_gemini import PUBLIC_VIDEO, FakeTransport


def test_overview9_extends_overview8_with_observation_only_contract() -> None:
    previous = prompt_for_version("overview-8")
    current = prompt_for_version("overview-9")
    assert current.startswith(previous)
    contract = current[len(previous) :]
    for text in (
        "touching at the contact location",
        "not a participant",
        "earliest visible",
        "Never invent an unseen first-touch frame",
        "single-vehicle rollovers",
        "not a new contact",
        "party_type",
        "two-wheeler with its rider",
        "motion_state_before_contact",
        "movement",
        "roundabout_entry_state",
        "not_in_roundabout",
        "entry_origin",
        "relative_position_of_other twice",
        "including while circulating inside a roundabout",
        "possible control locations are visible",
        "none_confirmed",
        "circulating_roundabout",
        "signal_state_when_crossing_stop_line",
        "including vehicles that are in",
        "crosses its stop line",
        "not_visible",
        "Never omit",
        "Cite the moment",
    ):
        assert text in contract
    assert not re.search(r"\b(legal|fault|violations?|fines?|priority)\b", contract, re.IGNORECASE)


def test_overview8_prompt_bytes_unchanged() -> None:
    # Captured from fresh-start 3ef3067 before adding overview-9.
    expected = "1720df2a5f16cad2cf8a20e6d73c0c5679120d299ac7056597b8059685fde84b"
    assert hashlib.sha256(prompt_for_version("overview-8").encode()).hexdigest() == expected


def night1_shape(*, lead_signal: str | None = "green") -> dict[str, Any]:
    records = [
        ("scene", "conflict_zone_type", "signalised_junction"),
        ("scene", "officer_directing_traffic", "no"),
    ]
    for party in ("camera vehicle", "dark sedan ahead"):
        records += [
            (party, "party_type", "car"),
            (party, "movement", "straight"),
            (party, "approach_control_faced", "signal"),
        ]
    records.append(("camera vehicle", "signal_state_when_crossing_stop_line", "green"))
    if lead_signal is not None:
        records.append(("dark sedan ahead", "signal_state_when_crossing_stop_line", lead_signal))
    return {
        "parties": [
            {"description": name, "timestamp_seconds": 0.5, "confidence": 0.9}
            for name in ("camera vehicle", "dark sedan ahead")
        ],
        "events": [],
        "candidate_collisions": [],
        "behaviour_observations": [],
        "location_conditions": [],
        "text_readings": [],
        "driver_observations": [],
        "collision_observations": [],
        "drowsiness_observations": [],
        "structured_facts": [
            dict(subject=subject, field=field, value=value, other_party="", timestamp_seconds=0.5, confidence=0.9)
            for subject, field, value in records
        ],
    }


def test_overview9_live_and_replay_use_overview7_schema(tmp_path: Path) -> None:
    video = tmp_path / "synthetic.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    req = AIRequest(incident_input_hash([(Role.EXTERIOR, digest)]), "overview-9", (AIViewRef(Role.EXTERIOR, digest),))
    settings = GeminiSettings(model="gemini-3.8-flash")
    transport = FakeTransport([json.dumps(night1_shape())])
    response = GeminiAIConnection(settings, PublicResearchVideo(video, True), transport=transport).analyse(req)
    assert response.facts is not None
    assert transport.calls[0][0].startswith(prompt_for_version("overview-9"))
    assert ReplayOverviewAIConnection(settings, response.calls).analyse(req).facts == response.facts
    eight = AIRequest(req.input_hash, "overview-8", req.views)
    assert overview_request_hash(req, settings) != overview_request_hash(eight, settings)
    client = FakeClient()
    GoogleVideoTransport(client).generate(PublicResearchVideo(video, True), prompt_for_version("overview-9"), settings)
    assert client.generated["config"]["response_schema"] == OVERVIEW_7_SCHEMA


@pytest.mark.parametrize(
    "lead_signal,expected", [("green", "No Incident"), (None, "Cannot Determine"), ("red", "No collision")]
)
def test_overview9_night1_shape_signal_state_decides_incident_status(
    lead_signal: str | None, expected: str, run_log: RunLog
) -> None:
    """T8: a green crossing for every signal-facing vehicle is what lets night1 read No Incident."""
    digest = sha256_hex(PUBLIC_VIDEO)
    request = AIRequest(
        incident_input_hash([(Role.EXTERIOR, digest)]), "overview-9", (AIViewRef(Role.EXTERIOR, digest),)
    )
    settings = GeminiSettings(model="gemini-3.8-flash")
    call = ModelCall(
        request_hash=overview_request_hash(request, settings),
        input_hash=request.input_hash,
        prompt_version="overview-9",
        model=settings.model,
        view_id=digest,
        fps=settings.fps,
        raw_response=json.dumps(night1_shape(lead_signal=lead_signal)),
        latency_ms=0,
        retry_index=0,
    )
    report = analyse_incident(
        [make_view(PUBLIC_VIDEO)],
        Origin.REPLAY,
        ai=ReplayOverviewAIConnection(settings, (call,)),
        run_log=run_log,
        prompt_version="overview-9",
        agreement_settings=AgreementSettings(n_runs=3),
    )
    assert report.is_valid
    assert report.incident_status == expected
