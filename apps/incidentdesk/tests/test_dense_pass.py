"""Tests for RPT-19 dense collision pass (ADR 0008, spec 001)."""

from __future__ import annotations

import pytest

from incident_report.ai.dense import (
    CandidateWindow,
    DenseSettings,
    calculate_overview_collision_recall,
    compute_windows,
)
from incident_report.ai.errors import AIConnectionError
from incident_report.facts import CitedFact


def test_dense_settings_defaults_and_validation() -> None:
    settings = DenseSettings(model="gemini-3.8-flash")
    # Config values labelled 'project assumption, to be validated' (ADR 0006).
    assert settings.fps == 20.0
    assert settings.window_before_seconds == 2.0
    assert settings.window_after_seconds == 2.0
    assert settings.max_retries == 2


@pytest.mark.parametrize("fps", [0.0, 10.0, 14.9, 24.1, 30.0, float("nan"), float("inf")])
def test_dense_settings_refuses_fps_outside_supported_configurable_range(fps: float) -> None:
    """ADR 0008 specifies 15-30 fps, but google-genai VideoMetadata.fps accepts (0, 24].

    Intersection is [15, 24].
    """
    with pytest.raises(AIConnectionError, match="Dense FPS must be finite and in the range"):
        DenseSettings(model="gemini-3.8-flash", fps=fps)


@pytest.mark.parametrize("window", [0.0, -1.0, float("nan")])
def test_dense_settings_refuses_invalid_window_lengths(window: float) -> None:
    with pytest.raises(AIConnectionError, match="window lengths must be finite and positive"):
        DenseSettings(model="gemini-3.8-flash", window_before_seconds=window)
    with pytest.raises(AIConnectionError, match="window lengths must be finite and positive"):
        DenseSettings(model="gemini-3.8-flash", window_after_seconds=window)


def test_dense_settings_from_env() -> None:
    env = {
        "GEMINI_MODEL": "gemini-3.8-flash",
        "GEMINI_DENSE_FPS": "18.5",
        "GEMINI_DENSE_WINDOW_BEFORE_SECONDS": "1.5",
        "GEMINI_DENSE_WINDOW_AFTER_SECONDS": "2.5",
    }
    settings = DenseSettings.from_env(env)
    assert settings.fps == 18.5
    assert settings.window_before_seconds == 1.5
    assert settings.window_after_seconds == 2.5


def test_compute_windows_bounds_windows_by_zero_and_view_duration() -> None:
    settings = DenseSettings(model="gemini-3.8-flash", window_before_seconds=2.0, window_after_seconds=2.0)
    candidates = [
        CitedFact(description="possible collision near start", view_id="view-a", timestamp_seconds=1.0, confidence=0.8),
        CitedFact(description="possible collision mid-clip", view_id="view-a", timestamp_seconds=5.0, confidence=0.9),
        CitedFact(description="possible collision near end", view_id="view-a", timestamp_seconds=9.5, confidence=0.85),
    ]
    durations = {"view-a": 10.0}
    windows = compute_windows(candidates, settings, view_durations=durations)

    assert len(windows) == 3
    # Near start: clamped at 0.0
    assert windows[0] == CandidateWindow(
        view_id="view-a",
        candidate_timestamp=1.0,
        start_seconds=0.0,
        end_seconds=3.0,
        candidate_description="possible collision near start",
    )
    # Mid-clip: 5.0 - 2.0 = 3.0, 5.0 + 2.0 = 7.0
    assert windows[1] == CandidateWindow(
        view_id="view-a",
        candidate_timestamp=5.0,
        start_seconds=3.0,
        end_seconds=7.0,
        candidate_description="possible collision mid-clip",
    )
    # Near end: clamped at 10.0
    assert windows[2] == CandidateWindow(
        view_id="view-a",
        candidate_timestamp=9.5,
        start_seconds=7.5,
        end_seconds=10.0,
        candidate_description="possible collision near end",
    )


