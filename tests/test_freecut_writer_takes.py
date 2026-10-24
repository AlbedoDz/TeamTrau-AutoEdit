"""P3.0c: the FreeCut writer carries real curated take data into the teamtrau.json sidecar."""

import dataclasses
import json
import threading
from pathlib import Path

from src.core.types import EditClip, EditTimeline
from src.engine_api.runners import make_curate_runner
from src.engine_api.sessions import SessionStore
from src.integrations.freecut_writer import write_freecut_project
from tests.curation_fakes import FakeAdapter, pipeline_result, scene, timeline

DATA_KEYS = {"sourcePath", "role", "score", "is_golden", "takes"}
TAKE_KEYS = {"shot_id", "start", "end", "composite", "sharpness", "aesthetic", "content_role",
             "role_confidence", "included", "golden", "defects"}


def _sidecars(ws: Path) -> dict[str, dict]:
    out = {}
    for f in ws.glob("media/*/cache/ai/teamtrau.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        out[Path(d["data"]["sourcePath"]).name] = d["data"]
    return out


def _shots():
    a = scene(0, "a_roll", q=0.9)
    a.metadata.update({"role_confidence": 0.8, "golden": {"source_in": 0.5, "source_out": 3.5,
                       "defects": [{"type": "camera_shake", "start": 3.5, "end": 4.0,
                                    "description": "x"}]}})
    b = scene(1, "b_roll", q=0.4)
    b.metadata["included"] = False
    other = scene(2, "b_roll", q=0.7)
    other.scene = dataclasses.replace(other.scene, clip_path=Path("other.mp4"))
    return {"A01": a, "B01": b, "B02": other}


def test_sidecar_lists_the_takes_of_each_source(tmp_path: Path):
    ws = tmp_path / "ws"
    write_freecut_project(timeline(), ws, "T", shots=_shots())
    data = _sidecars(ws)["c.mp4"]
    assert set(data) == DATA_KEYS
    assert [t["shot_id"] for t in data["takes"]] == ["A01", "B01"]
    a01, b01 = data["takes"]
    assert set(a01) == TAKE_KEYS
    assert (a01["start"], a01["end"], a01["content_role"]) == (0.0, 4.0, "a_roll")
    assert a01["composite"] == 0.9 and a01["role_confidence"] == 0.8 and a01["included"] is True
    assert a01["golden"] == {"source_in": 0.5, "source_out": 3.5}
    assert a01["defects"] == [{"type": "camera_shake", "start": 3.5, "end": 4.0, "description": "x"}]
    assert b01["included"] is False and b01["golden"] is None and b01["defects"] == []
    # source-level summary comes from the best take, not from placeholders
    assert data["role"] == "a_roll" and data["score"] == 0.9 and data["is_golden"] is True
    json.dumps(data)


def test_sidecar_without_shots_has_no_placeholder_values(tmp_path: Path):
    ws = tmp_path / "ws"
    song = tmp_path / "song.wav"
    song.write_bytes(b"x")
    tl = EditTimeline(name="t", fps=30.0, canvas_width=1920, canvas_height=1080,
                      clips=[EditClip(source_path=Path("c.mp4"), source_in=0.0, frames=60,
                                      available=2.0)], soundtrack_path=song)
    write_freecut_project(tl, ws, "T")
    side = _sidecars(ws)
    assert side["c.mp4"] == {"sourcePath": side["c.mp4"]["sourcePath"], "role": None, "score": None,
                             "is_golden": False, "takes": []}
    assert side["song.wav"]["role"] == "music" and side["song.wav"]["takes"] == []


def test_curate_runner_writes_take_data(tmp_path: Path):
    res = pipeline_result(list(_shots().values()),
                          timelines={"rough_cut": timeline("rough_cut", "rough_cut")})
    runner = make_curate_runner(SessionStore(), adapter_factory=lambda: FakeAdapter(res))
    runner({"input_dir": str(tmp_path), "workspace": str(tmp_path / "ws")},
           lambda e: None, threading.Event())
    data = _sidecars(tmp_path / "ws")["c.mp4"]
    assert [t["shot_id"] for t in data["takes"]] == ["A01", "B01"]
    assert data["role"] == "a_roll"
