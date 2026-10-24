"""Production proxy generator: heuristics, cache key, commands, and encoder fallback."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.ffmpeg_utils import run_tool
from src.core.media_info import MediaInfo
from src.core.platform_caps import get_caps

logger = logging.getLogger(__name__)

_HEAVY_CODECS: frozenset[str] = frozenset({"hevc", "prores", "dnxhd", "av1", "vp9"})


class ProxyGenerationError(RuntimeError):
    """Raised when proxy video encoding fails across all tried encoders."""


@dataclass(frozen=True)
class ProxySpec:
    """Target proxy encoding parameters."""

    height: int = 1080
    fps: float | None = None  # None = keep source rate (still CFR)
    gop: int = 1  # 1 = all-intra


def needs_proxy(info: MediaInfo, max_side: int = 1920) -> list[str]:
    """Evaluate whether source media requires an intermediate proxy.

    Returns reasons in strict priority order:
    1. 'resolution' (max side > max_side)
    2. 'codec:<codec>' for heavy editing codecs (hevc, prores, dnxhd, av1, vp9)
    3. 'bit_depth' (bit_depth > 8)
    4. 'chroma' (pix_fmt contains 422 or 444)
    5. 'high_fps' (fps > 60.5)
    6. 'vfr' (variable frame rate)
    7. 'log' (logarithmic gamma / wide gamut profile)
    """
    reasons: list[str] = []

    width = info.width or 0
    height = info.height or 0
    if max(width, height) > max_side:
        reasons.append("resolution")

    if info.codec:
        codec_lower = info.codec.lower()
        if codec_lower in _HEAVY_CODECS:
            reasons.append(f"codec:{codec_lower}")

    if info.bit_depth is not None and info.bit_depth > 8:
        reasons.append("bit_depth")

    if info.pix_fmt and ("422" in info.pix_fmt or "444" in info.pix_fmt):
        reasons.append("chroma")

    if info.fps is not None and float(info.fps) > 60.5:
        reasons.append("high_fps")

    if info.is_vfr:
        reasons.append("vfr")

    if info.is_log:
        reasons.append("log")

    return reasons


def compute_proxy_key(
    src: Path | str,
    spec: ProxySpec = ProxySpec(),
    *,
    size: int | None = None,
    mtime_ns: int | None = None,
    **kwargs: Any,
) -> str:
    """Compute 16-character hex SHA-1 cache key for proxy parameters.

    Formula: sha1(f"{resolved path}|{size}|{mtime_ns}|{height}|{fps}|{gop}")[:16]
    """
    if kwargs:
        height = kwargs.get("height", spec.height)
        fps = kwargs.get("fps", spec.fps)
        gop = kwargs.get("gop", spec.gop)
        spec = ProxySpec(height=height, fps=fps, gop=gop)
        if mtime_ns is None and "mtime" in kwargs:
            mtime_val = kwargs["mtime"]
            mtime_ns = int(mtime_val * 1_000_000_000) if isinstance(mtime_val, float) else int(mtime_val)

    path_obj = Path(src).resolve()

    resolved_size = size
    resolved_mtime_ns = mtime_ns
    if resolved_size is None or resolved_mtime_ns is None:
        try:
            st = path_obj.stat()
            if resolved_size is None:
                resolved_size = st.st_size
            if resolved_mtime_ns is None:
                resolved_mtime_ns = st.st_mtime_ns
        except OSError:
            if resolved_size is None:
                resolved_size = 0
            if resolved_mtime_ns is None:
                resolved_mtime_ns = 0

    payload = (
        f"{path_obj}|{resolved_size}|{resolved_mtime_ns}|{spec.height}|{spec.fps}|{spec.gop}"
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def build_proxy_cmd(
    src: Path | str,
    dst_part: Path | str,
    encoder: str,
    spec: ProxySpec,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Construct FFmpeg CLI argument list for atomic proxy generation."""
    cmd: list[str] = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(src),
        "-vf",
        f"scale=-2:{spec.height}",
    ]

    if spec.fps is not None:
        cmd.extend(["-r", f"{spec.fps:g}"])

    cmd.extend(["-fps_mode", "cfr", "-pix_fmt", "yuv420p", "-bf", "0"])

    if encoder == "h264_nvenc":
        gop_str = "0" if spec.gop == 1 else str(spec.gop)
        cmd.extend([
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-rc",
            "vbr",
            "-cq",
            "23",
            "-g",
            gop_str,
        ])
    elif encoder == "libx264":
        cmd.extend([
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-g",
            str(spec.gop),
        ])
    else:
        cmd.extend([
            "-c:v",
            encoder,
            "-g",
            str(spec.gop),
            "-b:v",
            "20M",
        ])

    cmd.extend([
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(dst_part),
    ])

    return cmd


