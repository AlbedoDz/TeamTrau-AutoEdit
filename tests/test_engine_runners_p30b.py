"""P3.0b: classify, analyze_soundtrack, preflight and assemble job runners for the Studio panels."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import curation
from src.core.ontology import ContentRole
from src.core.project_io import audio_state_to_dict
from src.engine_api.runners import (
    make_assemble_runner,
    make_classify_runner,
    make_preflight_runner,
    make_soundtrack_runner,
)
from src.engine_api.sessions import SessionStore
from src.integrations.freecut_writer import write_freecut_project
from src.modules.media_classifier import ClassificationResult, ClipSignals
from tests.curation_fakes import FakeAdapter, pipeline_result, scene, timeline


def _footage(tmp_path: Path) -> Path:
    root = tmp_path / "footage"
    for rel in ("A-roll/a1.mp4", "A-roll/a2.mp4", "B-roll/b1.mp4", "B-roll/b2.mp4",
                "B-roll/b3.mp4", "Music/song.wav"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return root


def _fake_classify(calls: list[list[Path]]):
    def classify(paths: list[Path], max_workers: int = 4) -> list[ClassificationResult]:
        calls.append(list(paths))
        out = []
        for p in paths:
            a = "A-roll" in str(p)
            out.append(ClassificationResult(
                path=p, role=ContentRole.A_ROLL if a else ContentRole.B_ROLL,
                confidence=0.9 if a else 0.6, has_speech=a, reasons=["voice" if a else "no voice"],
                signals=ClipSignals(has_audio=True, voice_ratio=0.7 if a else 0.0, snr_db=18.0,
                                    face_ratio=0.2, motion=4.0)))
        return out
    return classify


def test_classify_runner_chunks_reports_progress_and_finds_music(tmp_path: Path):
    root = _footage(tmp_path)
    calls: list[list[Path]] = []
    runner = make_classify_runner(classify_fn=_fake_classify(calls), chunk_size=4)
    events: list[dict] = []
    out = runner({"input_dir": str(root)}, events.append, threading.Event())
    assert [len(c) for c in calls] == [4, 1]
    files = out["files"]
    assert [Path(f["path"]).name for f in files] == ["a1.mp4", "a2.mp4", "b1.mp4", "b2.mp4", "b3.mp4"]
    assert {Path(f["path"]).name: f["role"] for f in files}["a1.mp4"] == "a_roll"
    assert {Path(f["path"]).name: f["role"] for f in files}["b2.mp4"] == "b_roll"
    a1 = files[0]
    assert set(a1) == {"path", "role", "confidence", "has_speech", "reasons", "error", "signals"}
    assert a1["signals"] == {"has_audio": True, "voice_ratio": 0.7, "snr_db": 18.0,
                             "face_ratio": 0.2, "motion": 4.0}
    assert out["skipped"] == []
    assert Path(out["soundtrack"]).name == "song.wav"
    progress = [e for e in events if e.get("type") == "progress"]
    assert len(progress) == 2 and progress[-1]["pct"] == 100.0


def test_classify_runner_honours_cancel_between_chunks(tmp_path: Path):
    root = _footage(tmp_path)
    calls: list[list[Path]] = []
    ev = threading.Event()
    ev.set()
    runner = make_classify_runner(classify_fn=_fake_classify(calls), chunk_size=4)
    with pytest.raises(InterruptedError):
        runner({"input_dir": str(root)}, lambda e: None, ev)
    assert calls == []


def test_classify_runner_rejects_missing_dir(tmp_path: Path):
    runner = make_classify_runner(classify_fn=_fake_classify([]))
    with pytest.raises(FileNotFoundError):
        runner({"input_dir": str(tmp_path / "nope")}, lambda e: None, threading.Event())


def test_soundtrack_runner_returns_serialised_analysis(tmp_path: Path):
    song = tmp_path / "song.wav"
    song.write_bytes(b"x")
    beat = SimpleNamespace(timestamp=0.5, frame_idx=15, strength=1.0, is_downbeat=True)
    fake = SimpleNamespace(tempo_bpm=120.0, duration=10.0, sample_rate=44100, beats=[beat],
                           bar_grid=None, sections=[], cut_timestamps=[0.5], cadence_cuts=None,
                           extracted_audio_path=None)
    seen: list[Path] = []
    runner = make_soundtrack_runner(analyze_fn=lambda p: seen.append(Path(p)) or fake)
    out = runner({"path": str(song)}, lambda e: None, threading.Event())
    assert seen == [song]
    assert out == {"path": str(song), **audio_state_to_dict(fake)}
    with pytest.raises(FileNotFoundError):
        runner({"path": str(tmp_path / "missing.wav")}, lambda e: None, threading.Event())


def test_preflight_runner_reports_missing_media(tmp_path: Path):
    ws = tmp_path / "ws"
    pid = write_freecut_project(timeline(), ws, "Pre")
    out = make_preflight_runner()({"workspace": str(ws), "project_id": pid},
                                  lambda e: None, threading.Event())
    assert set(out) == {"ok", "checked_files", "issues"}
    assert out["ok"] is False
    assert any(i["code"] == "MEDIA_MISSING" and i["severity"] == "error" for i in out["issues"])


def _session(tmp_path: Path, store: SessionStore):
    res = pipeline_result([scene(0, "a_roll", q=0.9), scene(1, "b_roll", q=0.4)],
                          timelines={"rough_cut": timeline("rough_cut", "rough_cut")})
    sess, _ = curation.run_curation(FakeAdapter(res), tmp_path, None, tmp_path / "out")
    store.put(sess)
    return sess


def test_assemble_runner_uses_session_flags_and_writes_project(tmp_path: Path):
    store = SessionStore()
    sess = _session(tmp_path, store)
    store.patch(sess.session_id, {"A01": {"included": False}})
    song = tmp_path / "song.wav"
    song.write_bytes(b"x")
    adapter = FakeAdapter(pipeline_result([], timelines={"master": timeline("master", "music")}))
    runner = make_assemble_runner(store, adapter_factory=lambda: adapter)
    out = runner({"session_id": sess.session_id, "workspace": str(tmp_path / "ws"),
                  "soundtrack": str(song)}, lambda e: None, threading.Event())
    assert out["project_id"].startswith("p-")
    assert out["timeline_id"] == f"{sess.session_id}:master"
    assert out["total_cuts"] == 1
    kw = adapter.calls[-1][1]
    assert kw["stage"] == "sequence" and Path(kw["soundtrack"]) == song
    included = {s.metadata.get("included", True) for s in kw["scenes"]}
    assert included == {False, True}


def test_assemble_runner_validates_inputs(tmp_path: Path):
    store = SessionStore()
    sess = _session(tmp_path, store)
    runner = make_assemble_runner(store, adapter_factory=lambda: FakeAdapter())
    with pytest.raises(KeyError):
        runner({"session_id": "nope", "workspace": str(tmp_path)}, lambda e: None, threading.Event())
    with pytest.raises(ValueError):
        runner({"session_id": sess.session_id, "workspace": str(tmp_path),
                "soundtrack": str(tmp_path / "s.wav"), "cadence": "not_a_cadence"},
               lambda e: None, threading.Event())


class _SlowHandle:
    def __init__(self, result, cb):
        self.cancel_event = threading.Event()
        self.cancelled = False
        self._r = result
        self._done = threading.Event()

        def work() -> None:
            for i in range(50):
                if self.cancel_event.is_set():
                    break
                if cb is not None and cb.on_progress is not None:
                    cb.on_progress(i * 2.0, i * 2.0, "assembling")
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
        self.handle: _SlowHandle | None = None

    def submit_job(self, **kw):
        self.handle = _SlowHandle(self._result, kw.get("callbacks"))
        return self.handle


def test_assemble_runner_reports_progress_and_cancels(tmp_path: Path):
    store = SessionStore()
    sess = _session(tmp_path, store)
    song = tmp_path / "song.wav"
    song.write_bytes(b"x")
    params = {"session_id": sess.session_id, "workspace": str(tmp_path / "ws"), "soundtrack": str(song)}
    res = pipeline_result([], timelines={"master": timeline("master", "music")})

    events: list[dict] = []
    make_assemble_runner(store, adapter_factory=lambda: _SlowAdapter(res))(
        params, events.append, threading.Event())
    assert len([e for e in events if e.get("type") == "progress"]) >= 5

    slow = _SlowAdapter(res)
    ev = threading.Event()
    box: dict = {}

    def go() -> None:
        try:
            make_assemble_runner(store, adapter_factory=lambda: slow)(params, lambda e: None, ev)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            box["exc"] = exc

    th = threading.Thread(target=go)
    t0 = time.monotonic()
    th.start()
    time.sleep(0.1)
    ev.set()
    th.join(5)
    assert isinstance(box.get("exc"), InterruptedError)
    assert slow.handle is not None and slow.handle.cancelled
    assert time.monotonic() - t0 < 2.0
