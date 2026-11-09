"""overview-11 recovery: every negative or out-of-window contact gets one higher-detail request that replaces the
first answer, never merges with it, and fails visibly (spec 003 ticket 8, card SINGLE-REQUEST-MULTIRES sections 4-6)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from conftest import make_view
from incident_report.agreement import AgreementSettings
from incident_report.ai import AIRequest, AIViewRef
from incident_report.ai import overview11 as o11
from incident_report.ai.dense import DenseSettings
from incident_report.ai.gemini import (
    GeminiAIConnection,
    GeminiSettings,
    PublicResearchVideo,
    TransportError,
    overview_recovery_request_hash,
    overview_request_hash,
)
from incident_report.ai.google_video import GoogleVideoTransport
from incident_report.config import ProcessingSettings
from incident_report.domain import Origin, Role
from incident_report.entry import analyse_incident
from incident_report.facts import OverviewFacts
from incident_report.hashing import incident_input_hash, sha256_hex
from incident_report.rules.consistency import check_consistency
from incident_report.rules.models import FaultStatus
from incident_report.timings import StageTimings, capture_timings
from incident_report.web.templating import build_environment
from overview10_helpers import answer as answer10
from overview10_helpers import contact as contact10
from overview11_helpers import answer11, assessment11, contact11, negative11, row
from test_live_gemini import PUBLIC_VIDEO, FakeTransport

SETTINGS = GeminiSettings(model="gemini-3-flash-preview", fps=1.0, max_retries=0)
DURATION = 30.0


def plan_window(duration: float, centre: float | None = None) -> tuple[float, float]:
    """The detail window the plan chose (test_part_plan owns the placement policy)."""
    plan = o11.main_plan(duration, SETTINGS) if centre is None else o11.recovery_plan(duration, SETTINGS, centre)
    window = o11.window_of(plan)
    assert window is not None
    return window


WIN_START, WIN_END = plan_window(DURATION)  # a contact is "in the window" from WIN_START - 1 to WIN_END + 1 s
DIGEST = sha256_hex(PUBLIC_VIDEO)


def request_for(duration: float | None = DURATION, version: str = "overview-11") -> AIRequest:
    return AIRequest(
        input_hash=incident_input_hash([(Role.EXTERIOR, DIGEST)]),
        prompt_version=version,
        views=(AIViewRef(Role.EXTERIOR, DIGEST, duration),),
    )


def connect(tmp_path: Path, responses: list[Any], settings: GeminiSettings = SETTINGS) -> tuple[Any, FakeTransport]:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    transport = FakeTransport([r if isinstance(r, Exception) else json.dumps(r) for r in responses])
    return GeminiAIConnection(settings, PublicResearchVideo(video, True), transport=transport), transport


def sent(transport: FakeTransport, index: int) -> o11.MultiResPrompt:
    prompt = transport.calls[index][0]
    assert isinstance(prompt, o11.MultiResPrompt)
    return prompt


def in_window(at: float = 1.5, **kwargs: Any) -> dict[str, Any]:
    """A contact seen in the detail window (part time `at`, so full time WIN_START + at)."""
    return contact11("detail_window", at=at, **kwargs)


def positive() -> dict[str, Any]:
    return answer11(
        contacts=[in_window()],
        incident_assessment=assessment11(
            "detail_window", contact_interval_start_seconds=1.0, contact_interval_end_seconds=2.0
        ),
    )


def recovered_positive() -> dict[str, Any]:
    """A different answer (other colours) for the recovery request, to show the two are never merged."""
    return answer11(
        rows=[row("P1", colour_family="green"), row("P2", colour_family="blue", party_type="suv")],
        contacts=[contact11("whole_clip", at=WIN_START + 1.0)],
        incident_assessment=assessment11(
            "whole_clip", contact_interval_start_seconds=WIN_START + 0.5, contact_interval_end_seconds=WIN_START + 1.5
        ),
    )


def triggers_of(response: Any) -> list[str]:
    return list(response.facts.incident_assessments[0].recovery_triggers)


# --- the three triggers, and the one that must not fire ---------------------------------------------------------


def test_a_positive_contact_inside_the_window_runs_no_recovery(tmp_path: Path) -> None:
    connection, transport = connect(tmp_path, [positive()])
    response = connection.analyse(request_for())
    assert len(transport.calls) == 1
    (assessment,) = response.facts.incident_assessments
    assert assessment.recovery is None and assessment.recovery_triggers == []
    assert (assessment.detail_window_start_seconds, assessment.detail_window_end_seconds) == (WIN_START, WIN_END)
    assert [c.prompt_version for c in response.calls] == ["overview-11"]


def test_a_whole_clip_contact_within_a_second_of_the_window_runs_no_recovery(tmp_path: Path) -> None:
    near = answer11(
        contacts=[contact11("whole_clip", at=WIN_END + 1.0)],
        incident_assessment=assessment11(
            "whole_clip", contact_interval_start_seconds=WIN_END + 0.5, contact_interval_end_seconds=WIN_END + 1.0
        ),
    )
    connection, transport = connect(tmp_path, [near])
    connection.analyse(request_for())
    assert len(transport.calls) == 1


def test_trigger_a_a_negative_state_runs_one_recovery(tmp_path: Path) -> None:
    cannot_assess = answer11(
        contacts=[contact11("whole_clip", at=15.0)],
        incident_assessment=assessment11(
            None, contact_state="cannot_assess", contact_interval_start_seconds=None, contact_interval_end_seconds=None
        ),
    )
    connection, transport = connect(tmp_path, [cannot_assess, cannot_assess])
    response = connection.analyse(request_for())
    assert len(transport.calls) == 2
    assert triggers_of(response) == ["negative_state"]


def test_trigger_b_no_typed_contact_runs_one_recovery(tmp_path: Path) -> None:
    claims_contact = answer11(contacts=[])  # the assessment says contact_observed but lists no contact
    connection, transport = connect(tmp_path, [claims_contact, claims_contact])
    response = connection.analyse(request_for())
    assert len(transport.calls) == 2
    assert triggers_of(response) == ["no_typed_contact"]


def test_trigger_c_a_contact_outside_the_window_runs_one_recovery(tmp_path: Path) -> None:
    early = answer11(contacts=[contact11("whole_clip", at=3.0)])  # 12.5 s is the lowest edge: 3.0 is outside
    connection, transport = connect(tmp_path, [early, early])
    response = connection.analyse(request_for())
    assert len(transport.calls) == 2
    assert triggers_of(response) == ["contact_outside_detail_window"]


def test_a_plain_negative_triggers_both_a_and_b(tmp_path: Path) -> None:
    connection, transport = connect(tmp_path, [negative11(), negative11()])
    response = connection.analyse(request_for())
    assert triggers_of(response) == ["negative_state", "no_typed_contact"]


def test_a_negative_on_a_clip_no_longer_than_the_window_is_still_rechecked_at_the_cap(tmp_path: Path) -> None:
    connection, transport = connect(tmp_path, [negative11(), negative11()])
    response = connection.analyse(request_for(2.5))
    assert len(transport.calls) == 2
    (only,) = sent(transport, 1).video_parts
    assert only.fps == 24.0
    assert response.facts.incident_assessments[0].recovery == "agreed_no_contact"


def test_an_early_contact_on_a_short_clip_reaches_recovery_and_keeps_its_whole_clip_time(tmp_path: Path) -> None:
    # 8 s clip: a contact at 0.5 s is more than a second before the window, so it was seen only at 1 fps.
    early = answer11(
        contacts=[contact11("whole_clip", at=0.5)],
        incident_assessment=assessment11(
            "whole_clip", contact_interval_start_seconds=0.25, contact_interval_end_seconds=0.75
        ),
    )
    connection, transport = connect(tmp_path, [early, early])
    response = connection.analyse(request_for(8.0))
    assert len(transport.calls) == 2
    assert response.facts.incident_assessments[0].recovery == "replaced"
    assert response.facts.incident_assessments[0].recovery_triggers == ["contact_outside_detail_window"]
    recovery = sent(transport, 1)
    assert [p.fps for p in recovery.video_parts] == [15.0, 15.0]  # 120 / 8 s, then the detail window
    assert recovery.window == plan_window(8.0, 0.5)
    (contact,) = response.facts.collision_observations
    assert contact.timestamp_seconds == 0.5 and contact.part_time_seconds == 0.5 and contact.source_part == "whole_clip"


# --- the recovery request -----------------------------------------------------------------------------------------


def test_the_recovery_request_does_not_mention_the_first_answer(tmp_path: Path) -> None:
    first = negative11(
        observation_notes=[{"party_code": "P1", "timestamp_seconds": 1.0, "note": "QUOKKA drives past slowly."}],
        incident_assessment={
            **assessment11(
                None,
                contact_state="no_contact_observed",
                contact_interval_start_seconds=None,
                contact_interval_end_seconds=None,
            ),
            "assessment_note": "ZEBRA nothing touches.",
        },
        events=[{"description": "NARWHAL event", "timestamp_seconds": 2.0, "confidence": 0.9}],
        rows=[row("P1", colour_family="other", colour_other="MAGENTA-PAINT"), row("P2")],
    )
    connection, transport = connect(tmp_path, [first, negative11()])
    connection.analyse(request_for())
    main, recovery = sent(transport, 0), sent(transport, 1)
    assert str(recovery) == o11.user_prompt() + "\nview_role: exterior"
    assert recovery.system_instruction == o11.system_instruction()
    visible = " ".join([str(recovery), recovery.system_instruction, *[p.intro for p in recovery.video_parts]])
    for secret in ("QUOKKA", "ZEBRA", "NARWHAL", "MAGENTA"):
        assert secret not in visible
    assert str(main) == o11.user_prompt() + "\nview_role: exterior"


def test_the_recovery_request_samples_the_whole_clip_at_the_recovery_rate(tmp_path: Path) -> None:
    connection, transport = connect(tmp_path, [negative11(), negative11()])
    connection.analyse(request_for())
    recovery = sent(transport, 1)
    (only,) = recovery.video_parts
    assert only.fps == 4.0 and only.start_offset is None  # 120 frames / 30 s
    assert recovery.thinking_level == "low"


def test_a_late_contact_gets_a_window_centred_on_the_earliest_such_contact(tmp_path: Path) -> None:
    two = answer11(
        contacts=[contact11("whole_clip", at=25.0, subject="P2", other="P1"), contact11("whole_clip", at=3.0)],
        incident_assessment=assessment11(
            "whole_clip", contact_interval_start_seconds=2.0, contact_interval_end_seconds=4.0
        ),
    )
    connection, transport = connect(tmp_path, [two, negative11()])
    connection.analyse(request_for())
    recovery = sent(transport, 1)
    assert [p.fps for p in recovery.video_parts] == [4.0, 15.0]
    assert recovery.window == plan_window(30.0, 3.0)


def test_hashes_fps_and_labels_of_the_recorded_calls(tmp_path: Path) -> None:
    early = answer11(contacts=[contact11("whole_clip", at=3.0)])
    connection, transport = connect(tmp_path, [early, negative11()])
    request = request_for()
    response = connection.analyse(request)
    main, recovery = response.calls
    assert (main.prompt_version, recovery.prompt_version) == ("overview-11", "overview-11-recovery")
    assert main.request_hash == overview_request_hash(request, SETTINGS)
    assert recovery.request_hash == overview_recovery_request_hash(request, SETTINGS, 3.0)
    assert main.request_hash != recovery.request_hash
    assert (main.fps, recovery.fps) == (1.0, 4.0)


# --- the recovery result -------------------------------------------------------------------------------------------


def test_a_successful_recovery_replaces_the_first_answer_and_never_merges(tmp_path: Path) -> None:
    connection, transport = connect(tmp_path, [negative11(), recovered_positive()])
    response = connection.analyse(request_for())
    facts = response.facts
    (assessment,) = facts.incident_assessments
    assert assessment.recovery == "replaced"
    assert assessment.contact_state == "contact_observed"
    assert [p.description for p in facts.parties] == ["P1 green sedan", "P2 blue SUV"]
    assert len(facts.party_checklist) == 2  # not four: codes are per answer
    assert [c.prompt_version for c in response.calls] == ["overview-11", "overview-11-recovery"]
    assert all(c.raw_response for c in response.calls)  # both answers stay in the Run log


def test_a_recovery_that_agrees_there_is_no_contact_is_recorded_as_such(tmp_path: Path) -> None:
    connection, transport = connect(tmp_path, [negative11(), negative11()])
    response = connection.analyse(request_for())
    assert response.facts.incident_assessments[0].recovery == "agreed_no_contact"
    assert len(transport.calls) == 2


def test_there_is_at_most_one_recovery(tmp_path: Path) -> None:
    connection, transport = connect(tmp_path, [negative11(), negative11(), negative11()])
    connection.analyse(request_for())
    assert len(transport.calls) == 2


def test_a_recovery_that_is_again_outside_the_window_does_not_recover_again(tmp_path: Path) -> None:
    early = answer11(contacts=[contact11("whole_clip", at=3.0)])
    connection, transport = connect(tmp_path, [early, early, early])
    response = connection.analyse(request_for())
    assert len(transport.calls) == 2
    assert response.facts.incident_assessments[0].recovery == "replaced"


def test_a_recovery_answer_that_needs_repair_gets_the_one_allowed_repair(tmp_path: Path) -> None:
    bad = {**negative11(), "events": "not a list"}
    connection, transport = connect(tmp_path, [negative11(), bad, negative11()])
    response = connection.analyse(request_for())
    assert [c.prompt_version for c in response.calls] == [
        "overview-11",
        "overview-11-recovery",
        "overview-11-recovery-repair",
    ]
    assert response.facts.incident_assessments[0].recovery == "agreed_no_contact"
    assert len(transport.calls) == 3


def test_recovery_text_is_the_error_free_template_even_when_repaired(tmp_path: Path) -> None:
    bad = {**negative11(), "events": "not a list"}
    connection, transport = connect(tmp_path, [negative11(), bad, negative11()])
    connection.analyse(request_for())
    repair_text = str(sent(transport, 2))
    assert "QUOKKA" not in repair_text
    assert repair_text.startswith(o11.user_prompt())
    assert "CRITICAL" in repair_text


def test_the_main_answer_is_repaired_before_the_recovery_check(tmp_path: Path) -> None:
    bad = {**negative11(), "events": "not a list"}
    connection, transport = connect(tmp_path, [bad, negative11(), negative11()])
    response = connection.analyse(request_for())
    assert [c.prompt_version for c in response.calls] == ["overview-11", "overview-11-repair", "overview-11-recovery"]


# --- recovery failure ----------------------------------------------------------------------------------------------


def failed_recovery(tmp_path: Path, first: dict[str, Any]) -> tuple[Any, FakeTransport]:
    connection, transport = connect(tmp_path, [first, TransportError(500)])
    return connection.analyse(request_for()), transport


def test_a_failed_recovery_keeps_the_first_answer_and_says_so(tmp_path: Path) -> None:
    response, transport = failed_recovery(tmp_path, negative11())
    (assessment,) = response.facts.incident_assessments
    assert assessment.recovery == "failed"
    assert assessment.contact_state == "no_contact_observed"
    assert [c.prompt_version for c in response.calls] == ["overview-11", "overview-11-recovery"]
    assert response.calls[1].error == "Gemini request failed."
    assert len(transport.calls) == 2


def test_an_unparseable_recovery_fails_after_its_one_repair(tmp_path: Path) -> None:
    bad = {**negative11(), "events": "not a list"}
    connection, transport = connect(tmp_path, [negative11(), bad, bad])
    response = connection.analyse(request_for())
    assert response.facts.incident_assessments[0].recovery == "failed"
    assert len(transport.calls) == 3


def test_a_failed_negative_recovery_is_a_scene_consistency_issue(tmp_path: Path) -> None:
    response, _ = failed_recovery(tmp_path, negative11())
    issues = [i for i in check_consistency(response.facts, {DIGEST: DURATION}) if i.code == "recovery_failed"]
    assert [i.subject for i in issues] == ["scene"]
    assert "second check" in issues[0].reason


def test_a_failed_recovery_for_a_late_contact_marks_only_those_parties(tmp_path: Path) -> None:
    early = answer11(
        rows=[row("P1"), row("P2", colour_family="red", party_type="suv"), row("P3", colour_family="blue")],
        contacts=[contact11("whole_clip", at=3.0)],
        incident_assessment=assessment11(
            "whole_clip", contact_interval_start_seconds=2.0, contact_interval_end_seconds=4.0
        ),
    )
    response, _ = failed_recovery(tmp_path, early)
    issues = [i for i in check_consistency(response.facts, {DIGEST: DURATION}) if i.code == "recovery_failed"]
    assert sorted(i.subject for i in issues) == ["P1 silver sedan", "P2 red SUV"]
    assert all("contact seen only at low frame rate; the detail check failed" in i.reason for i in issues)


def test_a_successful_recovery_adds_no_recovery_failed_issue(tmp_path: Path) -> None:
    connection, _ = connect(tmp_path, [negative11(), recovered_positive()])
    facts = connection.analyse(request_for()).facts
    assert "recovery_failed" not in {i.code for i in check_consistency(facts, {DIGEST: DURATION})}


# --- the first answer, as a Report ---------------------------------------------------------------------------------


def six_second_video(seconds: int = 6) -> bytes:
    done = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=32x32:d={seconds}:r=2",
            "-c:v",
            "mpeg4",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-movflags",
            "frag_keyframe+empty_moov",
            "-f",
            "mp4",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    )  # noqa: S607
    return done.stdout


class SlowTransport(FakeTransport):
    def generate(self, video: PublicResearchVideo, prompt: str, settings: GeminiSettings) -> str:
        time.sleep(0.05)
        return super().generate(video, prompt, settings)


class WithDense:
    """A connection with a close-up pass, so the entry point would run dense-2 if it were allowed to."""

    def __init__(self, inner: GeminiAIConnection) -> None:
        self.inner = inner
        self.transport = inner.transport
        self.windows = 0

    def analyse(self, request: AIRequest) -> Any:
        return self.inner.analyse(request)

    def repair(self, request: AIRequest, response: Any, errors: Any) -> Any:
        return self.inner.repair(request, response, errors)

    def analyse_window(self, request: AIRequest, window: Any) -> Any:
        self.windows += 1
        return OverviewFacts(
            behaviour_observations=[], location_conditions=[], parties=[], events=[], candidate_collisions=[]
        ), ()


def run_report(
    run_log: Any,
    tmp_path: Path,
    responses: list[Any],
    version: str = "overview-11",
    transport_cls: Any = FakeTransport,
    seconds: int = 6,
) -> tuple[Any, Any]:
    clip = six_second_video(seconds)
    path = tmp_path / "clip.mp4"
    path.write_bytes(clip)
    transport = transport_cls([r if isinstance(r, Exception) else json.dumps(r) for r in responses])
    connection = WithDense(GeminiAIConnection(SETTINGS, PublicResearchVideo(path, True), transport=transport))
    report = analyse_incident(
        [make_view(clip)],
        Origin.BENCHMARK,
        ai=connection,
        run_log=run_log,
        prompt_version=version,
        dense_settings=DenseSettings(model="gemini-3.8-flash"),
        agreement_settings=AgreementSettings(n_runs=1),
        processing_settings=ProcessingSettings(concurrent_analysis=False),
    )
    return report, connection


# six-second clip: its detail window covers most of it; the late-contact test uses a longer clip.
def short_clip_negative() -> dict[str, Any]:
    return negative11(events=[])


def short_clip_positive() -> dict[str, Any]:
    return answer11(
        contacts=[in_short(1.0)],
        incident_assessment=assessment11(
            "detail_window", contact_interval_start_seconds=0.5, contact_interval_end_seconds=1.5
        ),
        events=[{"description": "P1 hits P2", "timestamp_seconds": 2.5, "confidence": 0.9}],
    )


def in_short(at: float) -> dict[str, Any]:
    return contact11("detail_window", at=at)


def test_a_failed_negative_recovery_is_cannot_determine_not_no_collision(run_log: Any, tmp_path: Path) -> None:
    report, _ = run_report(run_log, tmp_path, [short_clip_negative(), TransportError(500)])
    assert report.is_valid, report.validation_errors
    assert report.incident_status == "Cannot Determine"
    assert {i.code for i in report.consistency_issues} >= {"recovery_failed"}
    assert all(p.status == FaultStatus.CANNOT_DETERMINE for p in report.parties)


def test_a_negative_that_the_recovery_confirms_is_still_not_a_collision(run_log: Any, tmp_path: Path) -> None:
    report, _ = run_report(run_log, tmp_path, [short_clip_negative(), short_clip_negative()])
    assert report.incident_status != "Collision"
    assert "recovery_failed" not in {i.code for i in report.consistency_issues}
    assert report.facts is not None and report.facts.incident_assessments[0].recovery == "agreed_no_contact"


LATE = plan_window(12.0)[1] + 1.5  # more than a second after the window on a 12 s clip


def test_a_failed_recovery_for_a_late_contact_makes_those_parties_cannot_determine(
    run_log: Any, tmp_path: Path
) -> None:
    late = answer11(
        contacts=[contact11("whole_clip", at=LATE)],
        incident_assessment=assessment11(
            "whole_clip", contact_interval_start_seconds=LATE - 0.2, contact_interval_end_seconds=LATE + 0.1
        ),
        events=[{"description": "P1 hits P2", "timestamp_seconds": LATE, "confidence": 0.9}],
        observation_notes=[],
    )
    report, _ = run_report(run_log, tmp_path, [late, TransportError(500)], seconds=12)
    assert report.incident_status == "Collision"
    assert {p.status for p in report.parties} == {FaultStatus.CANNOT_DETERMINE}
    assert any("detail check failed" in p.reason for p in report.parties)


# --- no dense-2, timings, latency -----------------------------------------------------------------------------


def test_overview_11_skips_the_close_up_pass_and_records_it_as_skipped(run_log: Any, tmp_path: Path) -> None:
    report, connection = run_report(run_log, tmp_path, [short_clip_positive()])
    assert connection.windows == 0
    assert report.incident_status == "Collision"
    assert "dense" not in report.stage_latencies_ms
    assert report.stage_latencies_ms["dense_skipped"] == 1.0


def test_overview_10_still_runs_the_close_up_pass(run_log: Any, tmp_path: Path) -> None:
    ten = answer10(
        contacts=[contact10(at=1.0)],
        incident_assessment={
            "assessment_note": "x",
            "camera_role": "fixed_camera",
            "contact_state": "contact_observed",
            "contact_interval_start_seconds": 0.5,
            "contact_interval_end_seconds": 1.5,
        },
        events=[{"description": "P1 hits P2", "timestamp_seconds": 1.0, "confidence": 0.9}],
    )
    report, connection = run_report(run_log, tmp_path, [ten], version="overview-10")
    assert connection.windows >= 1
    assert "dense" in report.stage_latencies_ms and "dense_skipped" not in report.stage_latencies_ms


def test_run_latency_spans_the_recovery_and_the_stage_names_are_present(run_log: Any, tmp_path: Path) -> None:
    report, _ = run_report(
        run_log, tmp_path, [short_clip_negative(), short_clip_negative()], transport_cls=SlowTransport
    )
    stored = run_log.get(report.run_id)
    assert stored.total_latency_ms >= 100  # two requests of 0.05 s each, recovery included
    timings = stored.stage_timings_ms
    assert "overview.run.0.total" in timings and timings["overview.run.0.total"] >= 100
    for name in ("upload", "processing_wait", "generate", "repair", "recovery.generate", "recovery.repair"):
        assert f"overview.run.0.{name}" in timings


class ScriptedClient:
    """A fake SDK client that answers generate_content from a script, as the real transport sees it."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.files = self
        self.models = self
        self.deleted: list[str] = []

    def upload(self, *, file: Any, config: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(name="files/x", uri="https://example.invalid/x", state=SimpleNamespace(name="ACTIVE"))

    def generate_content(self, **kwargs: Any) -> SimpleNamespace:
        time.sleep(0.01)
        text = next(self.responses)
        return SimpleNamespace(
            model_dump_json=lambda **_: json.dumps(
                {"candidates": [{"finish_reason": "STOP", "content": {"parts": [{"text": text}]}}]}
            )
        )

    def delete(self, *, name: str) -> None:
        self.deleted.append(name)


def test_stage_timings_keep_the_main_generate_apart_from_the_recovery_generate(tmp_path: Path) -> None:
    video = tmp_path / "public.mp4"
    video.write_bytes(PUBLIC_VIDEO)
    bad = json.dumps({**negative11(), "events": "not a list"})
    client = ScriptedClient([bad, json.dumps(negative11()), bad, json.dumps(negative11())])
    transport = GoogleVideoTransport(client, inline_enabled=False)
    connection = GeminiAIConnection(SETTINGS, PublicResearchVideo(video, True), transport=transport)
    timings = StageTimings()
    with capture_timings(timings), transport.report_scope():
        connection.analyse(request_for())
    seen = timings.snapshot()
    for name in ("upload", "processing_wait", "generate", "repair", "recovery.generate", "recovery.repair"):
        assert name in seen, name
    assert 9 <= seen["generate"] < 40 and 9 <= seen["recovery.generate"] < 40
    assert client.deleted == ["files/x"]  # one upload served the main request and the recovery


# --- the officer Report ------------------------------------------------------------------------------------------


def render(facts: OverviewFacts) -> str:
    from test_invalid_report_rendering import make_report

    report = make_report(is_valid=True, summary="x").model_copy(update={"facts": facts})
    return build_environment().get_template("report.html").render(report=report, review_workspace=False)


def test_the_report_says_checked_twice_only_when_the_recovery_agreed_there_was_no_contact(tmp_path: Path) -> None:
    connection, _ = connect(tmp_path, [negative11(), negative11()])
    agreed = connection.analyse(request_for()).facts
    assert "checked twice (whole clip at higher detail)" in render(agreed)
    connection, _ = connect(tmp_path, [negative11(), recovered_positive()])
    replaced = connection.analyse(request_for()).facts
    assert "checked twice" not in render(replaced)
    connection, _ = connect(tmp_path, [positive()])
    single = connection.analyse(request_for()).facts
    assert "checked twice" not in render(single)


def test_the_checked_twice_line_sits_with_escaped_model_text(tmp_path: Path) -> None:
    hostile = negative11(
        incident_assessment={
            **assessment11(
                None,
                contact_state="no_contact_observed",
                contact_interval_start_seconds=None,
                contact_interval_end_seconds=None,
            ),
            "assessment_note": "<script>alert(1)</script>",
        }
    )
    connection, _ = connect(tmp_path, [hostile, hostile])
    html = render(connection.analyse(request_for()).facts)
    assert "checked twice (whole clip at higher detail)" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
