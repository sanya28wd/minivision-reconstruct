"""FIX-PROMPT-SIGNAL: versioned observation contract and conservative replay."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from conftest import make_view
from incident_report.agreement import AgreementSettings
from incident_report.ai import AIConnectionError, AIRequest, AIViewRef
from incident_report.ai.gemini import (
    GeminiAIConnection,
    GeminiSettings,
    PublicResearchVideo,
    overview_request_hash,
    parse_overview,
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


def test_overview8_extends_overview7_with_observation_only_contract() -> None:
    previous = prompt_for_version("overview-7")
    current = prompt_for_version("overview-8")
    assert current.startswith(previous)
    contract = current[len(previous) :]
    for text in (
        "every vehicle party",
        "exterior view",
        "one approach_control_faced",
        "signal",
        "stop_sign_or_line",
        "give_way_sign_or_line",
        "officer",
        "none_confirmed",
        "not_in_view",
        "unknown",
        "Never omit",
        "Cite the moment",
        "camera vehicle",
        "parties, subjects and other_party",
        "exactly",
        "approach is visible",
        "no signal, sign, marking or officer",
        "approach is not visible",
    ):
        assert text in contract
    assert not re.search(r"\b(legal|fault|violations?|fines?)\b", contract, re.IGNORECASE)


def test_overview1_through_7_prompt_bytes_match_prechange_baseline() -> None:
    # Captured from fresh-start d0eedf5 before adding overview-8.
    hashes = (
        "d6fbf14fc29b8a183e976be2c1ffb54c12957501d6f742b5dcbfbe44aa489385",
        "c238b04fe800ebfd12e54fab70bd3456f0e61ee50610588fe52a872f1eaacf97",
        "10eea647ba3ac7b678e168c56f7ba38fb38aa170a2e39eaf7ac341844c310c20",
        "91653a709c197a27ab0147d837602095bb9f3385351f58f07232ef83c4a6c647",
        "1cf4fc6b4b0791a26f3e76190945ab09c1102abfcac8f95e154df049f1eaac3a",
        "e21b9fbc11a51c2136c1fd54b7b6c28cc2b2865650f80644bd462ed8d1666a09",
        "53b40e81ea83ae7132b1c571613b3e3ec9fee99aafd3cb546ce7969958d0a3fb",
    )
    for version, expected in enumerate(hashes, 1):
        assert hashlib.sha256(prompt_for_version(f"overview-{version}").encode()).hexdigest() == expected


def day1_shape(*, control: bool = True) -> dict[str, Any]:
    records = [("scene", "conflict_zone_type", "mid_block"), ("camera vehicle", "party_type", "car")]
    if control:
        records.append(("camera vehicle", "approach_control_faced", "none_confirmed"))
    return {
        "parties": [{"description": "camera vehicle", "timestamp_seconds": 0.0, "confidence": 0.9}],
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


def test_overview8_live_repair_and_replay_use_overview7_schema(tmp_path: Path) -> None:
    video = tmp_path / "synthetic.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    req = AIRequest(incident_input_hash([(Role.EXTERIOR, digest)]), "overview-8", (AIViewRef(Role.EXTERIOR, digest),))
    settings = GeminiSettings(model="gemini-3.8-flash")
    invalid = day1_shape()
    del invalid["drowsiness_observations"]
    transport = FakeTransport([json.dumps(invalid), json.dumps(day1_shape())])
    response = GeminiAIConnection(settings, PublicResearchVideo(video, True), transport=transport).analyse(req)
    assert response.facts is not None
    assert len(response.calls) == 2
    assert all(prompt.startswith(prompt_for_version("overview-8")) for prompt, _ in transport.calls)
    assert ReplayOverviewAIConnection(settings, response.calls).analyse(req).facts == response.facts
    client = FakeClient()
    GoogleVideoTransport(client).generate(PublicResearchVideo(video, True), prompt_for_version("overview-8"), settings)
    assert client.generated["config"]["response_schema"] == OVERVIEW_7_SCHEMA


@pytest.mark.parametrize(
    "field",
    ["structured_facts", "text_readings", "driver_observations", "collision_observations", "drowsiness_observations"],
)
def test_overview8_preserves_required_observation_arrays(field: str) -> None:
    data = day1_shape()
    del data[field]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "road", prompt_version="overview-8", view_role="exterior")


@pytest.mark.parametrize("control,expected", [(True, "No Incident"), (False, "Cannot Determine")])
def test_overview8_day1_shape_replay_preserves_incident_status(control: bool, expected: str, run_log: RunLog) -> None:
    digest = sha256_hex(PUBLIC_VIDEO)
    request = AIRequest(
        incident_input_hash([(Role.EXTERIOR, digest)]), "overview-8", (AIViewRef(Role.EXTERIOR, digest),)
    )
    settings = GeminiSettings(model="gemini-3.8-flash")
    call = ModelCall(
        request_hash=overview_request_hash(request, settings),
        input_hash=request.input_hash,
        prompt_version="overview-8",
        model=settings.model,
        view_id=digest,
        fps=settings.fps,
        raw_response=json.dumps(day1_shape(control=control)),
        latency_ms=0,
        retry_index=0,
    )
    report = analyse_incident(
        [make_view(PUBLIC_VIDEO)],
        Origin.REPLAY,
        ai=ReplayOverviewAIConnection(settings, (call,)),
        run_log=run_log,
        prompt_version="overview-8",
        agreement_settings=AgreementSettings(n_runs=3),
    )
    assert report.is_valid
    assert report.incident_status == expected
    assert report.violations == ()
    assert report.agreement is not None
    saved = run_log.get(report.run_id).report
    assert saved is not None and saved.incident_status == expected
