"""Platform capabilities detection: OS, GPU, hardware encoders/decoders, Resolve paths."""

from __future__ import annotations

import functools
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from src.core.ffmpeg_utils import run_tool

logger = logging.getLogger(__name__)

OsName = Literal["windows", "macos", "linux"]
TorchDevice = Literal["cuda", "mps", "cpu"]

H264_LADDER: tuple[str, ...] = (
    "h264_nvenc",
    "h264_videotoolbox",
    "h264_qsv",
    "h264_amf",
    "h264_vaapi",
    "libx264",
)
HEVC_LADDER: tuple[str, ...] = (
    "hevc_nvenc",
    "hevc_videotoolbox",
    "hevc_qsv",
    "hevc_amf",
    "hevc_vaapi",
    "libx265",
)

_USE_DEFAULT: Any = object()


class _CallableEncoder(str):
    def __call__(self) -> str:
        return str(self)


@dataclass(frozen=True)
class PlatformCaps:
    """Hardware and platform encoding capabilities."""

    os: OsName
    ffmpeg: str | None
    h264_encoders: tuple[str, ...]
    hevc_encoders: tuple[str, ...]
    hwaccels: tuple[str, ...]
    torch_device: TorchDevice

    @property
    def best_h264(self) -> Any:
        """Return the highest-priority usable H.264 encoder, or None."""
        return _CallableEncoder(self.h264_encoders[0]) if self.h264_encoders else None

    def best_hevc(self) -> str | None:
        """Return the highest-priority usable HEVC encoder, or None."""
        return self.hevc_encoders[0] if self.hevc_encoders else None


@dataclass(frozen=True)
class ResolvePaths:
    """DaVinci Resolve scripting and binary paths for the current OS."""

    api_dir: Path
    lib_path: Path
    exe_candidates: tuple[Path, ...]


def detect_os(platform_name: str | None = None) -> OsName:
    """Detect normalised OS name ('windows', 'macos', 'linux') or raise ValueError."""
    name = sys.platform if platform_name is None else platform_name
    if name in ("win32", "cygwin"):
        return "windows"
    if name == "darwin":
        return "macos"
    if name.startswith("linux"):
        return "linux"
    raise ValueError(f"Unsupported OS/platform: {name}")


def parse_ffmpeg_encoders(text: str) -> set[str]:
    """Parse available encoder names from ffmpeg -encoders output."""
    encoders: set[str] = set()
    after_sep = False
    for line in text.splitlines():
        line_clean = line.strip()
        if not after_sep:
            if "------" in line_clean:
                after_sep = True
            continue
        parts = line.split()
        if len(parts) >= 2:
            encoders.add(parts[1])
    return encoders


def parse_ffmpeg_hwaccels(text: str) -> tuple[str, ...]:
    """Parse hardware acceleration methods from ffmpeg -hwaccels output."""
    methods: list[str] = []
    after_header = False
    for line in text.splitlines():
        stripped = line.strip()
        if not after_header:
            if "Hardware acceleration methods:" in stripped:
                after_header = True
            continue
        if stripped:
            methods.append(stripped)
    return tuple(methods)


def _is_encoder_allowed_on_os(enc: str, os_name: OsName) -> bool:
    """Determine whether an encoder backend is supported on the target OS."""
    if enc.endswith("_nvenc"):
        return os_name in ("windows", "linux")
    if enc.endswith("_videotoolbox"):
        return os_name == "macos"
    if enc.endswith("_qsv"):
        return os_name in ("windows", "linux")
    if enc.endswith("_amf"):
        return os_name == "windows"
    if enc.endswith("_vaapi"):
        return os_name == "linux"
    if enc in ("libx264", "libx265"):
        return True
    return False


def order_encoders(
    available: set[str],
    ladder: tuple[str, ...],
    os_name: OsName,
) -> tuple[str, ...]:
    """Filter and sort available encoders according to preference ladder and OS compatibility."""
    ordered: list[str] = []
    for enc in ladder:
        if enc in available and _is_encoder_allowed_on_os(enc, os_name):
            ordered.append(enc)
    return tuple(ordered)


def pick_torch_device(cuda: bool, mps: bool) -> TorchDevice:
    """Select the best PyTorch compute device ('cuda' > 'mps' > 'cpu')."""
    if cuda:
        return "cuda"
    if mps:
        return "mps"
    return "cpu"


