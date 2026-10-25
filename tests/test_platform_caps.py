"""platform_caps: OS / encoders / hwaccels / torch / Resolve paths (no real ffmpeg)."""

from pathlib import PureWindowsPath

import pytest

from src.core import platform_caps as pc

ENCODERS_TXT = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V....D h264_qsv             H.264 (Intel Quick Sync Video acceleration) (codec h264)
 V....D h264_amf             AMD AMF H.264 Encoder (codec h264)
 V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
 V....D libx265              libx265 H.265 / HEVC (codec hevc)
 A....D aac                  AAC (Advanced Audio Coding)
"""
HWACCELS_TXT = "Hardware acceleration methods:\ncuda\ndxva2\nqsv\nd3d11va\n\n"


class _R:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_parse_encoders():
    assert pc.parse_ffmpeg_encoders(ENCODERS_TXT) == {
        "libx264", "h264_nvenc", "h264_qsv", "h264_amf", "hevc_nvenc", "libx265", "aac",
    }


def test_parse_hwaccels_keeps_order():
    assert pc.parse_ffmpeg_hwaccels(HWACCELS_TXT) == ("cuda", "dxva2", "qsv", "d3d11va")


@pytest.mark.parametrize(
    "raw, expected",
    [("win32", "windows"), ("cygwin", "windows"), ("darwin", "macos"), ("linux", "linux")],
)
def test_detect_os(raw, expected):
    assert pc.detect_os(raw) == expected


def test_detect_os_rejects_unknown():
    with pytest.raises(ValueError):
        pc.detect_os("sunos5")


def test_order_encoders_follows_ladder_and_os():
    avail = pc.parse_ffmpeg_encoders(ENCODERS_TXT) | {"h264_videotoolbox", "h264_vaapi"}
    assert pc.order_encoders(avail, pc.H264_LADDER, "windows") == (
        "h264_nvenc", "h264_qsv", "h264_amf", "libx264",
    )
    assert pc.order_encoders(avail, pc.H264_LADDER, "macos") == ("h264_videotoolbox", "libx264")
    assert pc.order_encoders(avail, pc.H264_LADDER, "linux") == (
        "h264_nvenc", "h264_qsv", "h264_vaapi", "libx264",
    )
    assert pc.order_encoders({"aac"}, pc.H264_LADDER, "windows") == ()


def test_pick_torch_device():
    assert pc.pick_torch_device(cuda=True, mps=False) == "cuda"
    assert pc.pick_torch_device(cuda=True, mps=True) == "cuda"
    assert pc.pick_torch_device(cuda=False, mps=True) == "mps"
    assert pc.pick_torch_device(cuda=False, mps=False) == "cpu"


def test_probe_smoke_tests_hardware_encoders_only():
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=None):
        calls.append(list(cmd))
        assert timeout is not None
        if "-encoders" in cmd:
            return _R(0, ENCODERS_TXT)
        if "-hwaccels" in cmd:
            return _R(0, HWACCELS_TXT)
        return _R(1 if ("h264_nvenc" in cmd or "hevc_nvenc" in cmd) else 0)

    caps = pc.probe(ffmpeg="ffmpeg", os_name="windows", run=fake_run, smoke_test=True,
                    torch_device="cpu")
    assert caps.os == "windows"
    assert caps.ffmpeg == "ffmpeg"
    assert caps.h264_encoders == ("h264_qsv", "h264_amf", "libx264")
    assert caps.hevc_encoders == ("libx265",)
    assert caps.hwaccels == ("cuda", "dxva2", "qsv", "d3d11va")
    assert caps.torch_device == "cpu"
    smoked = [c[c.index("-c:v") + 1] for c in calls if "-c:v" in c]
    assert "libx264" not in smoked and "libx265" not in smoked
    assert set(smoked) == {"h264_nvenc", "h264_qsv", "h264_amf", "hevc_nvenc"}


def test_probe_without_smoke_test_trusts_listing():
    def fake_run(cmd, timeout=None):
        if "-encoders" in cmd:
            return _R(0, ENCODERS_TXT)
        return _R(0, HWACCELS_TXT)

    caps = pc.probe(ffmpeg="ffmpeg", os_name="linux", run=fake_run, smoke_test=False,
                    torch_device="cuda")
    assert caps.h264_encoders == ("h264_nvenc", "h264_qsv", "libx264")
    assert caps.hevc_encoders == ("hevc_nvenc", "libx265")


def test_probe_without_ffmpeg():
    def boom(cmd, timeout=None):
        raise AssertionError("must not run anything without ffmpeg")

    caps = pc.probe(ffmpeg=None, os_name="linux", run=boom, smoke_test=True, torch_device="cpu")
    assert caps.ffmpeg is None
    assert (caps.h264_encoders, caps.hevc_encoders, caps.hwaccels) == ((), (), ())


def test_best_encoder_helpers():
    caps = pc.PlatformCaps(os="linux", ffmpeg="ffmpeg", h264_encoders=("h264_vaapi", "libx264"),
                           hevc_encoders=(), hwaccels=(), torch_device="cpu")
    assert caps.best_h264() == "h264_vaapi"
    assert caps.best_hevc() is None


@pytest.mark.parametrize(
    "os_name, lib_name",
    [("windows", "fusionscript.dll"), ("macos", "fusionscript.so"), ("linux", "fusionscript.so")],
)
def test_resolve_paths_per_os(os_name, lib_name):
    paths = pc.resolve_paths(os_name)
    # The paths are built for os_name, not the host; Windows name rules accept both separators.
    assert PureWindowsPath(str(paths.lib_path)).name == lib_name
    assert PureWindowsPath(str(paths.api_dir)).name == "Scripting"
    assert len(paths.exe_candidates) >= 1
