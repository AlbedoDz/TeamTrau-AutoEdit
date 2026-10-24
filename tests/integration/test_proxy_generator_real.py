"""Real ffmpeg: all-intra CFR proxy with stereo 48 kHz audio."""

import shutil
import subprocess
from pathlib import Path

from src.modules import proxy_generator as pg

FFPROBE = shutil.which("ffprobe")


def _probe(path: Path, *args: str) -> str:
    return subprocess.run([FFPROBE, "-v", "error", *args, str(path)], capture_output=True,
                          text=True, check=True, timeout=60).stdout


def test_libx264_all_intra_proxy(make_clip, tmp_path: Path):
    src = make_clip("Lễ src.mp4", duration=2.0, size="640x360")
    out = pg.make_proxy(src, tmp_path / "proxies", pg.ProxySpec(height=240), encoders=("libx264",))
    assert out.is_file() and out.suffix == ".mp4"
    types = {t.strip(",") for t in _probe(out, "-select_streams", "v:0", "-show_entries",
                                          "frame=pict_type", "-of", "csv=p=0").split()}
    assert types == {"I"}
    v = _probe(out, "-select_streams", "v:0", "-show_entries",
               "stream=height,r_frame_rate,avg_frame_rate", "-of", "csv=p=0").strip().split(",")
    assert v[0] == "240" and v[1] == v[2]
    a = _probe(out, "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels",
               "-of", "csv=p=0").strip().split(",")
    assert a == ["48000", "2"]