def resolve_paths(os_name: OsName) -> ResolvePaths:
    """Return standard DaVinci Resolve scripting and executable paths for the given OS."""
    if os_name == "windows":
        api_dir = Path(
            r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
        )
        lib_path = Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll")
        exe_candidates = (
            Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"),
            Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve Studio\Resolve.exe"),
        )
    elif os_name == "macos":
        api_dir = Path(
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
        )
        lib_path = Path(
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
        )
        exe_candidates = (
            Path("/Applications/DaVinci Resolve/DaVinci Resolve.app"),
        )
    elif os_name == "linux":
        api_dir = Path("/opt/resolve/Developer/Scripting")
        lib_path = Path("/opt/resolve/libs/Fusion/fusionscript.so")
        exe_candidates = (
            Path("/opt/resolve/bin/resolve"),
        )
    else:
        raise ValueError(f"Unsupported OS for Resolve paths: {os_name}")

    return ResolvePaths(
        api_dir=api_dir,
        lib_path=lib_path,
        exe_candidates=exe_candidates,
    )


def probe(
    ffmpeg: str | None = _USE_DEFAULT,
    os_name: OsName | None = None,
    run: Callable[..., Any] = run_tool,
    smoke_test: bool = True,
    torch_device: TorchDevice | None = None,
) -> PlatformCaps:
    """Probe OS, FFmpeg encoders/hwaccels, and PyTorch device capabilities."""
    actual_os: OsName = detect_os() if os_name is None else os_name
    actual_ffmpeg: str | None = shutil.which("ffmpeg") if ffmpeg is _USE_DEFAULT else ffmpeg

    if torch_device is None:
        try:
            import torch

            cuda_avail = bool(torch.cuda.is_available())
            mps_avail = bool(hasattr(torch.backends, "mps") and torch.backends.mps.is_available())
            torch_device = pick_torch_device(cuda=cuda_avail, mps=mps_avail)
        except Exception:
            torch_device = "cpu"

    if actual_ffmpeg is None:
        return PlatformCaps(
            os=actual_os,
            ffmpeg=None,
            h264_encoders=(),
            hevc_encoders=(),
            hwaccels=(),
            torch_device=torch_device,
        )

    try:
        encoders_res = run([actual_ffmpeg, "-hide_banner", "-encoders"], timeout=15)
        raw_encoders = (
            parse_ffmpeg_encoders(
                encoders_res.stdout
                if isinstance(encoders_res.stdout, str)
                else encoders_res.stdout.decode("utf-8", "replace")
            )
            if getattr(encoders_res, "returncode", 1) == 0
            else set()
        )
    except (subprocess.TimeoutExpired, OSError):
        raw_encoders = set()

    try:
        hwaccels_res = run([actual_ffmpeg, "-hide_banner", "-hwaccels"], timeout=15)
        raw_hwaccels = (
            parse_ffmpeg_hwaccels(
                hwaccels_res.stdout
                if isinstance(hwaccels_res.stdout, str)
                else hwaccels_res.stdout.decode("utf-8", "replace")
            )
            if getattr(hwaccels_res, "returncode", 1) == 0
            else ()
        )
    except (subprocess.TimeoutExpired, OSError):
        raw_hwaccels = ()

    h264_candidates = order_encoders(raw_encoders, H264_LADDER, actual_os)
    hevc_candidates = order_encoders(raw_encoders, HEVC_LADDER, actual_os)

    if not smoke_test:
        return PlatformCaps(
            os=actual_os,
            ffmpeg=actual_ffmpeg,
            h264_encoders=h264_candidates,
            hevc_encoders=hevc_candidates,
            hwaccels=raw_hwaccels,
            torch_device=torch_device,
        )

    def _smoke_test_candidate(enc: str) -> bool:
        if enc in ("libx264", "libx265"):
            return True
        smoke_cmd = [
            actual_ffmpeg,
            "-hide_banner",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=256x144:d=0.1",
            "-frames:v",
            "1",
            "-c:v",
            enc,
            "-f",
            "null",
            "-",
        ]
        try:
            res = run(smoke_cmd, timeout=20)
            return getattr(res, "returncode", 1) == 0
        except Exception:
            return False

    h264_encoders = tuple(enc for enc in h264_candidates if _smoke_test_candidate(enc))
    hevc_encoders = tuple(enc for enc in hevc_candidates if _smoke_test_candidate(enc))

    return PlatformCaps(
        os=actual_os,
        ffmpeg=actual_ffmpeg,
        h264_encoders=h264_encoders,
        hevc_encoders=hevc_encoders,
        hwaccels=raw_hwaccels,
        torch_device=torch_device,
    )


@functools.cache
def get_caps() -> PlatformCaps:
    """Return cached platform capabilities."""
    return probe()
