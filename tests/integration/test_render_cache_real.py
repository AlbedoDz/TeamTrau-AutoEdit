"""Real ffmpeg render of cache segments: every frame is an I-frame, frame count exact."""

import shutil
import subprocess
from pathlib import Path

from src.core.types import EditClip, EditTimeline
from src.modules import render_cache as rc

FFPROBE = shutil.which("ffprobe")


def test_render_two_segments_all_intra(make_clip, tmp_path: Path):
    a = make_clip("a.mp4", duration=3.0)
    b = make_clip("b.mp4", duration=3.0)
    t = EditTimeline(name="rc", fps=30.0, canvas_width=320, canvas_height=180,
                     clips=[EditClip(a, 0.0, 30, 3.0), EditClip(b, 0.5, 30, 2.5),
                            EditClip(a, 1.0, 45, 2.0)])
    cache = tmp_path / "cache"
    out = rc.render_missing(t, cache, max_segment_sec=2.0, force_cpu=True)
    status = rc.cache_status(t, cache, max_segment_sec=2.0)
    assert out == [s.path for s in status] and all(s.ready for s in status)
    for s in status:
        lines = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pict_type",
             "-of", "csv=p=0", str(s.path)], capture_output=True, text=True, check=True,
            timeout=60).stdout.split()
        assert {x.strip(",") for x in lines} == {"I"}
        assert len(lines) == s.segment.frames
