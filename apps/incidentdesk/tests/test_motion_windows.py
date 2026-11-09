"""Synthetic video checks for the opt-in motion experiment; no model calls."""

import subprocess
from pathlib import Path

import pytest

from incident_report.ai.dense import CandidateWindow
from incident_report.ai.motion import DECODE_COMMAND, detect_motion
from incident_report.model_calls import ModelCall
from incident_report.runlog import RunLog


def video(tmp_path: Path, frames: list[bytes]) -> Path:
    path = tmp_path / "synthetic.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pixel_format",
            "gray",
            "-video_size",
            "32x24",
            "-framerate",
            "10",
            "-i",
            "pipe:0",
            "-c:v",
            "ffv1",
            str(path),
        ],
        input=b"".join(frames),
        check=True,
        capture_output=True,
    )
    return path


def test_local_motion_is_deterministic_and_cut_is_excluded(tmp_path: Path) -> None:
    black = bytes(32 * 24)
    white = bytes([255]) * (32 * 24)
    path = video(tmp_path, [black] * 10 + [white] * 10)
    with path.open("rb") as stream:
        assert detect_motion(stream).candidates == ()
    patch = bytes(255 if 8 <= x < 20 and 8 <= y < 16 else 0 for y in range(24) for x in range(32))
    path = video(tmp_path, [black] * 10 + [patch] * 10)
    with path.open("rb") as stream:
        first = detect_motion(stream)
    with path.open("rb") as stream:
        second = detect_motion(stream)
    assert first == second
    assert first.candidates == (1.0,)


def test_union_merges_sources_caps_and_keeps_views_separate() -> None:
    from incident_report.ai.dense import CandidateWindow, DenseSettings
    from incident_report.ai.motion import select_windows

    overview = (CandidateWindow("a", 2.0, 0.0, 4.0, "possible contact"),)
    selected, dropped = select_windows(
        overview,
        {"a": (3.0, 15.0), "b": (3.0,)},
        DenseSettings(model="gemini-3.8-flash"),
        {"a": 16.0, "b": 4.0},
        cap=2,
    )
    assert [(w.view_id, w.start_seconds, w.end_seconds, w.source) for w in selected] == [
        ("a", 0.0, 5.0, "both"),
        ("a", 13.0, 16.0, "motion"),
    ]
    assert [(w.view_id, w.source) for w in dropped] == [("b", "motion")]


@pytest.mark.parametrize("flag", [None, "0", "1"])
def test_entry_flag_and_persisted_provenance_ignore_user_text(
    tmp_path: Path,
    run_log: RunLog,
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
) -> None:
    from incident_report.ai import AIRequest, AIResponse
    from incident_report.ai.dense import DenseSettings
    from incident_report.domain import Origin, ReportView, Role
    from incident_report.entry import analyse_incident
    from incident_report.facts import OverviewFacts
    from incident_report.hashing import sha256_hex
    from incident_report.storage import LocalEvidenceStore

    if flag is None:
        monkeypatch.delenv("INCIDENT_MOTION_WINDOWS", raising=False)
    else:
        monkeypatch.setenv("INCIDENT_MOTION_WINDOWS", flag)
    black = bytes(32 * 24)
    patch = bytes(255 if 8 <= x < 20 and 8 <= y < 16 else 0 for y in range(24) for x in range(32))
    path = video(tmp_path, [black] * 10 + [patch] * 10)
    key = sha256_hex(path.read_bytes())
    store = LocalEvidenceStore(tmp_path / "evidence")
    with path.open("rb") as stream:
        store.put(stream, sha256=key)
    hostile = "officer feedback ; $(touch stolen) -i https://invalid/"
    view = ReportView(
        filename=hostile,
        label=hostile,
        acquisition_method=hostile,
        role=Role.EXTERIOR,
        sha256=key,
        duration_seconds=2.0,
    )
    commands = []
    real_run = subprocess.run

    def capture(*args: object, **kwargs: object) -> object:
        commands.append(args[0])
        return real_run(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", capture)

    class FakeAI:
        def __init__(self) -> None:
            self.windows: list[CandidateWindow] = []

        def analyse(self, request: AIRequest) -> AIResponse:
            return AIResponse(
                summary="synthetic",
                provenance="synthetic",
                calls=(),
                facts=OverviewFacts(
                    parties=[], events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
                ),
            )

        def analyse_window(
            self, request: AIRequest, window: CandidateWindow
        ) -> tuple[OverviewFacts, tuple[ModelCall, ...]]:
            self.windows.append(window)
            return OverviewFacts(
                parties=[], events=[], candidate_collisions=[], behaviour_observations=[], location_conditions=[]
            ), ()

    ai = FakeAI()
    report = analyse_incident(
        [view],
        origin=Origin.REPLAY,
        ai=ai,
        run_log=run_log,
        evidence=store,
        officer_question=hostile,
        dense_settings=DenseSettings(model="gemini-3.8-flash"),
    )
    persisted = run_log.get(report.run_id)
    assert persisted is not None and persisted.report is not None
    if flag == "1":
        assert len(ai.windows) == 1
        assert persisted.motion_window_selection is not None
        assert persisted.motion_window_selection.selected[0].source == "motion"
        assert persisted.motion_window_selection.selected[0].view_id == key
        assert commands == [DECODE_COMMAND]
        assert all(hostile not in str(window) for window in ai.windows)
        assert run_log.list_runs()[0].motion_window_selection == persisted.motion_window_selection
        assert run_log.list_all_runs()[0].run.motion_window_selection == persisted.motion_window_selection
        assert "motion_window_selection" not in report.model_dump()
    else:
        assert ai.windows == []
        assert commands == []
        assert persisted.motion_window_selection is None


def test_translation_shake_is_separate_from_motion_candidates(tmp_path: Path) -> None:
    import random

    rng = random.Random(42)  # noqa: S311 deterministic synthetic texture, no secrets
    texture = bytes(rng.randrange(256) for _ in range(32 * 24))
    shifted = bytes(texture[y * 32 + max(0, x - 1)] for y in range(24) for x in range(32))
    path = video(tmp_path, [texture] * 10 + [shifted] * 10)
    with path.open("rb") as stream:
        result = detect_motion(stream)
    assert result.candidates == ()
    assert 1.0 in result.camera_shake


def test_variable_frame_timing_uses_presentation_timestamps(tmp_path: Path) -> None:
    black = bytes(32 * 24)
    patch = bytes(255 if 8 <= x < 20 and 8 <= y < 16 else 0 for y in range(24) for x in range(32))
    original = video(tmp_path, [black] * 10 + [patch] * 10)
    delayed = tmp_path / "delayed.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(original),
            "-vf",
            "setpts=if(gte(N\\,10)\\,PTS+1/TB\\,PTS)",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "ffv1",
            str(delayed),
        ],
        check=True,
        capture_output=True,
    )
    with delayed.open("rb") as stream:
        assert detect_motion(stream).candidates == (2.0,)