def test_overview_collision_recall_metric_calculation() -> None:
    # When ground truth is empty or None, recall is None
    candidates = [
        CitedFact(description="contact at 5.0s", view_id="v1", timestamp_seconds=5.0, confidence=0.9),
    ]
    assert calculate_overview_collision_recall(candidates, None) is None
    assert calculate_overview_collision_recall(candidates, []) is None

    # When ground truth is provided: 2 true collisions at 5.1s and 12.0s
    truth = [5.1, 12.0]
    # tolerance = 2.0s (default): 5.0 matches 5.1 (diff 0.1 <= 2.0), 12.0 has no match
    # Recall = 1 / 2 = 0.5
    recall = calculate_overview_collision_recall(candidates, truth, tolerance_seconds=2.0)
    assert recall == 0.5

    # If candidates also flagged 11.5s:
    candidates.append(CitedFact(description="contact at 11.5s", view_id="v1", timestamp_seconds=11.5, confidence=0.8))
    # Both true collisions matched -> Recall = 2 / 2 = 1.0
    assert calculate_overview_collision_recall(candidates, truth, tolerance_seconds=2.0) == 1.0


class FakeDenseTransport:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, float, float, float]] = []

    def generate_window(
        self,
        video: object,
        prompt: str,
        settings: DenseSettings,
        *,
        start_offset_seconds: float,
        end_offset_seconds: float,
    ) -> str:
        self.calls.append((prompt, settings.fps, start_offset_seconds, end_offset_seconds))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


DENSE_RAW = (
    '{"parties":[{"description":"silver Toyota sedan","timestamp_seconds":0.5,"confidence":0.95},'
    '{"description":"white Nissan patrol SUV","timestamp_seconds":0.5,"confidence":0.92}],'
    '"events":[{"description":"silver Toyota sedan brakes heavily","timestamp_seconds":0.5,"confidence":0.9},'
    '{"description":"silver Toyota sedan front strikes white Nissan patrol SUV rear (striker: silver Toyota sedan, struck: white Nissan patrol SUV)","timestamp_seconds":0.75,"confidence":0.95}],'
    '"candidate_collisions":[{"description":"settled contact: silver Toyota sedan front strikes white Nissan patrol SUV rear","timestamp_seconds":0.75,"confidence":0.95}]}'
)


def test_dense_pass_returns_facts_with_attached_view_id_and_call_record(tmp_path: object) -> None:
    from pathlib import Path

    from incident_report.ai import AIRequest, AIViewRef
    from incident_report.ai.dense import DensePassAIConnection
    from incident_report.ai.gemini import PublicResearchVideo
    from incident_report.domain import Role
    from incident_report.hashing import incident_input_hash, sha256_hex
    from video_fixtures import synthetic_video

    video_bytes = synthetic_video(b"dense-test-clip")
    video_path = Path(str(tmp_path)) / "video.mp4"
    video_path.write_bytes(video_bytes)
    view_hash = sha256_hex(video_bytes)

    req = AIRequest(
        input_hash=incident_input_hash([(Role.EXTERIOR, view_hash)]),
        prompt_version="dense-1",
        views=(AIViewRef(Role.EXTERIOR, view_hash, duration_seconds=5.0),),
    )
    window = CandidateWindow(
        view_id=view_hash,
        candidate_timestamp=0.75,
        start_seconds=0.0,
        end_seconds=2.75,
        candidate_description="possible contact involving silver Toyota sedan",
    )

    transport = FakeDenseTransport([DENSE_RAW])
    settings = DenseSettings(model="gemini-3.8-flash", fps=20.0)
    conn = DensePassAIConnection(settings, PublicResearchVideo(video_path, True), transport=transport)

    facts, calls = conn.analyse_window(req, window)

    assert len(calls) == 1
    call = calls[0]
    assert call.prompt_version == "dense-1"
    assert call.fps == 20.0
    assert call.view_id == view_hash
    assert call.raw_response == DENSE_RAW
    assert call.status_code is None
    assert call.retry_index == 0

    # Transport received window start and end offset and fps
    assert len(transport.calls) == 1
    prompt, fps, start_sec, end_sec = transport.calls[0]
    assert fps == 20.0
    assert start_sec == 0.0
    assert end_sec == 2.75
    # Prompt does not contain raw view_id (the code attaches it)
    assert view_hash not in prompt

    # Facts: view_id attached to all groups
    assert len(facts.parties) == 2
    assert facts.parties[0].view_id == view_hash
    assert facts.parties[1].view_id == view_hash
    assert facts.events[1].view_id == view_hash
    assert facts.candidate_collisions[0].view_id == view_hash
    assert "striker: silver Toyota sedan" in facts.events[1].description


