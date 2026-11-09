"""HTTP regressions for delayed completion, safe retry and independent runs."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from pathlib import Path
import os
import shutil

import pytest

from integration.tools_worker.test_replay import replay_cases
from integration.tools_worker.test_worker import ROOT, request


@pytest.mark.parametrize("other_run", [False, True])
def test_export_queue_retry_and_run_isolation(tmp_path, monkeypatch, other_run):
    from integration.tools_worker.app import create_app
    from integration.tools_worker import replay
    from minivision_reconstruct.reconstruction.storage import load_bundle

    cases = replay_cases(tmp_path)
    case_id, run_id = "mig02-multi-review", "week8-incident"
    if other_run:
        case_id, run_id = "mig02-relative", "week8-incident-relative"
        case = cases / case_id
        shutil.copytree(
            ROOT / f"contracts/evidence-v1/fixtures/{case_id}/{case_id}", case
        )
        shared = Path(os.environ["INCIDENTDESK_TEST_CASES_ROOT"]) / case_id
        for source in (
            *shared.glob("original/*"),
            shared / f"analysis/reconstruction/{run_id}/replay.mp4",
        ):
            destination = case / source.relative_to(shared)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    run = cases / case_id / "analysis/reconstruction" / run_id
    markers = load_bundle(run, "initial", "initial").markers.model_dump(mode="json")
    entered, release = Event(), Event()

    def delayed_export(*args, **kwargs):
        entered.set()
        assert release.wait(15), "test did not release its own export"

    monkeypatch.setattr(replay, "export_reconstruction", delayed_export)
    monkeypatch.setattr(replay.ReplayMediaConnector, "start", lambda self: None)
    monkeypatch.setattr(replay.ReplayMediaConnector, "close", lambda self: None)
    app = create_app(cases, "test-local-token")
    common = {
        "reviewer": "Reviewer",
        "reason": "Concurrency regression",
        "scene_revision": "initial",
        "marker_revision": "initial",
    }
    export = {
        **common,
        "action": "reconstruction_export",
        "source_frame": 0,
        "action_id": "00000000-0000-0000-0000-000000000001",
    }
    save = {
        **common,
        "action": "marker_save",
        "document": markers,
        "action_id": "00000000-0000-0000-0000-000000000002",
    }
    before = set((run / "markers").glob("revision-*"))
    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(
            request, app, "/cases/mig02-multi-review/replay/week8-incident", data=export
        )
        try:
            assert entered.wait(10)
            queued = executor.submit(
                request, app, f"/cases/{case_id}/replay/{run_id}", data=save
            )
            if other_run:
                # This run must finish while the other run's export is still blocked.
                assert queued.result(timeout=5).status_code == 200
            else:
                with pytest.raises(TimeoutError):
                    queued.result(
                        timeout=0.05
                    )  # app stops waiting; worker remains queued
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200
        saved = queued.result(timeout=10)
    retry = request(app, f"/cases/{case_id}/replay/{run_id}", data=save)
    assert saved.status_code == retry.status_code == 200
    assert retry.json() == saved.json()
    completed = request(app, f"/cases/{case_id}/replay/{run_id}/completed", data=save)
    assert completed.status_code == 200 and completed.json() == saved.json()
    unknown = request(
        app,
        f"/cases/{case_id}/replay/{run_id}/completed",
        data={**save, "action_id": "00000000-0000-0000-0000-000000000099"},
    )
    assert unknown.status_code == 409 and unknown.json()["write_state"] == "refused"
    assert len(set((run / "markers").glob("revision-*")) - before) == 1
