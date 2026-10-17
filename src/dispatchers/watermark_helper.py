"""Helper utilities for brand watermark positioning and FFmpeg filter generation."""

import logging
from typing import Literal

logger = logging.getLogger(__name__)

WatermarkPosition = Literal["top-left", "top-right", "bottom-left", "bottom-right", "center"]


def compute_watermark_overlay_expr(
    position: str = "top-right",
    margin: int = 40,
) -> str:
    """Compute FFmpeg overlay x:y coordinates expression for a given position and margin.

    Args:
        position: One of 'top-left', 'top-right', 'bottom-left', 'bottom-right', 'center'.
        margin: Distance in pixels from canvas edges.

    Returns:
        String expression formatted for FFmpeg overlay filter, e.g., 'x=W-w-40:y=40'.
    """
    pos = (position or "top-right").lower().strip()
    if pos in ("top-left", "topleft"):
        return f"x={margin}:y={margin}"
    if pos in ("bottom-left", "bottomleft"):
        return f"x={margin}:y=H-h-{margin}"
    if pos in ("bottom-right", "bottomright"):
        return f"x=W-w-{margin}:y=H-h-{margin}"
    if pos in ("center", "middle"):
        return "x=(W-w)/2:y=(H-h)/2"
    # Default: top-right
    return f"x=W-w-{margin}:y={margin}"


def build_watermark_filter_lines(
    wm_input_idx: int,
    base_v_label: str,
    out_v_label: str,
    position: str = "top-right",
    opacity: float = 0.85,
    margin: int = 40,
) -> list[str]:
    """Generate FFmpeg filter lines to adjust watermark opacity and overlay onto video.

    Args:
        wm_input_idx: FFmpeg input index for the watermark image (e.g., 2).
        base_v_label: Input video stream label (e.g., 'vconcat').
        out_v_label: Output video stream label (e.g., 'vout').
        position: Placement on canvas.
        opacity: Alpha multiplier between 0.0 and 1.0.
        margin: Edge margin in pixels.

    Returns:
        List of filter strings to include in filter_complex.
    """
    safe_opacity = max(0.0, min(1.0, float(opacity)))
    overlay_expr = compute_watermark_overlay_expr(position=position, margin=margin)

    alpha_filter = (
        f"[{wm_input_idx}:v]format=rgba,colorchannelmixer=aa={safe_opacity:.2f}[wm_alpha];"
    )
    overlay_filter = (
        f"[{base_v_label}][wm_alpha]overlay={overlay_expr}:shortest=1[{out_v_label}];"
    )

    return [alpha_filter, overlay_filter]
