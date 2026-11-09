"""RPT-06 through seam 1, persisted metadata and the HTTP boundary."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from conftest import make_view
from incident_report.ai import ReplayAIConnection
from incident_report.domain import Attribution, InvalidIncident, Origin
from incident_report.entry import analyse_incident
from incident_report.runlog import RunLog
from incident_report.storage import LocalEvidenceStore

SESSION = uuid.UUID("11111111-1111-4111-8111-111111111111")
LINK = uuid.UUID("22222222-2222-4222-8222-222222222222")


def test_non_video_is_rejected_even_with_video_filename_and_before_ai(
    tmp_path: Path, recordings_dir: Path, run_log: RunLog
) -> None:
    with pytest.raises(InvalidIncident, match="supported video"):
        analyse_incident(
            [make_view(b"not video", filename="camera.mp4")],
            Origin.APP,
            ai=ReplayAIConnection(recordings_dir),
            run_log=run_log,
            evidence=LocalEvidenceStore(tmp_path / "evidence"),
        )
    assert run_log.list_runs() == []


def test_full_run_keeps_original_and_persists_intake_after_restart(
    tmp_path: Path, recordings_dir: Path, run_log: RunLog
) -> None:
    import hashlib
    import json
    from datetime import UTC, datetime

    from conftest import write_recording
    from incident_report.domain import Role
    from incident_report.entry import PROMPT_VERSION
    from incident_report.hashing import hash_stream, incident_input_hash
    from video_fixtures import synthetic_video

    content = synthetic_video(b"intake-full-run")
    key = hashlib.sha256(content).hexdigest()
    write_recording(recordings_dir, incident_input_hash([(Role.EXTERIOR, key)]), PROMPT_VERSION, "Recorded.")
    store = LocalEvidenceStore(tmp_path / "objects")
    before = datetime.now(UTC)
    report = analyse_incident(
        [make_view(content, filename="<b>camera.bin", label="<script>front</script>", acquisition_method="DVR export")],
        Origin.APP,
        ai=ReplayAIConnection(recordings_dir),
        run_log=run_log,
        evidence=store,
        attribution=Attribution(session_id=SESSION, link_id=LINK),
    )
    after = datetime.now(UTC)
    view = report.views[0]
    assert view.sha256 == key
    assert view.size_bytes == len(content)
    assert view.mime_type == "video/mp4"
    assert view.uploaded_at is not None
    assert before <= view.uploaded_at <= after
    assert view.session_id == str(SESSION)
    assert view.working_copy is not None
    assert view.working_copy.parent_sha256 == key
    assert view.working_copy.sha256 == key
    assert view.ffprobe_stdout is not None
    assert json.loads(view.ffprobe_stdout)["streams"][0]["codec_type"] == "video"
    assert view.ffprobe_stderr == ""
    assert run_log.intake_views(report.run_id) == report.views
    with LocalEvidenceStore(tmp_path / "objects").get(key) as original:
        assert hash_stream(original) == (key, len(content))


@pytest.mark.parametrize(
    "setting,value,message",
    [
        ("max_upload_bytes", 100, "size limit"),
        ("max_duration_seconds", 0.5, "duration limit"),
        ("hash_timeout_seconds", 0.000000001, "timed out"),
        ("store_timeout_seconds", 0.000000001, "timed out"),
        ("copy_timeout_seconds", 0.000000001, "timed out"),
        ("probe_timeout_seconds", 0.000000001, "ffprobe timeout"),
    ],
)
def test_configured_caps_and_stage_deadlines_reject_without_ai(
    setting: str,
    value: float,
    message: str,
    tmp_path: Path,
    recordings_dir: Path,
    run_log: RunLog,
) -> None:
    from dataclasses import replace

    from incident_report.intake_limits import IntakeLimits
    from video_fixtures import synthetic_video

    limits = replace(IntakeLimits(), **{setting: value})  # type: ignore[arg-type]  # parameter selects typed field
    with pytest.raises(InvalidIncident, match=message):
        analyse_incident(
            [make_view(synthetic_video(b"caps"))],
            Origin.APP,
            ai=ReplayAIConnection(recordings_dir),
            run_log=run_log,
            evidence=LocalEvidenceStore(tmp_path / "limited"),
            limits=limits,
        )
    assert run_log.list_runs() == []


def test_http_rejects_spoofed_type_and_shows_valid_evidence_in_header(
    tmp_path: Path,
    recordings_dir: Path,
    run_log: RunLog,
) -> None:
    from access_client import authorized_client
    from conftest import write_recording
    from incident_report.domain import Role
    from incident_report.entry import PROMPT_VERSION
    from incident_report.hashing import incident_input_hash, sha256_hex
    from incident_report.web import create_app
    from video_fixtures import synthetic_video

    store = LocalEvidenceStore(tmp_path / "http")
    client = authorized_client(
        create_app(ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=store), run_log.database_url
    )
    bad = client.post("/reports", data={"role": "exterior"}, files={"video": ("real.mp4", b"not video", "video/mp4")})
    assert bad.status_code == 422
    assert "supported video" in bad.text
    content = synthetic_video(b"header")
    key = sha256_hex(content)
    write_recording(recordings_dir, incident_input_hash([(Role.EXTERIOR, key)]), PROMPT_VERSION, "A summary.")
    response = client.post(
        "/reports",
        data={"role": "exterior", "label": "<b>front", "acquisition_method": "<script>DVR"},
        files={"video": ("<b>camera.txt", content, "text/plain")},
    )
    assert response.status_code == 200
    header = response.text.split("<h2>Summary</h2>")[0]
    assert key in header and "&lt;script&gt;DVR" in header
    assert "&lt;b&gt;camera.txt" in header and "&lt;b&gt;front" in header
    assert "<script>DVR" not in header
    (run,) = run_log.list_runs()
    assert run.attribution is not None
    assert run_log.intake_views(run.run_id)[0].session_id == str(run.attribution.session_id)


def test_corrupt_video_rejection_retains_probe_output_but_no_original(
    tmp_path: Path,
    recordings_dir: Path,
    run_log: RunLog,
) -> None:
    from incident_report.hashing import sha256_hex

    content = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp41"
    store = LocalEvidenceStore(tmp_path / "corrupt")
    with pytest.raises(InvalidIncident, match="corrupt") as rejected:
        analyse_incident(
            [make_view(content)],
            Origin.APP,
            ai=ReplayAIConnection(recordings_dir),
            run_log=run_log,
            evidence=store,
            attribution=Attribution(session_id=SESSION, link_id=LINK),
        )
    assert rejected.value.intake_id is not None
    (metadata,) = run_log.intake_views(rejected.value.intake_id)
    assert metadata.sha256 == sha256_hex(content)
    assert metadata.session_id == str(SESSION)
    assert metadata.ffprobe_stdout is not None
    assert metadata.ffprobe_stderr is not None and "moov atom not found" in metadata.ffprobe_stderr
    assert metadata.validation_error is not None
    assert not store.exists(metadata.sha256)


def test_a_blocked_input_read_times_out_without_waiting_for_it(
    tmp_path: Path,
    recordings_dir: Path,
    run_log: RunLog,
) -> None:
    import io
    import threading
    import time
    from dataclasses import replace

    from incident_report.domain import Role, View
    from incident_report.intake_limits import IntakeLimits

    release = threading.Event()

    class Blocked(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            release.wait(5)
            return super().read(size)

    started = time.monotonic()
    try:
        with pytest.raises(InvalidIncident, match="timed out"):
            analyse_incident(
                [View(Blocked(b"late"), "late.mp4", Role.EXTERIOR)],
                Origin.APP,
                ai=ReplayAIConnection(recordings_dir),
                run_log=run_log,
                evidence=LocalEvidenceStore(tmp_path / "blocked"),
                limits=replace(IntakeLimits(), hash_timeout_seconds=0.05),
            )
        assert time.monotonic() - started < 2
    finally:
        release.set()


def test_non_fragmented_mp4_is_valid_and_ffprobe_output_is_kept_verbatim(
    tmp_path: Path,
    recordings_dir: Path,
    run_log: RunLog,
) -> None:
    import subprocess

    from conftest import write_recording
    from incident_report.domain import Role
    from incident_report.entry import PROMPT_VERSION
    from incident_report.hashing import incident_input_hash, sha256_hex

    path = tmp_path / "regular.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=1",
            "-frames:v",
            "1",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
    )
    content = path.read_bytes()
    write_recording(
        recordings_dir, incident_input_hash([(Role.EXTERIOR, sha256_hex(content))]), PROMPT_VERSION, "Regular MP4."
    )
    report = analyse_incident(
        [make_view(content)],
        Origin.APP,
        ai=ReplayAIConnection(recordings_dir),
        run_log=run_log,
        evidence=LocalEvidenceStore(tmp_path / "regular-store"),
    )
    with path.open("rb") as source:
        expected = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "fd",
                "-format_whitelist",
                "mov,matroska,webm,avi",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                "-i",
                "fd:",
            ],
            stdin=source,
            capture_output=True,
            check=True,
        )
    assert report.views[0].ffprobe_stdout == expected.stdout.decode("utf-8")
    assert report.views[0].ffprobe_stderr == expected.stderr.decode("utf-8")


def test_streams_are_read_only_in_bounded_chunks_and_failed_ai_keeps_metadata(
    tmp_path: Path,
    recordings_dir: Path,
    run_log: RunLog,
) -> None:
    import io

    from incident_report.ai import RecordingNotFound
    from incident_report.domain import Role, View
    from incident_report.hashing import sha256_hex
    from video_fixtures import synthetic_video

    content = synthetic_video(b"bounded" * 400000)

    class Bounded(io.BytesIO):
        largest_read = 0

        def read(self, size: int | None = -1) -> bytes:
            assert size is not None and 0 < size < len(content)
            self.largest_read = max(self.largest_read, size)
            return super().read(size)

    source = Bounded(content)
    store = LocalEvidenceStore(tmp_path / "bounded")
    with pytest.raises(RecordingNotFound):
        analyse_incident(
            [View(source, "camera.mp4", Role.EXTERIOR)],
            Origin.APP,
            ai=ReplayAIConnection(recordings_dir),
            run_log=run_log,
            evidence=store,
        )
    (run,) = run_log.list_runs()
    (metadata,) = run_log.intake_views(run.run_id)
    assert source.largest_read <= 1024 * 1024
    assert metadata.sha256 == sha256_hex(content)
    assert metadata.working_copy is not None
    assert store.exists(metadata.sha256)
    assert metadata.validation_error is None


def test_partial_incident_retains_metadata_for_each_received_view(
    tmp_path: Path,
    recordings_dir: Path,
    run_log: RunLog,
) -> None:
    from incident_report.hashing import sha256_hex
    from video_fixtures import synthetic_video

    first = synthetic_video(b"first accepted")
    store = LocalEvidenceStore(tmp_path / "partial")
    with pytest.raises(InvalidIncident) as rejected:
        analyse_incident(
            [make_view(first), make_view(b"bad second")],
            Origin.APP,
            ai=ReplayAIConnection(recordings_dir),
            run_log=run_log,
            evidence=store,
        )
    assert rejected.value.intake_id is not None
    first_metadata, second_metadata = run_log.intake_views(rejected.value.intake_id)
    assert first_metadata.working_copy is not None
    assert store.exists(sha256_hex(first))
    assert first_metadata.validation_error is None
    assert second_metadata.validation_error is not None
    assert not store.exists(second_metadata.sha256)
    assert run_log.list_runs() == []


def test_intake_limit_environment_defaults_and_invalid_settings() -> None:
    from incident_report.config import ConfigError
    from incident_report.intake_limits import IntakeLimits

    limits = IntakeLimits.from_env(
        {
            "INCIDENT_MAX_UPLOAD_BYTES": "2500",
            "INCIDENT_MAX_DURATION_SECONDS": "2.5",
            "INCIDENT_PROBE_TIMEOUT_SECONDS": "0.5",
            "INCIDENT_STAGE_WORKERS": "2",
        }
    )
    assert limits.max_upload_bytes == 2500
    assert limits.max_duration_seconds == 2.5
    assert limits.probe_timeout_seconds == 0.5
    assert limits.stage_workers == 2
    for value in ("0", "-1", "nan", "inf", "nonsense"):
        with pytest.raises(ConfigError):
            IntakeLimits.from_env({"INCIDENT_HASH_TIMEOUT_SECONDS": value})


def test_intake_record_session_is_the_share_link_session_that_uploaded_it(
    tmp_path: Path, recordings_dir: Path, run_log: RunLog
) -> None:
    from fastapi.testclient import TestClient

    from conftest import write_recording
    from incident_report.access import AccessStore
    from incident_report.domain import Role
    from incident_report.entry import PROMPT_VERSION
    from incident_report.hashing import incident_input_hash, sha256_hex
    from incident_report.web import create_app
    from video_fixtures import synthetic_video

    app = create_app(
        ai=ReplayAIConnection(recordings_dir), run_log=run_log, evidence=LocalEvidenceStore(tmp_path / "session")
    )
    link = AccessStore(run_log.database_url).create_link()
    visitor = TestClient(app, base_url="https://testserver")
    opened = visitor.post("/access/open", json={"link_id": link["link_id"], "secret": link["secret"]})
    assert opened.status_code == 200
    share_session = opened.json()["session_id"]
    content = synthetic_video(b"session-wiring")
    write_recording(
        recordings_dir, incident_input_hash([(Role.EXTERIOR, sha256_hex(content))]), PROMPT_VERSION, "Session summary."
    )

    response = visitor.post("/reports", data={"role": "exterior"}, files={"video": ("a.mp4", content, "video/mp4")})

    assert response.status_code == 200
    (run,) = run_log.list_runs()
    (record,) = run_log.intake_views(run.run_id)
    assert record.session_id == share_session
    assert run.attribution is not None and str(run.attribution.session_id) == share_session
