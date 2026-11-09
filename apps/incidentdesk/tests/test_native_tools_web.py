"""Native tools use attached selections, report access, escaped HTML and CSRF."""

import hashlib
import json
import re
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from access_client import authorized_client
from evidence_import_fixtures import FIXTURES
from incident_report.ai import ReplayAIConnection
from incident_report.domain import Origin, Report, ReportView, Role, Run, RunStatus
from incident_report.reconstruction.config import ReconstructionSettings
from incident_report.reconstruction.store import Workspace
from incident_report.runlog import RunLog
from incident_report.web import create_app


@pytest.mark.parametrize("response_kind", ["hostile", "captured"])
def test_track_page_uses_attached_selection_and_protects_write(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response_kind: str
) -> None:
    envelope = json.loads((FIXTURES / "mig02-relative/bundle.json").read_bytes())
    sha = envelope["sources"][0]["original_video_sha256"]
    report = Report(
        run_id=uuid.uuid4(),
        input_hash="a" * 64,
        prompt_version="synthetic",
        provenance="synthetic",
        summary="Synthetic",
        views=(
            ReportView(filename="front.mp4", role=Role.EXTERIOR, label="Front", acquisition_method=None, sha256=sha),
        ),
    )
    run_log.record(
        Run(
            run_id=report.run_id,
            origin=Origin.REPLAY,
            created_at=datetime.now(UTC),
            input_hash=report.input_hash,
            report=report,
            status=RunStatus.SUCCEEDED,
            total_latency_ms=0,
        )
    )
    import psycopg

    exports_root = tmp_path / "exports"
    exports_root.mkdir()
    shutil.copytree(FIXTURES / "mig02-relative", exports_root / "mig02-relative")
    settings = ReconstructionSettings(True, exports_root)
    with psycopg.connect(run_log.database_url) as connection:
        workspace = Workspace(connection, settings)
        workspace.attach(report.run_id, "mig02-relative", "Reviewer")
        link = workspace.current(report.run_id)[0]
    monkeypatch.setenv("INCIDENTDESK_RECONSTRUCTION", "true")
    monkeypatch.setenv("INCIDENTDESK_EVIDENCE_EXPORTS_ROOT", str(exports_root))
    monkeypatch.setenv("INCIDENTDESK_NATIVE_TOOLS", "true")
    from incident_report.reconstruction.native_client import ToolsWorker

    real_exchange = ToolsWorker.exchange

    calls: list[tuple[str, object]] = []

    def exchange(
        self: object, path: str, body: object = None, byte_range: str | None = None
    ) -> tuple[bytes, str, int, dict[str, str]]:
        calls.append((path, body))
        if "/replay/" in path:
            if isinstance(body, dict):
                if body.get("action") == "export_attach":
                    return b'{"bundle":"native-00000000000000000000000000000001"}', "application/json", 200, {}
                return b'{"revision":"revision-001","category":"scene"}', "application/json", 200, {}
            return (
                (Path(__file__).parent / "fixtures/native_tracks/replay-response.json").read_bytes(),
                "application/json",
                200,
                {},
            )
        if "/calibration/" in path:
            if isinstance(body, dict) and body.get("action") == "save":
                return b'{"revision":"revision-003"}', "application/json", 200, {}
            return (
                (Path(__file__).parent / "fixtures/native_tracks/calibration-response.json").read_bytes(),
                "application/json",
                200,
                {},
            )
        if response_kind == "captured":
            return (
                (Path(__file__).parent / "fixtures/native_tracks/response.json").read_bytes(),
                "application/json",
                200,
                {},
            )
        return (
            json.dumps({"records": [], "track_ids": [1], "last_frame": 5, "case_id": "<script>evil</script>"}).encode(),
            "application/json",
            200,
            {},
        )

    monkeypatch.setattr(ToolsWorker, "exchange", exchange)

    def stream(self: object, path: str, byte_range: str | None) -> tuple[Iterator[bytes], str, int, dict[str, str]]:
        calls.append((path, byte_range))
        return iter([b"01234567", b"89abcdef"]), "video/mp4", 206, {"Content-Range": "bytes 0-15/100"}

    monkeypatch.setattr(ToolsWorker, "stream", stream)
    app = create_app(ai=ReplayAIConnection(), run_log=run_log)
    client = authorized_client(app, run_log.database_url)
    url = f"/reports/{report.run_id}/tools/{link.link_id}/tracks"
    selected_frame = 68 if response_kind == "captured" else 0
    page = client.get(url, params={"frame": selected_frame})
    assert page.status_code == 200
    assert "Track review" in page.text
    assert "<script>evil</script>" not in page.text
    if response_kind == "hostile":
        assert "&lt;script&gt;evil&lt;/script&gt;" in page.text
    else:
        assert "mig02-relative" in page.text
        assert "observed" in page.text
        assert "automatic" in page.text
    assert calls[-1][0] == f"/cases/mig02-relative/tracks/staged-tracks?frame={selected_frame}"
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token is not None
    fields = {
        "action_id": str(uuid.uuid4()),
        "csrf_token": token[1],
        "reviewer": "Reviewer",
        "reason": "Correct class",
        "action": "correct",
        "track_id": "1",
        "frame_number": "0",
        "class_name": "truck",
        "case_id": "../../outside",
    }
    count = len(calls)
    assert client.post(url, data={**fields, "csrf_token": "bad"}).status_code == 403
    assert client.post(url, data=fields, headers={"origin": "https://outside.example"}).status_code == 403
    assert len(calls) == count
    assert client.post(url, data=fields, follow_redirects=False).status_code == 303
    assert calls[-1][0] == "/cases/mig02-relative/tracks/staged-tracks"
    assert calls[-1][1] == {
        "action_id": fields["action_id"],
        "reviewer": "Reviewer",
        "reason": "Correct class",
        "action": "correct",
        "payload": {"track_id": 1, "frame_number": 0, "class_name": "truck"},
    }
    entries = [
        entry
        for entry in run_log.audit.list_entries()
        if entry.action.startswith("native.tracks.") and entry.run_id == report.run_id
    ]
    assert [entry.action for entry in entries] == ["native.tracks.requested", "native.tracks.saved"]
    assert entries[-1].session_id is not None and entries[-1].run_id == report.run_id
    assert entries[-1].details == {
        "operation": "correct",
        "reviewer_sha256": hashlib.sha256(b"Reviewer").hexdigest(),
        "reason_sha256": hashlib.sha256(b"Correct class").hexdigest(),
    }
    box_fields = {**fields, "class_name": "", "bbox": "10,10,20,20"}
    count = len(calls)
    for bbox in ("nan,10,20,20", "10,inf,20,20", "10,10,-inf,20"):
        nonfinite = client.post(url, data={**box_fields, "bbox": bbox})
        assert nonfinite.status_code == 400
        assert "finite" in nonfinite.json()["error"].lower()
    assert len(calls) == count
    assert client.post(url, data=box_fields, follow_redirects=False).status_code == 303
    assert calls[-1][1]["payload"] == {"track_id": 1, "frame_number": 0, "bbox": [10.0, 10.0, 20.0, 20.0]}
    create_fields = {**box_fields, "action": "create_track", "new_track_id": "8", "class_name": "car"}
    assert client.post(url, data=create_fields, follow_redirects=False).status_code == 303
    assert calls[-1][1]["payload"]["track_id"] == 8
    original_append = run_log.audit.append

    def fail_saved_audit(**kwargs: Any) -> object:
        if kwargs.get("action") == "native.tracks.saved":
            raise psycopg.OperationalError("Synthetic audit outage")
        return original_append(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(run_log.audit, "append", fail_saved_audit)
        count = len(calls)
        failed_confirmation = client.post(url, data=fields)
        assert failed_confirmation.status_code == 503
        assert "action completed" in failed_confirmation.text
        assert "do not repeat" in failed_confirmation.text
        assert len(calls) == count + 1
    assert client.get(url.replace(str(link.link_id), str(uuid.uuid4()))).status_code == 404
    original_decision = Workspace.decision

    def missing_source(self: Workspace, import_id: str) -> Any:
        decision = original_decision(self, import_id)
        assert decision.envelope is not None
        return decision.model_copy(update={"envelope": decision.envelope.model_copy(update={"sources": ()})})

    with monkeypatch.context() as patch:
        patch.setattr(Workspace, "decision", missing_source)
        count = len(calls)
        assert client.get(url).status_code == 404
        assert len(calls) == count
    calibration_url = url.removesuffix("tracks") + "calibration"
    calibration_page = client.get(calibration_url, params={"frame": 68})
    assert calibration_page.status_code == 200
    assert "Control points" in calibration_page.text
    assert "calibration-points.js" in calibration_page.text
    assert (
        calls[-1][0] == f"/cases/mig02-relative/calibration/{envelope['sources'][0]['calibration_revision']}?frame=68"
    )
    calibration_fields = {
        "action_id": str(uuid.uuid4()),
        "csrf_token": token[1],
        "reviewer": "Reviewer",
        "reason": "Check points",
        "action": "preview",
        "frame": "68",
        "mode": "non_metric",
        "world_unit": "relative_units",
        "validation_tolerance": "0.01",
        "control_text": "0,0,0,0",
        "validation_text": "1,1,1,1",
        "features": "[]",
    }
    count = len(calls)
    assert client.post(calibration_url, data={**calibration_fields, "csrf_token": "bad"}).status_code == 403
    assert len(calls) == count
    assert client.post(calibration_url, data=calibration_fields).status_code == 200
    assert calls[-1][1]["features"] == []
    saved = client.post(calibration_url, data={**calibration_fields, "action": "save"}, follow_redirects=False)
    assert saved.status_code == 303
    assert "revision=revision-003" in saved.headers["location"] and "frame=68" in saved.headers["location"]
    assert client.get(saved.headers["location"]).status_code == 200
    assert calls[-1][0] == "/cases/mig02-relative/calibration/revision-003?frame=68"
    replay_url = url.removesuffix("tracks") + "replay"
    replay_page = client.get(replay_url, params={"frame": 68})
    assert replay_page.status_code == 200
    assert "replay-adapter.js" in replay_page.text
    assert "Incident analysis" in replay_page.text and "Movement" in replay_page.text
    assert "127.0.0.1:8766" not in replay_page.text
    scene_fields = {
        "csrf_token": token[1],
        "reviewer": "Reviewer",
        "reason": "Scene edit",
        "action": "scene_save",
        "action_id": "00000000-0000-0000-0000-000000000010",
        "document": "{}",
        "scene_revision": "initial",
        "marker_revision": "initial",
    }
    from urllib.error import URLError

    from incident_report.reconstruction import native_client

    class OfflineOpener:
        def __init__(self, failure: Exception) -> None:
            self.failure = failure

        def open(self, *args: Any, **kwargs: Any) -> Any:
            raise self.failure

    for outage in (URLError("private transport details"), TimeoutError("private timeout details")):
        with monkeypatch.context() as patch:
            patch.setenv("INCIDENTDESK_TOOLS_TOKEN", "synthetic-private-token")
            patch.setattr(ToolsWorker, "exchange", real_exchange)
            patch.setattr(native_client, "build_opener", lambda *args, failure=outage: OfflineOpener(failure))
            count = len(calls)
            for tool_url, submitted_fields in (
                (url, fields),
                (calibration_url, {**calibration_fields, "action": "save"}),
                (replay_url, scene_fields),
            ):
                unavailable = client.get(tool_url)
                assert unavailable.status_code == 503
                failed_save = client.post(tool_url, data=submitted_fields)
                assert failed_save.status_code == 503
                assert "did not confirm" in failed_save.text
                assert "private" not in unavailable.text + failed_save.text
            assert len(calls) == count
    count = len(calls)
    assert client.post(replay_url, data={**scene_fields, "csrf_token": "bad"}).status_code == 403
    assert len(calls) == count
    invalid_document = client.post(replay_url, data={**scene_fields, "document": "{"})
    assert invalid_document.status_code == 422 and invalid_document.json()["write_state"] == "refused"
    blank_attribution = client.post(replay_url, data={**scene_fields, "reviewer": "   "})
    assert blank_attribution.status_code == 422 and blank_attribution.json()["detail"]["write_state"] == "refused"
    assert len(calls) == count
    assert client.post(replay_url, data=scene_fields).status_code == 200
    assert calls[-1][1]["document"] == {}
    assert calls[-1][0] == f"/cases/mig02-relative/replay/{envelope['sources'][0]['reconstruction_run']}"
    assessment_fields = {
        **scene_fields,
        "action": "incident_review",
        "target_kind": "assessment",
        "target_id": "assessment-1",
        "decision_action": "modify",
        "modified_outcome": "NO_SUPPORTED_CONTRIBUTOR",
        "change_contributors": "yes",
        "modified_contributing_track_ids": "",
    }
    assert client.post(replay_url, data=assessment_fields).status_code == 200
    assert calls[-1][1]["decision"]["modified_contributing_track_ids"] == []
    video = client.get(replay_url + "/video", headers={"Range": "bytes=0-15"})
    assert video.status_code == 206 and video.content == b"0123456789abcdef"
    assert video.headers["content-range"] == "bytes 0-15/100"
    assert str(calls[-1][1]) == "bytes=0-15"
    reviewed_bundle = exports_root / "native-00000000000000000000000000000001"
    shutil.copytree(exports_root / "mig02-relative", reviewed_bundle)
    changed_envelope = {**envelope, "created_at": "2026-11-06T00:00:00Z"}
    (reviewed_bundle / "bundle.json").write_text(json.dumps(changed_envelope), encoding="utf-8")
    from incident_report.reconstruction.mapping import map_track, mapping_history, track_options

    with psycopg.connect(run_log.database_url) as connection:
        workspace = Workspace(connection, settings)
        track = track_options(workspace, report.run_id, link)[0]
        map_track(
            workspace,
            report.run_id,
            link.import_id,
            link.view_sha256,
            track.track_ref,
            "cannot_determine",
            None,
            "Reviewer",
            "Identity unknown",
        )
    original_attach = Workspace.attach

    def refuse_new_bundle(self: Workspace, incident_id: uuid.UUID, bundle: str, reviewer: str) -> object:
        if bundle.startswith("native-"):
            raise ValueError("Synthetic attachment failure")
        return original_attach(self, incident_id, bundle, reviewer)

    with monkeypatch.context() as patch:
        patch.setattr(Workspace, "attach", refuse_new_bundle)
        partial = client.post(replay_url, data={**scene_fields, "action": "export_attach"})
        assert partial.status_code == 503
        assert partial.json()["bundle"] == reviewed_bundle.name
        assert "Export completed" in partial.json()["error"]
    attached = client.post(replay_url, data={**scene_fields, "action": "export_attach"})
    assert attached.status_code == 200
    with monkeypatch.context() as patch:
        patch.setattr(
            Workspace, "attach", lambda *args: (_ for _ in ()).throw(AssertionError("readback must not attach"))
        )
        recovered = client.post(
            replay_url, data={**scene_fields, "action": "export_attach"}, headers={"X-Native-Retry": "1"}
        )
        assert recovered.status_code == 200
        assert recovered.json()["new_link_id"] == attached.json()["new_link_id"]
        assert calls[-1][0].endswith("/completed")
    count = len(calls)
    assert client.post(replay_url, data={**scene_fields, "action": "export_attach"}).status_code == 404
    assert len(calls) == count
    with psycopg.connect(run_log.database_url) as connection:
        workspace = Workspace(connection, settings)
        current = workspace.current(report.run_id)
        assert len(current) == 1 and current[0].import_id != link.import_id
        assert len(workspace.history(report.run_id)) == 2
        assert mapping_history(workspace, report.run_id)[0].stale
    new_url = url.replace(str(link.link_id), attached.json()["new_link_id"])
    assert client.get(new_url).status_code == 200
    incident_url = f"/reports/{report.run_id}"
    review_native = client.get(incident_url + "/review")
    assert new_url in review_native.text
    assert "/reconstruct/tracks/" not in review_native.text
    native_print = client.get(incident_url + "/print").content
    monkeypatch.setenv("INCIDENTDESK_NATIVE_TOOLS", "false")
    assert client.get(url).status_code == 404
    assert client.get(new_url).status_code == 404
    legacy_page = client.get(incident_url + "/review")
    assert "/reconstruct/tracks/" in legacy_page.text
    assert new_url not in legacy_page.text
    assert client.get(incident_url + "/print").content == native_print


def test_invalid_native_flag_ignored_when_reconstruction_disabled(
    run_log: RunLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INCIDENTDESK_RECONSTRUCTION", "false")
    monkeypatch.setenv("INCIDENTDESK_NATIVE_TOOLS", "invalid")
    app = create_app(ai=ReplayAIConnection(), run_log=run_log)
    client = authorized_client(app, run_log.database_url)
    assert client.get(f"/reports/{uuid.uuid4()}/tools/{uuid.uuid4()}/tracks").status_code == 404
    monkeypatch.setenv("INCIDENTDESK_RECONSTRUCTION", "true")
    with pytest.raises(ValueError, match="INCIDENTDESK_NATIVE_TOOLS"):
        create_app(ai=ReplayAIConnection(), run_log=run_log)
