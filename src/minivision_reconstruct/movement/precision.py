"""Display rounding that never shows more precision than the stated uncertainty supports."""

import math

from minivision_reconstruct.movement.models import MovementError


def rounding_step(uncertainty: float) -> float:
    """Return one significant figure of the uncertainty as the display step."""
    if not math.isfinite(uncertainty) or uncertainty <= 0:
        raise MovementError(f"Display rounding requires a positive finite uncertainty, received {uncertainty}.")
    exponent = math.floor(math.log10(uncertainty))
    leading = round(uncertainty / 10 ** exponent)
    if leading == 10:
        leading, exponent = 1, exponent + 1
    return float(leading * 10 ** exponent)


def round_to_uncertainty(value: float, uncertainty: float) -> tuple[float, float]:
    """Round a value to the decimal place of its uncertainty's leading significant figure."""
    step = rounding_step(uncertainty)
    decimals = -math.floor(math.log10(step))
    return float(round(value, decimals)), float(round(step, max(decimals, 0)))


def display_value(value: float, uncertainty: float, unit: str) -> str:
    """Format 'value ± uncertainty unit' at defensible precision."""
    rounded, step = round_to_uncertainty(value, uncertainty)
    decimals = max(0, -math.floor(math.log10(step)))
    suffix = "" if unit == "" else f" {unit}"
    return f"{rounded:.{decimals}f} ± {step:.{decimals}f}{suffix}"


def display_time(seconds: float, window: tuple[float, float], frame_period: float) -> str:
    """Format an event time no finer than one source frame, with its uncertainty window."""
    if frame_period <= 0 or window[0] > seconds or window[1] < seconds:
        raise MovementError(f"Invalid time window {window} for {seconds} s at frame period {frame_period}.")
    half_width = max(frame_period, (window[1] - window[0]) / 2)
    return display_value(seconds, half_width, "s")
