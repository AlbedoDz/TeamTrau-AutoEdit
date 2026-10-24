"""Proxy Generator for TeamTrau Engine & Studio Playback.

Generates standardized 8-bit H.264 4:2:0 proxies with CFR pacing, no B-frames (-bf 0),
AAC 48kHz stereo companion audio, and deterministic SHA-1 cache keying.
Encoder ladder: h264_nvenc -> libx264 -preset veryfast.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class ProxyGenerationError(RuntimeError):
    """Raised when proxy video encoding fails across all available encoders."""


def compute_proxy_key(
    src: Path | str,
    height: int = 1080,
    fps: float | None = None,
    gop: int = 1,
    *,
    size: int | None = None,
    mtime: float | int | None = None,
) -> str:
    """Compute 16-character hex SHA-1 cache key for proxy parameters.

    Formula: sha1(path|size|mtime|height|fps|gop)[:16]

    Args:
        src: Source video file path.
        height: Target proxy frame height (default: 1080).
        fps: Target frame rate, or None for source rate.
        gop: Group of pictures length (1 = all-intra).
        size: Optional byte size override (defaults to src.stat().st_size).
        mtime: Optional modification timestamp override (defaults to src.stat().st_mtime).

    Returns:
        16-character hexadecimal hash string.
    """
    path_obj = Path(src).resolve()
    resolved_path_str = str(path_obj)

    resolved_size = size
    if resolved_size is None and path_obj.exists():
        resolved_size = path_obj.stat().st_size
    elif resolved_size is None:
        resolved_size = 0

    resolved_mtime = mtime
    if resolved_mtime is None and path_obj.exists():
        resolved_mtime = path_obj.stat().st_mtime
    elif resolved_mtime is None:
        resolved_mtime = 0

    payload = f"{resolved_path_str}|{resolved_size}|{resolved_mtime}|{height}|{fps}|{gop}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def build_proxy_ffmpeg_cmd(
    src: Path | str,
    dst: Path | str,
    encoder: str,
    height: int = 1080,
    fps: float | None = None,
    gop: int = 1,
) -> list[str]:
    """Construct FFmpeg CLI argument list for proxy generation.

    Args:
        src: Source video file path.
        dst: Output proxy destination file path.
        encoder: Target encoder name ('h264_nvenc' or 'libx264').
        height: Target proxy frame height (even dimension scaling).
        fps: Optional target frame rate (CFR enforced).
        gop: Group of pictures length (1 = all-intra).

    Returns:
        List of command line argument strings for subprocess execution.
    """
    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-i",
        str(Path(src).resolve()),
        "-vf",
        f"scale=-2:{height}",
    ]

    if fps is not None:
        cmd.extend(["-r", str(fps)])

    cmd.extend([
        "-fps_mode",
        "cfr",
        "-pix_fmt",
        "yuv420p",
        "-bf",
        "0",
    ])

    if encoder == "h264_nvenc":
        # Note: In NVIDIA NVENC SDK, gopLength=0 specifies Intra-only encoding.
        # Passing -g 1 with -bf 0 triggers: 'InitializeEncoder failed: Gop Length should be greater than number of B frames + 1'
        nvenc_gop = 0 if gop == 1 else gop
        cmd.extend(["-c:v", "h264_nvenc", "-g", str(nvenc_gop)])
    elif encoder == "libx264":
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-g", str(gop)])
    else:
        cmd.extend(["-c:v", encoder, "-g", str(gop)])

    cmd.extend([
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(Path(dst).resolve()),
    ])

    return cmd


def make_proxy(
    src: Path | str,
    out_dir: Path | str,
    height: int = 1080,
    fps: float | None = None,
    gop: int = 1,
    force_encoder: str | None = None,
) -> Path:
    """Generate or retrieve a standardized H.264 proxy video.

    Executes encoder ladder: h264_nvenc -> libx264 -preset veryfast.

    Args:
        src: Source video file path.
        out_dir: Directory where proxy file will be stored.
        height: Proxy vertical resolution (default: 1080).
        fps: Target frame rate (default: None for source fps).
        gop: Group of pictures length (1 = all-intra, 6 = short GOP).
        force_encoder: Optional encoder override for testing/benchmarks.

    Returns:
        Path to the completed proxy MP4 file.

    Raises:
        FileNotFoundError: If source media does not exist.
        ProxyGenerationError: If encoding fails on all ladder tiers.
    """
    src_path = Path(src).resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"Source media not found: {src_path}")

    out_dir_path = Path(out_dir).resolve()
    out_dir_path.mkdir(parents=True, exist_ok=True)

    cache_key = compute_proxy_key(src_path, height=height, fps=fps, gop=gop)
    proxy_path = out_dir_path / f"{cache_key}.mp4"

    # Reuse existing non-empty proxy
    if proxy_path.exists() and proxy_path.stat().st_size > 0:
        logger.info("Proxy cache hit: %s", proxy_path)
        return proxy_path

    encoders = [force_encoder] if force_encoder else ["h264_nvenc", "libx264"]
    errors: list[str] = []

    for encoder in encoders:
        cmd = build_proxy_ffmpeg_cmd(
            src=src_path,
            dst=proxy_path,
            encoder=encoder,
            height=height,
            fps=fps,
            gop=gop,
        )
        logger.info("Attempting proxy encode with %s: %s", encoder, src_path.name)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode == 0 and proxy_path.exists() and proxy_path.stat().st_size > 0:
                logger.info("Proxy successfully generated with %s: %s", encoder, proxy_path)
                return proxy_path

            error_msg = f"Encoder '{encoder}' exited with code {result.returncode}:\n{result.stderr.strip()[-500:]}"
            logger.warning(error_msg)
            errors.append(error_msg)
            if proxy_path.exists():
                proxy_path.unlink(missing_ok=True)
        except Exception as exc:
            error_msg = f"Encoder '{encoder}' execution error: {exc}"
            logger.warning(error_msg)
            errors.append(error_msg)
            if proxy_path.exists():
                proxy_path.unlink(missing_ok=True)

    raise ProxyGenerationError(
        f"Failed to generate proxy for {src_path.name}. Ladder errors:\n" + "\n---\n".join(errors)
    )