def test_invalid_media_fails_without_inventing_candidates(tmp_path: Path) -> None:
    from incident_report.ai.errors import AIConnectionError

    invalid = tmp_path / "invalid.mp4"
    invalid.write_bytes(b"not video")
    with invalid.open("rb") as stream, pytest.raises(AIConnectionError, match="decoding failed"):
        detect_motion(stream)


@pytest.mark.parametrize("count", [9, 11])
def test_overview_windows_over_cap_are_all_kept_without_motion(count: int) -> None:
    from incident_report.ai.dense import DenseSettings
    from incident_report.ai.motion import select_windows

    overview = tuple(
        CandidateWindow("a", float(index * 10 + 1), float(index * 10), float(index * 10 + 2), "overview candidate")
        for index in range(count)
    )
    selected, dropped = select_windows(overview, {}, DenseSettings(model="gemini-3.8-flash"), {"a": 120.0})
    assert selected == overview
    assert dropped == ()


@pytest.mark.parametrize("cap", [0, 1, 2, 4])
def test_late_overview_and_both_windows_survive_earlier_motion_overflow(cap: int) -> None:
    from incident_report.ai.dense import DenseSettings
    from incident_report.ai.motion import select_windows

    overview = (
        CandidateWindow("a", 90.0, 88.0, 92.0, "late overview candidate"),
        CandidateWindow("z", 10.0, 8.0, 12.0, "last view overview candidate"),
    )
    selected, dropped = select_windows(
        overview,
        {"b": (5.0,), "a": (5.0, 15.0, 25.0, 35.0, 90.0)},
        DenseSettings(model="gemini-3.8-flash"),
        {"a": 100.0, "b": 100.0, "z": 100.0},
        cap=cap,
    )
    assert [(w.view_id, w.start_seconds, w.source) for w in selected if w.source != "motion"] == [
        ("a", 88.0, "both"),
        ("z", 8.0, "overview"),
    ]
    expected_kept = [("a", 5.0), ("a", 15.0)] if cap == 4 else []
    expected_dropped = (
        [("a", 25.0), ("a", 35.0), ("b", 5.0)]
        if cap == 4
        else [("a", 5.0), ("a", 15.0), ("a", 25.0), ("a", 35.0), ("b", 5.0)]
    )
    assert [(w.view_id, w.candidate_timestamp) for w in selected if w.source == "motion"] == expected_kept
    assert [(w.view_id, w.candidate_timestamp) for w in dropped] == expected_dropped
    assert all(w.source == "motion" for w in dropped)
    assert [(w.view_id, w.start_seconds) for w in selected] == sorted((w.view_id, w.start_seconds) for w in selected)
