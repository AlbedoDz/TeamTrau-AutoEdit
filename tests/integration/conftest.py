"""Real-FFmpeg integration fixtures: synthesize tiny clips with lavfi sources.

These tests exercise the actual ffmpeg filter graphs (no subprocess mocks), so they
catch audio/video desync, missing streams, unicode paths and rotation issues.
"""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from src.core.ffmpeg_utils import ffprobe_json

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="ffmpeg/ffprobe not available on PATH")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
            if not (FFMPEG and FFPROBE):
                item.add_marker(skip)


def _make_clip(
    out: Path,
    duration: float = 2.0,
    audio: bool = True,
    sample_rate: int = 48000,
    channels: int = 2,
    rotation: int = 0,
    size: str = "160x90",
) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={duration}",
    ]
    if audio:
        layout = "stereo" if channels == 2 else "mono"
        cmd += [
            "-f", "lavfi",
            "-i", f"sine=frequency=440:sample_rate={sample_rate}:duration={duration}",
            "-af", f"aformat=channel_layouts={layout}",
        ]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    tmp = out.with_name(out.stem + "_tmp" + out.suffix) if rotation else out
    cmd.append(str(tmp))
    subprocess.run(cmd, check=True)
    if rotation:
        subprocess.run(
            [
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-display_rotation:v:0", str(rotation), "-i", str(tmp),
                "-c", "copy", str(out),
            ],
            check=True,
        )
        tmp.unlink(missing_ok=True)
    return out


@pytest.fixture
def make_clip(tmp_path: Path) -> Callable[..., Path]:
    def _factory(name: str = "clip.mp4", **kwargs) -> Path:
        return _make_clip(tmp_path / "footage" / name, **kwargs)

    return _factory


@pytest.fixture
def make_tone(tmp_path: Path) -> Callable[..., Path]:
    def _factory(name: str = "music.wav", duration: float = 6.0, sample_rate: int = 44100) -> Path:
        out = tmp_path / "audio" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate={sample_rate}:duration={duration}",
                "-af", "aformat=channel_layouts=stereo",
                str(out),
            ],
            check=True,
        )
        return out

    return _factory


def stream_durations(path: Path) -> dict[str, float]:
    """Return {'video': seconds, 'audio': seconds} measured by ffprobe."""
    data = ffprobe_json(path) or {}
    out: dict[str, float] = {}
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind in ("video", "audio") and kind not in out:
            dur = s.get("duration") or data.get("format", {}).get("duration") or 0.0
            out[kind] = float(dur)
    return out


def stream_info(path: Path, kind: str) -> dict:
    data = ffprobe_json(path) or {}
    for s in data.get("streams", []):
        if s.get("codec_type") == kind:
            return s
    return {}
