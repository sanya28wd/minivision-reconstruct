"""RPT-31 synthetic-fixture responses; no real footage or live model calls."""

import json
from pathlib import Path
from typing import Any

import pytest

from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import parse_overview
from incident_report.facts import validate_citations


def test_verbatim_camera_overlay_is_cited_and_unverified() -> None:
    payload = {
        "parties": [],
        "events": [],
        "candidate_collisions": [],
        "behaviour_observations": [],
        "location_conditions": [],
        "structured_facts": [],
        "text_readings": [
            {"kind": "overlay_speed", "text": "081 km/h", "language": "en", "timestamp_seconds": 2.0, "confidence": 0.9}
        ],
    }
    facts = parse_overview(json.dumps(payload), "view-a", prompt_version="overview-3")
    reading = facts.text_readings[0]
    assert reading.text == "081 km/h"
    assert reading.view_id == "view-a"
    assert "camera overlay (unverified)" in reading.description
    assert not facts.structured_facts
    assert not validate_citations(facts, ["view-a"], view_durations={"view-a": 1.0}).is_valid


def test_non_uae_overlay_keeps_road_type_without_inferred_location() -> None:
    # synthetic-fixture: Turkish overlay is evidence of text, not a UAE location.
    payload = {
        "parties": [],
        "events": [],
        "candidate_collisions": [],
        "behaviour_observations": [],
        "location_conditions": [],
        "structured_facts": [
            {
                "subject": "scene",
                "field": "conflict_zone_type",
                "value": "roundabout_unsignalised",
                "other_party": "",
                "timestamp_seconds": 1.0,
                "confidence": 0.9,
            }
        ],
        "text_readings": [
            {"kind": "other", "text": "Diyarbakır", "language": "other", "timestamp_seconds": 1.0, "confidence": 0.9}
        ],
    }
    facts = parse_overview(json.dumps(payload), "turkish-fixture", prompt_version="overview-3")
    assert facts.location_conditions[0].description == "conflict_zone_type: roundabout_unsignalised"
    assert facts.location_conditions[0].view_id == "turkish-fixture"
    assert facts.location_conditions[0].timestamp_seconds == 1.0
    assert facts.text_readings[0].text == "Diyarbakır"
    assert all("UAE" not in f.description and "Dubai" not in f.description for f in facts.location_conditions)


def overview() -> dict[str, Any]:
    return {
        "parties": [],
        "events": [],
        "candidate_collisions": [],
        "behaviour_observations": [],
        "location_conditions": [],
        "structured_facts": [],
        "text_readings": [],
    }


def reading(kind: str, text: str) -> dict[str, Any]:
    return {"kind": kind, "text": text, "language": "ar", "timestamp_seconds": 1.0, "confidence": 0.9}


def test_named_location_copies_cited_arabic_road_reading() -> None:
    data = overview()
    data["text_readings"] = [reading("road_name", "شارع الاتحاد")]
    facts = parse_overview(json.dumps(data), "view-a", prompt_version="overview-3")
    assert facts.text_readings[0].text == "شارع الاتحاد"
    assert facts.location_conditions[0].description.endswith("شارع الاتحاد")
    assert facts.location_conditions[0].view_id == "view-a"
    assert facts.location_conditions[0].timestamp_seconds == 1.0


@pytest.mark.parametrize(
    "field,value", [("conflict_zone_type", "signalised_junction"), ("visibility_conditions", "night")]
)
def test_cited_conditions_use_closed_values(field: str, value: str) -> None:
    data = overview()
    data["structured_facts"] = [
        {
            "subject": "scene",
            "field": field,
            "value": value,
            "other_party": "",
            "timestamp_seconds": 1.0,
            "confidence": 0.9,
        }
    ]
    facts = parse_overview(json.dumps(data), "view-a", prompt_version="overview-3")
    assert facts.location_conditions[0].description == f"{field}: {value}"
    assert facts.location_conditions[0].view_id == "view-a"
    assert facts.location_conditions[0].timestamp_seconds == 1.0
    assert facts.location_conditions[0].confidence == 0.9
    data["structured_facts"][0]["value"] = "invented"
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "view-a", prompt_version="overview-3")