def test_dense_pass_retries_429_and_503_bounded_with_backoff(tmp_path: object) -> None:
    from pathlib import Path

    from incident_report.ai import AIRequest, AIViewRef
    from incident_report.ai.dense import DensePassAIConnection
    from incident_report.ai.gemini import PublicResearchVideo, TransportError
    from incident_report.domain import Role
    from incident_report.hashing import incident_input_hash, sha256_hex
    from video_fixtures import synthetic_video

    video_bytes = synthetic_video(b"retry-clip")
    video_path = Path(str(tmp_path)) / "video.mp4"
    video_path.write_bytes(video_bytes)
    view_hash = sha256_hex(video_bytes)

    req = AIRequest(
        input_hash=incident_input_hash([(Role.EXTERIOR, view_hash)]),
        prompt_version="dense-1",
        views=(AIViewRef(Role.EXTERIOR, view_hash, duration_seconds=5.0),),
    )
    window = CandidateWindow(
        view_id=view_hash,
        candidate_timestamp=0.75,
        start_seconds=0.0,
        end_seconds=2.75,
        candidate_description="possible contact",
    )

    sleeps: list[float] = []
    transport = FakeDenseTransport([TransportError(503), TransportError(429), DENSE_RAW])
    settings = DenseSettings(model="gemini-3.8-flash", fps=20.0)
    conn = DensePassAIConnection(
        settings, PublicResearchVideo(video_path, True), transport=transport, sleep=sleeps.append
    )

    facts, calls = conn.analyse_window(req, window)

    assert len(calls) == 3
    assert [c.status_code for c in calls] == [503, 429, None]
    assert [c.retry_index for c in calls] == [0, 1, 2]
    assert sleeps == [1.0, 2.0]
    assert len(facts.parties) == 2


def test_dense_pass_refuses_missing_confidence(tmp_path: object) -> None:
    from pathlib import Path

    from incident_report.ai import AIRequest, AIViewRef
    from incident_report.ai.dense import DensePassAIConnection
    from incident_report.ai.gemini import PublicResearchVideo
    from incident_report.domain import Role
    from incident_report.hashing import incident_input_hash, sha256_hex
    from video_fixtures import synthetic_video

    video_bytes = synthetic_video(b"confidence-clip")
    video_path = Path(str(tmp_path)) / "video.mp4"
    video_path.write_bytes(video_bytes)
    view_hash = sha256_hex(video_bytes)

    req = AIRequest(
        input_hash=incident_input_hash([(Role.EXTERIOR, view_hash)]),
        prompt_version="dense-1",
        views=(AIViewRef(Role.EXTERIOR, view_hash, duration_seconds=5.0),),
    )
    window = CandidateWindow(
        view_id=view_hash,
        candidate_timestamp=0.75,
        start_seconds=0.0,
        end_seconds=2.75,
        candidate_description="possible contact",
    )

    # Missing confidence in parties[0]
    bad_raw = DENSE_RAW.replace(',"confidence":0.95', "", 1)
    transport = FakeDenseTransport([bad_raw])
    settings = DenseSettings(model="gemini-3.8-flash", fps=20.0)
    conn = DensePassAIConnection(settings, PublicResearchVideo(video_path, True), transport=transport)

    with pytest.raises(AIConnectionError, match="Cannot Determine"):
        conn.analyse_window(req, window)


