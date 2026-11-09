"""Seam 2 overview tests: synthetic responses, never a live model call."""

from pathlib import Path

import pytest

from conftest import make_view
from incident_report.ai import AIConnectionError, AIRequest, AIResponse, AIViewRef
from incident_report.ai.gemini import GeminiAIConnection, GeminiSettings, PublicResearchVideo, TransportError
from incident_report.ai.overview_replay import ReplayOverviewAIConnection
from incident_report.domain import Origin, Role, RunStatus
from incident_report.entry import analyse_incident
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.runlog import RunLog
from video_fixtures import synthetic_video

# RPT-06 intake accepts only real video containers, so the public-video stand-in is a valid synthetic MP4.
PUBLIC_VIDEO = synthetic_video(b"public synthetic video")


def request() -> AIRequest:
    digest = sha256_hex(PUBLIC_VIDEO)
    return AIRequest(incident_input_hash([(Role.EXTERIOR, digest)]), "overview-1", (AIViewRef(Role.EXTERIOR, digest),))


@pytest.mark.parametrize("model", ["", "gemini-flash", "gemini-flash-latest", "gemini-3.8-flash-latest"])
def test_live_connection_requires_a_versioned_model_setting(model: str) -> None:
    with pytest.raises(AIConnectionError, match="GEMINI_MODEL"):
        GeminiSettings.from_env({"GEMINI_MODEL": model})


def test_private_footage_is_refused_before_transport_or_file_access(tmp_path: Path) -> None:
    with pytest.raises(AIConnectionError, match="public research"):
        PublicResearchVideo(tmp_path / "private.mp4", public_research=False)


class FakeTransport:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, float]] = []

    def generate(self, video: PublicResearchVideo, prompt: str, settings: GeminiSettings) -> str:
        self.calls.append((prompt, settings.fps))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


FACTS = '{"parties":[{"description":"silver Toyota sedan","timestamp_seconds":0.25,"confidence":0.9}],"events":[{"description":"silver Toyota sedan brakes","timestamp_seconds":0.5,"confidence":0.9}],"candidate_collisions":[{"description":"possible contact involving silver Toyota sedan","timestamp_seconds":0.75,"confidence":0.9}]}'


def test_native_overview_returns_cited_facts_and_replayable_call(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    req = request()
    raw = FACTS.replace("VIEW", req.views[0].sha256)
    transport = FakeTransport([raw])
    connection = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(video, True), transport=transport
    )

    response = connection.analyse(req)

    assert response.facts is not None
    assert response.facts.parties[0].description == "silver Toyota sedan"
    assert response.facts.events[0].timestamp_seconds == 0.5
    assert response.facts.candidate_collisions[0].view_id == req.views[0].sha256  # attached by the code
    assert response.calls[0].raw_response == raw
    assert response.calls[0].prompt_version == "overview-1"
    assert response.calls[0].latency_ms >= 0
    assert len(response.calls[0].request_hash) == 64
    assert transport.calls[0][1] == 1.0


def test_quota_retries_are_bounded_and_each_attempt_is_recorded(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    req = request()
    raw = FACTS.replace("VIEW", req.views[0].sha256)
    transport = FakeTransport([TransportError(429), TransportError(429), raw])
    sleeps: list[float] = []
    connection = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"),
        PublicResearchVideo(video, True),
        transport=transport,
        sleep=sleeps.append,
    )

    response = connection.analyse(req)

    assert len(response.calls) == 3
    assert [call.retry_index for call in response.calls] == [0, 1, 2]
    assert [call.status_code for call in response.calls] == [429, 429, None]
    assert sleeps == [1.0, 2.0]
    assert response.calls[-1].raw_response == raw


