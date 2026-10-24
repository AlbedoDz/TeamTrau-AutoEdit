"""Writer -> reader round trip on real media."""

import json
from pathlib import Path

import pytest

from src.core.types import EditClip, EditTimeline
from src.integrations.freecut_reader import read_freecut_project
from src.integrations.freecut_writer import write_freecut_project


def _tl(clips: list[EditClip]) -> EditTimeline:
    return EditTimeline(name="t", fps=30.0, canvas_width=1920, canvas_height=1080, clips=clips)


def test_round_trip(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=3.0)
    b = make_clip("b.mp4", duration=3.0)
    clips = [EditClip(a, 0.5, 45, 1.5), EditClip(b, 1.0, 30, 1.0), EditClip(a, 2.0, 15, 0.5)]
    ws = tmp_path / "ws"
    pid = write_freecut_project(_tl(clips), ws, "RT")
    got = read_freecut_project(ws, pid)
    assert got.project_id == pid
    assert got.timeline.fps == 30.0
    assert (got.timeline.canvas_width, got.timeline.canvas_height) == (1920, 1080)
    assert got.starts == (0, 45, 75)
    assert [(Path(c.source_path).resolve(), round(c.source_in, 3), c.frames)
            for c in got.timeline.clips] == [(a.resolve(), 0.5, 45), (b.resolve(), 1.0, 30),
                                              (a.resolve(), 2.0, 15)]


def test_reader_sorts_by_from_and_keeps_gaps(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=3.0)
    ws = tmp_path / "ws"
    two = [EditClip(a, 0.0, 30, 1.0), EditClip(a, 1.0, 30, 1.0)]
    pid = write_freecut_project(_tl(two), ws, "G")
    pfile = ws / "projects" / pid / "project.json"
    proj = json.loads(pfile.read_text(encoding="utf-8"))
    video = [i for i in proj["timeline"]["items"] if i["type"] == "video"]
    video[1]["from"] = 60
    proj["timeline"]["items"].reverse()
    pfile.write_text(json.dumps(proj), encoding="utf-8")
    assert read_freecut_project(ws, pid).starts == (0, 60)


def test_reader_rejects_newer_schema(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=2.0)
    ws = tmp_path / "ws"
    pid = write_freecut_project(_tl([EditClip(a, 0.0, 30, 1.0)]), ws, "S")
    pfile = ws / "projects" / pid / "project.json"
    proj = json.loads(pfile.read_text(encoding="utf-8"))
    proj["schemaVersion"] = 99
    pfile.write_text(json.dumps(proj), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        read_freecut_project(ws, pid)
