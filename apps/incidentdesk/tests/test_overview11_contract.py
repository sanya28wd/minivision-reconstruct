"""overview-11 request contract: detail window, part layout, one upload, thinking level, schema, hashes
(spec 003 ticket 8, card SINGLE-REQUEST-MULTIRES sections 1 and 2)."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from incident_report.ai import AIRequest, AIViewRef, part_plan
from incident_report.ai import overview10 as o10
from incident_report.ai import overview11 as o11
from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import (
    OVERVIEW_PROMPT_VERSIONS,
    PROMPT_VERSION,
    GeminiSettings,
    PublicResearchVideo,
    overview_recovery_request_hash,
    overview_request_hash,
    prompt_for_version,
)
from incident_report.ai.google_video import GoogleVideoTransport
from incident_report.domain import Role
from incident_report.facts import (
    OVERVIEW_10_SCHEMA,
    OVERVIEW_11_SCHEMA,
    CollisionObservation,
    ContactPart,
    IncidentAssessmentRow11,
)
from incident_report.hashing import incident_input_hash
from test_google_video_transport import FakeClient
from test_overview10_contract import STORED_HASHES, walk

SETTINGS = GeminiSettings(model="gemini-3-flash-preview", fps=1.0)
SHA = "a" * 64
# overview-10 request hash recorded from fresh-start d9a3508 before this ticket (same inputs as STORED_HASHES).
OVERVIEW_10_HASH = "9644792fa10690f638ecc3f5c779a6718f967aa412527a2d21848642aac2d23a"


def request_for(version: str, duration: float | None = 30.0) -> AIRequest:
    return AIRequest(
        input_hash=incident_input_hash([(Role.EXTERIOR, SHA)]),
        prompt_version=version,
        views=(AIViewRef(Role.EXTERIOR, SHA, duration),),
    )


def main_window(duration: float) -> tuple[float, float]:
    """The detail window the plan chose; the policy that places it is tested only in test_part_plan."""
    window = o11.window_of(o11.main_plan(duration, SETTINGS))
    assert window is not None
    return window


def recovery_window(duration: float, centre: float) -> tuple[float, float]:
    window = o11.window_of(o11.recovery_plan(duration, SETTINGS, centre))
    assert window is not None
    return window


def test_the_constants_carry_their_assumption_status() -> None:
    for module in (o11, part_plan):
        assert "to be validated (ADR 0006)" in Path(str(module.__file__)).read_text(encoding="utf-8")
    assert o11.TIME_TOLERANCE_SECONDS == 0.5
    assert o11.THINKING_LEVEL == "low"


# --- part layout ---------------------------------------------------------------------------------------------


def test_main_request_has_two_parts_in_the_cards_order_with_exact_lines() -> None:
    prompt = o11.main_prompt(30.0, SETTINGS, "\nview_role: exterior")
    start, end = main_window(30.0)
    assert prompt.window == (start, end)
    first, second = prompt.video_parts
    assert first.intro == "Part whole_clip: the full clip, 0.000 to 30.000 s, at 1 frames per second."
    assert (first.fps, first.start_offset, first.end_offset) == (1.0, None, None)
    assert second.intro == (
        f"Part detail_window: seconds {start:.3f} to {end:.3f} of the full clip, at 15 frames per second. "
        "Its own clock starts at 0.000."
    )
    assert (second.fps, second.start_offset, second.end_offset) == (15.0, f"{start:.3f}s", f"{end:.3f}s")
    assert str(prompt) == o11.user_prompt() + "\nview_role: exterior"
    assert prompt.system_instruction == o11.system_instruction()
    assert prompt.thinking_level == "low"


def test_a_clip_no_longer_than_the_window_sends_only_the_whole_clip_at_the_detail_rate() -> None:
    short = part_plan.DETAIL_WINDOW_SECONDS - 1.5
    prompt = o11.main_prompt(short, SETTINGS)
    assert prompt.window is None
    (only,) = prompt.video_parts
    assert only.intro == f"Part whole_clip: the full clip, 0.000 to {short:.3f} s, at 15 frames per second."
    assert only.fps == 15.0 and only.start_offset is None


def test_the_detail_rate_is_configurable_inside_the_sdk_range() -> None:
    settings = GeminiSettings(model="gemini-3-flash-preview", detail_fps=24.0)
    assert o11.main_prompt(30.0, settings).video_parts[1].fps == 24.0
    for bad in (14.9, 24.1, float("nan")):
        with pytest.raises(AIConnectionError):
            GeminiSettings(model="gemini-3-flash-preview", detail_fps=bad)


@pytest.mark.parametrize(
    "duration, expected",
    [(8.0, 15.0), (38.5, 120 / 38.5), (70.0, 2.0), (4.0, 24.0), (60.0, 2.0), (1.0, 24.0)],
)
def test_the_recovery_rate_is_adaptive_with_a_two_fps_floor_and_a_24_cap(duration: float, expected: float) -> None:
    assert part_plan.RECOVERY_FRAME_BUDGET == 120.0
    assert part_plan.recovery_fps(duration, 120.0) == pytest.approx(expected)


def test_the_frame_budget_is_validated() -> None:
    assert GeminiSettings(model="gemini-3-flash-preview").recovery_frame_budget == 120.0
    GeminiSettings(model="gemini-3-flash-preview", recovery_frame_budget=240.0)
    for bad in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(AIConnectionError):
            GeminiSettings(model="gemini-3-flash-preview", recovery_frame_budget=bad)


def test_settings_read_the_new_rates_from_the_environment() -> None:
    settings = GeminiSettings.from_env(
        {"GEMINI_MODEL": "gemini-3-flash-preview", "GEMINI_DETAIL_FPS": "20", "GEMINI_RECOVERY_FRAME_BUDGET": "240"}
    )
    assert (settings.detail_fps, settings.recovery_frame_budget) == (20.0, 240.0)


def test_recovery_request_is_the_whole_clip_at_the_adaptive_recovery_rate() -> None:
    prompt = o11.recovery_prompt(30.0, SETTINGS)
    assert prompt.window is None
    (only,) = prompt.video_parts
    assert only.intro == "Part whole_clip: the full clip, 0.000 to 30.000 s, at 4 frames per second."
    assert only.fps == 4.0
    assert prompt.thinking_level == "low"
    assert prompt.generate_stage == "recovery.generate"


def test_recovery_request_adds_a_window_around_the_given_moment() -> None:
    prompt = o11.recovery_prompt(30.0, SETTINGS, centre=20.0)
    start, end = recovery_window(30.0, 20.0)
    assert prompt.window == (start, end)
    whole, detail = prompt.video_parts
    assert whole.fps == 4.0
    assert detail.intro.startswith(
        f"Part detail_window: seconds {start:.3f} to {end:.3f} of the full clip, at 15 frames"
    )
    assert (detail.start_offset, detail.end_offset) == (f"{start:.3f}s", f"{end:.3f}s")


def test_a_short_clip_has_no_recovery_window_even_with_a_centre() -> None:
    assert o11.recovery_prompt(part_plan.DETAIL_WINDOW_SECONDS - 1.0, SETTINGS, centre=1.0).window is None


# --- transport: one upload, two parts of the same file ----------------------------------------------------------


def test_one_upload_serves_both_parts_with_the_same_file_and_different_metadata(tmp_path: Path) -> None:
    path = tmp_path / "public.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    uploads: list[bool] = []
    original = client.upload

    def upload(**kwargs: Any) -> SimpleNamespace:
        uploads.append(True)
        return original(**kwargs)

    client.upload = upload  # type: ignore[method-assign]
    transport = GoogleVideoTransport(client, inline_enabled=False)
    prompt = o11.main_prompt(30.0, SETTINGS, "\nview_role: exterior")
    transport.generate(PublicResearchVideo(path, True), prompt, SETTINGS)
    assert len(uploads) == 1
    contents = client.generated["contents"]
    assert [("text" in c) for c in contents] == [True, False, True, False, True]
    assert contents[0]["text"] == prompt.video_parts[0].intro
    assert contents[2]["text"] == prompt.video_parts[1].intro
    assert contents[4]["text"] == str(prompt)
    assert contents[1]["file_data"] == contents[3]["file_data"]
    assert contents[1]["video_metadata"] == {"fps": 1.0}
    start, end = main_window(30.0)
    assert contents[3]["video_metadata"] == {"fps": 15.0, "start_offset": f"{start:.3f}s", "end_offset": f"{end:.3f}s"}
    assert client.deleted == ["files/synthetic"]


def test_report_scope_uploads_once_for_main_and_recovery(tmp_path: Path) -> None:
    path = tmp_path / "public.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    uploads: list[bool] = []
    original = client.upload

    def upload(**kwargs: Any) -> SimpleNamespace:
        uploads.append(True)
        return original(**kwargs)

    client.upload = upload  # type: ignore[method-assign]
    transport = GoogleVideoTransport(client, inline_enabled=False)
    video = PublicResearchVideo(path, True)
    with transport.report_scope():
        transport.generate(video, o11.main_prompt(30.0, SETTINGS), SETTINGS)
        transport.generate(video, o11.recovery_prompt(30.0, SETTINGS), SETTINGS)
    assert len(uploads) == 1
    assert client.deleted == ["files/synthetic"]


def test_the_single_part_case_sends_one_video_part(tmp_path: Path) -> None:
    path = tmp_path / "public.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    GoogleVideoTransport(client).generate(
        PublicResearchVideo(path, True), o11.main_prompt(part_plan.DETAIL_WINDOW_SECONDS - 2.0, SETTINGS), SETTINGS
    )
    contents = client.generated["contents"]
    assert [("text" in c) for c in contents] == [True, False, True]
    assert contents[1]["video_metadata"] == {"fps": 15.0}


def test_overview_11_config_has_system_schema_and_low_thinking(tmp_path: Path) -> None:
    path = tmp_path / "public.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    transport = GoogleVideoTransport(client)
    for prompt in (o11.main_prompt(30.0, SETTINGS), o11.recovery_prompt(30.0, SETTINGS)):
        transport.generate(PublicResearchVideo(path, True), prompt, SETTINGS)
        config = client.generated["config"]
        assert config["thinking_config"] == {"thinking_level": "low"}
        assert config["system_instruction"] == o11.system_instruction()
        assert config["response_schema"] is OVERVIEW_11_SCHEMA
        assert config["response_mime_type"] == "application/json"


@pytest.mark.parametrize("version", [f"overview-{n}" for n in range(1, 11)])
def test_older_versions_send_no_thinking_config_and_the_old_single_part_shape(tmp_path: Path, version: str) -> None:
    path = tmp_path / "public.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    prompt = o10.build_prompt("\nview_role: exterior") if version == "overview-10" else prompt_for_version(version)
    GoogleVideoTransport(client).generate(PublicResearchVideo(path, True), prompt, SETTINGS)
    assert "thinking_config" not in client.generated["config"]
    contents = client.generated["contents"]
    assert len(contents) == 2 and contents[0]["video_metadata"] == {"fps": 1.0}


def test_the_old_overview_10_config_is_exactly_what_it_was(tmp_path: Path) -> None:
    path = tmp_path / "public.mp4"
    path.write_bytes(b"video")
    client = FakeClient()
    GoogleVideoTransport(client).generate(
        PublicResearchVideo(path, True), o10.build_prompt("\nview_role: exterior"), SETTINGS
    )
    assert client.generated["config"] == {
        "response_mime_type": "application/json",
        "system_instruction": o10.system_instruction(),
        "response_schema": OVERVIEW_10_SCHEMA,
    }


# --- versions and hashes -------------------------------------------------------------------------------------


def test_overview_11_is_a_known_version_and_the_default_is_unchanged() -> None:
    assert "overview-11" in OVERVIEW_PROMPT_VERSIONS
    assert OVERVIEW_PROMPT_VERSIONS == {f"overview-{n}" for n in range(1, 12)}
    assert PROMPT_VERSION == "overview-2"
    assert prompt_for_version("overview-11") == o11.user_prompt()


@pytest.mark.parametrize("version", sorted(STORED_HASHES))
def test_request_hashes_for_overview_1_to_9_are_still_unchanged(version: str) -> None:
    assert overview_request_hash(request_for(version, 30.0), SETTINGS) == STORED_HASHES[version]


def test_overview_10_request_hash_is_unchanged() -> None:
    assert overview_request_hash(request_for("overview-10"), SETTINGS) == OVERVIEW_10_HASH


def test_the_new_settings_do_not_change_an_older_hash() -> None:
    changed = GeminiSettings(model="gemini-3-flash-preview", fps=1.0, detail_fps=24.0, recovery_frame_budget=60.0)
    for version in ("overview-9", "overview-10"):
        assert overview_request_hash(request_for(version), changed) == overview_request_hash(
            request_for(version), SETTINGS
        )


def test_overview_11_hash_changes_with_system_text_user_text_window_rates_and_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = request_for("overview-11")
    base = overview_request_hash(request, SETTINGS)
    assert base == overview_request_hash(request, SETTINGS)
    assert base != overview_request_hash(request_for("overview-10"), SETTINGS)
    # window: a different duration moves the window
    assert overview_request_hash(request_for("overview-11", 31.0), SETTINGS) != base
    # rates
    assert overview_request_hash(request, GeminiSettings(model="gemini-3-flash-preview", fps=2.0)) != base
    assert overview_request_hash(request, GeminiSettings(model="gemini-3-flash-preview", detail_fps=20.0)) != base
    # thinking level
    monkeypatch.setattr(o11, "THINKING_LEVEL", "medium")
    assert overview_request_hash(request, SETTINGS) != base
    monkeypatch.undo()
    # prompt files
    real_load = o10._load
    monkeypatch.setattr(
        o10, "_load", lambda name: real_load(name) + ("changed system text\n" if "11_system" in name else "")
    )
    assert overview_request_hash(request, SETTINGS) != base
    monkeypatch.setattr(o10, "_load", lambda name: real_load(name) + ("changed user text" if "11_user" in name else ""))
    assert overview_request_hash(request, SETTINGS) != base


def test_overview_11_hash_needs_the_duration_to_know_the_window() -> None:
    with pytest.raises(AIConnectionError):
        overview_request_hash(request_for("overview-11", None), SETTINGS)


def test_the_recovery_hash_differs_from_the_main_hash_and_follows_its_window_and_rate() -> None:
    request = request_for("overview-11")
    main = overview_request_hash(request, SETTINGS)
    plain = overview_recovery_request_hash(request, SETTINGS, None)
    assert plain != main
    assert overview_recovery_request_hash(request, SETTINGS, 20.0) != plain
    assert (
        overview_recovery_request_hash(
            request, GeminiSettings(model="gemini-3-flash-preview", recovery_frame_budget=240.0), None
        )
        != plain
    )


def test_the_overview_11_system_text_is_the_file_plus_the_allowed_values_block() -> None:
    text = o11.system_instruction()
    assert text.startswith(o10._load("overview_11_system.md").rstrip("\n"))
    assert o10.allowed_values_block() in text
    assert o11.user_prompt() == o10._load("overview_11_user.md")


# --- schema ---------------------------------------------------------------------------------------------------


def _contact_items() -> dict[str, Any]:
    items: dict[str, Any] = OVERVIEW_11_SCHEMA["properties"]["collision_observations"]["items"]
    return items


def _assessment() -> dict[str, Any]:
    node: dict[str, Any] = OVERVIEW_11_SCHEMA["properties"]["incident_assessment"]
    return node


def test_source_part_is_a_required_enum_on_every_contact_item() -> None:
    items = _contact_items()
    assert items["properties"]["source_part"] == {"type": "STRING", "enum": list(get_args(ContactPart))}
    assert "source_part" in items["required"]
    assert items["propertyOrdering"].index("source_part") < items["propertyOrdering"].index("timestamp_seconds")
    assert set(items["required"]) == set(items["properties"]) == set(items["propertyOrdering"])


def test_contact_interval_part_is_a_nullable_enum_before_the_interval_times() -> None:
    node = _assessment()
    assert node["properties"]["contact_interval_part"] == {
        "type": "STRING",
        "enum": list(get_args(ContactPart)),
        "nullable": True,
    }
    order = node["propertyOrdering"]
    assert order.index("contact_interval_part") < order.index("contact_interval_start_seconds")
    assert order.index("contact_interval_part") == order.index("contact_interval_start_seconds") - 1
    assert "contact_interval_part" in node["required"]
    assert set(node["required"]) == set(node["properties"]) == set(order)


def test_the_part_enums_come_from_the_pydantic_literal() -> None:
    assert get_args(ContactPart) == ("whole_clip", "detail_window")
    for annotation in (
        CollisionObservation.model_fields["source_part"].annotation,
        IncidentAssessmentRow11.model_fields["contact_interval_part"].annotation,
    ):
        assert ContactPart in get_args(annotation)
        assert type(None) in get_args(annotation)


def test_other_party_on_structured_facts_says_when_it_is_filled() -> None:
    node = OVERVIEW_11_SCHEMA["properties"]["structured_facts"]["items"]["properties"]["other_party"]
    assert node["description"] == "Filled only on relative_position_of_other rows; empty on every other row."
    older = OVERVIEW_10_SCHEMA["properties"]["structured_facts"]["items"]["properties"]["other_party"]
    assert "description" not in older


def test_everything_else_in_the_schema_is_overview_10() -> None:
    new = copy.deepcopy(OVERVIEW_11_SCHEMA)
    new_items = new["properties"]["collision_observations"]["items"]
    for key in ("properties", "required", "propertyOrdering"):
        if key == "properties":
            del new_items[key]["source_part"]
        else:
            new_items[key].remove("source_part")
    node = new["properties"]["incident_assessment"]
    del node["properties"]["contact_interval_part"]
    node["required"].remove("contact_interval_part")
    node["propertyOrdering"].remove("contact_interval_part")
    del new["properties"]["structured_facts"]["items"]["properties"]["other_party"]["description"]
    assert new == OVERVIEW_10_SCHEMA


def test_the_schema_stays_inside_the_project_limits_and_overview_10_is_untouched() -> None:
    for node in walk(OVERVIEW_11_SCHEMA):
        if isinstance(node, dict):
            assert "maxItems" not in node and "minItems" not in node
    assert OVERVIEW_11_SCHEMA is not OVERVIEW_10_SCHEMA
    assert "source_part" not in str(OVERVIEW_10_SCHEMA)
    assert "contact_interval_part" not in str(OVERVIEW_10_SCHEMA)
