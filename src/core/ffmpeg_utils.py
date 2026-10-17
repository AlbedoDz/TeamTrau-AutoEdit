"""Shared FFmpeg/FFprobe subprocess helpers.

Every call decodes tool output as UTF-8 (with replacement) so that non-ASCII
file names (e.g. Vietnamese) echoed by ffmpeg on stderr never raise
UnicodeDecodeError under the Windows default cp1252 locale.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TEXT_KWARGS: dict[str, Any] = {"text": True, "encoding": "utf-8", "errors": "replace"}


def run_tool(
    cmd: list[str], timeout: float | None = None, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run an ffmpeg-family command capturing UTF-8 decoded stdout/stderr."""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=check,
        **TEXT_KWARGS,
    )


def popen_tool(cmd: list[str]) -> subprocess.Popen[str]:
    """Spawn an ffmpeg-family command with line-buffered UTF-8 pipes."""
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        **TEXT_KWARGS,
    )


def ffprobe_json(path: Path, timeout: float = 30.0) -> dict[str, Any] | None:
    """Return ffprobe streams+format JSON for a media file, or None on failure."""
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin or not Path(path).is_file():
        return None
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        res = run_tool(cmd, timeout=timeout)
        if res.returncode != 0 or not res.stdout:
            return None
        data = json.loads(res.stdout)
        return data if isinstance(data, dict) else None
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        logger.debug("ffprobe failed for %s: %s", path, exc)
        return None


def has_audio_stream(path: Path) -> bool:
    """Whether a media file contains at least one audio stream (cached by size+mtime)."""
    from src.core.media_info import probe_media  # local import: media_info builds on this module

    return probe_media(path).has_audio


def stream_rotation(stream: dict[str, Any]) -> int:
    """Extract display rotation in degrees (0/90/180/270) from an ffprobe video stream."""
    rot = 0.0
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                rot = float(sd["rotation"])
            except (TypeError, ValueError):
                pass
    tags = stream.get("tags", {}) or {}
    if not rot and "rotate" in tags:
        try:
            rot = float(tags["rotate"])
        except (TypeError, ValueError):
            pass
    return int(round(rot)) % 360


def atempo_chain(speed: float) -> str:
    """Build an atempo filter chain for any speed (each atempo stage must be in 0.5..2.0)."""
    if abs(speed - 1.0) < 1e-6:
        return ""
    factors: list[float] = []
    remaining = float(speed)
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={f:.6f}" for f in factors)


NVENC_ERROR_MARKERS: tuple[str, ...] = (
    "nvenc",
    "cuda",
    "no capable devices found",
    "cannot load nvcuda",
    "openencodesessionex",
    "nvidia",
)


def is_hw_encoder_error(stderr_text: str) -> bool:
    """Heuristic: does the ffmpeg stderr indicate a hardware encoder/driver failure?"""
    low = stderr_text.lower()
    return any(m in low for m in NVENC_ERROR_MARKERS)