@pytest.mark.parametrize(
    "change", ["missing_array", "missing_confidence", "wrong_view", "prose_location", "party_name"]
)
def test_invalid_evidence_is_refused(change: str) -> None:
    data = overview()
    data["text_readings"] = [reading("overlay_time", "12:43:01")]
    if change == "missing_array":
        del data["text_readings"]
    elif change == "missing_confidence":
        del data["text_readings"][0]["confidence"]
    elif change == "wrong_view":
        data["text_readings"][0]["view_id"] = "wrong"
    elif change == "prose_location":
        data["location_conditions"] = [{"description": "Dubai", "timestamp_seconds": 1.0, "confidence": 0.9}]
    else:
        data["parties"] = [{"description": "Cannot Determine sedan", "timestamp_seconds": 1.0, "confidence": 0.9}]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "view-a", prompt_version="overview-3")


@pytest.mark.parametrize("kind", ["overlay_date", "overlay_time", "overlay_speed", "overlay_gps", "other"])
def test_interior_allows_camera_text(kind: str) -> None:
    data = overview()
    data["text_readings"] = [reading(kind, "2026-01-02 51.50, -0.12")]
    facts = parse_overview(json.dumps(data), "view-a", prompt_version="overview-3", view_role="interior")
    assert facts.text_readings[0].text == "2026-01-02 51.50, -0.12"
    assert facts.location_conditions == []


@pytest.mark.parametrize("kind", ["road_name", "signboard"])
def test_interior_refuses_road_text(kind: str) -> None:
    data = overview()
    data["text_readings"] = [reading(kind, "Road")]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "view-a", prompt_version="overview-3", view_role="interior")


def test_overview_3_transport_and_replay_and_dense_retention(tmp_path: Path) -> None:
    from incident_report.ai.dense import consolidate_facts
    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo, prompt_for_version
    from incident_report.ai.google_video import GoogleVideoTransport
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.ai.overview_replay import ReplayOverviewAIConnection
    from incident_report.domain import Role
    from incident_report.facts import OVERVIEW_3_SCHEMA
    from incident_report.hashing import incident_input_hash, sha256_hex
    from test_google_video_transport import FakeClient
    from test_live_gemini import PUBLIC_VIDEO, FakeTransport

    video = tmp_path / "synthetic.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    req = AIRequest(incident_input_hash([(Role.EXTERIOR, digest)]), "overview-3", (AIViewRef(Role.EXTERIOR, digest),))
    data = overview()
    data["text_readings"] = [reading("overlay_speed", "081 km/h")]
    settings = GeminiSettings(model="gemini-3.8-flash")
    response = GeminiAIConnection(
        settings, PublicResearchVideo(video, True), transport=FakeTransport([json.dumps(data)])
    ).analyse(req)
    assert response.facts is not None
    replayed = ReplayOverviewAIConnection(settings, response.calls).analyse(req)
    assert replayed.facts == response.facts
    assert consolidate_facts(response.facts, []).text_readings == response.facts.text_readings
    client = FakeClient()
    GoogleVideoTransport(client).generate(PublicResearchVideo(video, True), prompt_for_version("overview-3"), settings)
    assert client.generated["config"]["response_schema"] == OVERVIEW_3_SCHEMA
    assert "text_readings" in OVERVIEW_3_SCHEMA["required"]
    assert "maxItems" not in json.dumps(OVERVIEW_3_SCHEMA)


