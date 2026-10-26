"""GPU-built analysis proxies: frame-accurate, 1920-wide copies of large footage for analysis.

Decoding 4K (often 10-bit HEVC) on the CPU is the slowest part of curation, and every analysis
pass (scene detection, motion, defect pruning, quality curves, scoring, keyframes) decodes the
clip again. Those passes all shrink frames to at most 1920 px before measuring anything, so
they get the same answers from a 1920-wide copy with the same frames and timestamps, which the
CPU decodes about five times faster. The copy is made once per file on the GPU (NVDEC decode,
``scale_cuda``, NVENC) with a CPU fallback, cached by path, size and mtime.

Only analysis reads go through :func:`analysis_path`: timelines, renders and NLE exports keep
the original media, and :func:`source_size` gives readers the original frame size for any
pixel maths (crop windows).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from src.core.ffmpeg_utils import ffprobe_json, run_tool, stream_rotation

logger = logging.getLogger(__name__)

PROXY_WIDTH = 1920


@dataclass(frozen=True)
class AnalysisProxy:
    original: Path
    proxy: Path
    width: int  # original frame size, for pixel maths in source coordinates
    height: int
    encoder: str


_REGISTRY: dict[Path, AnalysisProxy] = {}
_LOCK = threading.Lock()


def analysis_path(path: Path | str) -> Path:
    """The file an analysis pass should decode for ``path`` (its proxy, or itself)."""
    p = Path(path)
    with _LOCK:
        entry = _REGISTRY.get(p.resolve())
    return entry.proxy if entry is not None else p


def source_size(path: Path | str) -> tuple[int, int] | None:
    """Original frame size of a file that has an analysis proxy, else ``None``."""
    with _LOCK:
        entry = _REGISTRY.get(Path(path).resolve())
    return (entry.width, entry.height) if entry is not None else None


def clear() -> None:
    with _LOCK:
        _REGISTRY.clear()


def _video_size(path: Path) -> tuple[int, int] | None:
    """Display size (rotation applied), the size OpenCV hands frames back in."""
    data = ffprobe_json(path) or {}
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and s.get("width") and s.get("height"):
            w, h = int(s["width"]), int(s["height"])
            return (h, w) if stream_rotation(s) in (90, 270) else (w, h)
    return None


def _cache_path(src: Path, cache_dir: Path, width: int) -> Path:
    st = src.stat()
    key = f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|{width}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return cache_dir / "analysis_proxy" / f"{src.stem}_{h}_w{width}.mp4"


def _commands(ffmpeg: str, src: Path, out: Path, width: int) -> list[tuple[str, list[str]]]:
    """GPU first (NVDEC + scale_cuda + NVENC), then CPU. Frames and timestamps pass through."""
    common_out = ["-map", "0:v:0", "-an", "-sn", "-dn", "-fps_mode", "passthrough", "-g", "30"]
    return [
        (
            "h264_nvenc",
            [
                ffmpeg, "-y", "-hide_banner", "-nostdin", "-v", "error",
                "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(src),
                "-vf", f"scale_cuda={width}:-2:format=nv12",
                "-c:v", "h264_nvenc", "-preset", "p1", "-rc", "constqp", "-qp", "20",
                *common_out, str(out),
            ],
        ),
        (
            "libx264",
            [
                ffmpeg, "-y", "-hide_banner", "-nostdin", "-v", "error", "-i", str(src),
                "-vf", f"scale={width}:-2:flags=area,format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                *common_out, str(out),
            ],
        ),
    ]


def build_analysis_proxy(
    src: Path,
    cache_dir: Path,
    width: int = PROXY_WIDTH,
    ffmpeg: str | None = None,
) -> AnalysisProxy | None:
    """Make (or reuse) the proxy of ``src``; ``None`` when it is not needed or cannot be made."""
    size = _video_size(src)
    if size is None or size[0] <= width:
        return None
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    out = _cache_path(src, Path(cache_dir), width)
    if out.is_file() and out.stat().st_size > 0:
        return AnalysisProxy(src, out, size[0], size[1], "cached")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.part{out.suffix}")
    try:
        for encoder, cmd in _commands(ffmpeg, src, tmp, width):
            res = run_tool(cmd)
            if res.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                os.replace(tmp, out)
                return AnalysisProxy(src, out, size[0], size[1], encoder)
            logger.info("Analysis proxy via %s failed for %s: %s", encoder, src.name, (res.stderr or "")[-300:])
    finally:
        tmp.unlink(missing_ok=True)
    logger.warning("No analysis proxy for %s; analysis decodes the original", src.name)
    return None


def prepare_analysis_proxies(
    sources: Iterable[Path],
    cache_dir: Path,
    width: int = PROXY_WIDTH,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[AnalysisProxy]:
    """Build and register proxies for the sources wider than ``width``."""
    items = list(sources)
    made: list[AnalysisProxy] = []
    for i, src in enumerate(items, start=1):
        if on_progress is not None:
            on_progress(i, len(items), src.name)
        try:
            entry = build_analysis_proxy(src, cache_dir, width)
        except Exception as exc:  # a proxy only speeds analysis up; never fail curation over it
            logger.warning("Analysis proxy skipped for %s: %s", src.name, exc)
            entry = None
        if entry is not None:
            with _LOCK:
                _REGISTRY[src.resolve()] = entry
            made.append(entry)
    return made
