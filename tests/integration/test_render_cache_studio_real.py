"""Render cache for Studio: item signatures in the manifest, default cache dir, video-only segments."""

import json
import subprocess
import threading
from pathlib import Path

from src.core.types import EditClip, EditTimeline
from src.engine_api.runners import run_render_cache
from src.integrations.freecut_reader import read_freecut_project
from src.integrations.freecut_writer import write_freecut_project


def _stream_types(p: Path) -> list[str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return out.split()


def _project(tmp_path: Path, make_clip) -> tuple[Path, str]:
    a = make_clip("a.mp4", duration=2.0)
    b = make_clip("b.mp4", duration=2.0)
    clips = [EditClip(source_path=p, source_in=0.2, frames=20, available=1.0) for p in (a, b, a)]
    tl = EditTimeline(name="t", fps=30.0, canvas_width=640, canvas_height=360, clips=clips)
    ws = tmp_path / "ws"
    return ws, write_freecut_project(tl, ws, "RC")


def test_reader_exposes_item_signatures(tmp_path: Path, make_clip):
    ws, pid = _project(tmp_path, make_clip)
    ft = read_freecut_project(ws, pid)
    proj = json.loads((ws / "projects" / pid / "project.json").read_text(encoding="utf-8"))
    video = sorted((i for i in proj["timeline"]["items"] if i["type"] == "video"),
                   key=lambda i: i["from"])
    assert [s["id"] for s in ft.items] == [v["id"] for v in video]
    for s, v in zip(ft.items, video):
        assert set(s) == {"id", "mediaId", "from", "durationInFrames", "sourceStart", "sourceEnd",
                          "speed"}
        for k in ("mediaId", "from", "durationInFrames", "sourceStart", "sourceEnd"):
            assert s[k] == v[k]
        assert s["speed"] == v.get("speed", 1.0)


def test_render_cache_job_for_studio(tmp_path: Path, make_clip):
    ws, pid = _project(tmp_path, make_clip)
    events: list[dict] = []
    res = run_render_cache({"workspace": str(ws), "project_id": pid, "max_segment_sec": 1.0},
                           events.append, threading.Event())
    cache_dir = ws / ".teamtrau" / "render-cache" / pid
    assert Path(res["manifest"]) == cache_dir / "render_cache.json"
    assert Path(res["cache_dir"]) == cache_dir
    man = json.loads((cache_dir / "render_cache.json").read_text(encoding="utf-8"))
    ft = read_freecut_project(ws, pid)
    sigs = [s for seg in man["segments"] for s in seg["items"]]
    assert sigs == [dict(s) for s in ft.items]
    for seg in man["segments"]:
        assert seg["ready"] is True
        assert [s["from"] for s in seg["items"]][0] == seg["start_frame"]
        assert _stream_types(cache_dir / seg["file"]) == ["video"]
    assert not [p.name for p in cache_dir.iterdir() if ".part" in p.name]