def test_multiview_report_retains_each_text_citation(run_log: Any, tmp_path: Path) -> None:
    from conftest import make_view
    from incident_report.domain import Origin, Role
    from incident_report.entry import analyse_incident
    from incident_report.hashing import sha256_hex
    from incident_report.storage import LocalEvidenceStore
    from test_new_incident_screen import SpyAIConnection
    from video_fixtures import synthetic_video

    videos = [synthetic_video(b"location-front"), synthetic_video(b"location-rear")]
    hashes = [sha256_hex(v) for v in videos]
    data = overview()
    data["text_readings"] = [reading("overlay_speed", "081 km/h")]
    facts = {digest: parse_overview(json.dumps(data), digest, prompt_version="overview-3") for digest in hashes}
    report = analyse_incident(
        [make_view(v, Role.EXTERIOR) for v in videos],
        Origin.APP,
        ai=SpyAIConnection(per_view_facts=facts),
        run_log=run_log,
        prompt_version="overview-3",
        evidence=LocalEvidenceStore(tmp_path / "evidence"),
    )
    assert report.is_valid
    assert report.facts is not None
    assert [r.view_id for r in report.facts.text_readings] == hashes
    assert not report.facts.structured_facts


def test_overview_3_invalid_response_uses_bounded_repair(tmp_path: Path) -> None:
    from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo
    from incident_report.ai.interface import AIRequest, AIViewRef
    from incident_report.domain import Role
    from incident_report.hashing import incident_input_hash, sha256_hex
    from test_live_gemini import PUBLIC_VIDEO, FakeTransport

    video = tmp_path / "synthetic.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    req = AIRequest(incident_input_hash([(Role.EXTERIOR, digest)]), "overview-3", (AIViewRef(Role.EXTERIOR, digest),))
    good = overview()
    bad = overview()
    bad["text_readings"] = [reading("overlay_time", "12:01:10")]
    del bad["text_readings"][0]["confidence"]
    transport = FakeTransport([json.dumps(bad), json.dumps(good)])
    response = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(video, True), transport=transport
    ).analyse(req)
    assert response.facts is not None
    assert [c.prompt_version for c in response.calls] == ["overview-3", "overview-3-repair"]
    assert len(transport.calls) == 2


@pytest.mark.parametrize("text,language", [("شارع الاتحاد", "ar"), ("Al Ittihad Road", "en")])
def test_directional_signboard_is_verbatim_text_without_precise_location(text: str, language: str) -> None:
    data = overview()
    item = reading("signboard", text)
    item["language"] = language
    data["text_readings"] = [item]
    facts = parse_overview(json.dumps(data, ensure_ascii=False), "view-a", prompt_version="overview-3")
    assert facts.text_readings[0].text == text
    assert facts.text_readings[0].language == language
    assert facts.location_conditions == []


def test_interior_refuses_conditions() -> None:
    data = overview()
    data["structured_facts"] = [
        {
            "subject": "scene",
            "field": "visibility_conditions",
            "value": "clear",
            "other_party": "",
            "timestamp_seconds": 1.0,
            "confidence": 0.9,
        }
    ]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "view-a", prompt_version="overview-3", view_role="interior")


def test_l0_refuses_road_text_from_an_interior_view() -> None:
    from incident_report.ai.interface import AIViewRef
    from incident_report.domain import Role

    data = overview()
    data["text_readings"] = [reading("road_name", "Al Ittihad Road")]
    facts = parse_overview(json.dumps(data), "view-a", prompt_version="overview-3")
    result = validate_citations(facts, [AIViewRef(Role.INTERIOR, "view-a")])
    assert not result.is_valid
    assert any("interior" in error for error in result.errors)


@pytest.mark.parametrize("field", ["scene_type", "weather", "lighting", "road_surface"])
def test_unsourced_fields_are_rejected(field: str) -> None:
    data = overview()
    data["structured_facts"] = [
        {
            "subject": "scene",
            "field": field,
            "value": "unknown",
            "other_party": "",
            "timestamp_seconds": 1.0,
            "confidence": 0.9,
        }
    ]
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(data), "view-a", prompt_version="overview-3")
