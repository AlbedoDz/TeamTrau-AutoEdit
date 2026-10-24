"""Unit tests for proxy generator module.

Validates cache key calculation, FFmpeg command construction, NVENC GOP mapping,
and encoder ladder fallback logic without executing real encoding operations.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.modules.proxy_generator import (
    ProxyGenerationError,
    build_proxy_ffmpeg_cmd,
    compute_proxy_key,
    make_proxy,
)


def test_compute_proxy_key_deterministic_and_length() -> None:
    """Validate 16-character hex SHA-1 format and determinism."""
    key1 = compute_proxy_key(
        src=Path("/virtual/media/clip1.mp4"),
        height=1080,
        fps=60.0,
        gop=1,
        size=100_000_000,
        mtime=1700000000.0,
    )
    key2 = compute_proxy_key(
        src=Path("/virtual/media/clip1.mp4"),
        height=1080,
        fps=60.0,
        gop=1,
        size=100_000_000,
        mtime=1700000000.0,
    )
    assert len(key1) == 16
    assert key1 == key2
    # Verify exact sha1 computation
    payload = f"{Path('/virtual/media/clip1.mp4').resolve()}|100000000|1700000000.0|1080|60.0|1"
    expected = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    assert key1 == expected


def test_compute_proxy_key_sensitivity() -> None:
    """Validate cache key changes when any parameter is varied."""
    base_args = {
        "src": Path("/virtual/media/clip1.mp4"),
        "height": 1080,
        "fps": 60.0,
        "gop": 1,
        "size": 100_000_000,
        "mtime": 1700000000.0,
    }
    base_key = compute_proxy_key(**base_args)

    # Different height
    assert compute_proxy_key(**{**base_args, "height": 720}) != base_key
    # Different fps
    assert compute_proxy_key(**{**base_args, "fps": 30.0}) != base_key
    # Different gop
    assert compute_proxy_key(**{**base_args, "gop": 6}) != base_key
    # Different size
    assert compute_proxy_key(**{**base_args, "size": 100_000_001}) != base_key
    # Different mtime
    assert compute_proxy_key(**{**base_args, "mtime": 1700000001.0}) != base_key


def test_build_proxy_ffmpeg_cmd_nvenc_allintra() -> None:
    """Verify NVENC command with gop=1 maps to -g 0 (all-intra) and -bf 0."""
    cmd = build_proxy_ffmpeg_cmd(
        src=Path("input.mov"),
        dst=Path("output.mp4"),
        encoder="h264_nvenc",
        height=1080,
        fps=60.0,
        gop=1,
    )

    assert "ffmpeg" == cmd[0]
    assert "-vf" in cmd
    vf_idx = cmd.index("-vf")
    assert cmd[vf_idx + 1] == "scale=-2:1080"
    assert "-r" in cmd
    r_idx = cmd.index("-r")
    assert cmd[r_idx + 1] == "60.0"
    assert "-fps_mode" in cmd and cmd[cmd.index("-fps_mode") + 1] == "cfr"
    assert "-pix_fmt" in cmd and cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert "-bf" in cmd and cmd[cmd.index("-bf") + 1] == "0"
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "h264_nvenc"
    # Crucial poka-yoke check: NVENC with gop=1 must map to -g 0 to prevent SDK invalid param error
    assert "-g" in cmd and cmd[cmd.index("-g") + 1] == "0"
    assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0:v:0"
    assert "-map" in cmd and cmd[cmd.index("-map") + 3] == "0:a?"
    assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "aac"
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "48000"
    assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "2"


def test_build_proxy_ffmpeg_cmd_nvenc_gop6() -> None:
    """Verify NVENC command with gop=6 preserves -g 6."""
    cmd = build_proxy_ffmpeg_cmd(
        src=Path("input.mov"),
        dst=Path("output.mp4"),
        encoder="h264_nvenc",
        height=720,
        fps=None,
        gop=6,
    )
    assert "-r" not in cmd  # No -r when fps is None
    assert "-g" in cmd and cmd[cmd.index("-g") + 1] == "6"
    assert cmd[cmd.index("-vf") + 1] == "scale=-2:720"


def test_build_proxy_ffmpeg_cmd_libx264() -> None:
    """Verify libx264 command includes preset veryfast and verbatim gop."""
    cmd_intra = build_proxy_ffmpeg_cmd(
        src=Path("input.mov"),
        dst=Path("output.mp4"),
        encoder="libx264",
        height=1080,
        fps=30.0,
        gop=1,
    )
    assert "-c:v" in cmd_intra and cmd_intra[cmd_intra.index("-c:v") + 1] == "libx264"
    assert "-preset" in cmd_intra and cmd_intra[cmd_intra.index("-preset") + 1] == "veryfast"
    assert "-g" in cmd_intra and cmd_intra[cmd_intra.index("-g") + 1] == "1"

    cmd_gop6 = build_proxy_ffmpeg_cmd(
        src=Path("input.mov"),
        dst=Path("output.mp4"),
        encoder="libx264",
        height=1080,
        fps=30.0,
        gop=6,
    )
    assert "-g" in cmd_gop6 and cmd_gop6[cmd_gop6.index("-g") + 1] == "6"


def test_make_proxy_file_not_found(tmp_path: Path) -> None:
    """Verify FileNotFoundError is raised for non-existent source."""
    with pytest.raises(FileNotFoundError, match="Source media not found"):
        make_proxy(src=tmp_path / "non_existent.mp4", out_dir=tmp_path)


def test_make_proxy_cache_hit(tmp_path: Path) -> None:
    """Verify cache hit returns existing file without invoking subprocess."""
    src_file = tmp_path / "sample.mp4"
    src_file.write_bytes(b"dummy source bytes")

    cache_key = compute_proxy_key(src_file, height=1080, fps=None, gop=1)
    proxy_cached = tmp_path / "proxies" / f"{cache_key}.mp4"
    proxy_cached.parent.mkdir(parents=True, exist_ok=True)
    proxy_cached.write_bytes(b"cached proxy bytes")

    with patch("subprocess.run") as mock_run:
        result = make_proxy(src_file, out_dir=tmp_path / "proxies", height=1080, gop=1)
        assert result == proxy_cached
        mock_run.assert_not_called()


def test_make_proxy_ladder_fallback(tmp_path: Path) -> None:
    """Verify ladder falls back to libx264 when h264_nvenc fails."""
    src_file = tmp_path / "sample.mp4"
    src_file.write_bytes(b"dummy source bytes")
    out_dir = tmp_path / "proxies"

    cache_key = compute_proxy_key(src_file, height=1080, fps=None, gop=1)
    expected_dst = out_dir / f"{cache_key}.mp4"

    def fake_subprocess_run(cmd: list[str], **kwargs: object) -> MagicMock:
        mock_res = MagicMock()
        if "h264_nvenc" in cmd:
            mock_res.returncode = 1
            mock_res.stderr = "CUDA out of memory"
        elif "libx264" in cmd:
            mock_res.returncode = 0
            # Create the file as if ffmpeg wrote it
            expected_dst.write_bytes(b"simulated proxy content")
        return mock_res

    with patch("subprocess.run", side_effect=fake_subprocess_run) as mock_run:
        result = make_proxy(src_file, out_dir=out_dir, height=1080, gop=1)
        assert result == expected_dst
        assert mock_run.call_count == 2
        first_call_cmd = mock_run.call_args_list[0][0][0]
        second_call_cmd = mock_run.call_args_list[1][0][0]
        assert "h264_nvenc" in first_call_cmd
        assert "libx264" in second_call_cmd


def test_make_proxy_ladder_exhausted(tmp_path: Path) -> None:
    """Verify ProxyGenerationError raised when both encoders fail."""
    src_file = tmp_path / "sample.mp4"
    src_file.write_bytes(b"dummy source bytes")
    out_dir = tmp_path / "proxies"

    fail_mock = MagicMock()
    fail_mock.returncode = 1
    fail_mock.stderr = "Fatal error"

    with patch("subprocess.run", return_value=fail_mock):
        with pytest.raises(ProxyGenerationError, match="Failed to generate proxy"):
            make_proxy(src_file, out_dir=out_dir, height=1080, gop=1)
