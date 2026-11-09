from pathlib import Path

from incident_report.ai.dense import consolidate_facts
from incident_report.facts import CitedFact, OverviewFacts, PartyFact
from incident_report.rules.engine import evaluate_fault


def test_dense_only_party_is_cited_but_never_sent_to_rules() -> None:
    parties = [
        PartyFact(description=name, view_id="front", timestamp_seconds=1.0, confidence=0.8)
        for name in ("white Toyota sedan", "blue Nissan SUV")
    ]
    overview = OverviewFacts(
        parties=parties, events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
    )
    renamed = PartyFact(description="white sedan approaching", view_id="front", timestamp_seconds=2.0, confidence=0.95)
    collision = CitedFact(
        description="white sedan approaching strikes blue Nissan SUV",
        view_id="front",
        timestamp_seconds=2.0,
        confidence=0.95,
    )
    dense = OverviewFacts(
        parties=[renamed],
        events=[],
        candidate_collisions=[collision],
        behaviour_observations=[],
        location_conditions=[],
    )
    merged = consolidate_facts(overview, [dense])
    assert merged.parties == parties
    assert merged.unmatched_dense_parties == [renamed]
    assert merged.candidate_collisions == [collision]
    assert OverviewFacts.model_validate_json(merged.model_dump_json()) == merged
    faults = evaluate_fault(merged)
    assert len(faults) == 2
    assert all(f.status.value == "Cannot Determine" for f in faults)
    assert all("party_type" in f.reason for f in faults)


def test_exact_name_match_still_merges_attributes() -> None:
    party = PartyFact(
        description="white Toyota sedan",
        view_id="front",
        timestamp_seconds=1.0,
        confidence=0.8,
        color="white",
        body_type="sedan",
    )
    overview = OverviewFacts(
        parties=[party], events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
    )
    dense = overview.model_copy(
        update={"parties": [party.model_copy(update={"make_model": "Toyota", "confidence": 0.95})]}
    )
    merged = consolidate_facts(overview, [dense])
    assert len(merged.parties) == 1
    assert merged.parties[0].make_model == "Toyota"
    assert merged.parties[0].color == "white"
    assert merged.parties[0].body_type == "sedan"
    assert merged.parties[0].confidence == 0.95
    assert merged.parties[0].timestamp_seconds == 1.0
    assert merged.unmatched_dense_parties == []


def test_dense_2_sends_overview_names_and_hashes_complete_prompt(tmp_path: Path) -> None:
    from dataclasses import replace

    from incident_report.ai.dense import CandidateWindow, DensePassAIConnection, DenseSettings, dense_request_hash
    from incident_report.ai.gemini import PublicResearchVideo
    from incident_report.ai.interface import AIRequest
    from test_dense_pass import DENSE_RAW, FakeDenseTransport

    names = ("white Toyota sedan", 'blue Nissan "Patrol" SUV')
    request = AIRequest(input_hash="abc", prompt_version="dense-2", views=())
    window = CandidateWindow("front", 2.0, 0.0, 4.0, "possible contact", overview_party_descriptions=names)
    settings = DenseSettings(model="gemini-3.8-flash")
    transport = FakeDenseTransport([DENSE_RAW])
    connection = DensePassAIConnection(settings, PublicResearchVideo(tmp_path / "clip.mp4", True), transport=transport)
    _, calls = connection.analyse_window(request, window)
    prompt = transport.calls[0][0]
    import json

    assert json.dumps(names, ensure_ascii=False) in prompt
    assert "parties, events and candidate_collisions" in prompt
    assert "exactly those names" in prompt
    assert "visible words only" in prompt
    assert calls[0].prompt_version == "dense-2"
    assert calls[0].request_hash == dense_request_hash(request, window, settings)
    assert calls[0].request_hash != dense_request_hash(
        request, replace(window, overview_party_descriptions=("red sedan",)), settings
    )
    assert calls[0].request_hash != dense_request_hash(
        request, replace(window, candidate_description="another contact"), settings
    )


def test_dense_1_prompt_text_and_recording_hash_are_unchanged() -> None:
    import hashlib

    from incident_report.ai.dense import DENSE_PROMPT, CandidateWindow, DenseSettings, dense_request_hash
    from incident_report.ai.interface import AIRequest

    assert (
        hashlib.sha256(DENSE_PROMPT.encode()).hexdigest()
        == "0fa93bfcd43687805ee503ad6086f63f0557e0d48be32a3a88229a35034c3486"
    )
    request = AIRequest(input_hash="abc", prompt_version="dense-1", views=())
    window = CandidateWindow("front", 2.0, 0.0, 4.0, "possible contact")
    # Frozen pre-change hash, independently computed from the original dense-1 implementation.
    assert (
        dense_request_hash(request, window, DenseSettings(model="gemini-3.8-flash"))
        == "562aa2ac46f9d414420b85bb57da8a43aa67bc63f28ec38749e856416e2a90fc"
    )


def test_unmatched_party_citations_are_validated() -> None:
    from incident_report.facts import validate_citations

    party = PartyFact(description="white sedan", view_id="missing", timestamp_seconds=8.0, confidence=0.4)
    facts = OverviewFacts(
        parties=[],
        unmatched_dense_parties=[party],
        events=[],
        candidate_collisions=[],
        behaviour_observations=[],
        location_conditions=[],
    )
    result = validate_citations(facts, ["front"])
    assert not result.is_valid
    assert any("unmatched_dense_party" in error and "not in known views" in error for error in result.errors)
    assert any("unmatched_dense_party" in error and "confidence" in error for error in result.errors)


def test_dense_typed_facts_for_unmatched_parties_are_not_rebound() -> None:
    from incident_report.facts import StructuredFact

    party = PartyFact(description="white Toyota sedan", view_id="front", timestamp_seconds=1.0, confidence=0.9)
    overview = OverviewFacts(
        parties=[party], events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
    )
    renamed = party.model_copy(update={"description": "white sedan approaching"})
    typed = StructuredFact(
        subject=renamed.description,
        field="party_type",
        value="car",
        other_party="",
        view_id="front",
        timestamp_seconds=1.0,
        confidence=0.9,
    )
    dense = overview.model_copy(update={"parties": [renamed], "structured_facts": [typed]})
    merged = consolidate_facts(overview, [dense])
    assert merged.parties == [party]
    assert merged.unmatched_dense_parties == [renamed]
    assert merged.structured_facts == []
    assert dense.structured_facts == [typed]
