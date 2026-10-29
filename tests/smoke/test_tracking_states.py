"""Smoke coverage for ByteTrack state export and presentation interpolation."""

from minivision_reconstruct.domain.models import RawDetection, TrackRecord, TrackerConfiguration
from minivision_reconstruct.perception.bytetrack import ByteTrackConnector
from minivision_reconstruct.perception.track_states import interpolate_short_gaps


def _configuration() -> TrackerConfiguration:
    return TrackerConfiguration("bytetrack", 0.25, 0.10, 0.25, 30, 0.80, True, 5)


def _detection(x: float) -> RawDetection:
    return RawDetection("car", 0.9, x, 20.0, x + 40.0, 60.0)


def test_bytetrack_persists_an_id_after_confirmation() -> None:
    tracker = ByteTrackConnector(_configuration())
    first = tracker.update((_detection(10.0),), "case", "run", 0, 0.0)
    assert len(first) == 1
    assert first[0].track_id == 1
    second = tracker.update((_detection(12.0),), "case", "run", 1, 1 / 30)
    assert len(second) == 1
    assert second[0].track_id == 1
    third = tracker.update((_detection(14.0),), "case", "run", 2, 2 / 30)
    assert third[0].track_id == 1


def test_short_closed_gap_becomes_interpolated() -> None:
    records = (_record(0, "observed", 0.0, 10.0), _record(1, "lost", None, None), _record(2, "recovered", 20.0, 30.0))
    finalized = interpolate_short_gaps(records, 5)
    assert finalized[1].state == "interpolated"
    assert finalized[1].bbox_x1 == 10.0


def test_long_gap_remains_lost() -> None:
    records = (_record(0, "observed", 0.0, 10.0), *(_record(number, "lost", None, None) for number in range(1, 7)), _record(7, "recovered", 70.0, 80.0))
    finalized = interpolate_short_gaps(records, 5)
    assert all(record.state == "lost" for record in finalized[1:7])


def _record(frame_number: int, state: str, x1: float | None, x2: float | None) -> TrackRecord:
    return TrackRecord("case", "run", frame_number, frame_number / 30, 1, 2, "car", 0.9 if x1 is not None else None, x1, 0.0 if x1 is not None else None, x2, 10.0 if x1 is not None else None, state, frame_number + 1, 0, "1.0")
