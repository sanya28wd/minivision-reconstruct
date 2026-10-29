"""Per-observation road-plane position uncertainty from tracking quality and calibration."""

import math

import numpy as np
from numpy.typing import NDArray

from minivision_reconstruct.domain.models import CalibrationDocument
from minivision_reconstruct.movement.models import MovementError, UncertaintyConfig
from minivision_reconstruct.reconstruction.models import Observation


def unit_factor(calibration: CalibrationDocument) -> float:
    """Match Week 6 projection: feet become metres, other units are unchanged."""
    return 0.3048 if calibration.world_unit == "ft" else 1.0


def homography_jacobian(homography: NDArray[np.float64], pixel: tuple[float, float]) -> NDArray[np.float64]:
    """Analytic 2×2 derivative of world position with respect to image pixel."""
    x, y = pixel
    numerator_x = homography[0, 0] * x + homography[0, 1] * y + homography[0, 2]
    numerator_y = homography[1, 0] * x + homography[1, 1] * y + homography[1, 2]
    denominator = homography[2, 0] * x + homography[2, 1] * y + homography[2, 2]
    if abs(denominator) < 1e-12:
        raise MovementError(f"Pixel {pixel} lies on the calibration horizon; its road position is undefined.")
    return np.asarray([
        [(homography[0, 0] * denominator - numerator_x * homography[2, 0]) / denominator ** 2,
         (homography[0, 1] * denominator - numerator_x * homography[2, 1]) / denominator ** 2],
        [(homography[1, 0] * denominator - numerator_y * homography[2, 0]) / denominator ** 2,
         (homography[1, 1] * denominator - numerator_y * homography[2, 1]) / denominator ** 2],
    ], dtype=np.float64)


def world_units_per_pixel(calibration: CalibrationDocument, pixel: tuple[float, float]) -> float:
    """Worst-direction ground distance spanned by one pixel at this image location."""
    jacobian = homography_jacobian(np.asarray(calibration.homography, dtype=np.float64), pixel)
    return float(np.sqrt(np.linalg.eigvalsh(jacobian @ jacobian.T).max())) * unit_factor(calibration)


def world_is_right_handed(calibration: CalibrationDocument) -> bool:
    """A non-mirrored top-down world flips the y-down image orientation (negative Jacobian determinant)."""
    centre = (calibration.image_width / 2, calibration.image_height / 2)
    return float(np.linalg.det(homography_jacobian(np.asarray(calibration.homography, dtype=np.float64), centre))) < 0


def footpoint_pixel(observation: Observation) -> tuple[float, float] | None:
    """Bottom-centre road-contact approximation used by Week 6 projection."""
    if observation.bbox is None:
        return None
    return ((observation.bbox[0] + observation.bbox[2]) / 2, observation.bbox[3])


def footpoint_sigma_pixels(observation: Observation, config: UncertaintyConfig) -> float:
    """One-sigma image contact-point error from box size, confidence, and observation origin."""
    if observation.bbox is None:
        raise MovementError(f"Track {observation.track_id}, frame {observation.source_frame} has no box for uncertainty.")
    sigma = max(config.minimum_footpoint_sigma_pixels, config.footpoint_sigma_fraction_of_box_height * (observation.bbox[3] - observation.bbox[1]))
    if observation.detection_confidence is not None and observation.detection_confidence < config.low_confidence_threshold:
        sigma *= config.low_confidence_multiplier
    if observation.state == "interpolated" or observation.provenance == "review_interpolation":
        sigma *= config.interpolated_multiplier
    if observation.provenance == "reviewer":
        sigma *= config.reviewer_multiplier
    return sigma


def observation_uncertainty(observation: Observation, calibration: CalibrationDocument, config: UncertaintyConfig) -> tuple[float, float]:
    """Return (relative, absolute) uncertainty radii at the configured coverage.

    Relative uncertainty is contact-point error only; it governs displacement, lane membership
    against geometry from the same calibration, and stop tests. Absolute uncertainty adds the
    held-out calibration error in quadrature for map positions and separations.
    """
    pixel = footpoint_pixel(observation)
    if pixel is None:
        raise MovementError(f"Track {observation.track_id}, frame {observation.source_frame} is unavailable.")
    relative_sigma = footpoint_sigma_pixels(observation, config) * world_units_per_pixel(calibration, pixel)
    calibration_sigma = calibration.validation_rmse * unit_factor(calibration)
    absolute_sigma = math.hypot(relative_sigma, calibration_sigma)
    return config.coverage_factor * relative_sigma, config.coverage_factor * absolute_sigma
