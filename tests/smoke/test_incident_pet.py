"""Week 8 phase 2: post-encroachment time on hand-built tracks with hand-computed answers.

Tracks run at 10 fps along straight lines through the origin, one unit per frame, offset half a unit so no
sample sits exactly on a disc boundary. Track 1 runs along +x and passes the origin at frame 19.5. Track 2 runs
along +y, starting `delay` frames later, and passes the origin at frame delay + 19.5.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from minivision_reconstruct.incident.models import ConflictConfig, IncidentConfig
from minivision_reconstruct.incident.pet import post_encroachment
from minivision_reconstruct.movement.models import SmoothedPoint
from minivision_reconstruct.movement.motion import MotionContext
from minivision_reconstruct.reconstruction.storage import read_record


PERIOD = 0.1
CONTEXT = MotionContext(case_id="pet-case", frame_period=PERIOD, unit="m", right_handed=True, created_by="test")
CONFIG = read_record(Path("configs/incident.json").resolve(), IncidentConfig).conflict  # floor 1.0, coverage 2.0, max PET 5 s


def track(track_id: int, start: int, position: Callable[[int], tuple[float, float]],
          uncertainty: float = 0.25, lost: range = range(0), length: int = 41) -> tuple[SmoothedPoint, ...]:
    points = []
    for frame in range(start, start + length):
        available = frame not in lost
        xy = position(frame - start) if available else None
        points.append(SmoothedPoint(
            track_id=track_id, source_frame=frame, source_time=frame * PERIOD, class_name="car",
            state="observed" if available else "lost", provenance="automatic",
            segment_id=(int(frame >= lost.start) if lost else 0) if available else None,
            raw_position=xy, smoothed_position=xy, smoothed=available, footpoint_pixel=None,
            relative_uncertainty=uncertainty if available else None, position_uncertainty=uncertainty if available else None, unit="m",
        ))
    return tuple(points)


def eastbound(uncertainty: float = 0.25) -> tuple[SmoothedPoint, ...]:
    return track(1, 0, lambda step: (step - 19.5, 0.0), uncertainty)


def northbound(delay: int, uncertainty: float = 0.25, lost: range = range(0), x: float = 0.0) -> tuple[SmoothedPoint, ...]:
    return track(2, delay, lambda step: (x, step - 19.5), uncertainty, lost)


def test_pet_clean_crossing_matches_hand_computed_value() -> None:
    # Radius = max(1.0, 2.0 × hypot(0.25, 0.25)) = 1.0. Track 1 is inside at frames 19–20 (x = ±0.5), outside at 21.
    # Track 2 (delay 12) is first inside at frame 31. Continuous truth: track 1 leaves x = 1 at 2.05 s, track 2
    # reaches y = -1 at 3.05 s, so PET = 1.0 s.
    (record,) = post_encroachment(eastbound() + northbound(12), CONFIG, CONTEXT)
    assert (record.track_first, record.track_second) == (1, 2)
    assert (record.first_enter_frame, record.first_exit_frame, record.second_enter_frame) == (19, 21, 31)
    assert record.radius == pytest.approx(1.0)
    assert record.location == pytest.approx((0.0, 0.0))
    assert abs(record.pet_seconds - 1.0) <= PERIOD
    assert record.pet_window[0] <= 1.0 <= record.pet_window[1]
    assert not record.simultaneous_occupancy
    assert record.confidence == "high"
    assert any("no rate of travel" in signal for signal in record.evidence_signals)


def test_pet_conflict_area_grows_with_position_uncertainty() -> None:
    # Radius = 2.0 × hypot(1, 1) ≈ 2.83. Continuous truth: track 1 leaves at frame 19.5 + 2.83, track 2 (delay 12)
    # enters at frame 31.5 − 2.83, so PET ≈ (12 − 5.66) × 0.1 ≈ 0.634 s. Frame-based: (29 − 23) × 0.1 = 0.6 s.
    (record,) = post_encroachment(eastbound(1.0) + northbound(12, 1.0), CONFIG, CONTEXT)
    assert record.radius == pytest.approx(2.0 * 2 ** 0.5)
    assert (record.first_enter_frame, record.first_exit_frame, record.second_enter_frame) == (17, 23, 29)
    assert abs(record.pet_seconds - 0.634) <= PERIOD
    assert record.pet_window[1] - record.pet_window[0] > 2 * PERIOD


def test_pet_simultaneous_occupancy_is_zero_or_negative() -> None:
    # Delay 1: track 2 is inside from frame 20 while track 1 is still inside until frame 21.
    (record,) = post_encroachment(eastbound() + northbound(1), CONFIG, CONTEXT)
    assert record.pet_seconds == pytest.approx(-0.1)
    assert record.simultaneous_occupancy
    assert record.track_first == 1, "the road user that entered first is listed first"
    assert any("same time" in signal for signal in record.evidence_signals)


def test_pet_paths_that_never_cross_or_cross_too_late() -> None:
    parallel = track(2, 5, lambda step: (step - 19.5, 6.0))
    assert post_encroachment(eastbound() + parallel, CONFIG, CONTEXT) == ()
    stops_short = northbound(12, x=25.0)
    assert post_encroachment(eastbound() + stops_short, CONFIG, CONTEXT) == ()
    assert post_encroachment(eastbound() + northbound(80), CONFIG, CONTEXT) == (), "PET 7.8 s exceeds maximum_pet_seconds"


def test_pet_never_bridges_a_lost_gap() -> None:
    # Track 2 crosses the origin at frame 31.5; it is lost for frames 27–36, so no observed path crosses track 1.
    assert post_encroachment(eastbound() + northbound(12, lost=range(27, 37)), CONFIG, CONTEXT) == ()
    # Lost from frame 33 on: the crossing is observed, but track 2 is never seen leaving the conflict area.
    (record,) = post_encroachment(eastbound() + northbound(12, lost=range(33, 53)), CONFIG, CONTEXT)
    assert record.second_enter_frame == 31
    assert record.confidence == "low"
    assert any("exit not observed" in signal for signal in record.evidence_signals)


def test_pet_shared_path_is_one_conflict() -> None:
    # A follower on exactly the same line overlaps the leader's path along its whole length: one record, not one per vertex.
    follower = track(2, 15, lambda step: (step - 19.5, 0.0))
    records = post_encroachment(eastbound() + follower, CONFIG, CONTEXT)
    assert len(records) == 1
    assert records[0].track_first == 1
