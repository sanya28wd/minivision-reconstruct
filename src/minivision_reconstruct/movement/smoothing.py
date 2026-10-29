"""Segment-local position smoothing that keeps raw positions and never bridges unavailable frames."""

from collections import defaultdict

import numpy as np
from scipy.signal import savgol_filter

from minivision_reconstruct.domain.models import CalibrationDocument
from minivision_reconstruct.movement.models import DistanceUnit, MovementError, SmoothedPoint, SmoothingConfig, UncertaintyConfig
from minivision_reconstruct.movement.uncertainty import footpoint_pixel, observation_uncertainty
from minivision_reconstruct.reconstruction.models import Observation


def split_segments(observations: tuple[Observation, ...]) -> tuple[tuple[Observation, ...], ...]:
    """Group one track's available observations into runs of consecutive source frames."""
    segments: list[list[Observation]] = []
    previous: Observation | None = None
    for observation in observations:
        if observation.position is None:
            previous = None
            continue
        if previous is None or observation.source_frame != previous.source_frame + 1:
            segments.append([])
        segments[-1].append(observation)
        previous = observation
    return tuple(tuple(segment) for segment in segments)


def smooth_tracks(
    observations: tuple[Observation, ...], calibration: CalibrationDocument,
    smoothing: SmoothingConfig, uncertainty: UncertaintyConfig, unit: DistanceUnit,
) -> tuple[SmoothedPoint, ...]:
    """Return one smoothed point per input observation, ordered by track then frame."""
    if smoothing.window_frames % 2 == 0 or smoothing.polynomial_order >= smoothing.window_frames:
        raise MovementError("Smoothing window must be odd and larger than the polynomial order.")
    by_track: dict[int, list[Observation]] = defaultdict(list)
    for observation in observations:
        by_track[observation.track_id].append(observation)
    result: list[SmoothedPoint] = []
    for track_id in sorted(by_track):
        ordered = tuple(sorted(by_track[track_id], key=lambda item: item.source_frame))
        if len({item.source_frame for item in ordered}) != len(ordered):
            raise MovementError(f"Track {track_id} has duplicate source frames.")
        smoothed: dict[int, tuple[tuple[float, float], int, bool]] = {}
        for segment_id, segment in enumerate(split_segments(ordered)):
            raw = np.asarray([item.position for item in segment], dtype=np.float64)
            applied = len(segment) >= smoothing.window_frames
            values = savgol_filter(raw, smoothing.window_frames, smoothing.polynomial_order, axis=0, mode="interp") if applied else raw
            for item, value in zip(segment, values, strict=True):
                smoothed[item.source_frame] = ((float(value[0]), float(value[1])), segment_id, applied)
        for item in ordered:
            if item.position is None:
                result.append(SmoothedPoint(
                    track_id=track_id, source_frame=item.source_frame, source_time=item.source_time, class_name=item.class_name,
                    state=item.state, provenance=item.provenance, segment_id=None, raw_position=None, smoothed_position=None,
                    smoothed=False, footpoint_pixel=None, relative_uncertainty=None, position_uncertainty=None, unit=unit,
                ))
                continue
            position, segment_id, applied = smoothed[item.source_frame]
            relative, absolute = observation_uncertainty(item, calibration, uncertainty)
            result.append(SmoothedPoint(
                track_id=track_id, source_frame=item.source_frame, source_time=item.source_time, class_name=item.class_name,
                state=item.state, provenance=item.provenance, segment_id=segment_id, raw_position=item.position,
                smoothed_position=position, smoothed=applied, footpoint_pixel=footpoint_pixel(item),
                relative_uncertainty=relative, position_uncertainty=absolute, unit=unit,
            ))
    return tuple(result)