def test_dense_replay_replays_from_recorded_calls(tmp_path: object) -> None:
    from pathlib import Path

    from incident_report.ai import AIRequest, AIViewRef
    from incident_report.ai.dense import DensePassAIConnection
    from incident_report.ai.dense_replay import ReplayDenseAIConnection
    from incident_report.ai.gemini import PublicResearchVideo
    from incident_report.domain import Role
    from incident_report.hashing import incident_input_hash, sha256_hex
    from video_fixtures import synthetic_video

    video_bytes = synthetic_video(b"replay-clip")
    video_path = Path(str(tmp_path)) / "video.mp4"
    video_path.write_bytes(video_bytes)
    view_hash = sha256_hex(video_bytes)

    req = AIRequest(
        input_hash=incident_input_hash([(Role.EXTERIOR, view_hash)]),
        prompt_version="dense-1",
        views=(AIViewRef(Role.EXTERIOR, view_hash, duration_seconds=5.0),),
    )
    window = CandidateWindow(
        view_id=view_hash,
        candidate_timestamp=0.75,
        start_seconds=0.0,
        end_seconds=2.75,
        candidate_description="possible contact",
    )

    transport = FakeDenseTransport([DENSE_RAW])
    settings = DenseSettings(model="gemini-3.8-flash", fps=20.0)
    live_conn = DensePassAIConnection(settings, PublicResearchVideo(video_path, True), transport=transport)
    live_facts, calls = live_conn.analyse_window(req, window)

    # Replay without video or transport
    replay = ReplayDenseAIConnection(settings, calls)
    replayed_facts = replay.analyse_window(req, window)
    assert replayed_facts == live_facts

    # Config change raises AIConnectionError
    changed_settings = DenseSettings(model="gemini-3.8-flash", fps=18.0)
    replay_changed = ReplayDenseAIConnection(changed_settings, calls)
    with pytest.raises(AIConnectionError, match="recording"):
        replay_changed.analyse_window(req, window)


def test_consolidate_facts_settles_contact_and_enriches_events() -> None:
    from incident_report.ai.dense import consolidate_facts
    from incident_report.facts import OverviewFacts, PartyFact

    overview = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(description="silver Toyota sedan", view_id="v1", timestamp_seconds=0.5, confidence=0.9),
            PartyFact(description="white Nissan patrol SUV", view_id="v1", timestamp_seconds=0.5, confidence=0.9),
        ],
        events=[CitedFact(description="sedan approaches", view_id="v1", timestamp_seconds=0.2, confidence=0.85)],
        candidate_collisions=[
            CitedFact(description="possible collision", view_id="v1", timestamp_seconds=0.75, confidence=0.9)
        ],
    )
    dense = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(description="silver Toyota sedan", view_id="v1", timestamp_seconds=0.5, confidence=0.95),
            PartyFact(description="white Nissan patrol SUV", view_id="v1", timestamp_seconds=0.5, confidence=0.9),
        ],
        events=[
            CitedFact(
                description=(
                    "silver Toyota sedan front strikes white Nissan patrol SUV rear "
                    "(striker: silver Toyota sedan, struck: white Nissan patrol SUV)"
                ),
                view_id="v1",
                timestamp_seconds=0.75,
                confidence=0.95,
            ),
        ],
        candidate_collisions=[
            CitedFact(
                description="settled collision: silver Toyota sedan strikes white Nissan patrol SUV",
                view_id="v1",
                timestamp_seconds=0.75,
                confidence=0.95,
            ),
        ],
    )
    consolidated = consolidate_facts(overview, [dense])
    assert len(consolidated.parties) == 2
    assert len(consolidated.events) == 2
    assert consolidated.events[0].description == "sedan approaches"
    assert "striker: silver Toyota sedan" in consolidated.events[1].description
    assert len(consolidated.candidate_collisions) == 1
    assert consolidated.candidate_collisions[0].description.startswith("settled collision:")


