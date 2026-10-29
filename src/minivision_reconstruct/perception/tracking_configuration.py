"""Loading and validation for explicit ByteTrack configuration."""

import json
from pathlib import Path

from minivision_reconstruct.domain.errors import TrackerConfigurationError
from minivision_reconstruct.domain.models import TrackerConfiguration


def load_tracker_configuration(path: Path) -> TrackerConfiguration:
    """Load a complete tracker configuration without implicit defaults."""
    if not path.is_file():
        raise TrackerConfigurationError(f"Tracker configuration does not exist: '{path}'.")
    try:
        value: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        required = (
            "tracker_type", "track_high_thresh", "track_low_thresh", "new_track_thresh", "track_buffer",
            "match_thresh", "fuse_score", "interpolation_max_gap_frames",
        )
        missing = [field for field in required if field not in value]
        if missing:
            raise TrackerConfigurationError(f"Tracker configuration '{path}' is missing: {', '.join(missing)}.")
        configuration = TrackerConfiguration(
            tracker_type=str(value["tracker_type"]),
            track_high_thresh=float(value["track_high_thresh"]),
            track_low_thresh=float(value["track_low_thresh"]),
            new_track_thresh=float(value["new_track_thresh"]),
            track_buffer=int(value["track_buffer"]),
            match_thresh=float(value["match_thresh"]),
            fuse_score=bool(value["fuse_score"]),
            interpolation_max_gap_frames=int(value["interpolation_max_gap_frames"]),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise TrackerConfigurationError(f"Invalid tracker configuration '{path}': {error}") from error
    _validate(configuration, path)
    return configuration


def _validate(configuration: TrackerConfiguration, path: Path) -> None:
    if configuration.tracker_type != "bytetrack":
        raise TrackerConfigurationError(f"Only ByteTrack is supported in '{path}', got '{configuration.tracker_type}'.")
    thresholds = (configuration.track_low_thresh, configuration.track_high_thresh, configuration.new_track_thresh, configuration.match_thresh)
    if any(value < 0 or value > 1 for value in thresholds):
        raise TrackerConfigurationError(f"Tracker thresholds must be within [0, 1] in '{path}'.")
    if configuration.track_low_thresh > configuration.track_high_thresh:
        raise TrackerConfigurationError(f"track_low_thresh must not exceed track_high_thresh in '{path}'.")
    if configuration.track_buffer <= 0 or configuration.interpolation_max_gap_frames < 0:
        raise TrackerConfigurationError(f"track_buffer must be positive and interpolation_max_gap_frames non-negative in '{path}'.")
