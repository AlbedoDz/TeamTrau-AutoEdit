"""Exact frame-rate arithmetic shared by every exporter and renderer.

Floats such as 29.97 are only labels; NLEs and ffmpeg need the exact rational
(30000/1001). ``frame_rate_fraction`` is the single place that maps a float rate to it.
"""

from fractions import Fraction

# Broadcast (NTSC-style) rates are n*1000/1001. Only these whole rates have a 1001 variant.
_NTSC_WHOLE_RATES: tuple[int, ...] = (24, 30, 48, 60, 120)
_NTSC_TOLERANCE = 0.01


def frame_rate_fraction(fps: float | Fraction | int) -> Fraction:
    """Exact frame rate: 23.976 -> 24000/1001, 29.97 -> 30000/1001, 59.94 -> 60000/1001,
    119.88 -> 120000/1001, 47.952 -> 48000/1001; everything else is rationalised
    (24 -> 24, 30 -> 30, 25 -> 25, 12.5 -> 25/2).
    """
    if isinstance(fps, Fraction):
        return fps
    value = float(fps)
    if value <= 0:
        raise ValueError(f"Frame rate must be positive, got {fps!r}")
    for whole in _NTSC_WHOLE_RATES:
        ntsc = whole * 1000 / 1001
        # Integer rates (24.0) never fall within 0.01 of their own NTSC variant (23.976).
        if abs(value - ntsc) < _NTSC_TOLERANCE:
            return Fraction(whole * 1000, 1001)
    return Fraction(value).limit_denominator(1000)


def is_ntsc_rate(fps: float | Fraction | int) -> bool:
    """True for n*1000/1001 rates (23.976, 29.97, 59.94, ...)."""
    return frame_rate_fraction(fps).denominator == 1001
