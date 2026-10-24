"""Render cache planning, keys, status, rendering orchestration (fake renderer)."""

import json
import os
from pathlib import Path

import pytest

from src.core.config import PipelineConfig
from src.core.types import CropWindow, DeliverSettings, EditClip, EditTimeline, RenderJobResult
from src.dispatchers.nvenc_renderer import NvencRenderer
from src.modules import render_cache as rc


def _touch(p: Path) -> Path:
    p.write_bytes(b"media")
    return p


def clip(p: Path, frames: int = 30, source_in: float = 0.0, **kw) -> EditClip:
    return EditClip(source_path=p, source_in=source_in, frames=frames, available=frames / 30, **kw)


def tl(clips: list[EditClip], fps: float = 30.0, w: int = 1920, h: int = 1080) -> EditTimeline:
    return EditTimeline(name="t", fps=fps, canvas_width=w, canvas_height=h, clips=clips)


def _has(cmd: list[str], pair: list[str]) -> bool:
    return any(cmd[i:i + len(pair)] == pair for i in range(len(cmd)))


def test_intra_only_h264_args():
    s = DeliverSettings(canvas_width=1920, canvas_height=1080, fps=60.0, master="h264")
    r = NvencRenderer(PipelineConfig(), settings=s, intra_only=True)
    _, nv = r.master_encoder_args(s, True)
    assert _has(nv, ["-g", "0"]) and _has(nv, ["-bf", "0"])
    _, cpu = r.master_encoder_args(s, False)
    assert _has(cpu, ["-g", "1"]) and _has(cpu, ["-bf", "0"])
    _, plain = NvencRenderer(PipelineConfig(), settings=s).master_encoder_args(s, True)
    assert "-g" not in plain


def test_intra_only_requires_h264_settings():
    hevc = DeliverSettings(canvas_width=1920, canvas_height=1080, fps=30.0, master="hevc")
    with pytest.raises(ValueError):
        NvencRenderer(PipelineConfig(), settings=hevc, intra_only=True)
    with pytest.raises(ValueError):
        NvencRenderer(PipelineConfig(), settings=None, intra_only=True)


def test_segments_group_cuts_up_to_limit(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    segs = rc.plan_segments(tl([clip(f, 90) for _ in range(5)]), max_segment_sec=10.0)
    assert [s.clip_indices for s in segs] == [(0, 1, 2), (3, 4)]
    assert [(s.start_frame, s.frames) for s in segs] == [(0, 270), (270, 180)]
    assert [s.index for s in segs] == [0, 1]


def test_long_clip_is_its_own_segment(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    segs = rc.plan_segments(tl([clip(f, 600), clip(f, 30), clip(f, 30)]), max_segment_sec=10.0)
    assert [s.clip_indices for s in segs] == [(0,), (1, 2)]


def test_dissolve_is_never_split(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    clips = [clip(f, 120), clip(f, 120), clip(f, 120, transition_in_frames=10)]
    assert [s.clip_indices for s in rc.plan_segments(tl(clips), 10.0)] == [(0,), (1, 2)]


def test_gaps_force_a_boundary(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    segs = rc.plan_segments(tl([clip(f, 30), clip(f, 30)]), 10.0, starts=[0, 60])
    assert [(s.clip_indices, s.start_frame) for s in segs] == [((0,), 0), ((1,), 60)]


def test_key_ignores_position_and_labels(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    g = _touch(tmp_path / "b.mp4")
    seg = [clip(f, 30, 1.0)]
    k = rc.segment_key(tl(seg), seg)
    assert len(k) == 40
    assert rc.segment_key(tl([clip(g, 99), *seg]), seg) == k
    labelled = [clip(f, 30, 1.0, label="x", markers=[{"t": 1}], scene_start=3.0)]
    assert rc.segment_key(tl(labelled), labelled) == k


def test_key_changes_with_pixels(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    base = [clip(f, 30, 1.0)]
    k = rc.segment_key(tl(base), base)
    variants = [
        [clip(f, 31, 1.0)],
        [clip(f, 30, 1.5)],
        [clip(f, 30, 1.0, speed=2.0)],
        [clip(f, 30, 1.0, crop_window=CropWindow(x=0, y=0, width=608, height=1080))],
        [clip(f, 30, 1.0, transition_in_frames=5)],
    ]
    for v in variants:
        assert rc.segment_key(tl(v), v) != k
    assert rc.segment_key(tl(base, fps=60.0), base) != k
    assert rc.segment_key(tl(base, w=1080, h=1920), base) != k
    os.utime(f, ns=(1_000_000_000, 1_000_000_000))
    assert rc.segment_key(tl(base), base) != k


class FakeRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object, Path]] = []

    def render_edit_timeline(self, edit_tl, output_file, force_cpu=False, **kw):
        out = Path(output_file)
        self.calls.append((len(edit_tl.clips), edit_tl.soundtrack_path, out))
        out.write_bytes(b"seg")
        return RenderJobResult(output_path=out, encoder_used="fake", duration=1.0,
                               file_size_bytes=3)


def test_status_and_render_missing(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    t = tl([clip(f, 90) for _ in range(5)])
    t.soundtrack_path = tmp_path / "song.mp3"
    cache = tmp_path / "cache"
    st = rc.cache_status(t, cache, max_segment_sec=10.0)
    assert [s.ready for s in st] == [False, False]
    assert all(s.path == cache / f"{s.segment.key}.mp4" for s in st)
    cache.mkdir()
    st[0].path.write_bytes(b"old")
    fake = FakeRenderer()
    out = rc.render_missing(t, cache, renderer=fake, max_segment_sec=10.0)
    assert out == [st[1].path]
    assert fake.calls[0][0] == 2 and fake.calls[0][1] is None
    assert fake.calls[0][2].name.endswith(".part.mp4")
    assert st[1].path.read_bytes() == b"seg"
    assert not list(cache.glob("*.part*"))
    assert [s.ready for s in rc.cache_status(t, cache, max_segment_sec=10.0)] == [True, True]


def test_render_failure_cleans_part_file(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    t = tl([clip(f, 30)])
    cache = tmp_path / "cache"

    class Broken:
        def render_edit_timeline(self, edit_tl, output_file, force_cpu=False, **kw):
            Path(output_file).write_bytes(b"half")
            raise RuntimeError("encoder died")

    with pytest.raises(RuntimeError, match="encoder died"):
        rc.render_missing(t, cache, renderer=Broken())
    assert not list(cache.glob("*.part*"))


def test_manifest(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    t = tl([clip(f, 90) for _ in range(5)])
    cache = tmp_path / "cache"
    path = rc.write_manifest(t, cache, max_segment_sec=10.0)
    assert path == cache / rc.MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["fps"] == 30.0 and data["canvas"] == [1920, 1080]
    assert [(s["index"], s["start_frame"], s["frames"], s["ready"]) for s in data["segments"]] == [
        (0, 0, 270, False), (1, 270, 180, False)]
    assert all(s["file"] == f"{s['key']}.mp4" for s in data["segments"])
    assert not list(cache.glob("*.tmp"))
