"""Curation sessions survive an engine restart: saved under the workspace, reloaded on demand."""

import json
import threading
from pathlib import Path

from starlette.testclient import TestClient

from src.core import curation
from src.engine_api.app import create_app
from src.engine_api.runners import default_project_name, make_assemble_runner, make_curate_runner
from src.engine_api.session_files import load_session, save_session, session_file
from src.engine_api.sessions import SessionStore
from tests.curation_fakes import FakeAdapter, pipeline_result, scene, timeline

TOKEN = "s" * 32
H = {"Authorization": f"Bearer {TOKEN}"}


def _result():
    a = scene(0, "a_roll", q=0.9)
    a.metadata.update({"golden": {"source_in": 0.5, "source_out": 3.5, "defects": []},
                       "role_confidence": 0.8, "source": Path("D:/x.mp4")})
    return pipeline_result([a, scene(1, "b_roll", q=0.4), scene(2, None, q=0.7)],
                           timelines={"rough_cut": timeline("rough_cut", "rough_cut")})


def _session(tmp_path: Path, workspace: Path | None = None):
    sess, _ = curation.run_curation(FakeAdapter(_result()), tmp_path, None, tmp_path / "out")
    sess.workspace = workspace
    sess.project_id = "p-rough"
    return sess


def test_save_and_load_round_trip(tmp_path: Path):
    ws = tmp_path / "ws"
    sess = _session(tmp_path, ws)
    sess.config.min_beat_interval = 0.55
    sess.sequence = pipeline_result([], timelines={"beat": timeline("beat")})
    sess.sequence.active_variant_name = "beat"

    path = save_session(sess)
    assert path == session_file(ws, sess.session_id)
    assert json.loads(path.read_text(encoding="utf-8"))["project_id"] == "p-rough"

    back = load_session(ws, sess.session_id)
    assert back is not None
    assert back.session_id == sess.session_id and back.project_id == "p-rough"
    assert back.workspace == ws and back.input_dir == sess.input_dir
    assert back.output_dir == sess.output_dir
    assert back.config.min_beat_interval == 0.55
    assert list(back.shots) == list(sess.shots)
    a01 = back.shots["A01"]
    assert a01.composite_score == sess.shots["A01"].composite_score
    assert a01.metadata["golden"]["source_in"] == 0.5
    assert a01.metadata["source"] == str(Path("D:/x.mp4"))  # non-JSON values stored as text
    assert list(back.result.edit_timelines) == ["rough_cut"]
    assert back.result.scored_scenes[0] is back.shots["A01"]  # shots and result share scenes
    assert back.sequence is not None and back.sequence.active_variant_name == "beat"
    assert list(back.sequence.edit_timelines) == ["beat"]


def test_load_rejects_unsafe_ids_and_missing_files(tmp_path: Path):
    assert load_session(tmp_path, "../../etc") is None
    assert load_session(tmp_path, "nope") is None


def test_store_reloads_from_disk_and_saves_patches(tmp_path: Path):
    ws = tmp_path / "ws"
    sess = _session(tmp_path, ws)
    SessionStore().put(sess)  # put saves to disk

    fresh = SessionStore()  # a restarted engine
    assert fresh.get(sess.session_id) is None
    got = fresh.get(sess.session_id, workspace=ws)
    assert got is not None and list(got.shots) == ["A01", "B01", "B02"]

    fresh.patch(sess.session_id, {"A01": {"included": False, "content_role": "b_roll"}})
    again = load_session(ws, sess.session_id)
    assert again is not None
    assert again.shots["A01"].metadata["included"] is False
    assert again.shots["A01"].metadata["content_role"] == "b_roll"


def test_http_get_and_patch_restore_with_workspace_query(tmp_path: Path):
    ws = tmp_path / "ws"
    sess = _session(tmp_path, ws)
    save_session(sess)
    c = TestClient(create_app(token=TOKEN, runners={}, sessions=SessionStore()))
    url = f"/v1/sessions/{sess.session_id}"
    assert c.get(url, headers=H).status_code == 404
    q = {"workspace": str(ws)}
    assert c.get(url, headers=H, params=q).json()["session_id"] == sess.session_id
    r = c.patch(url, headers=H, params=q, json={"shots": {"B01": {"included": False}}})
    assert r.status_code == 200
    assert load_session(ws, sess.session_id).shots["B01"].metadata["included"] is False


def test_curate_saves_session_and_assemble_reloads_it(tmp_path: Path):
    ws = tmp_path / "ws"
    footage = tmp_path / "Wedding A"
    footage.mkdir()
    store = SessionStore()
    curate = make_curate_runner(store, adapter_factory=lambda: FakeAdapter(_result()))
    out = curate({"input_dir": str(footage), "workspace": str(ws)}, lambda e: None, threading.Event())
    sid = out["session_id"]
    saved = load_session(ws, sid)
    assert saved is not None and saved.project_id == out["project_id"]

    # Engine restarted: assemble finds the session on disk through its workspace.
    seq = pipeline_result([], timelines={"master": timeline("master")})
    assemble = make_assemble_runner(SessionStore(), adapter_factory=lambda: FakeAdapter(seq))
    soundtrack = tmp_path / "song.mp3"
    soundtrack.write_bytes(b"")
    res = assemble({"session_id": sid, "workspace": str(ws), "soundtrack": str(soundtrack)},
                   lambda e: None, threading.Event())
    assert res["project_id"].startswith("p-")
    after = load_session(ws, sid)
    assert after is not None and after.sequence is not None
    assert list(after.sequence.edit_timelines) == ["master"]

    names = {p["id"]: p["name"] for p in json.loads((ws / "index.json").read_text("utf-8"))["projects"]}
    assert names[out["project_id"]].startswith("Wedding A – Rough cut – ")
    assert names[res["project_id"]].startswith("Wedding A – Sequence – ")


def test_default_project_name_format():
    from datetime import datetime

    when = datetime(2026, 10, 25, 22, 30)
    assert default_project_name(Path("D:/Cuoi A"), "Rough cut", when=when) == "Cuoi A – Rough cut – 2026-10-25 22:30"
    assert default_project_name(Path("D:/X"), "Sequence (punchy)", when=when) == "X – Sequence (punchy) – 2026-10-25 22:30"
