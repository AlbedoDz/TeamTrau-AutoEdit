"""FreeCut writer on real media: layout, frame math, linked audio, proxies, atomicity."""

import json
import re
from pathlib import Path

from src.core.types import EditClip, EditTimeline
from src.integrations.freecut_writer import write_freecut_project
from src.modules.proxy_generator import compute_proxy_key


def _tl(paths: list[Path], soundtrack: Path | None = None) -> EditTimeline:
    clips = [EditClip(source_path=p, source_in=0.5, frames=45, available=1.5) for p in paths]
    return EditTimeline(name="t", fps=30.0, canvas_width=1920, canvas_height=1080, clips=clips,
                        soundtrack_path=soundtrack)


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def test_project_layout_and_frame_math(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=3.0)
    b = make_clip("b clip Việt.mp4", duration=3.0)
    ws = tmp_path / "ws"
    pid = write_freecut_project(_tl([a, b, a]), ws, "Demo")
    assert re.fullmatch(r"p-[0-9a-f-]{36}", pid)
    proj = _load(ws / "projects" / pid / "project.json")
    assert proj["schemaVersion"] == 15 and proj["id"] == pid and proj["name"] == "Demo"
    items = proj["timeline"]["items"]
    video = sorted((i for i in items if i["type"] == "video"), key=lambda i: i["from"])
    assert [v["from"] for v in video] == [0, 45, 90]
    assert all(v["durationInFrames"] == 45 and v.get("src", "") == "" for v in video)
    assert all(v["sourceStart"] == 15 and v["sourceEnd"] == 60 for v in video)
    media_ids = {v["mediaId"] for v in video}
    assert len(media_ids) == 2
    for mid in media_ids:
        assert (ws / "media" / mid / "metadata.json").is_file()
        link = _load(ws / "media" / mid / "source.link.json")
        assert Path(link["path"]).resolve() in {a.resolve(), b.resolve()}
    audio = [i for i in items if i["type"] == "audio"]
    assert sorted(x["linkedGroupId"] for x in audio) == sorted(v["linkedGroupId"] for v in video)
    index = _load(ws / "index.json")
    assert pid in [p["id"] for p in index["projects"]]
    assert (ws / ".freecut-workspace.json").is_file()
    assert not list(ws.rglob("*.tmp"))


def test_new_project_each_call(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=2.0)
    ws = tmp_path / "ws"
    p1 = write_freecut_project(_tl([a]), ws, "One")
    p2 = write_freecut_project(_tl([a]), ws, "Two")
    assert p1 != p2
    ids = [p["id"] for p in _load(ws / "index.json")["projects"]]
    assert p1 in ids and p2 in ids
    assert _load(ws / "projects" / p1 / "project.json")["name"] == "One"


def test_proxy_is_adoptable(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=2.0)
    px = make_clip("a_proxy.mp4", duration=2.0, size="160x90")
    ws = tmp_path / "ws"
    pid = write_freecut_project(_tl([a]), ws, "Px", proxies={a: px})
    key = compute_proxy_key(a)
    pdir = ws / "content" / "proxies" / key
    assert (pdir / "proxy.mp4").is_file()
    meta = _load(pdir / "meta.json")
    assert (meta["version"], meta["status"], meta["codec"]) == (4, "ready", "avc")
    video = [i for i in _load(ws / "projects" / pid / "project.json")["timeline"]["items"]
             if i["type"] == "video"]
    md = _load(ws / "media" / video[0]["mediaId"] / "metadata.json")
    assert md["contentHash"] == key
