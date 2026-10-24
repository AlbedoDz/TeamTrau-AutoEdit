"""P3.0a: curation sessions over the engine API (store, presets, GET/PATCH, curate keeps session, cancel/progress)."""

import json
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.core import curation
from src.core.service import JobCallbacks
from src.core.stage_options import CADENCE_NAMES, GENRE_KEYS
from src.engine_api.app import create_app
from src.engine_api.runners import make_curate_runner
from src.engine_api.sessions import SessionStore, session_to_dict, shot_to_dict
from tests.curation_fakes import FakeAdapter, pipeline_result, scene, timeline

TOKEN = "s" * 32
H = {"Authorization": f"Bearer {TOKEN}"}
SHOT_KEYS = {"shot_id", "clip_path", "start", "end", "duration", "composite", "sharpness",
             "aesthetic", "motion_energy", "content_role", "role_confidence", "included",
             "golden", "defects"}


def _result():
    golden = {"source_in": 0.5, "source_out": 3.5, "trimmed_sec": 1.0,
              "defects": [{"type": "camera_shake", "start": 3.5, "end": 4.0, "description": "x"}]}
    a = scene(0, "a_roll", q=0.9)
    a.metadata.update({"golden": golden, "role_confidence": 0.8})
    return pipeline_result([a, scene(1, "b_roll", q=0.4), scene(2, None, q=0.7)],
                           timelines={"rough_cut": timeline("rough_cut", "rough_cut")})


def _session(tmp_path: Path):
    sess, _ = curation.run_curation(FakeAdapter(_result()), tmp_path, None, tmp_path / "out")
    return sess


def test_shot_to_dict_is_json_safe_and_complete(tmp_path: Path):
    sess = _session(tmp_path)
    d = shot_to_dict("A01", sess.shots["A01"])
    assert set(d) == SHOT_KEYS
    assert d["shot_id"] == "A01" and d["content_role"] == "a_roll" and d["included"] is True
    assert (d["start"], d["end"], d["duration"]) == (0.0, 4.0, 4.0)
    assert d["composite"] == pytest.approx(0.9) and d["role_confidence"] == pytest.approx(0.8)
    assert d["golden"]["source_in"] == 0.5 and d["golden"]["trimmed_sec"] == 1.0
    assert d["defects"] == [{"type": "camera_shake", "start": 3.5, "end": 4.0, "description": "x"}]
    b = shot_to_dict("B02", sess.shots["B02"])
    assert b["golden"] is None and b["defects"] == [] and b["content_role"] is None
    json.dumps([d, b])


def test_session_to_dict_orders_shots(tmp_path: Path):
    sess = _session(tmp_path)
    d = session_to_dict(sess)
    assert set(d) == {"session_id", "input_dir", "takes", "shots"}
    assert d["takes"] == 3
    assert [s["shot_id"] for s in d["shots"]] == ["A01", "B01", "B02"]


def test_store_is_lru_and_patch_validates(tmp_path: Path):
    store = SessionStore(max_sessions=2)
    s1, s2, s3 = _session(tmp_path), _session(tmp_path), _session(tmp_path)
    for s in (s1, s2):
        store.put(s)
    assert store.get(s1.session_id) is s1  # s1 becomes most recently used
    store.put(s3)
    assert store.get(s2.session_id) is None
    assert store.get(s1.session_id) is s1 and store.get(s3.session_id) is s3

    store.patch(s1.session_id, {"A01": {"included": False}, "B01": {"content_role": "a_roll"}})
    assert s1.shots["A01"].metadata["included"] is False
    assert s1.shots["B01"].metadata["content_role"] == "a_roll"
    with pytest.raises(KeyError):
        store.patch(s1.session_id, {"Z99": {"included": True}})
    with pytest.raises(KeyError):
        store.patch("nope", {"A01": {"included": True}})
    with pytest.raises(ValueError):
        store.patch(s1.session_id, {"A01": {"content_role": "c_roll"}})
    with pytest.raises(ValueError):
        store.patch(s1.session_id, {"A01": {"composite": 1.0}})
    with pytest.raises(ValueError):
        store.patch(s1.session_id, {"A01": {"included": "yes"}})


def test_presets_endpoint():
    c = TestClient(create_app(token=TOKEN, runners={}))
    assert c.get("/v1/presets").status_code == 401
    p = c.get("/v1/presets", headers=H).json()
    assert {g["key"] for g in p["genres"]} == set(GENRE_KEYS)
    assert all(isinstance(g["display_name"], str) and g["display_name"] for g in p["genres"])
    assert p["cadences"] == list(CADENCE_NAMES)
    assert set(p["deliver"]["master"]) == {"hevc", "h264", "prores_422", "prores_hq", "dnxhr_hqx"}
    assert set(p["deliver"]["fit_mode"]) == {"auto", "fit", "fill", "stretch"}
    assert set(p["deliver"]["subtitles"]) == {"none", "srt", "ass", "burn"}