def test_exhausted_quota_and_invalid_facts_preserve_call_records(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    cases: list[list[str | Exception]] = [[TransportError(429)] * 3, ['{"events":[]}']]
    for responses in cases:
        connection = GeminiAIConnection(
            GeminiSettings(model="gemini-3.8-flash"),
            PublicResearchVideo(video, True),
            transport=FakeTransport(responses),
            sleep=lambda _: None,
        )
        with pytest.raises(AIConnectionError) as failure:
            connection.analyse(request())
        assert len(failure.value.calls) == len(responses)
        assert failure.value.calls[-1].raw_response == (responses[-1] if isinstance(responses[-1], str) else None)


def test_live_calls_and_retry_counts_survive_the_run_log_on_success_and_failure(
    tmp_path: Path, run_log: RunLog
) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    raw = FACTS.replace("VIEW", request().views[0].sha256)
    for output in (raw, '{"events":[]}'):
        connection = GeminiAIConnection(
            GeminiSettings(model="gemini-3.8-flash"),
            PublicResearchVideo(video, True),
            transport=FakeTransport([TransportError(429), output]),
            sleep=lambda _: None,
        )
        if output == raw:
            report = analyse_incident(
                [make_view(PUBLIC_VIDEO)],
                Origin.BENCHMARK,
                ai=connection,
                run_log=run_log,
                prompt_version="overview-1",
            )
            assert report.facts is not None
        else:
            with pytest.raises(AIConnectionError):
                analyse_incident(
                    [make_view(PUBLIC_VIDEO)],
                    Origin.BENCHMARK,
                    ai=connection,
                    run_log=run_log,
                    prompt_version="overview-1",
                )
    runs = run_log.list_runs()
    assert [r.status for r in runs] == [RunStatus.SUCCEEDED, RunStatus.FAILED]
    assert [r.retry_count for r in runs] == [1, 1]
    assert runs[0].calls[-1].raw_response == raw
    assert runs[1].calls[-1].raw_response == '{"events":[]}'
    assert runs[1].report is None


def test_overview_replays_without_video_or_transport_and_refuses_config_changes(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    settings = GeminiSettings(model="gemini-3.8-flash")
    req = request()
    raw = FACTS.replace("VIEW", req.views[0].sha256)
    live = GeminiAIConnection(settings, PublicResearchVideo(video, True), transport=FakeTransport([raw])).analyse(req)
    replay = ReplayOverviewAIConnection(settings, live.calls)
    assert replay.analyse(req).facts == live.facts
    assert replay.analyse(req).provenance == "recorded-gemini-api"
    assert replay.analyse(req).calls == ()
    with pytest.raises(AIConnectionError, match="recording"):
        ReplayOverviewAIConnection(GeminiSettings(model="gemini-3.8-flash", fps=0.5), live.calls).analyse(req)


@pytest.mark.parametrize("change", ["missing_timestamp", "negative_timestamp", "wrong_view", "nonfinite_timestamp"])
def test_bad_citations_are_refused_with_raw_output_preserved(tmp_path: Path, change: str) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    raw = FACTS.replace("VIEW", request().views[0].sha256)
    if change == "missing_timestamp":
        raw = raw.replace(',"timestamp_seconds":0.25', "")
    elif change == "negative_timestamp":
        raw = raw.replace('"timestamp_seconds":0.25', '"timestamp_seconds":-1.0')
    elif change == "wrong_view":
        # A model that still volunteers a view reference must name the view that was sent.
        raw = raw.replace('"timestamp_seconds":0.25', '"view_id":"another-view","timestamp_seconds":0.25', 1)
    else:
        raw = raw.replace('"timestamp_seconds":0.25', '"timestamp_seconds":NaN')
    with pytest.raises(AIConnectionError) as failure:
        GeminiAIConnection(
            GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(video, True), transport=FakeTransport([raw])
        ).analyse(request())
    assert failure.value.calls[0].raw_response == raw


def test_smoke_command_requires_public_declaration_before_sdk_creation(capsys: pytest.CaptureFixture[str]) -> None:
    from incident_report.ai.smoke import main

    assert main(["private.mp4", "--role", "exterior"]) == 1
    assert "public research" in capsys.readouterr().err


def test_live_repair_reprompts_with_errors_and_records_repair_call(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    req = request()
    raw = FACTS.replace("VIEW", req.views[0].sha256)
    repair_raw = raw.replace("silver Toyota sedan", "white Nissan patrol SUV")
    transport = FakeTransport([raw, repair_raw])
    connection = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(video, True), transport=transport
    )
    initial_response = connection.analyse(req)
    repaired_response = connection.repair(req, initial_response, ["event[0] out of range"])
    assert len(repaired_response.calls) == 2
    assert repaired_response.calls[-1].prompt_version == "overview-1-repair"
    assert repaired_response.facts is not None
    assert repaired_response.facts.parties[0].description == "white Nissan patrol SUV"


def test_a_fact_without_confidence_is_refused_not_treated_as_fully_confident(tmp_path: Path) -> None:
    """ADR 0006: missing confidence is a validation failure (no report), never 1.0."""
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    raw = FACTS.replace("VIEW", request().views[0].sha256).replace(',"confidence":0.9', "", 1)
    assert raw.count("confidence") == 2
    with pytest.raises(AIConnectionError, match="Cannot Determine") as failure:
        GeminiAIConnection(
            GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(video, True), transport=FakeTransport([raw])
        ).analyse(request())
    assert failure.value.calls[0].raw_response == raw


def test_503_unavailable_is_retried_like_429_with_backoff_and_each_attempt_recorded(tmp_path: Path) -> None:
    """RPT-07 review follow-up 1: Google's guidance for 503 UNAVAILABLE is retry with backoff."""
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    req = request()
    raw = FACTS.replace("VIEW", req.views[0].sha256)
    sleeps: list[float] = []
    connection = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"),
        PublicResearchVideo(video, True),
        transport=FakeTransport([TransportError(503), TransportError(429), TransportError(503), raw]),
        sleep=sleeps.append,
    )
    with pytest.raises(AIConnectionError) as exhausted:
        connection.analyse(req)
    # max_retries=2 bounds the loop at 3 attempts even when every answer is a retryable status.
    assert [c.status_code for c in exhausted.value.calls] == [503, 429, 503]
    assert [c.retry_index for c in exhausted.value.calls] == [0, 1, 2]
    assert sleeps == [1.0, 2.0]

    sleeps.clear()
    connection = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"),
        PublicResearchVideo(video, True),
        transport=FakeTransport([TransportError(503), raw]),
        sleep=sleeps.append,
    )
    response = connection.analyse(req)
    assert [c.status_code for c in response.calls] == [503, None]
    assert [c.retry_index for c in response.calls] == [0, 1]
    assert response.calls[-1].raw_response == raw
    assert sleeps == [1.0]
    assert response.facts is not None