def build_proxy_ffmpeg_cmd(
    src: Path | str,
    dst: Path | str,
    encoder: str,
    height: int = 1080,
    fps: float | None = None,
    gop: int = 1,
) -> list[str]:
    """Compatibility wrapper for spike benchmarks."""
    return build_proxy_cmd(src, dst, encoder, ProxySpec(height=height, fps=fps, gop=gop))


def make_proxy(
    src: Path | str,
    out_dir: Path | str,
    spec: ProxySpec = ProxySpec(),
    *,
    encoders: Sequence[str] | None = None,
    run: Any = run_tool,
    ffmpeg: str | None = None,
    timeout: float = 3600.0,
) -> Path:
    """Generate or retrieve a standardized H.264 proxy video with atomic replace."""
    src_path = Path(src).resolve()
    out_dir_path = Path(out_dir).resolve()
    out_dir_path.mkdir(parents=True, exist_ok=True)

    cache_key = compute_proxy_key(src_path, spec)
    final_path = out_dir_path / f"{cache_key}.mp4"

    if final_path.exists():
        logger.debug("Proxy cache hit: %s", final_path)
        return final_path

    if encoders is None:
        try:
            encoders_to_try: tuple[str, ...] = get_caps().h264_encoders or ("libx264",)
        except Exception as exc:
            logger.warning("Failed to query platform caps for h264 encoders: %s", exc)
            encoders_to_try = ("libx264",)
    else:
        encoders_to_try = tuple(encoders)

    ffmpeg_bin = ffmpeg if ffmpeg is not None else (shutil.which("ffmpeg") or "ffmpeg")
    part_path = out_dir_path / f"{cache_key}.mp4.part"
    encoder_errors: list[str] = []

    for enc in encoders_to_try:
        if part_path.exists():
            part_path.unlink(missing_ok=True)

        cmd = build_proxy_cmd(
            src=src_path,
            dst_part=part_path,
            encoder=enc,
            spec=spec,
            ffmpeg=ffmpeg_bin,
        )
        logger.info("Attempting proxy encode with %s: %s", enc, src_path.name)
        try:
            res = run(cmd, timeout=timeout)
            if res.returncode == 0 and part_path.exists():
                os.replace(part_path, final_path)
                logger.info("Proxy successfully generated with %s: %s", enc, final_path)
                return final_path

            stderr = getattr(res, "stderr", "") or ""
            last_line = (
                stderr.strip().splitlines()[-1]
                if stderr.strip()
                else f"process returned code {res.returncode}"
            )
            encoder_errors.append(f"{enc}: {last_line}")
        except Exception as exc:
            encoder_errors.append(f"{enc}: {exc}")
        finally:
            if not final_path.exists() and part_path.exists():
                part_path.unlink(missing_ok=True)

    error_summary = "; ".join(encoder_errors)
    raise ProxyGenerationError(
        f"Proxy generation failed for '{src_path.name}' across encoders {encoders_to_try}: {error_summary}"
    )
