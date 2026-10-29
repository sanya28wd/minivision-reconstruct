"""Explicit detector configuration loading."""

import json
from pathlib import Path

from minivision_reconstruct.domain.errors import DetectorConfigurationError
from minivision_reconstruct.domain.models import DetectorConfiguration


def load_detector_configuration(path: Path) -> DetectorConfiguration:
    """Load one complete JSON detector configuration without implicit values."""
    if not path.is_file():
        raise DetectorConfigurationError(f"Detector configuration does not exist: '{path}'.")
    try:
        value: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        required = ("model_name", "weights_path", "weight_source", "model_family", "model_license", "device", "confidence_threshold", "inference_image_size", "frame_stride")
        missing = [field for field in required if field not in value]
        if missing:
            raise DetectorConfigurationError(f"Detector configuration '{path}' is missing: {', '.join(missing)}.")
        configuration = DetectorConfiguration(
            model_name=str(value["model_name"]),
            weights_path=(path.parent.parent / str(value["weights_path"])).resolve(),
            weight_source=str(value["weight_source"]),
            model_family=str(value["model_family"]),
            model_license=str(value["model_license"]),
            device=str(value["device"]),
            confidence_threshold=float(value["confidence_threshold"]),
            inference_image_size=int(value["inference_image_size"]),
            frame_stride=int(value["frame_stride"]),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise DetectorConfigurationError(f"Invalid detector configuration '{path}': {error}") from error
    if not 0 <= configuration.confidence_threshold <= 1:
        raise DetectorConfigurationError(f"confidence_threshold must be within [0, 1] in '{path}'.")
    if configuration.inference_image_size <= 0 or configuration.frame_stride <= 0:
        raise DetectorConfigurationError(f"inference_image_size and frame_stride must be positive in '{path}'.")
    return configuration