def test_503_retry_count_survives_the_run_log(tmp_path: Path, run_log: RunLog) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    raw = FACTS.replace("VIEW", request().views[0].sha256)
    connection = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"),
        PublicResearchVideo(video, True),
        transport=FakeTransport([TransportError(503), raw]),
        sleep=lambda _: None,
    )
    report = analyse_incident(
        [make_view(PUBLIC_VIDEO)], Origin.BENCHMARK, ai=connection, run_log=run_log, prompt_version="overview-1"
    )
    run = run_log.get(report.run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert run.retry_count == 1
    assert [c.status_code for c in run.calls] == [503, None]


def test_other_provider_failures_are_not_retried(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    for code in (400, 403, 500, None):
        transport = FakeTransport([TransportError(code), TransportError(code)])
        connection = GeminiAIConnection(
            GeminiSettings(model="gemini-3.8-flash"),
            PublicResearchVideo(video, True),
            transport=transport,
            sleep=lambda _: None,
        )
        with pytest.raises(AIConnectionError) as failure:
            connection.analyse(request())
        assert len(failure.value.calls) == 1, code


def test_the_code_attaches_the_view_id_the_model_is_never_asked_for_it(tmp_path: Path) -> None:
    """RPT-07 review follow-up 2: the model returns timestamps and descriptions only."""
    from incident_report.ai.gemini import PROMPT

    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    req = request()
    digest = req.views[0].sha256
    raw = (
        '{"parties":[{"description":"silver Toyota sedan","timestamp_seconds":0.25,"confidence":0.9}],'
        '"events":[],"candidate_collisions":[],'
        '"behaviour_observations":[{"description":"sedan brakes","timestamp_seconds":0.5,"confidence":0.8}],'
        '"location_conditions":[{"description":"dry road","timestamp_seconds":0.0,"confidence":0.7}]}'
    )
    assert "view_id" not in raw
    transport = FakeTransport([raw, raw])
    connection = GeminiAIConnection(
        GeminiSettings(model="gemini-3.8-flash"), PublicResearchVideo(video, True), transport=transport
    )
    response = connection.analyse(req)
    assert response.facts is not None
    facts = response.facts
    cited = [*facts.parties, *facts.behaviour_observations, *facts.location_conditions]
    assert len(cited) == 3
    assert {f.view_id for f in cited} == {digest}

    repaired = connection.repair(req, response, ["event[0] out of range"])
    assert repaired.facts is not None
    assert {f.view_id for f in repaired.facts.parties} == {digest}

    # Neither prompt (first call or repair) asks for, or exposes, a view id.
    assert "view_id" not in PROMPT
    for prompt, _fps in transport.calls:
        assert "view_id" not in prompt
        assert digest not in prompt


def test_a_stray_view_reference_must_match_the_view_sent_and_is_never_trusted_otherwise(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    req = request()
    base = (
        '{"parties":[{"description":"silver Toyota sedan",VIEWREF"timestamp_seconds":0.25,"confidence":0.9}],'
        '"events":[],"candidate_collisions":[]}'
    )

    def run(ref: str) -> AIResponse:
        connection = GeminiAIConnection(
            GeminiSettings(model="gemini-3.8-flash"),
            PublicResearchVideo(video, True),
            transport=FakeTransport([base.replace("VIEWREF", ref)]),
        )
        return connection.analyse(req)

    ok = run(f'"view_id":"{req.views[0].sha256}",')
    assert ok.facts is not None
    assert ok.facts.parties[0].view_id == req.views[0].sha256
    # The observed failure: the model put a timestamp in view_id. That is refused, not copied through.
    for bad in ('"view_id":"00:02",', '"view_id":"",', '"view_id":7,'):
        with pytest.raises(AIConnectionError, match="Cannot Determine"):
            run(bad)


def test_504_deadline_advises_increasing_timeout_without_retrying(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    transport = FakeTransport([TransportError(504), FACTS])
    sleeps: list[float] = []
    settings = GeminiSettings(model="gemini-3.8-flash")
    connection = GeminiAIConnection(
        settings, PublicResearchVideo(video, True), transport=transport, sleep=sleeps.append
    )

    with pytest.raises(AIConnectionError, match="GEMINI_HTTP_TIMEOUT_MS") as failure:
        connection.analyse(request())

    assert "increase" in str(failure.value).lower()
    assert "504" in str(failure.value)
    assert "No report was produced" in str(failure.value)
    assert len(failure.value.calls) == 1
    assert failure.value.calls[0].status_code == 504
    assert failure.value.calls[0].retry_index == 0
    assert failure.value.calls[0].raw_response is None
    assert len(transport.calls) == 1
    assert sleeps == []
    assert settings.http_timeout_ms == 60000
