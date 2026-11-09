"""Calibration HTTP output must match the unchanged producer public functions."""

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from integration.tools_worker.test_worker import fixture_cases, request


def test_calibration_save_is_byte_identical_to_public_workflow(tmp_path):
    from integration.tools_worker.app import create_app
    from minivision_reconstruct.calibration import (
        build_calibration,
        parse_correspondence_rows,
        parse_road_feature,
        save_calibration_revision,
    )
    from minivision_reconstruct.domain.models import FrameRate
    from minivision_reconstruct.video.reader import read_frame_at

    cases = fixture_cases(tmp_path)
    case = cases / "mig02-multi-review"
    video = case / "original/mig02-multi-review-staged-synthetic.avi"
    video.parent.mkdir(exist_ok=True)
    shutil.copyfile(
        Path(os.environ["INCIDENTDESK_TEST_CASES_ROOT"]) / video.relative_to(cases),
        video,
    )
    direct = tmp_path / "direct"
    shutil.copytree(case, direct)
    original = json.loads((case / "analysis/calibration/revision-001/calibration.json").read_bytes())
    points = lambda rows: "\n".join(
        ",".join(str(row[key]) for key in ("image_x", "image_y", "world_x", "world_y")) for row in rows
    )
    features = [
        {
            "feature_id": row["feature_id"],
            "name": row["name"],
            "kind": row["kind"],
            "points": "\n".join(f"{x},{y}" for x, y in row["image_points"]),
        }
        for row in original["features"]
    ]
    body = {
        "action_id": str(uuid4()),
        "action": "save",
        "reviewer": "Reviewer",
        "reason": "Recheck calibration",
        "frame": 0,
        "mode": original["mode"],
        "world_unit": original["world_unit"],
        "validation_tolerance": original["validation_tolerance"],
        "control_text": points(original["control_points"]),
        "validation_text": points(original["validation_points"]),
        "features": features,
    }
    frame = read_frame_at(video, FrameRate(25, 1), 0)
    document = build_calibration(
        "mig02-multi-review",
        original["source_video_sha256"],
        640,
        480,
        body["mode"],
        body["world_unit"],
        parse_correspondence_rows(body["control_text"], "control"),
        parse_correspondence_rows(body["validation_text"], "validation"),
        body["validation_tolerance"],
        tuple(parse_road_feature(row["feature_id"], row["name"], row["kind"], row["points"]) for row in features),
    )
    expected = save_calibration_revision(direct, document, frame.image)
    app = create_app(cases, "test-local-token")
    response = request(app, "/cases/mig02-multi-review/calibration/revision-001", data=body)
    assert response.status_code == 200, response.text
    actual = case / "analysis/calibration" / response.json()["revision"]
    assert {path.name: path.read_bytes() for path in actual.iterdir()} == {
        path.name: path.read_bytes() for path in expected.iterdir()
    }
    assert {path.name for path in actual.iterdir()} == {
        "calibration.json",
        "road_overlay.png",
    }
    before = {path.name for path in (case / "analysis/calibration").iterdir()}
    repeated = request(app, "/cases/mig02-multi-review/calibration/revision-001", data=body)
    assert repeated.status_code == 200 and repeated.json() == response.json()
    assert {path.name for path in (case / "analysis/calibration").iterdir()} == before
    changed = request(
        app, "/cases/mig02-multi-review/calibration/revision-001", data={**body, "reason": "Different intent"}
    )
    assert changed.status_code == 409
    refused = request(
        app,
        "/cases/mig02-multi-review/calibration/revision-001",
        data={**body, "mode": "invalid", "action_id": str(uuid4())},
    )
    assert refused.status_code == 422
    assert {path.name for path in (case / "analysis/calibration").iterdir()} == before
