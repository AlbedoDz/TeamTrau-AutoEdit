"""Production proxy generator: heuristics, cache key, commands, encoder fallback."""

from fractions import Fraction
from pathlib import Path

import pytest

from src.core.media_info import MediaInfo
from src.modules import proxy_generator as pg


class _R:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


def info(**kw) -> MediaInfo:
    base = dict(path=Path("x.mp4"), has_video=True, width=1920, height=1080, fps=Fraction(30),
                codec="h264", pix_fmt="yuv420p", bit_depth=8)
    base.update(kw)
    return MediaInfo(**base)


def _has(cmd: list[str], pair: list[str]) -> bool:
    return any(cmd[i:i + len(pair)] == pair for i in range(len(cmd)))


def test_light_1080p_h264_needs_no_proxy():
    assert pg.needs_proxy(info()) == []


@pytest.mark.parametrize(
    "kw, reason",
    [
        (dict(width=3840, height=2160), "resolution"),
        (dict(width=2160, height=3840), "resolution"),
        (dict(codec="hevc"), "codec:hevc"),
        (dict(codec="prores"), "codec:prores"),
        (dict(bit_depth=10, pix_fmt="yuv420p10le"), "bit_depth"),
        (dict(bit_depth=10, pix_fmt="yuv422p10le"), "chroma"),
        (dict(fps=Fraction(120000, 1001)), "high_fps"),
        (dict(is_vfr=True), "vfr"),
        (dict(is_log=True), "log"),
    ],
)
def test_heavy_media_reasons(kw, reason):
    assert reason in pg.needs_proxy(info(**kw))


def test_reason_order():
    heavy = info(width=3840, height=2160, codec="hevc", bit_depth=10, pix_fmt="yuv422p10le")
    assert pg.needs_proxy(heavy) == ["resolution", "codec:hevc", "bit_depth", "chroma"]


def test_key_is_stable_and_parameter_sensitive(tmp_path: Path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    k = pg.compute_proxy_key(f)
    assert len(k) == 16 and int(k, 16) >= 0
    assert pg.compute_proxy_key(f) == k
    assert pg.compute_proxy_key(f, pg.ProxySpec(height=720)) != k
    assert pg.compute_proxy_key(f, pg.ProxySpec(gop=6)) != k
    assert pg.compute_proxy_key(f, pg.ProxySpec(fps=60.0)) != k
    assert pg.compute_proxy_key(f, size=999) != k
    assert pg.compute_proxy_key(f, mtime_ns=1) != k


def test_nvenc_intra_command(tmp_path: Path):
    cmd = pg.build_proxy_cmd(tmp_path / "in.mov", tmp_path / "out.mp4.part", "h264_nvenc",
                             pg.ProxySpec(fps=60.0))
    assert cmd[0] == "ffmpeg"
    assert "scale=-2:1080" in cmd
    for pair in (["-c:v", "h264_nvenc"], ["-g", "0"], ["-bf", "0"], ["-pix_fmt", "yuv420p"],
                 ["-fps_mode", "cfr"], ["-r", "60"], ["-c:a", "aac"], ["-ar", "48000"],
                 ["-ac", "2"], ["-f", "mp4"]):
        assert _has(cmd, pair), pair
    assert cmd[-1].endswith("out.mp4.part")


def test_libx264_and_gop_variants(tmp_path: Path):
    cpu = pg.build_proxy_cmd(tmp_path / "in.mov", tmp_path / "o.part", "libx264", pg.ProxySpec())
    assert _has(cpu, ["-c:v", "libx264"]) and _has(cpu, ["-g", "1"]) and _has(cpu, ["-bf", "0"])
    assert _has(cpu, ["-preset", "veryfast"])
    assert "-r" not in cpu and _has(cpu, ["-fps_mode", "cfr"])
    gop6 = pg.build_proxy_cmd(tmp_path / "in.mov", tmp_path / "o.part", "h264_nvenc",
                              pg.ProxySpec(height=720, gop=6))
    assert _has(gop6, ["-g", "6"]) and "scale=-2:720" in gop6


def test_make_proxy_falls_back_and_renames(tmp_path: Path):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"v")
    tried: list[str] = []

    def fake_run(cmd, timeout=None):
        assert timeout is not None
        tried.append(cmd[cmd.index("-c:v") + 1])
        if "h264_nvenc" in cmd:
            return _R(1, "nvenc not available")
        Path(cmd[-1]).write_bytes(b"proxy")
        return _R(0)

    out = pg.make_proxy(src, tmp_path / "px", encoders=("h264_nvenc", "libx264"), run=fake_run)
    assert tried == ["h264_nvenc", "libx264"]
    assert out == tmp_path / "px" / f"{pg.compute_proxy_key(src)}.mp4"
    assert out.read_bytes() == b"proxy"
    assert not list((tmp_path / "px").glob("*.part"))


def test_make_proxy_reuses_existing(tmp_path: Path):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"v")
    out_dir = tmp_path / "px"
    out_dir.mkdir()
    existing = out_dir / f"{pg.compute_proxy_key(src)}.mp4"
    existing.write_bytes(b"old")

    def boom(cmd, timeout=None):
        raise AssertionError("must not encode again")

    assert pg.make_proxy(src, out_dir, encoders=("libx264",), run=boom) == existing


def test_make_proxy_all_encoders_fail(tmp_path: Path):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"v")

    def failing(cmd, timeout=None):
        Path(cmd[-1]).write_bytes(b"half")
        return _R(1, "Error while encoding")

    with pytest.raises(pg.ProxyGenerationError) as exc:
        pg.make_proxy(src, tmp_path / "px", encoders=("h264_nvenc", "libx264"), run=failing)
    assert "h264_nvenc" in str(exc.value) and "libx264" in str(exc.value)
    assert not list((tmp_path / "px").glob("*.part"))
