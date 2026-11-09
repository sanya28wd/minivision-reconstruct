"""Sample reports tab (RPT-40): recorded model output replayed by current code, never an AI call."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from access_client import authorized_client
from incident_report.ai import AIRequest, AIResponse
from incident_report.runlog import RunLog
from incident_report.sample_reports import BANNER, DEFAULT_SAMPLES_DIR, load_samples
from incident_report.web import create_app

LICENCE = "unknown — owner to confirm"
SAMPLE_IDS = ("day1", "night1", "major-01", "minor-01")


class _RaisingAI:
    """Stands in for the AI connection: any call is a test failure."""

    def analyse(self, request: AIRequest) -> AIResponse:
        raise AssertionError("A sample page must never call the AI connection.")

    def repair(self, request: AIRequest, response: AIResponse, errors: list[str]) -> AIResponse:
        raise AssertionError("A sample page must never call the AI connection.")


def _client(run_log: RunLog, samples_dir: Path | None = None) -> TestClient:
    app = create_app(ai=_RaisingAI(), run_log=run_log, samples_dir=samples_dir)
    return authorized_client(app, run_log.database_url)


def _copy_samples(tmp_path: Path) -> Path:
    target = tmp_path / "samples"
    shutil.copytree(DEFAULT_SAMPLES_DIR, target)
    return target


# --- the committed samples -----------------------------------------------------------------------------


def test_every_exported_recording_replays_into_a_valid_report() -> None:
    samples = load_samples()
    assert tuple(s.sample_id for s in samples) == SAMPLE_IDS
    for sample in samples:
        report = sample.report
        assert report.is_valid, (sample.sample_id, report.validation_errors)
        assert report.prompt_version == "overview-2"
        assert report.provenance == "recorded-gemini-api"
        assert [v.filename for v in report.views] == [sample.source_clip]
        assert report.facts is not None


def test_manifest_records_clip_and_unknown_licence() -> None:
    manifest = json.loads((DEFAULT_SAMPLES_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in manifest["samples"]] == list(SAMPLE_IDS)
    for entry in manifest["samples"]:
        assert entry["licence"] == LICENCE
        assert entry["source_clip"] == entry["view"]["filename"]
        assert (DEFAULT_SAMPLES_DIR / entry["recording"]).is_file()


def test_recordings_hold_only_model_calls_and_all_calls_share_the_prompt() -> None:
    manifest = json.loads((DEFAULT_SAMPLES_DIR / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["samples"]:
        data = json.loads((DEFAULT_SAMPLES_DIR / entry["recording"]).read_text(encoding="utf-8"))
        assert list(data) == ["calls"]
        for call in data["calls"]:
            assert call["prompt_version"] in {entry["prompt_version"], entry["prompt_version"] + "-repair"}
            assert call["view_id"] == entry["view"]["sha256"]


def test_samples_folder_has_no_video_and_no_local_paths_or_secrets() -> None:
    files = [p for p in DEFAULT_SAMPLES_DIR.rglob("*") if p.is_file()]
    assert files
    assert not [p for p in files if p.suffix.lower() in {".mp4", ".mov", ".avi", ".webm", ".mkv"}]
    forbidden = re.compile(r"AIza[0-9A-Za-z_-]{20}|[A-Za-z]:\\\\Users|/Users/|/home/|[\w.+-]+@[\w-]+\.[a-z]{2,}")
    for path in files:
        assert not forbidden.search(path.read_text(encoding="utf-8")), path.name


def test_the_repaired_recording_is_replayed_through_its_repair_call() -> None:
    sample = next(s for s in load_samples() if s.sample_id == "minor-01")
    # The first attempt fails citation checks; the recorded repair call is what makes the report valid.
    assert sample.report.is_valid
    assert sample.report.summary == "Overview facts repaired."


# --- replay failures never become invented reports ----------------------------------------------------


def test_a_recording_that_no_longer_matches_is_dropped_not_patched(tmp_path: Path) -> None:
    target = _copy_samples(tmp_path)
    path = target / "recordings" / "night1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for call in data["calls"]:
        call["request_hash"] = "0" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    ids = tuple(s.sample_id for s in load_samples(target))
    assert ids == ("day1", "major-01", "minor-01")


def test_missing_samples_folder_gives_an_empty_list(tmp_path: Path) -> None:
    assert load_samples(tmp_path / "nothing-here") == ()


def test_unreadable_manifest_gives_an_empty_list(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    assert load_samples(tmp_path) == ()


# --- the pages --------------------------------------------------------------------------------------------


def test_samples_list_and_every_sample_page_work_with_the_ai_connection_raising(run_log: RunLog) -> None:
    client = _client(run_log)
    listing = client.get("/samples")
    assert listing.status_code == 200
    assert BANNER in listing.text
    for sample_id in SAMPLE_IDS:
        assert f'href="/samples/{sample_id}"' in listing.text
        page = client.get(f"/samples/{sample_id}")
        assert page.status_code == 200, sample_id
        assert BANNER in page.text
        assert f"<title>{BANNER}" in page.text
        assert "Video not included" in page.text
        assert "Parties" in page.text
        # No review workspace, feedback or "New incident" links that need a stored run.
        assert "/review" not in page.text
        assert "/feedback?run_id" not in page.text


def test_banner_is_the_exact_text_and_comes_before_the_report(run_log: RunLog) -> None:
    assert BANNER == "Sample report from research footage — not a real case."
    page = _client(run_log).get("/samples/day1").text
    assert page.index(BANNER, page.index("<main")) < page.index("<h1>")


def test_sample_page_shows_source_clip_and_unknown_licence(run_log: RunLog) -> None:
    page = _client(run_log).get("/samples/major-01").text
    assert "MAJOR_01.mp4" in page
    assert LICENCE in page


def test_unknown_sample_is_404(run_log: RunLog) -> None:
    client = _client(run_log)
    assert client.get("/samples/not-a-sample").status_code == 404
    assert client.get("/samples/..%2fmanifest").status_code == 404


def test_samples_need_a_share_session(run_log: RunLog) -> None:
    anonymous = TestClient(create_app(ai=_RaisingAI(), run_log=run_log), base_url="https://testserver")
    assert anonymous.get("/samples").status_code == 403
    assert anonymous.get("/samples/day1").status_code == 403


def test_sample_text_is_escaped(run_log: RunLog, tmp_path: Path) -> None:
    target = _copy_samples(tmp_path)
    path = target / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["samples"][0]["title"] = "<script>alert(1)</script>"
    # The clip name is also the view's filename, so both change together and the sample still loads.
    manifest["samples"][0]["source_clip"] = "<img src=x onerror=alert(2)>.mp4"
    manifest["samples"][0]["view"]["filename"] = "<img src=x onerror=alert(2)>.mp4"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    client = _client(run_log, target)
    for url in ("/samples", "/samples/day1"):
        html = client.get(url).text
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "&lt;img src=x" in html
