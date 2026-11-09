"""overview-10 request contract: schema, enum parity, prompt loading, allowed values, hashes, transport (spec 003 ticket 3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from incident_report.ai import AIRequest, AIViewRef
from incident_report.ai import overview10 as o10
from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import (
    GeminiAIConnection,
    GeminiSettings,
    PublicResearchVideo,
    overview_request_hash,
    parse_overview,
    prompt_for_version,
)
from incident_report.ai.google_video import GoogleVideoTransport
from incident_report.domain import Role
from incident_report.facts import (
    FIELD_VALUES,
    OVERVIEW_10_SCHEMA,
    OVERVIEW_10_STRUCTURED_FIELDS,
    OVERVIEW_10_TOP_LEVEL_ORDER,
    OVERVIEW_SCHEMA,
    CameraRole,
    ChecklistManoeuvre,
    ChecklistMarking,
    ChecklistPartyType,
    CollisionObservationKind,
    ColourFamily,
    ContactState,
    ContactVisibility,
    IncidentAssessmentRow,
    PartyChecklistRow,
    RoadClassBasis,
    RoadClassOnApproach,
    RoundaboutExitPosition,
    StructuredField,
    TurnDirection,
)
from incident_report.hashing import incident_input_hash, sha256_hex
from overview10_helpers import VIEW, answer, assessment, parse, row
from test_google_video_transport import FakeClient
from test_live_gemini import PUBLIC_VIDEO, FakeTransport

REPO = Path(__file__).resolve().parents[1]
SETTINGS = GeminiSettings(model="gemini-3-flash-preview", fps=1.0)
SHA = "a" * 64

# overview-1..9 request hashes recorded from fresh-start before this ticket (model gemini-3-flash-preview, fps 1,
# one exterior view whose sha256 is "a" * 64). They must never change.
STORED_HASHES = {
    "overview-1": "4db3bf5a86633e0cb899c3e870f5105e30554d4f0cb7c3fb90b8698c06af0ea2",
    "overview-2": "fa1b6db0b0f909e5cfd989de1a64a8a8356cfb5a34900b1b42d2f7e331520c4a",
    "overview-3": "655a9b0decb8a678ee75057fa6e5f633acb94c975c410936011a0b326763292a",
    "overview-4": "65b9242de1afff64b52dfe8b876c5605ce4d39cda30eb83a3e32279771da37af",
    "overview-5": "6e09a249921bab5996c0d26c17da60750dd033f846767b7bff6f6a4b7cd34e53",
    "overview-6": "4880c5abd105b2a439426ad7a995070f62ca1c51e4cc34a38b07d8c444b6699f",
    "overview-7": "7895ae2380fb6d3a453187aafc9df7b3768da5d0b6a813fdd4f7c98a77ad9eee",
    "overview-8": "be66d67c0fe1148d9e92f8da44774d9c6839e8c17bd6e166b5af6f75975ac27f",
    "overview-9": "ffd0399a6e10cff8c4cdb493a60295c2a393a6d6a2ad355e8fc05d63b5fbf4eb",
}


def request_for(version: str) -> AIRequest:
    return AIRequest(
        input_hash=incident_input_hash([(Role.EXTERIOR, SHA)]),
        prompt_version=version,
        views=(AIViewRef(Role.EXTERIOR, SHA, 30.0),),
    )


def walk(node: Any) -> list[Any]:
    nodes = [node]
    if isinstance(node, dict):
        for value in node.values():
            nodes.extend(walk(value))
    elif isinstance(node, list):
        for value in node:
            nodes.extend(walk(value))
    return nodes


# --- schema shape -------------------------------------------------------------------------------------------


def test_schema_has_no_parties_and_no_candidate_collisions_and_everything_is_required() -> None:
    properties = OVERVIEW_10_SCHEMA["properties"]
    assert "parties" not in properties
    assert "candidate_collisions" not in properties
    assert set(OVERVIEW_10_SCHEMA["required"]) == set(properties)
    assert set(properties) == set(OVERVIEW_10_TOP_LEVEL_ORDER)


def test_schema_top_level_and_row_property_ordering_is_observe_first() -> None:
    assert OVERVIEW_10_SCHEMA["propertyOrdering"][:5] == [
        "observation_notes",
        "incident_assessment",
        "party_checklist",
        "collision_observations",
        "structured_facts",
    ]
    checklist = OVERVIEW_10_SCHEMA["properties"]["party_checklist"]["items"]
    assert checklist["propertyOrdering"] == list(PartyChecklistRow.model_fields)
    assert checklist["required"] == list(PartyChecklistRow.model_fields)
    assert list(checklist["properties"]) == list(PartyChecklistRow.model_fields)


def test_incident_assessment_is_one_object_in_the_cards_order_with_nullable_intervals() -> None:
    node = OVERVIEW_10_SCHEMA["properties"]["incident_assessment"]
    assert node["type"] == "OBJECT"
    order = [
        "assessment_note",
        "camera_role",
        "contact_state",
        "contact_interval_start_seconds",
        "contact_interval_end_seconds",
    ]
    assert node["propertyOrdering"] == order == node["required"] == list(node["properties"])
    assert list(IncidentAssessmentRow.model_fields) == order
    assert node["properties"]["contact_interval_start_seconds"] == {"type": "NUMBER", "nullable": True}
    assert node["properties"]["contact_interval_end_seconds"] == {"type": "NUMBER", "nullable": True}


def test_schema_stays_inside_the_project_gemini_limits() -> None:
    nodes = walk(OVERVIEW_10_SCHEMA)
    assert not any(isinstance(n, dict) and "maxItems" in n for n in nodes)
    for name, spec in OVERVIEW_10_SCHEMA["properties"].items():
        if spec["type"] == "ARRAY":  # flat: no array inside an array item
            assert all(p["type"] != "ARRAY" for p in spec["items"]["properties"].values()), name


@pytest.mark.parametrize(
    ("field", "literal"),
    [
        ("party_type", ChecklistPartyType),
        ("marking", ChecklistMarking),
        ("colour_family", ColourFamily),
        ("manoeuvre", ChecklistManoeuvre),
        ("turn_direction", TurnDirection),
        ("roundabout_exit_position", RoundaboutExitPosition),
        ("road_class_on_approach", RoadClassOnApproach),
        ("road_class_basis", RoadClassBasis),
    ],
)
def test_checklist_enums_match_the_pydantic_literals(field: str, literal: Any) -> None:
    schema_enum = OVERVIEW_10_SCHEMA["properties"]["party_checklist"]["items"]["properties"][field]["enum"]
    assert schema_enum == list(get_args(literal))
    assert PartyChecklistRow.model_fields[field].annotation == literal


def test_assessment_and_contact_enums_match_the_pydantic_literals() -> None:
    props = OVERVIEW_10_SCHEMA["properties"]["incident_assessment"]["properties"]
    assert props["camera_role"]["enum"] == list(get_args(CameraRole))
    assert props["contact_state"]["enum"] == list(get_args(ContactState))
    collisions = OVERVIEW_10_SCHEMA["properties"]["collision_observations"]["items"]["properties"]
    assert collisions["visibility"]["enum"] == list(get_args(ContactVisibility))
    assert collisions["kind"]["enum"] == list(get_args(CollisionObservationKind))


def test_structured_field_enum_omits_the_four_derived_fields() -> None:
    enum = OVERVIEW_10_SCHEMA["properties"]["structured_facts"]["items"]["properties"]["field"]["enum"]
    assert enum == list(OVERVIEW_10_STRUCTURED_FIELDS)
    for derived in ("party_type", "movement", "roundabout_entry_state", "entry_origin"):
        assert derived not in enum
        assert derived in get_args(StructuredField)
    assert set(enum) == set(get_args(StructuredField)) - {
        "party_type",
        "movement",
        "roundabout_entry_state",
        "entry_origin",
    }


def test_older_schema_is_untouched_and_keeps_its_old_shape() -> None:
    assert "propertyOrdering" not in OVERVIEW_SCHEMA
    assert "incident_assessment" not in OVERVIEW_SCHEMA["properties"]


# --- Pydantic rules: iff, codes, intervals ------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"party_type": "other", "party_type_other": ""},
        {"party_type": "sedan", "party_type_other": "tractor"},
        {"colour_family": "other", "colour_other": "  "},
        {"colour_family": "red", "colour_other": "teal"},
        {"manoeuvre": "other", "manoeuvre_other": ""},
        {"manoeuvre": "u_turn", "manoeuvre_other": "reversing"},
    ],
)
def test_other_text_is_present_if_and_only_if_the_value_is_other(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PartyChecklistRow.model_validate(row(**overrides))


def test_other_text_accepted_when_value_is_other() -> None:
    PartyChecklistRow.model_validate(row(party_type="other", party_type_other="tractor"))


@pytest.mark.parametrize("code", ["V1", "p1", "P0", "P01", "CAM2", "", "Car 1"])
def test_party_code_must_be_p_number_or_cam(code: str) -> None:
    with pytest.raises(ValidationError):
        PartyChecklistRow.model_validate(row(code))


@pytest.mark.parametrize(
    "rows",
    [
        [row("P1"), row("P1")],
        [row("P1"), row("P3")],
        [row("P2")],
        [row("CAM"), row("CAM")],
    ],
)
def test_codes_are_unique_consecutive_and_at_most_one_cam(rows: list[dict[str, Any]]) -> None:
    with pytest.raises(AIConnectionError):
        parse(answer(rows=rows, contacts=[]))


def test_one_cam_plus_consecutive_codes_is_accepted() -> None:
    facts = parse(answer(rows=[row("P2"), row("CAM"), row("P1")], contacts=[]))
    assert {r.party_code for r in facts.party_checklist} == {"P1", "P2", "CAM"}


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "contact_state": "contact_observed",
            "contact_interval_start_seconds": None,
            "contact_interval_end_seconds": None,
        },
        {"contact_state": "contact_possible_obscured", "contact_interval_end_seconds": None},
        {"contact_state": "no_contact_observed"},
        {"contact_state": "cannot_assess"},
        {"contact_interval_start_seconds": 4.0, "contact_interval_end_seconds": 3.0},
    ],
)
def test_interval_is_null_if_and_only_if_no_contact_or_cannot_assess(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IncidentAssessmentRow.model_validate(assessment(**overrides))


@pytest.mark.parametrize("state", ["no_contact_observed", "cannot_assess"])
def test_null_interval_is_valid_for_no_contact_and_cannot_assess(state: str) -> None:
    IncidentAssessmentRow.model_validate(
        assessment(contact_state=state, contact_interval_start_seconds=None, contact_interval_end_seconds=None)
    )


def test_assessment_note_length_is_checked_by_pydantic_not_the_schema() -> None:
    with pytest.raises(ValidationError):
        IncidentAssessmentRow.model_validate(assessment(assessment_note=""))
    with pytest.raises(ValidationError):
        IncidentAssessmentRow.model_validate(assessment(assessment_note="x" * 201))
    IncidentAssessmentRow.model_validate(assessment(assessment_note="x" * 200))
    assert "maxLength" not in json.dumps(OVERVIEW_10_SCHEMA)


@pytest.mark.parametrize("key", OVERVIEW_10_TOP_LEVEL_ORDER)
def test_every_top_level_array_is_required_by_the_parser(key: str) -> None:
    payload = answer()
    del payload[key]
    with pytest.raises(AIConnectionError):
        parse(payload)


@pytest.mark.parametrize("derived", ["party_type", "movement", "roundabout_entry_state", "entry_origin"])
def test_derived_fields_from_the_model_are_rejected(derived: str) -> None:
    fact = {
        "subject": "P1",
        "field": derived,
        "value": "unknown",
        "other_party": "",
        "timestamp_seconds": 1.0,
        "confidence": 0.9,
    }
    with pytest.raises(AIConnectionError):
        parse(answer(structured_facts=[fact]))


@pytest.mark.parametrize("extra", ["parties", "candidate_collisions"])
def test_model_may_not_return_parties_or_candidate_collisions(extra: str) -> None:
    with pytest.raises(AIConnectionError):
        parse(answer(**{extra: []}))


# --- prompt files, allowed values, hash ---------------------------------------------------------------------


def test_prompt_files_ship_inside_the_package_and_load_with_lf_endings() -> None:
    from importlib import resources

    for name in ("overview_10_system.md", "overview_10_user.md"):
        resource = resources.files("incident_report.ai").joinpath("prompts", name)
        assert resource.is_file()
        assert "\r" not in resource.read_text(encoding="utf-8")
    assert "\r" not in o10.system_instruction()
    assert "\r" not in o10.user_prompt()


def test_crlf_copy_loads_to_the_same_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "prompts"
    folder.mkdir()
    (folder / "overview_10_system.md").write_bytes(b"line one\r\nline two\r\n")
    (folder / "overview_10_user.md").write_bytes(b"user\r\ntext")
    import importlib.resources as resources_module

    monkeypatch.setattr(resources_module, "files", lambda package: tmp_path)
    assert o10._load("overview_10_system.md") == "line one\nline two\n"
    assert o10._load("overview_10_user.md") == "user\ntext"
    assert o10.normalise_line_endings("a\r\nb\rc\n") == "a\nb\nc\n"


def test_gitattributes_pins_prompt_files_to_lf() -> None:
    lines = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "src/incident_report/ai/prompts/*.md text eol=lf" in lines


def test_pyproject_does_not_exclude_the_prompt_files() -> None:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["src/incident_report"]' in text
    assert "exclude" not in text.split("[tool.hatch.build.targets.wheel]")[1].split("[tool.pytest")[0]


def test_allowed_values_block_lists_every_requested_field_with_its_exact_values() -> None:
    block = o10.allowed_values_block()
    assert block.startswith("<allowed_values>\n") and block.endswith("\n</allowed_values>")
    lines = set(block.splitlines())
    for name, values in FIELD_VALUES.items():
        line = f"{name}: {', '.join(values)}"
        if name in {"party_type", "movement", "roundabout_entry_state", "entry_origin"}:
            assert line not in lines
        else:
            assert line in lines
    assert "posted_speed_limit_kmh: digits read from a visible sign, not_visible, unknown" in lines
    assert (
        "text_readings.kind: signboard, road_name, overlay_date, overlay_time, overlay_speed, overlay_gps, other"
        in lines
    )
    assert "text_readings.language: ar, en, other, unknown" in lines
    assert "collision_observations.injury_grade: serious, moderate, minor, unknown" in lines
    assert any(line.startswith("driver_observations.kind: phone_in_hand") for line in lines)
    assert any(line.startswith("drowsiness_observations.kind: eye_closure") for line in lines)
    assert any(line.startswith("collision_observations.kind: vehicle_contact_vehicle") for line in lines)


def test_every_structured_field_the_prompt_names_has_a_row_in_the_allowed_values() -> None:
    system = o10.system_instruction()
    block = o10.allowed_values_block()
    for field in OVERVIEW_10_STRUCTURED_FIELDS:
        if field in system.split("<allowed_values>")[0]:
            assert f"\n{field}: " in block, field


def test_system_instruction_is_the_prompt_file_plus_the_allowed_values_block() -> None:
    text = o10.system_instruction()
    assert text.startswith(o10._load("overview_10_system.md").rstrip("\n"))
    assert o10.allowed_values_block() in text


def test_overview_10_hash_covers_system_user_and_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    request = request_for("overview-10")
    base = overview_request_hash(request, SETTINGS)
    assert base == overview_request_hash(request, SETTINGS)
    real_load = o10._load
    monkeypatch.setattr(
        o10, "_load", lambda name: real_load(name) + ("changed system text\n" if "system" in name else "")
    )
    assert overview_request_hash(request, SETTINGS) != base
    monkeypatch.setattr(o10, "_load", lambda name: real_load(name) + ("changed user text" if "user" in name else ""))
    assert overview_request_hash(request, SETTINGS) != base


@pytest.mark.parametrize("version", sorted(STORED_HASHES))
def test_request_hashes_for_overview_1_to_9_are_unchanged(version: str) -> None:
    assert overview_request_hash(request_for(version), SETTINGS) == STORED_HASHES[version]


def test_prompt_for_older_versions_does_not_know_overview_10_text() -> None:
    assert prompt_for_version("overview-10") == o10.user_prompt()
    assert "party_checklist" not in prompt_for_version("overview-9")


# --- transport: system instruction and schema chosen by version ---------------------------------------------


def test_transport_sends_system_instruction_and_overview_10_schema(tmp_path: Path) -> None:
    path = tmp_path / "public.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    prompt = o10.build_prompt("\nview_role: exterior")
    GoogleVideoTransport(client).generate(PublicResearchVideo(path, True), prompt, SETTINGS)
    config = client.generated["config"]
    assert config["system_instruction"] == o10.system_instruction()
    assert config["response_schema"] is OVERVIEW_10_SCHEMA
    assert config["response_mime_type"] == "application/json"
    text_part = client.generated["contents"][1]["text"]
    assert text_part == o10.user_prompt() + "\nview_role: exterior"
    assert type(text_part) is str


def test_transport_keeps_the_old_config_for_older_prompts(tmp_path: Path) -> None:
    path = tmp_path / "public.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    GoogleVideoTransport(client).generate(PublicResearchVideo(path, True), prompt_for_version("overview-9"), SETTINGS)
    assert "system_instruction" not in client.generated["config"]
    assert client.generated["config"]["response_schema"]["required"][-1] == "drowsiness_observations"


def test_connection_sends_the_structured_prompt_and_hash_for_overview_10(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    request = AIRequest(
        input_hash=incident_input_hash([(Role.EXTERIOR, digest)]),
        prompt_version="overview-10",
        views=(AIViewRef(Role.EXTERIOR, digest, 30.0),),
    )
    raw = json.dumps(answer())
    transport = FakeTransport([raw])
    connection = GeminiAIConnection(SETTINGS, PublicResearchVideo(video, True), transport=transport)
    response = connection.analyse(request)
    sent = transport.calls[0][0]
    assert sent == o10.user_prompt() + "\nview_role: exterior"
    assert getattr(sent, "system_instruction") == o10.system_instruction()  # noqa: B009
    assert response.facts is not None and response.facts.incident_assessments
    assert response.calls[0].request_hash == overview_request_hash(request, SETTINGS)


def test_repair_for_overview_10_keeps_the_system_instruction(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    digest = sha256_hex(PUBLIC_VIDEO)
    request = AIRequest(
        input_hash=incident_input_hash([(Role.EXTERIOR, digest)]),
        prompt_version="overview-10",
        views=(AIViewRef(Role.EXTERIOR, digest, 30.0),),
    )
    transport = FakeTransport([json.dumps(answer())])
    connection = GeminiAIConnection(SETTINGS, PublicResearchVideo(video, True), transport=transport)
    from incident_report.ai import AIResponse

    connection.repair(request, AIResponse("x", "gemini-api", calls=()), ["a citation problem"])
    sent = transport.calls[0][0]
    assert "a citation problem" in sent
    assert getattr(sent, "system_instruction") == o10.system_instruction()  # noqa: B009


# --- parser version gates -----------------------------------------------------------------------------------


def test_parser_accepts_overview_10_and_old_versions_still_parse_a_stored_overview_9_payload() -> None:
    assert parse(answer()).incident_assessments
    stored = {
        "parties": [{"description": "silver sedan", "timestamp_seconds": 1.0, "confidence": 0.9}],
        "events": [],
        "candidate_collisions": [],
        "behaviour_observations": [],
        "location_conditions": [],
        "structured_facts": [],
        "text_readings": [],
        "driver_observations": [],
        "collision_observations": [],
        "drowsiness_observations": [],
    }
    facts = parse_overview(json.dumps(stored), VIEW, prompt_version="overview-9", view_role="exterior")
    assert facts.party_checklist == [] and facts.incident_assessments == [] and facts.observation_notes == []
    with pytest.raises(AIConnectionError):
        parse_overview(json.dumps(answer()), VIEW, prompt_version="overview-9", view_role="exterior")


def test_overview_10_rejects_an_answer_missing_a_required_array_in_the_facts_gate() -> None:
    from incident_report.facts import OverviewFacts

    with pytest.raises(ValidationError):
        OverviewFacts.model_validate(
            {
                "parties": [],
                "events": [],
                "candidate_collisions": [],
                "behaviour_observations": [],
                "location_conditions": [],
            },
            context={"prompt_version": "overview-10"},
        )
