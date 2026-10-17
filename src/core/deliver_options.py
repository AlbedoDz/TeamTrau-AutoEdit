"""Turn user-typed Deliver options (CLI flags, MCP tool arguments) into ``DeliverSettings``.

Every value arrives as a string or number; this module resolves presets, parses ``WxH`` / fractional
frame rates and variant lists, and reports problems as ``ValueError`` with a message that names the
offending option, so callers can wrap it (``click.BadParameter``) without rewording.
"""

import dataclasses
import re
from fractions import Fraction
from typing import get_args

from src.core.config import PRESETS
from src.core.types import (
    VALID_AUDIO_RATES,
    DeliverColor,
    DeliverFitMode,
    DeliverMaster,
    DeliverSettings,
    DeliverSubtitles,
)

_CANVAS_RE = re.compile(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$")


def parse_canvas(text: str) -> tuple[int, int]:
    """``"1080x1920"`` -> (1080, 1920); raises ValueError otherwise."""
    m = _CANVAS_RE.match(text or "")
    if not m:
        raise ValueError(f"canvas must look like 1920x1080, got {text!r}")
    return int(m.group(1)), int(m.group(2))


def parse_fps(value: str | float | int) -> float:
    """Frame rate from ``29.97``, ``"30000/1001"`` or ``"25"``; raises ValueError when not positive."""
    try:
        if isinstance(value, str) and "/" in value:
            fps = float(Fraction(value.strip()))
        else:
            fps = float(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"fps must be a positive number such as 29.97 or 30000/1001, got {value!r}") from exc
    if fps <= 0:
        raise ValueError(f"fps must be a positive number, got {value!r}")
    return fps


def parse_variants(text: str | list[str] | None) -> list[str] | None:
    """``"all"`` -> ["all"]; ``"master,cinematic"`` -> both keys; None/empty -> None (keep default)."""
    if text is None:
        return None
    items = text if isinstance(text, list) else [part.strip() for part in str(text).split(",")]
    items = [i for i in items if i]
    if not items:
        return None
    lowered = [i.lower() for i in items]
    if "all" in lowered:
        return ["all"]
    return ["active" if i.lower() == "active" else i for i in items]


def _choice(option: str, value: str, literal: object) -> str:
    allowed = get_args(literal)
    text = str(value).strip().lower()
    if text not in allowed:
        raise ValueError(f"{option} must be one of {', '.join(allowed)}; got {value!r}")
    return text


def build_deliver_settings(
    base: DeliverSettings,
    *,
    preset: str | None = None,
    canvas: str | None = None,
    fps: str | float | None = None,
    fit: str | None = None,
    color: str | None = None,
    master: str | None = None,
    subtitles: str | None = None,
    audio_rate: int | None = None,
    audio_channels: int | None = None,
    variants: str | list[str] | None = None,
    relink: bool | None = None,
    watermark_in_nle: bool | None = None,
) -> DeliverSettings:
    """``base`` with the given overrides applied (None = keep). Raises ValueError on bad input.

    ``preset`` sets canvas and frame rate together; an explicit ``canvas`` / ``fps`` wins over it,
    but ``preset`` and ``canvas`` together are ambiguous and rejected.
    """
    changes: dict[str, object] = {}
    if preset is not None:
        if canvas is not None:
            raise ValueError("use either preset or canvas, not both")
        key = preset.strip()
        match = next((k for k in PRESETS if k.lower() == key.lower()), None)
        if match is None:
            raise ValueError(f"unknown preset {preset!r}; available: {', '.join(PRESETS)}")
        changes.update(canvas_width=PRESETS[match].width, canvas_height=PRESETS[match].height, fps=PRESETS[match].fps)
    if canvas is not None:
        changes["canvas_width"], changes["canvas_height"] = parse_canvas(canvas)
    if fps is not None:
        changes["fps"] = parse_fps(fps)
    if fit is not None:
        changes["fit_mode"] = _choice("fit", fit, DeliverFitMode)
    if color is not None:
        changes["color"] = _choice("color", color, DeliverColor)
    if master is not None:
        changes["master"] = _choice("master", master, DeliverMaster)
    if subtitles is not None:
        changes["subtitles"] = _choice("subtitles", subtitles, DeliverSubtitles)
    if audio_rate is not None:
        if audio_rate not in VALID_AUDIO_RATES:
            raise ValueError(f"audio-rate must be one of {', '.join(map(str, VALID_AUDIO_RATES))}; got {audio_rate!r}")
        changes["audio_rate"] = audio_rate
    if audio_channels is not None:
        if audio_channels not in (1, 2):
            raise ValueError(f"audio-channels must be 1 or 2; got {audio_channels!r}")
        changes["audio_channels"] = audio_channels
    parsed_variants = parse_variants(variants)
    if parsed_variants is not None:
        changes["variants"] = parsed_variants
    if relink is not None:
        changes["relink_original"] = relink
    if watermark_in_nle is not None:
        changes["watermark_in_nle"] = watermark_in_nle
    return dataclasses.replace(base, **changes)  # DeliverSettings validates on construction