def test_sessions_get_and_patch_over_http(tmp_path: Path):
    store = SessionStore()
    sess = _session(tmp_path)
    store.put(sess)
    c = TestClient(create_app(token=TOKEN, runners={}, sessions=store))
    url = f"/v1/sessions/{sess.session_id}"
    assert c.get(url).status_code == 401
    got = c.get(url, headers=H).json()
    assert got["session_id"] == sess.session_id and len(got["shots"]) == 3
    assert c.get("/v1/sessions/nope", headers=H).status_code == 404
    r = c.patch(url, headers=H, json={"shots": {"A01": {"included": False}}})
    assert r.status_code == 200
    assert {s["shot_id"]: s["included"] for s in r.json()["shots"]}["A01"] is False
    assert c.patch(url, headers=H, json={"shots": {"Z9": {"included": False}}}).status_code == 404
    bad_role = c.patch(url, headers=H, json={"shots": {"A01": {"content_role": "x"}}})
    assert bad_role.status_code == 400
    assert c.patch(url, headers=H, json=[1, 2]).status_code == 400
    assert c.patch(url, headers=H, json={"other": {}}).status_code == 400


def test_curate_runner_keeps_session_and_returns_shots(tmp_path: Path):
    store = SessionStore()
    runner = make_curate_runner(store, adapter_factory=lambda: FakeAdapter(_result()))
    events: list[dict] = []
    out = runner({"input_dir": str(tmp_path), "workspace": str(tmp_path / "ws")},
                 events.append, threading.Event())
    sid = out["session_id"]
    assert store.get(sid) is not None
    assert [s["shot_id"] for s in out["shots"]] == ["A01", "B01", "B02"]
    assert out["project_id"].startswith("p-")
    assert out["summary"]["takes"] == 3


class _SlowHandle:
    def __init__(self, result, cb: JobCallbacks | None):
        self.cancel_event = threading.Event()
        self.cancelled = False
        self._r = result
        self._done = threading.Event()

        def work() -> None:
            for i in range(50):
                if self.cancel_event.is_set():
                    break
                if cb is not None and cb.on_progress is not None:
                    cb.on_progress(i * 2.0, i * 2.0, "working")
                time.sleep(0.02)
            self._done.set()

        threading.Thread(target=work, daemon=True).start()

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def cancel(self) -> bool:
        self.cancelled = True
        self.cancel_event.set()
        return True

    @property
    def result(self):
        return self._r


class _SlowAdapter:
    def __init__(self, result):
        self._result = result
        self.kw: dict = {}
        self.handle: _SlowHandle | None = None

    def submit_job(self, **kw):
        self.kw = kw
        self.handle = _SlowHandle(self._result, kw.get("callbacks"))
        return self.handle


def test_run_curation_forwards_progress(tmp_path: Path):
    seen: list[float] = []
    cb = JobCallbacks(on_progress=lambda s, t, m: seen.append(t))
    adapter = _SlowAdapter(_result())
    sess, summary = curation.run_curation(adapter, tmp_path, None, tmp_path / "out",
                                          callbacks=cb, cancel_event=threading.Event())
    assert adapter.kw["callbacks"] is cb and adapter.kw["run_async"] is True
    assert len(seen) >= 10 and summary["takes"] == 3


def test_run_curation_cancels_the_pipeline(tmp_path: Path):
    adapter = _SlowAdapter(_result())
    ev = threading.Event()
    box: dict = {}

    def go() -> None:
        try:
            curation.run_curation(adapter, tmp_path, None, tmp_path / "out", cancel_event=ev)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            box["exc"] = exc

    t0 = time.monotonic()
    th = threading.Thread(target=go)
    th.start()
    time.sleep(0.1)
    ev.set()
    th.join(5)
    assert isinstance(box.get("exc"), InterruptedError)
    assert adapter.handle is not None and adapter.handle.cancelled
    assert time.monotonic() - t0 < 2.0


def test_curate_runner_emits_progress_events(tmp_path: Path):
    store = SessionStore()
    runner = make_curate_runner(store, adapter_factory=lambda: _SlowAdapter(_result()))
    events: list[dict] = []
    runner({"input_dir": str(tmp_path), "workspace": str(tmp_path / "ws")},
           events.append, threading.Event())
    progress = [e for e in events if e.get("type") == "progress"]
    assert len(progress) >= 5
    assert all(0.0 <= e["pct"] <= 100.0 for e in progress)