def test_analyse_incident_runs_dense_pass_and_records_stage_latencies_and_recall(
    tmp_path: object, run_log: object
) -> None:
    from pathlib import Path

    from incident_report.ai import AIConnection, AIRequest, AIResponse
    from incident_report.ai.dense import DenseSettings
    from incident_report.domain import Origin, Role, View
    from incident_report.entry import analyse_incident
    from incident_report.facts import OverviewFacts, PartyFact
    from incident_report.hashing import sha256_hex
    from incident_report.model_calls import ModelCall
    from video_fixtures import synthetic_video

    video_bytes = synthetic_video(b"pipeline-clip")
    video_path = Path(str(tmp_path)) / "video.mp4"
    video_path.write_bytes(video_bytes)
    view_hash = sha256_hex(video_bytes)

    overview_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(description="silver Toyota sedan", view_id=view_hash, timestamp_seconds=0.5, confidence=0.9),
            PartyFact(description="white Nissan patrol SUV", view_id=view_hash, timestamp_seconds=0.5, confidence=0.9),
        ],
        events=[CitedFact(description="sedan approaches", view_id=view_hash, timestamp_seconds=0.2, confidence=0.85)],
        candidate_collisions=[
            CitedFact(description="possible collision", view_id=view_hash, timestamp_seconds=0.75, confidence=0.9)
        ],
    )

    class MockTwoPassAI(AIConnection):
        def __init__(self) -> None:
            self.dense_windows_called: list[CandidateWindow] = []

        def analyse(self, request: AIRequest) -> AIResponse:
            return AIResponse(
                summary="overview summary",
                provenance="mock-overview",
                facts=overview_facts,
                calls=(
                    ModelCall(
                        request_hash="aa" * 32,
                        input_hash=request.input_hash,
                        prompt_version=request.prompt_version,
                        model="gemini-3.8-flash",
                        view_id=request.views[0].sha256,
                        fps=1.0,
                        raw_response='{"overview": true}',
                        latency_ms=50.0,
                    ),
                ),
            )

        def analyse_window(
            self, request: AIRequest, window: CandidateWindow
        ) -> tuple[OverviewFacts, tuple[ModelCall, ...]]:
            assert request.prompt_version == "dense-2"
            assert window.overview_party_descriptions == ("silver Toyota sedan", "white Nissan patrol SUV")
            self.dense_windows_called.append(window)
            dense_facts = OverviewFacts(
                behaviour_observations=[],
                location_conditions=[],
                parties=[
                    PartyFact(
                        description="silver Toyota sedan",
                        view_id=window.view_id,
                        timestamp_seconds=0.5,
                        confidence=0.95,
                    ),
                    PartyFact(
                        description="white Nissan patrol SUV",
                        view_id=window.view_id,
                        timestamp_seconds=0.5,
                        confidence=0.9,
                    ),
                    PartyFact(
                        description="silver sedan approaching",
                        view_id=window.view_id,
                        timestamp_seconds=0.5,
                        confidence=0.9,
                    ),
                ],
                events=[
                    CitedFact(
                        description="impact (striker: silver Toyota sedan, struck: white Nissan patrol SUV)",
                        view_id=window.view_id,
                        timestamp_seconds=window.candidate_timestamp,
                        confidence=0.95,
                    ),
                ],
                candidate_collisions=[
                    CitedFact(
                        description="confirmed collision: silver Toyota sedan strikes white Nissan patrol SUV",
                        view_id=window.view_id,
                        timestamp_seconds=window.candidate_timestamp,
                        confidence=0.95,
                    ),
                ],
            )
            dense_call = ModelCall(
                request_hash="bb" * 32,
                input_hash=request.input_hash,
                prompt_version="dense-1",
                model="gemini-3.8-flash",
                view_id=window.view_id,
                fps=20.0,
                raw_response='{"dense": true}',
                latency_ms=75.0,
            )
            return dense_facts, (dense_call,)

    ai = MockTwoPassAI()
    import io

    view = View(
        stream=io.BytesIO(video_bytes),
        filename="front.mp4",
        role=Role.EXTERIOR,
        duration_seconds=5.0,
    )

    # Call analyse_incident with ground truth collision at 0.7s (tolerance 2.0s -> match!)
    report = analyse_incident(
        [view],
        Origin.BENCHMARK,
        ai=ai,
        run_log=run_log,  # type: ignore[arg-type]
        ground_truth_collisions=[0.7],
        dense_settings=DenseSettings(model="gemini-3.8-flash", fps=20.0),
    )

    # Dense pass was executed on candidate window
    assert len(ai.dense_windows_called) == 1
    assert ai.dense_windows_called[0].candidate_timestamp == 0.75
    assert ai.dense_windows_called[0].start_seconds == 0.0
    # Clamped to view duration (synthetic_video has duration 1.0s)
    assert ai.dense_windows_called[0].end_seconds == 1.0

    # Stage latencies recorded for intake, overview, dense, validation
    assert "intake" in report.stage_latencies_ms
    assert "overview" in report.stage_latencies_ms
    assert "dense" in report.stage_latencies_ms
    assert "validation" in report.stage_latencies_ms
    assert report.stage_latencies_ms["dense"] >= 0

    # Overview collision recall recorded: 1 / 1 = 1.0
    assert report.overview_collision_recall == 1.0

    # Dense facts recorded and consolidated into report.facts
    assert len(report.dense_facts) == 1
    assert report.facts is not None
    assert len(report.facts.parties) == 2
    assert report.facts.candidate_collisions[0].description.startswith("confirmed collision:")

    assert [p.description for p in report.facts.unmatched_dense_parties] == ["silver sedan approaching"]
    assert report.dense_facts[0].parties[-1].description == "silver sedan approaching"
    assert len(report.parties) == 2

    # Run log contains both calls (overview and dense) and exposes properties
    run = run_log.get(report.run_id)  # type: ignore[attr-defined]
    assert len(run.calls) == 2
    assert run.calls[0].prompt_version == "skeleton-1"
    assert run.calls[1].prompt_version == "dense-1"
    assert run.stage_latencies_ms == report.stage_latencies_ms
    assert run.overview_collision_recall == 1.0


