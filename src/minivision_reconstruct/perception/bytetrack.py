"""Thin, explicit connector around Ultralytics' ByteTrack implementation."""

from dataclasses import asdict
from types import SimpleNamespace

import numpy as np

from minivision_reconstruct.domain.errors import TrackingError
from minivision_reconstruct.domain.models import RawDetection, TrackRecord, TrackerConfiguration, SUPPORTED_CLASS_NAMES


CLASS_IDS: dict[str, int] = {"person": 0, "bicycle": 1, "car": 2, "motorcycle": 3, "bus": 4, "truck": 5}


class DetectionBatch:
    """Small results-like adapter required by Ultralytics BYTETracker.update."""

    def __init__(self, detections: tuple[RawDetection, ...]) -> None:
        self._detections = detections
        self.conf = np.asarray([detection.confidence for detection in detections], dtype=np.float32)
        self.cls = np.asarray([CLASS_IDS[detection.label] for detection in detections], dtype=np.float32)
        self.xywh = np.asarray(
            [
                [
                    (detection.x1 + detection.x2) / 2,
                    (detection.y1 + detection.y2) / 2,
                    detection.x2 - detection.x1,
                    detection.y2 - detection.y1,
                ]
                for detection in detections
            ],
            dtype=np.float32,
        ).reshape((-1, 4))

    def __len__(self) -> int:
        return len(self._detections)

    def __getitem__(self, index: np.ndarray) -> "DetectionBatch":
        selected = np.flatnonzero(index) if getattr(index, "dtype", None) == np.dtype(bool) else np.asarray(index)
        return DetectionBatch(tuple(self._detections[int(value)] for value in selected))


class ByteTrackConnector:
    """Stateful external-system connector that emits typed, per-frame track states."""

    def __init__(self, configuration: TrackerConfiguration) -> None:
        try:
            from ultralytics.trackers.byte_tracker import BYTETracker

            self._tracker = BYTETracker(SimpleNamespace(**asdict(configuration)))
        except Exception as error:
            raise TrackingError(f"Could not initialize Ultralytics ByteTrack: {error}") from error
        self._previous_lost_ids: set[int] = set()

    def update(
        self,
        detections: tuple[RawDetection, ...],
        case_id: str,
        run_id: str,
        frame_number: int,
        timestamp_seconds: float,
    ) -> tuple[TrackRecord, ...]:
        """Associate one frame of supported detections and expose observed/lost/recovered states."""
        supported = tuple(detection for detection in detections if detection.label in SUPPORTED_CLASS_NAMES)
        batch = DetectionBatch(supported)
        try:
            output = self._tracker.update(batch)
        except Exception as error:
            raise TrackingError(f"ByteTrack failed for case '{case_id}', frame {frame_number}: {error}") from error
        records = [self._active_record(row, supported, case_id, run_id, frame_number, timestamp_seconds) for row in output]
        active_ids = {record.track_id for record in records}
        lost_records = [self._lost_record(track, case_id, run_id, frame_number, timestamp_seconds) for track in self._tracker.lost_stracks]
        self._previous_lost_ids = {record.track_id for record in lost_records if record.track_id not in active_ids}
        return tuple(sorted((*records, *lost_records), key=lambda record: record.track_id))

    def _active_record(
        self,
        row: np.ndarray,
        detections: tuple[RawDetection, ...],
        case_id: str,
        run_id: str,
        frame_number: int,
        timestamp_seconds: float,
    ) -> TrackRecord:
        track_id = int(row[4])
        detection_index = int(row[7])
        if detection_index < 0 or detection_index >= len(detections):
            raise TrackingError(f"ByteTrack returned invalid detection index {detection_index} for frame {frame_number}.")
        detection = detections[detection_index]
        state = "recovered" if track_id in self._previous_lost_ids else "observed"
        track = next((candidate for candidate in self._tracker.tracked_stracks if candidate.track_id == track_id), None)
        if track is None:
            raise TrackingError(f"ByteTrack returned missing active track {track_id} for frame {frame_number}.")
        return TrackRecord(
            case_id=case_id, run_id=run_id, frame_number=frame_number, timestamp_seconds=timestamp_seconds,
            track_id=track_id, class_id=CLASS_IDS[detection.label], class_name=detection.label,
            confidence=detection.confidence, bbox_x1=detection.x1, bbox_y1=detection.y1, bbox_x2=detection.x2, bbox_y2=detection.y2,
            state=state, track_age_frames=frame_number - track.start_frame + 1, gap_length_frames=0, schema_version="1.0",
        )

    def _lost_record(self, track: object, case_id: str, run_id: str, frame_number: int, timestamp_seconds: float) -> TrackRecord:
        track_id = int(getattr(track, "track_id"))
        class_id = int(getattr(track, "cls"))
        class_name = next((name for name, value in CLASS_IDS.items() if value == class_id), "car")
        end_frame = int(getattr(track, "end_frame"))
        start_frame = int(getattr(track, "start_frame"))
        return TrackRecord(
            case_id=case_id, run_id=run_id, frame_number=frame_number, timestamp_seconds=timestamp_seconds,
            track_id=track_id, class_id=class_id, class_name=class_name, confidence=None,
            bbox_x1=None, bbox_y1=None, bbox_x2=None, bbox_y2=None, state="lost",
            track_age_frames=max(0, end_frame - start_frame + 1), gap_length_frames=frame_number - end_frame,
            schema_version="1.0",
        )
