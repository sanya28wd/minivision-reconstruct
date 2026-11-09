"""Repeated review intents cannot undo or save twice."""

from uuid import uuid4

from integration.tools_worker.test_replay import replay_cases
from integration.tools_worker.test_worker import request


def test_double_undo_and_stale_review_head(tmp_path):
    from integration.tools_worker.app import create_app
    from minivision_reconstruct.perception import review_workflow as workflow

    cases = replay_cases(tmp_path)
    ctx = workflow.open_review_context(cases, "mig02-multi-review", "staged-tracks")
    for name in ("truck", "bus"):
        workflow.append_review_event(
            ctx,
            "Reviewer",
            "correct",
            {
                "track_id": 1,
                "frame_number": 0,
                "class_name": name,
            },
        )
    app = create_app(cases, "test-local-token")
    path = "/cases/mig02-multi-review/tracks/staged-tracks"
    events = workflow.read_review_events(ctx.event_log_path, ctx.case_id, ctx.source_run_id)
    head = events[-1]
    body = {
        "action": "undo",
        "action_id": str(uuid4()),
        "reviewer": "Reviewer",
        "reason": "Undo the displayed action",
        "payload": {},
        "expected_event_id": head.event_id,
        "expected_sequence": head.sequence,
    }
    first = request(app, path, data=body)
    assert first.status_code == 200, first.text
    saved = ctx.event_log_path.read_bytes()
    repeated = request(app, path, data=body)
    assert repeated.status_code == 200 and repeated.json() == first.json()
    assert ctx.event_log_path.read_bytes() == saved
    stale = request(app, path, data={**body, "action_id": str(uuid4())})
    assert stale.status_code == 409 and "latest" in stale.json()["error"].lower()
    assert ctx.event_log_path.read_bytes() == saved
    changed = request(app, path, data={**body, "reason": "Different intent"})
    assert changed.status_code == 409
    assert ctx.event_log_path.read_bytes() == saved
    current = request(app, path).json()
    assert current["latest_event_id"] != head.event_id
    assert current["latest_sequence"] == head.sequence + 1


def test_double_track_save_and_changed_input(tmp_path):
    from integration.tools_worker.app import create_app

    cases = replay_cases(tmp_path)
    app = create_app(cases, "test-local-token")
    path = "/cases/mig02-multi-review/tracks/staged-tracks"
    body = {
        "action": "correct",
        "action_id": str(uuid4()),
        "reviewer": "Reviewer",
        "reason": "Correct class",
        "payload": {"track_id": 1, "frame_number": 0, "class_name": "truck"},
    }
    first = request(app, path, data=body)
    assert first.status_code == 200
    events = cases / "mig02-multi-review/analysis/reviews/staged-tracks/events.jsonl"
    saved = events.read_bytes()
    assert request(app, path, data=body).json() == first.json()
    assert events.read_bytes() == saved
    assert request(app, path, data={**body, "reason": "Changed"}).status_code == 409
    assert events.read_bytes() == saved