def test_dense_pass_does_not_run_when_no_candidate_collisions(tmp_path: object, run_log: object) -> None:
    import io
    from pathlib import Path

    from incident_report.ai import AIConnection, AIRequest, AIResponse
    from incident_report.ai.dense import DenseSettings
    from incident_report.domain import Origin, Role, View
    from incident_report.entry import analyse_incident
    from incident_report.facts import OverviewFacts, PartyFact
    from incident_report.hashing import sha256_hex
    from incident_report.model_calls import ModelCall
    from video_fixtures import synthetic_video

    video_bytes = synthetic_video(b"no-collision-clip")
    video_path = Path(str(tmp_path)) / "video.mp4"
    video_path.write_bytes(video_bytes)
    view_hash = sha256_hex(video_bytes)

    # Overview facts without candidate collisions
    overview_facts = OverviewFacts(
        behaviour_observations=[],
        location_conditions=[],
        parties=[
            PartyFact(description="silver Toyota sedan", view_id=view_hash, timestamp_seconds=0.5, confidence=0.9)
        ],
        events=[
            CitedFact(description="sedan drives along road", view_id=view_hash, timestamp_seconds=0.2, confidence=0.85)
        ],
        candidate_collisions=[],
    )

    class MockAI(AIConnection):
        def __init__(self) -> None:
            self.dense_called = False

        def analyse(self, request: AIRequest) -> AIResponse:
            return AIResponse(
                summary="safe driving",
                provenance="mock-overview",
                facts=overview_facts,
                calls=(
                    ModelCall(
                        request_hash="cc" * 32,
                        input_hash=request.input_hash,
                        prompt_version=request.prompt_version,
                        model="gemini-3.8-flash",
                        view_id=request.views[0].sha256,
                        fps=1.0,
                        raw_response='{"overview": true}',
                        latency_ms=40.0,
                    ),
                ),
            )

        def analyse_window(
            self, request: AIRequest, window: CandidateWindow
        ) -> tuple[OverviewFacts, tuple[ModelCall, ...]]:
            self.dense_called = True
            raise AssertionError("Dense pass must not run when there are no candidate collisions!")

    ai = MockAI()
    view = View(
        stream=io.BytesIO(video_bytes),
        filename="front.mp4",
        role=Role.EXTERIOR,
        duration_seconds=5.0,
    )

    report = analyse_incident(
        [view],
        Origin.BENCHMARK,
        ai=ai,
        run_log=run_log,  # type: ignore[arg-type]
        dense_settings=DenseSettings(model="gemini-3.8-flash", fps=20.0),
    )

    assert not ai.dense_called
    assert len(report.dense_facts) == 0
    assert report.facts == overview_facts
    assert "dense" in report.stage_latencies_ms
    assert report.stage_latencies_ms["dense"] >= 0
