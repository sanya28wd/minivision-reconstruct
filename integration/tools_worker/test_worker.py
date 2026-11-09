"""Worker HTTP boundary tests; run in the reconstruction WSL environment."""

import asyncio
import shutil
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[2]


def request(app, path, token="test-local-token", client_host="127.0.0.1", data=None):
    async def run():
        transport = httpx.ASGITransport(app=app, client=(client_host, 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            headers = {} if token is None else {"Authorization": f"Bearer {token}"}
            if data is None:
                return await client.get(path, headers=headers)
            return await client.post(path, headers=headers, json=data)

    return asyncio.run(run())


def fixture_cases(tmp_path):
    source = ROOT / "contracts/evidence-v1/fixtures/mig02-multi-review/mig02-multi-review"
    destination = tmp_path / "cases"
    shutil.copytree(source, destination / source.name)
    return destination


def test_every_worker_request_requires_token_and_loopback(tmp_path):
    from integration.tools_worker.app import create_app

    app = create_app(fixture_cases(tmp_path), "test-local-token")
    assert request(app, "/health", token=None).status_code == 401
    assert request(app, "/health", token="wrong").status_code == 401
    assert request(app, "/health", client_host="192.0.2.1").status_code == 403
    response = request(app, "/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_selected_fixture_run_and_unknown_identifiers(tmp_path):
    from integration.tools_worker.app import create_app

    app = create_app(fixture_cases(tmp_path), "test-local-token")
    selected = "/cases/mig02-multi-review/runs/week8-incident"
    response = request(app, selected)
    assert response.status_code == 200
    assert response.json() == {
        "case_id": "mig02-multi-review",
        "run_id": "week8-incident",
    }
    assert request(app, "/cases/missing/runs/week8-incident").status_code == 404
    assert request(app, "/cases/mig02-multi-review/runs/missing").status_code == 404


@pytest.mark.parametrize("identifier", ["..", ".", "%2E%2E", "%2Ftmp", "C:%5Ccases", "bad%5Cid", "bad%2Fid"])
@pytest.mark.parametrize("target", ["case", "run"])
def test_browser_paths_are_not_case_or_run_ids(tmp_path, identifier, target):
    from integration.tools_worker.app import create_app

    app = create_app(fixture_cases(tmp_path), "test-local-token")
    case = identifier if target == "case" else "mig02-multi-review"
    run = identifier if target == "run" else "week8-incident"
    response = request(app, f"/cases/{case}/runs/{run}")
    assert response.status_code in {404, 405}
    assert str(tmp_path) not in response.text


@pytest.mark.parametrize("location", ["case", "run", "output", "dangling"])
def test_case_links_and_output_links_are_refused(tmp_path, location):
    from integration.tools_worker.app import create_app

    cases = fixture_cases(tmp_path)
    case = cases / "mig02-multi-review"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("untouched")
    if location == "case":
        shutil.move(case, outside / "case")
        case.symlink_to(outside / "case", target_is_directory=True)
    elif location == "run":
        run = case / "analysis/reconstruction/week8-incident"
        shutil.move(run, outside / "run")
        run.symlink_to(outside / "run", target_is_directory=True)
    else:
        (case / "linked-output").symlink_to(
            outside if location == "output" else outside / "missing",
            target_is_directory=True,
        )
    app = create_app(cases, "test-local-token")
    response = request(app, "/cases/mig02-multi-review/runs/week8-incident")
    assert response.status_code == 404
    assert (outside / "sentinel").read_text() == "untouched"


def test_worker_refuses_unconfigured_secret_and_root(tmp_path):
    from integration.tools_worker.app import create_app

    for token in ("", " ", "line\nbreak", "unicode-\u00e9"):
        with pytest.raises(ValueError):
            create_app(tmp_path, token)
    with pytest.raises(ValueError):
        create_app(Path("relative"), "test-local-token")


@pytest.mark.parametrize(
    "action,payload,prepare",
    [
        ("correct", {"track_id": 1, "frame_number": 0, "class_name": "truck"}, []),
        (
            "correct",
            {
                "track_id": 1,
                "frame_number": 0,
                "bbox": [10, 10, 20, 20],
                "frame_width": 640,
                "frame_height": 480,
            },
            [],
        ),
        ("delete_observation", {"track_id": 1, "frame_number": 0}, []),
        ("delete_track", {"track_id": 4}, []),
        ("split", {"track_id": 1, "frame_number": 175, "new_track_id": 8}, []),
        (
            "merge",
            {"primary_track_id": 1, "secondary_track_id": 8},
            [("split", {"track_id": 1, "frame_number": 175, "new_track_id": 8})],
        ),
        (
            "repair_gap",
            {"track_id": 1, "start_frame": 1, "end_frame": 1},
            [("delete_observation", {"track_id": 1, "frame_number": 1})],
        ),
        (
            "create_track",
            {
                "track_id": 8,
                "frame_number": 0,
                "timestamp_seconds": 0.0,
                "class_name": "car",
                "bbox": [10, 10, 20, 20],
                "frame_width": 640,
                "frame_height": 480,
            },
            [],
        ),
        (
            "add",
            {
                "track_id": 1,
                "frame_number": 1,
                "timestamp_seconds": 0.04,
                "class_name": "car",
                "bbox": [10, 10, 20, 20],
                "frame_width": 640,
                "frame_height": 480,
            },
            [("delete_observation", {"track_id": 1, "frame_number": 1})],
        ),
        (
            "undo",
            {},
            [("correct", {"track_id": 1, "frame_number": 0, "class_name": "truck"})],
        ),
        (
            "export",
            {},
            [("correct", {"track_id": 1, "frame_number": 0, "class_name": "truck"})],
        ),
    ],
)
def test_track_correction_writes_same_event_bytes_as_public_workflow(tmp_path, monkeypatch, action, payload, prepare):
    import os
    from datetime import UTC, datetime
    from uuid import UUID
    from minivision_reconstruct.perception import review_workflow as workflow
    from integration.tools_worker.app import create_app

    cases = fixture_cases(tmp_path)
    case = cases / "mig02-multi-review"
    video = case / "original/mig02-multi-review-staged-synthetic.avi"
    video.parent.mkdir(exist_ok=True)
    shutil.copyfile(
        Path(os.environ["INCIDENTDESK_TEST_CASES_ROOT"]) / video.relative_to(cases),
        video,
    )
    direct = tmp_path / "direct"
    shutil.copytree(cases, direct)

    class FixedTime:
        @staticmethod
        def now(zone):
            return datetime(2026, 11, 6, tzinfo=UTC)

    monkeypatch.setattr(workflow, "datetime", FixedTime)
    monkeypatch.setattr(workflow, "uuid4", lambda: UUID(int=1))
    context = workflow.open_review_context(direct, "mig02-multi-review", "staged-tracks")
    worker_context = workflow.open_review_context(cases, "mig02-multi-review", "staged-tracks")
    for operation, input_payload in prepare:
        for selected in (context, worker_context):
            workflow.append_review_event(selected, "Reviewer", operation, input_payload)
    if action in {"undo", "export"}:
        automatic = workflow.load_track_records(context.source_records_path, context.case_id, context.source_run_id)
        if action == "undo":
            workflow.undo_latest_review_event(context, "Reviewer", automatic)
        else:
            direct_export = workflow.export_reviewed_revision(context, automatic)
    else:
        workflow.append_review_event(context, "Reviewer", action, {**payload, "reason": "Correct vehicle class"})
    app = create_app(cases, "test-local-token")
    response = request(
        app,
        "/cases/mig02-multi-review/tracks/staged-tracks",
        data={
            "action_id": "00000000-0000-0000-0000-000000000001",
            **(
                {
                    "expected_event_id": workflow.read_review_events(
                        worker_context.event_log_path, worker_context.case_id, worker_context.source_run_id
                    )[-1].event_id,
                    "expected_sequence": workflow.read_review_events(
                        worker_context.event_log_path, worker_context.case_id, worker_context.source_run_id
                    )[-1].sequence,
                }
                if action == "undo"
                else {}
            ),
            "reviewer": "Reviewer",
            "reason": "Correct vehicle class",
            "action": action,
            "payload": payload,
        },
    )
    assert response.status_code == 200, response.text
    actual = case / "analysis/reviews/staged-tracks/events.jsonl"
    assert actual.read_bytes() == context.event_log_path.read_bytes()
    if action == "export":
        worker_export = actual.parent / response.json()["revision"]
        expected_files = {
            path.relative_to(direct_export).as_posix(): path.read_bytes()
            for path in direct_export.rglob("*")
            if path.is_file()
        }
        assert expected_files.keys() == {
            "manifest.json",
            "tracks.jsonl",
            "reviewed.avi",
        }
        assert {
            path.relative_to(worker_export).as_posix(): path.read_bytes()
            for path in worker_export.rglob("*")
            if path.is_file()
        } == expected_files


def test_invalid_review_action_is_sanitized_and_does_not_append(tmp_path):
    import os
    from integration.tools_worker.app import create_app

    cases = fixture_cases(tmp_path)
    video = cases / "mig02-multi-review/original/mig02-multi-review-staged-synthetic.avi"
    video.parent.mkdir(exist_ok=True)
    shutil.copyfile(
        Path(os.environ["INCIDENTDESK_TEST_CASES_ROOT"]) / video.relative_to(cases),
        video,
    )
    events = cases / "mig02-multi-review/analysis/reviews/staged-tracks/events.jsonl"
    before = events.read_bytes() if events.exists() else b""
    app = create_app(cases, "test-local-token")
    response = request(
        app,
        "/cases/mig02-multi-review/tracks/staged-tracks",
        data={
            "action_id": "00000000-0000-0000-0000-000000000002",
            "reviewer": "Reviewer",
            "reason": "Bad selection",
            "action": "correct",
            "payload": {"track_id": 99999, "frame_number": 0, "class_name": "truck"},
        },
    )
    assert response.status_code == 422
    assert str(tmp_path) not in response.text
    assert (events.read_bytes() if events.exists() else b"") == before


def test_worker_refuses_oversized_write_before_workflow(tmp_path):
    from integration.tools_worker.app import create_app

    app = create_app(fixture_cases(tmp_path), "test-local-token")
    response = request(
        app,
        "/cases/mig02-multi-review/tracks/staged-tracks",
        data={"padding": "x" * (2 * 1024 * 1024)},
    )
    assert response.status_code == 413
