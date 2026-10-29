"""Week 8 phase 8a: the ACCIDENT subset is stratified, scene-disjoint, and registered only from files on disk."""

import csv
from pathlib import Path

import pytest

from minivision_reconstruct.incident.accident_benchmark import REGISTRY_COLUMNS, SubsetPlan, register_subset, select_subset, write_selection
from minivision_reconstruct.incident.models import IncidentError


TYPES = ("rear-end", "head-on", "sideswipe", "t-bone", "single")
WEATHERS = ("clear", "night", "rain", "sunset", "wet")


def manifest(path: Path) -> Path:
    columns = ("rgb_path", "annotations_path", "type", "accident_time", "accident_frame", "center_x", "center_y", "x1", "y1", "x2", "y2",
               "map", "weather", "camera_position", "no_frames", "duration", "height", "width", "annotations_start_offset")
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for number in range(400):
            kind, weather, town = TYPES[number % 5], WEATHERS[(number // 5) % 5], f"Town0{number % 4 + 3}"
            name = f"{town}_{kind}_{weather}_{number}"
            writer.writerow({"rgb_path": f"synthetic_videos/videos/{kind}/{name}.mp4", "annotations_path": f"synthetic_videos/annotations/{name}.json.gz",
                             "type": kind, "accident_time": "7.5", "accident_frame": "150", "center_x": "0.5", "center_y": "0.5", "x1": "0.4", "y1": "0.4",
                             "x2": "0.6", "y2": "0.6", "map": town, "weather": weather, "camera_position": str(number), "no_frames": "400",
                             "duration": "20.0", "height": "1080", "width": "1920", "annotations_start_offset": "50"})
    return path


def test_selection_is_stratified_and_scene_disjoint(tmp_path: Path) -> None:
    rows = select_subset(manifest(tmp_path / "manifest.csv"), SubsetPlan(3, 5, 8))
    assert len(rows) == 40
    for kind in TYPES:
        assert sum(row["type"] == kind and row["split"] == "week8_dev" for row in rows) == 3
        assert sum(row["type"] == kind and row["split"] == "week8_heldout" for row in rows) == 5
    scenes = {split: {(row["map"], row["camera_position"]) for row in rows if row["split"] == split} for split in ("week8_dev", "week8_heldout")}
    assert not scenes["week8_dev"] & scenes["week8_heldout"] and len(scenes["week8_dev"] | scenes["week8_heldout"]) == 40
    assert select_subset(tmp_path / "manifest.csv", SubsetPlan(3, 5, 8)) == rows, "selection is deterministic for a seed"
    assert {row["weather"] for row in rows if row["split"] == "week8_dev"} == set(WEATHERS)
    with pytest.raises(IncidentError, match="scene-disjoint"):
        select_subset(tmp_path / "manifest.csv", SubsetPlan(60, 60, 8))


def test_registration_hashes_downloaded_clips_only(tmp_path: Path) -> None:
    rows = select_subset(manifest(tmp_path / "manifest.csv"), SubsetPlan(1, 1, 8))
    write_selection(rows, tmp_path / "selection.csv")
    with pytest.raises(IncidentError, match="already exists"):
        write_selection(rows, tmp_path / "selection.csv")
    registry = tmp_path / "registry.csv"
    registry.write_text(",".join(REGISTRY_COLUMNS) + "\n")
    data = tmp_path / "data"
    present = rows[0]
    for key in ("rgb_path", "annotations_path"):
        (data / present[key]).parent.mkdir(parents=True, exist_ok=True)
        (data / present[key]).write_bytes(b"clip")
    registered, missing = register_subset(tmp_path / "selection.csv", data, registry, tmp_path, "2026-10-27")
    assert registered == [present["local_video_id"]] and len(missing) == len(rows) - 1
    assert register_subset(tmp_path / "selection.csv", data, registry, tmp_path, "2026-10-27")[0] == [], "registered clips are never duplicated"
    uncompressed = rows[1]
    (data / uncompressed["rgb_path"]).parent.mkdir(parents=True, exist_ok=True)
    (data / uncompressed["rgb_path"]).write_bytes(b"clip")
    (data / uncompressed["annotations_path"]).with_suffix("").write_bytes(b"{}")
    assert register_subset(tmp_path / "selection.csv", data, registry, tmp_path, "2026-10-27")[0] == [uncompressed["local_video_id"]], \
        "Kaggle's uncompressed annotation is accepted"
    row, second = list(csv.DictReader(registry.open()))
    assert second["annotation_path"].endswith(".json")
    assert row["labels_used_for_tuning"] == "false" and len(row["sha256"]) == 64
    assert row["development_allowed"] == ("false" if present["split"] == "week8_heldout" else "true")


def test_carla_calibration_maps_the_camera_pose_onto_the_road(tmp_path: Path) -> None:
    import json
    import numpy as np
    from minivision_reconstruct.incident.accident_benchmark import CameraModel, carla_calibration
    annotation = tmp_path / "clip.json"
    # 20 m up, looking 45 degrees down along +x: the image centre sees the road 20 m ahead.
    annotation.write_text(json.dumps({"sensor": {"location": {"x": 5.0, "y": 7.0, "z": 20.0}, "rotation": {"pitch": -45.0, "yaw": 0.0, "roll": 0.0}}}))
    calibration = carla_calibration(annotation, "case", "a" * 64, 1920, 1080, CameraModel(90.0, 0.0, 150.0))
    assert calibration.mode == "metric" and calibration.validation_passed and calibration.metric_claims_permitted

    def world(u: float, v: float) -> tuple[float, float]:
        x, y, w = np.asarray(calibration.homography) @ (u, v, 1.0)
        return x / w, y / w

    assert np.allclose(world(960, 540), (20.0, 0.0), atol=1e-6)
    assert world(1400, 540)[1] < 0, "image right is negative y in the right-handed local frame"
    annotation.write_text(json.dumps({"sensor": {"location": {"x": 0, "y": 0, "z": 20.0}, "rotation": {"pitch": 40.0, "yaw": 0, "roll": 0}}}))
    with pytest.raises(IncidentError, match="reach the road"):
        carla_calibration(annotation, "case", "a" * 64, 1920, 1080, CameraModel(90.0, 0.0, 150.0))


def test_primary_candidate_is_label_blind_and_pair_uses_labelled_boxes() -> None:
    from types import SimpleNamespace
    from minivision_reconstruct.incident.accident_benchmark import pair_correct, primary_candidate
    event = lambda kind, confidence, peak, ids=(1, 2): SimpleNamespace(event_type=kind, confidence=confidence, peak_time=peak, peak_frame=int(peak * 10), track_ids=ids)
    events = [event("near_miss_candidate", "high", 1.0), event("collision_candidate", "low", 2.0), event("collision_candidate", "medium", 5.0),
              event("collision_candidate", "medium", 3.0)]
    assert primary_candidate(events).peak_time == 3.0, "most confident first, earliest peak on ties; near misses never chosen"
    assert primary_candidate(events[:1]) is None
    annotation = {"base": [{"iteration": 41 + frame, "objects": [{"id": 7, "2d_bbox": [[0, 0], [10, 10]]}, {"id": 8, "2d_bbox": [[20, 0], [30, 10]]}]}
                           for frame in range(40)], "collision": [{"ids": [7, 8]}]}
    tracks = {1: {30: (21, 0, 30, 10)}, 2: {28: (0, 1, 10, 10)}, 3: {30: (100, 100, 120, 120)}}
    assert pair_correct(event("collision_candidate", "high", 3.0), annotation, tracks, 0.3) is True, "boxes within reach of a lost frame count"
    assert pair_correct(event("collision_candidate", "high", 3.0, (1, 3)), annotation, tracks, 0.3) is False
    assert pair_correct(event("collision_candidate", "high", 3.0, (1, 9)), annotation, tracks, 0.3) is None


def test_a_failed_clip_stays_in_the_table_as_a_miss(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from minivision_reconstruct.incident.accident_benchmark import FAILURE_SUFFIX, evaluate_clip, summarise
    row = select_subset(manifest(tmp_path / "manifest.csv"), SubsetPlan(1, 1, 8))[0]
    failure = tmp_path / "case" / "analysis" / "incident" / f"incident-002{FAILURE_SUFFIX}"
    failure.parent.mkdir(parents=True)
    failure.write_text("ReconstructionError: no observations\n")
    config = SimpleNamespace(config_id="incident-week8-002")
    result = evaluate_clip(row, tmp_path / "case", tmp_path, "incident-002", config)
    assert result["proposed"] is False and result["pipeline_failure"].startswith("ReconstructionError")
    summary = summarise([result])
    assert summary["clips"] == 1 and summary["detection_rate"] == 0 and summary["pipeline_failures"] == 1


def real_manifest(path: Path) -> Path:
    columns = ("path", "type", "rollover", "accident_time", "accident_frame", "center_x", "center_y", "x1", "y1", "x2", "y2", "region",
               "scene_layout", "weather", "day_time", "quality", "no_frames", "duration", "height", "width", "split_in_distribution", "split_geo_aware")
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for number in range(300):
            kind, side = TYPES[number % 5], "train" if number % 3 == 0 else "test"
            source = f"src{number // 2}" if number < 20 else f"src{number}"  # the first sources hold two clips each
            writer.writerow({"path": f"real_videos/{source}_0{number % 2}.mp4", "type": kind, "rollover": "0", "accident_time": "5.0",
                             "accident_frame": "100", "center_x": "0.5", "center_y": "0.5", "x1": "0.4", "y1": "0.4", "x2": "0.6", "y2": "0.6",
                             "region": "UAE" if side == "train" else "Alabama", "scene_layout": "highway", "weather": ("normal", "rain")[number % 2],
                             "day_time": ("day", "night")[(number // 2) % 2], "quality": "Fine", "no_frames": "300", "duration": "10.0",
                             "height": "360" if number % 7 == 0 else "720", "width": "1280", "split_in_distribution": side, "split_geo_aware": side})
        writer.writerow({"path": "real_videos/shared_00.mp4", "type": "head-on", "rollover": "0", "accident_time": "1", "accident_frame": "1",
                         "center_x": "0.5", "center_y": "0.5", "x1": "0", "y1": "0", "x2": "1", "y2": "1", "region": "UAE", "scene_layout": "highway",
                         "weather": "normal", "day_time": "day", "quality": "Fine", "no_frames": "1", "duration": "1", "height": "720", "width": "1280",
                         "split_in_distribution": "train", "split_geo_aware": "train"})
        writer.writerow({"path": "real_videos/shared_01.mp4", "type": "head-on", "rollover": "0", "accident_time": "1", "accident_frame": "1",
                         "center_x": "0.5", "center_y": "0.5", "x1": "0", "y1": "0", "x2": "1", "y2": "1", "region": "Alabama", "scene_layout": "highway",
                         "weather": "normal", "day_time": "day", "quality": "Fine", "no_frames": "1", "duration": "1", "height": "720", "width": "1280",
                         "split_in_distribution": "test", "split_geo_aware": "test"})
    return path


def test_real_selection_separates_cameras_and_locks_the_test_clips(tmp_path: Path) -> None:
    from minivision_reconstruct.incident.accident_benchmark import RealSubsetPlan, select_real_subset
    plan = RealSubsetPlan(per_type_dev=3, per_type_test=5, min_height=720, seed=9, exclude=frozenset({"src150_00"}))
    rows = select_real_subset(real_manifest(tmp_path / "real.csv"), plan)
    assert len(rows) == 40 and select_real_subset(tmp_path / "real.csv", plan) == rows, "quotas met; deterministic for a seed"
    for kind in TYPES:
        assert sum(row["type"] == kind and row["split"] == "real_dev" for row in rows) == 3
        assert sum(row["type"] == kind and row["split"] == "real_test" for row in rows) == 5
    assert all(row["official_split"] == ("train" if row["split"] == "real_dev" else "test") for row in rows), "dev from official train, test from official test"
    sources = [row["local_video_id"].rsplit("_", 1)[0] for row in rows]
    assert len(sources) == len(set(sources)), "one clip per camera source"
    assert "shared" not in sources, "a source on both sides of the official split is dropped"
    assert "src150" not in sources and all(int(row["height"]) >= 720 for row in rows)
    assert {row["day_time"] for row in rows if row["split"] == "real_test"} == {"day", "night"}
    write_selection(rows, tmp_path / "selection.csv")
    registry = tmp_path / "registry.csv"
    registry.write_text(",".join(REGISTRY_COLUMNS) + "\n")
    data = tmp_path / "data"
    (data / "manifests").mkdir(parents=True)
    (data / "manifests" / "real.csv").write_text("labels")
    for row in rows[:2] + [next(row for row in rows if row["split"] == "real_test")]:
        (data / row["rgb_path"]).parent.mkdir(parents=True, exist_ok=True)
        (data / row["rgb_path"]).write_bytes(b"clip")
    registered, missing = register_subset(tmp_path / "selection.csv", data, registry, tmp_path, "2026-10-28")
    assert len(registered) == 3 and len(missing) == 37
    entries = {entry["local_video_id"]: entry for entry in csv.DictReader(registry.open())}
    for row in rows:
        if row["local_video_id"] in entries:
            entry = entries[row["local_video_id"]]
            assert entry["development_allowed"] == ("false" if row["split"] == "real_test" else "true") and entry["labels_used_for_tuning"] == "false"


def test_impact_labels_measure_whether_one_track_spans_the_impact() -> None:
    from minivision_reconstruct.incident.impact_labels import keyframes_for, label_from_choices, suggest_choices, track_continuity
    keyframes = keyframes_for(100, 20.0, 400)
    assert keyframes == (60, 70, 80, 90, 100, 110, 120, 130, 140) and keyframes_for(10, 20.0, 400)[0] == 0
    # Two road users approach the impact box from left and right and stop there; a far-away car is noise.
    gap = lambda frame: (100 - min(frame, 100)) * 2
    proposals = {frame: [[300 - gap(frame), 200, 340 - gap(frame), 230, 2, 0.9],
                         [360 + gap(frame), 200, 400 + gap(frame), 230, 2, 0.9],
                         [10, 10, 30, 25, 2, 0.5]] for frame in keyframes}
    suggestion = suggest_choices(keyframes, proposals, 100, (300.0, 190.0, 400.0, 240.0), ("A", "B"), 3.0)
    assert suggestion == {"A": [0] * 9, "B": [1] * 9}, "the two vehicles at the impact box are followed outward; the far car is ignored"
    clip = {"local_video_id": "clip", "split": "real_dev", "type": "t-bone", "accident_frame": "100", "no_frames": "400", "duration": "20"}
    label = label_from_choices(clip, keyframes, proposals, {"A": [0] * 9, "B": [1] * 8 + [None]}, "tester", "", "verified")
    tracks = {7: {frame: tuple(proposals[frame][0][:4]) for frame in keyframes},                           # A keeps one ID
              8: {frame: tuple(proposals[frame][1][:4]) for frame in keyframes if frame < 100},             # B's track ends at impact
              9: {frame: tuple(proposals[frame][1][:4]) for frame in keyframes if frame >= 100}}            # and restarts with a new ID
    result = track_continuity(label, tracks, 0.3)
    assert result["A"]["same_id_through_impact"] is True and result["A"]["covered"] == 9
    assert result["B"]["same_id_through_impact"] is False and result["B"]["visible"] == 8
