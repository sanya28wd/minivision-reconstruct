"""Replay/incident writes through HTTP preserve public producer revision bytes."""

from datetime import UTC, datetime
import os
from pathlib import Path
import shutil

import pytest

from integration.tools_worker.test_worker import fixture_cases, request


def replay_cases(tmp_path):
    cases = fixture_cases(tmp_path)
    case = cases / "mig02-multi-review"
    shared = Path(os.environ["INCIDENTDESK_TEST_CASES_ROOT"])
    for relative in (
        "original/mig02-multi-review-staged-synthetic.avi",
        "analysis/reconstruction/week8-incident/replay.mp4",
        "analysis/movement/mig02-movement/trajectories.csv",
    ):
        destination = case / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(shared / case.name / relative, destination)
    return cases


@pytest.mark.parametrize("action", ["scene_save", "marker_save", "incident_review"])
def test_replay_revisions_match_public_workflow(tmp_path, monkeypatch, action):
    from integration.tools_worker.app import create_app
    from integration.tools_worker import replay
    from minivision_reconstruct.reconstruction import storage
    from minivision_reconstruct.incident import review
    from minivision_reconstruct.incident.models import ReviewDecision

    class FixedTime:
        @staticmethod
        def now(zone):
            return datetime(2026, 11, 6, tzinfo=UTC)

    monkeypatch.setattr(storage, "datetime", FixedTime)
    monkeypatch.setattr(review, "datetime", FixedTime)
    monkeypatch.setattr(replay, "datetime", FixedTime)
    cases = replay_cases(tmp_path)
    direct = tmp_path / "direct"
    shutil.copytree(cases, direct)
    relative = "mig02-multi-review/analysis/reconstruction/week8-incident"
    run = direct / relative
    bundle = storage.load_bundle(run, "initial", "initial")
    body = {
        "action": action,
        "reviewer": "Reviewer",
        "reason": "Parity review",
        "action_id": "00000000-0000-0000-0000-000000000001",
        "scene_revision": "initial",
        "marker_revision": "initial",
        "incident_run": "mig02-incident",
        "review_revision": "initial",
    }
    if action == "scene_save":
        document = bundle.scene.model_copy(update={"rotation_degrees": 5.0})
        body["document"] = document.model_dump(mode="json")
        expected = storage.save_scene_revision(run, document, "Reviewer")
        category = "scenes"
    elif action == "marker_save":
        document = bundle.markers.model_dump(mode="json")
        document["markers"].append(
            {
                "marker_id": "native-note-1",
                "source_frame": 0,
                "source_time": 0.0,
                "position": None,
                "track_ids": [1],
                "label": "<script>marker</script>",
                "note": "Marker note",
                "reviewer": "Reviewer",
                "created_at": FixedTime.now(UTC).isoformat(),
            }
        )
        from minivision_reconstruct.reconstruction.models import MarkerDocument

        body["document"] = document
        expected = storage.save_marker_revision(
            run, MarkerDocument.model_validate(document), "Reviewer", bundle
        )
        category = "markers"
    else:
        incident = direct / "mig02-multi-review/analysis/incident/mig02-incident"
        body["review_revision"] = review.review_revisions(incident)[-1]
        proposal = review.load_incident_run(incident).events[0]
        decision = ReviewDecision(
            target_kind="event",
            target_id=proposal.event_id,
            action="confirm",
            note="Parity review",
            modified_peak_time=None,
            modified_location=None,
            modified_outcome=None,
            modified_contributing_track_ids=None,
            reviewer="Reviewer",
            decided_at=FixedTime.now(UTC).isoformat(),
        )
        body["decision"] = decision.model_dump(mode="json")
        expected = review.save_review_revision(
            incident,
            review.with_decision(
                review.load_review(incident, body["review_revision"]).decisions,
                decision,
            ),
            "Reviewer",
        )
        category = "reviews"
    app = create_app(cases, "test-local-token")
    path = "/cases/mig02-multi-review/replay/week8-incident"
    response = request(app, path, data=body)
    assert response.status_code == 200, response.text
    actual_root = (
        cases / relative
        if action != "incident_review"
        else cases / "mig02-multi-review/analysis/incident/mig02-incident"
    )
    actual = actual_root / category / response.json()["revision"]
    assert {p.name: p.read_bytes() for p in actual.iterdir()} == {
        p.name: p.read_bytes() for p in expected.iterdir()
    }
    assert request(app, path, data=body).json() == response.json()
    assert len(list((actual_root / category).glob("revision-*"))) == len(
        list((expected.parent).glob("revision-*"))
    )
    if action == "incident_review":
        before = sorted(
            path.name for path in (actual_root / "reviews").glob("revision-*")
        )
        stale = request(
            app,
            path,
            data={
                **body,
                "action_id": "00000000-0000-0000-0000-000000000099",
                "review_revision": "initial",
            },
        )
        assert stale.status_code == 422
        assert (
            sorted(path.name for path in (actual_root / "reviews").glob("revision-*"))
            == before
        )
        exported = review.export_reviewed_view(incident, expected.name)
        body.update(
            action="incident_export",
            action_id="00000000-0000-0000-0000-000000000002",
            review_revision=expected.name,
        )
        response = request(app, path, data=body)
        assert response.status_code == 200, response.text
        actual = actual_root / "reviewed" / response.json()["revision"]
        assert {p.name: p.read_bytes() for p in actual.iterdir()} == {
            p.name: p.read_bytes() for p in exported.iterdir()
        }


def test_replay_serves_exact_assets_and_video_byte_ranges(tmp_path):
    import asyncio
    import httpx
    from integration.tools_worker.app import create_app
    from minivision_reconstruct.reconstruction.media import ASSET_DIRECTORY

    cases = replay_cases(tmp_path)
    app = create_app(cases, "test-local-token")
    base = "/cases/mig02-multi-review/replay/week8-incident"
    for filename in ("workspace.js", "workspace.css", "renderer.js"):
        response = request(app, base + "/assets/" + filename)
        assert response.status_code == 200
        assert response.content == (ASSET_DIRECTORY / filename).read_bytes()

    async def ranged():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40000)),
            base_url="http://127.0.0.1",
        ) as client:
            return await client.get(
                base + "/video",
                headers={
                    "Authorization": "Bearer test-local-token",
                    "Range": "bytes=0-15",
                },
            )

    response = asyncio.run(ranged())
    video = (
        cases / "mig02-multi-review/analysis/reconstruction/week8-incident/replay.mp4"
    )
    assert response.status_code == 206
    assert response.content == video.read_bytes()[:16]
    assert response.headers["content-range"] == f"bytes 0-15/{video.stat().st_size}"
