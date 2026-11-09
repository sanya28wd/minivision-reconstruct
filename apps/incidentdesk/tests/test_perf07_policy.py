"""PERF-07: part policies (middle / coverage / oracle), per-part media_resolution and the measurement fields.

Offline only. The policy that places parts lives in ai.part_plan; these tests ask it, and check the wire form against
the installed google-genai types.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.genai import types as genai_types

from incident_report.ai import overview11 as o11
from incident_report.ai.errors import AIConnectionError
from incident_report.ai.gemini import GeminiSettings
from incident_report.ai.google_video import _contents
from incident_report.ai.part_plan import COVERAGE_FRAME_BUDGET, PartPlan, coverage_fps, plan_parts

MODEL = "gemini-3-flash-preview"


def main(duration: float, **kw: Any) -> list[PartPlan]:
    return plan_parts(duration, mode="main", whole_fps=1.0, detail_fps=15.0, **kw)


# --- the three policies -------------------------------------------------------------------------------------------


def test_middle_is_todays_plan_and_the_default() -> None:
    assert main(30.0) == main(30.0, policy="middle")
    assert main(30.0) == [PartPlan("whole_clip", 0.0, 30.0, 1.0), PartPlan("detail_window", 13.0, 17.0, 15.0)]


def test_coverage_is_one_whole_clip_part_at_the_budget_rate() -> None:
    assert COVERAGE_FRAME_BUDGET == 180.0
    assert main(30.0, policy="coverage") == [PartPlan("whole_clip", 0.0, 30.0, 6.0)]


@pytest.mark.parametrize(
    ("duration", "expected"), [(4.0, 24.0), (12.0, 15.0), (30.0, 6.0), (38.0, 180.0 / 38.0), (400.0, 1.0)]
)
def test_coverage_fps_formula(duration: float, expected: float) -> None:
    assert coverage_fps(duration, 180.0) == pytest.approx(expected)
    assert main(duration, policy="coverage")[0].fps == pytest.approx(expected)
    assert 0 < coverage_fps(duration, 180.0) <= 24


def test_coverage_uses_the_budget_setting() -> None:
    assert main(30.0, policy="coverage", coverage_frame_budget=90.0)[0].fps == pytest.approx(3.0)


def test_oracle_centres_the_window_on_the_given_time() -> None:
    whole, window = main(30.0, policy="oracle", oracle_centre_seconds=20.0)
    assert whole == PartPlan("whole_clip", 0.0, 30.0, 1.0)
    assert window == PartPlan("detail_window", 18.0, 22.0, 15.0)
    # shifted, not shrunk, at the edges
    assert main(30.0, policy="oracle", oracle_centre_seconds=29.5)[1] == PartPlan("detail_window", 26.0, 30.0, 15.0)


def test_oracle_without_a_time_is_refused() -> None:
    with pytest.raises(ValueError, match="oracle"):
        main(30.0, policy="oracle")


def test_recovery_is_unchanged_by_every_policy() -> None:
    base = plan_parts(30.0, mode="recovery", contacts_seconds=[20.0], whole_fps=1.0, detail_fps=15.0)
    for policy in ("middle", "coverage", "oracle"):
        got = plan_parts(
            30.0,
            mode="recovery",
            contacts_seconds=[20.0],
            whole_fps=1.0,
            detail_fps=15.0,
            policy=policy,
            media_resolution="high",
            oracle_centre_seconds=3.0,
        )
        assert got == base


def test_resolution_goes_on_the_detail_part_or_the_single_coverage_part_only() -> None:
    whole, detail = main(30.0, media_resolution="high")
    assert (whole.resolution, detail.resolution) == ("default", "high")
    assert main(30.0, policy="coverage", media_resolution="high")[0].resolution == "high"
    assert main(30.0, policy="oracle", oracle_centre_seconds=5.0, media_resolution="high")[1].resolution == "high"
    assert main(3.0, media_resolution="high")[0].resolution == "high"  # short clip: the one detail-rate part


# --- settings: where the policy comes from, and the oracle refusal ---------------------------------------------------


def test_settings_default_to_todays_behaviour() -> None:
    s = GeminiSettings(model=MODEL)
    assert (s.part_policy, s.media_resolution, s.oracle_contact_seconds) == ("middle", "default", None)
    assert s.coverage_frame_budget == COVERAGE_FRAME_BUDGET


def test_production_entry_refuses_the_oracle_policy() -> None:
    # from_env is the only way production builds settings; the oracle policy cannot come through it.
    with pytest.raises(AIConnectionError, match="evaluation only"):
        GeminiSettings.from_env({"GEMINI_MODEL": MODEL, "GEMINI_PART_POLICY": "oracle"})
    ok = GeminiSettings.from_env(
        {
            "GEMINI_MODEL": MODEL,
            "GEMINI_PART_POLICY": "coverage",
            "GEMINI_MEDIA_RESOLUTION": "high",
            "GEMINI_COVERAGE_FRAME_BUDGET": "90",
        }
    )
    assert (ok.part_policy, ok.media_resolution, ok.coverage_frame_budget) == ("coverage", "high", 90.0)


def test_oracle_settings_need_a_benchmark_time_and_other_policies_refuse_one() -> None:
    with pytest.raises(AIConnectionError):
        GeminiSettings(model=MODEL, part_policy="oracle")
    with pytest.raises(AIConnectionError):
        GeminiSettings(model=MODEL, part_policy="middle", oracle_contact_seconds=3.0)
    assert GeminiSettings(model=MODEL, part_policy="oracle", oracle_contact_seconds=3.0).oracle_contact_seconds == 3.0


@pytest.mark.parametrize(
    "bad",
    [
        {"part_policy": "locator"},
        {"media_resolution": "ultra"},
        {"coverage_frame_budget": 0.0},
        {"coverage_frame_budget": float("inf")},
    ],
)
def test_bad_policy_settings_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(AIConnectionError):
        GeminiSettings(model=MODEL, **bad)


def test_the_prompt_carries_the_policy_plan() -> None:
    cov = GeminiSettings(model=MODEL, part_policy="coverage", media_resolution="high")
    prompt = o11.main_prompt(30.0, cov)
    assert [(p.label, p.fps, p.media_resolution) for p in prompt.video_parts] == [("whole_clip", 6.0, "high")]
    assert prompt.window is None
    orc = GeminiSettings(model=MODEL, part_policy="oracle", oracle_contact_seconds=20.0)
    assert o11.window_of(o11.main_plan(30.0, orc)) == (18.0, 22.0)
    rec = o11.recovery_prompt(30.0, cov)  # recovery never takes the resolution
    assert all(p.media_resolution == "default" for p in rec.video_parts)


def test_request_hash_payload_is_unchanged_by_default_and_changes_with_resolution() -> None:
    d = GeminiSettings(model=MODEL)
    base = o11.parts_for_hash(o11.main_prompt(30.0, d).video_parts)
    assert all(len(row) == 5 for row in base)  # default hash payload is byte-for-byte today's
    hi = o11.parts_for_hash(o11.main_prompt(30.0, GeminiSettings(model=MODEL, media_resolution="high")).video_parts)
    assert hi != base


# --- the wire form ---------------------------------------------------------------------------------------------------


def test_high_resolution_is_the_sdk_per_part_media_resolution_field() -> None:
    settings = GeminiSettings(model=MODEL, part_policy="coverage", media_resolution="high")
    contents = _contents(b"x", o11.main_prompt(30.0, settings), {"fps": 1.0})
    video_parts = [c for c in contents if "inline_data" in c]
    assert len(video_parts) == 1
    assert video_parts[0]["media_resolution"] == {"level": "MEDIA_RESOLUTION_HIGH"}
    part = genai_types.Part.model_validate(video_parts[0])
    assert part.media_resolution is not None
    assert part.media_resolution.level == genai_types.PartMediaResolutionLevel.MEDIA_RESOLUTION_HIGH
    assert part.video_metadata is not None and part.video_metadata.fps == 6.0


def test_default_resolution_adds_no_field_to_the_wire() -> None:
    contents = _contents(b"x", o11.main_prompt(30.0, GeminiSettings(model=MODEL)), {"fps": 1.0})
    assert all("media_resolution" not in c for c in contents)
    for c in contents:
        genai_types.Part.model_validate(c)


def test_only_the_detail_part_carries_high_in_the_default_policy() -> None:
    contents = _contents(b"x", o11.main_prompt(30.0, GeminiSettings(model=MODEL, media_resolution="high")), {})
    flags = ["media_resolution" in c for c in contents if "inline_data" in c]
    assert flags == [False, True]


def test_part_fps_follows_the_policy_plan_actually_sent() -> None:
    cov = GeminiSettings(model=MODEL, part_policy="coverage")
    assert o11.part_fps_of(o11.main_plan(30.0, cov)) == {"whole_clip": 6.0}
    orc = GeminiSettings(model=MODEL, part_policy="oracle", oracle_contact_seconds=20.0)
    assert o11.part_fps_of(o11.main_plan(30.0, orc)) == {"whole_clip": 1.0, "detail_window": 15.0}
