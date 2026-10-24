# TeamTrau Studio v3 — Phase 3 (TeamTrau panels in the Studio), rolling-wave plan

> **Parent plan:** `2026-10-24-teamtrau-studio-v3.md` (Phase 3 outline).
> **Spec:** `docs/superpowers/specs/2026-10-24-teamtrau-studio-v3-design.md`.
> **Status before Phase 3:**
> - Phase 1 is done and merged into engine `main`.
> - Phase 2 is done on Studio `teamtrau/main`.
> - Gate G4 is accepted at 60 fps after P4.5 fixed FreeCut's pooled-element bug. P4.6 tuning is in progress.

**Rolling wave.** This rule was learned in v1 and re-confirmed in P4.2. Only the next task carries locked tests here. Each later task is detailed, with locked tests the architect has validated against a reference implementation, right before it is sent.

## Facts this plan relies on (from two code surveys, 2026-10-25)

**Engine (`E:\antigravity\TeamTrau-AutoEdit`)**
- The job API has only the `proxies`, `render_cache` and `curate` runners.
- `run_curate`:
  - Calls `curation.run_curation`, which waits on the adapter with **no callbacks and no cancel**. A registry cancel is only noticed after curation ends.
  - **Drops the `CurationSession`**: only the summary and the project id are returned.
- `curation.run_curation` (`src/core/curation.py:100`) calls `adapter.submit_job(..., stage="rough_cut")`.
  - `submit_job` accepts `callbacks: JobCallbacks` and `run_async`.
  - `JobHandle.cancel()` sets the pipeline's cancel event, which the stages check via `_check_cancel`.
- Shot data lives in `ScoredScene` and its JSON-safe `metadata`:
  - `content_role`, `role_confidence`, `included`
  - `golden{source_in, source_out, defects[{type, start, end, description}], trimmed_sec, ...}`
  - `is_long_shot`, `golden_window`
  - The Sequence stage honours `metadata["included"]`.
- **The FreeCut writer's `teamtrau.json` sidecar has placeholder values.** It reads `camera_role` and `motion_energy`, which do not exist on `EditClip`. So every clip shows `role=b_roll`, `score=0.7` and `defects=[]`, and the per-take golden data never reaches the Studio.
- Wrappable functions:

  | Function | Location | Behaviour |
  |---|---|---|
  | `media_classifier.classify_batch` | | Blocking; never raises |
  | `adapter.quick_analyze_audio` | | Blocking; cached |
  | `audio_state_to_dict` | | |
  | `curation.assemble_sequence` | | |
  | `submit_deliver_job` | | Cancellable, with progress |
  | `preflight_timeline(tl).to_dict()` | | |
  | `AutoEditAgentController.run(prompt, callbacks, cancel_event)` | | |
  | Presets | `GENRE_KEYS`, `GENRE_PRESETS[k].display_name`, `CADENCE_NAMES`, the `DeliverSettings` Literal types | |

**Studio (`E:\antigravity\teamtrau-studio`)**
- **Sidebar tab:** the `EditorSidebarTab` union and `SIDEBAR_TABS` in `src/config/editor-workspaces.ts`. The `categories` array and lazy panels in `src/features/editor/components/media-sidebar.tsx`; use the AI tab as the template. Icons come from lucide-react.
- **One undo step:** one `executeTimelineCommand(type, action)` call, which is `execute` in `timeline-command-store.ts`. Inside it, call raw store mutators (`useItemsStore.getState()._addItems`, `setTracks`, `useMarkersStore.getState().setMarkers`), never public actions, because nested `execute` calls each push an undo entry.
  - Item factory: `buildMediaTimelineItems` (`timeline/utils/media-timeline-item-builder.ts:195`).
  - Media import: `useMediaLibraryStore.importHandles(handles, {storageMode: 'link'})`.
- **No ruler overlay hook exists.** Beats, bars and sections need a new component inside the timeline feature, modelled on `timeline-project-markers.tsx`.
- **AI sidecars:** `AiOutputKind` is a closed union in `ai-outputs/types.ts`; adding a `teamtrau` kind is the supported way to read `media/{id}/cache/ai/*.json`.
- **Hotkeys:** `HOTKEYS` in `src/config/hotkeys.ts`, with `react-hotkeys-hook` 5. Panel scoping uses `enabled:`. P and X are unbound. F is `KEYFRAME_FIT` in the dopesheet only.
- **Boundaries:**
  - Cross-feature imports go only through `deps/*-contract.ts`, each with an `export *` wrapper.
  - The edge budgets have little headroom: `editor -> timeline` is 71/73.
  - `check:changed-health` runs on push.
- **Tests:** `vite-plus/test`. Real-store style: `color-timeline-navigator.test.tsx`. Mocked-store style: `media-card.test.tsx`.

## Task list

| Task | Repo | What | Detailed |
|---|---|---|---|
| **P3.0a** | Engine | Server-side curation sessions; `GET /v1/presets`; `GET/PATCH /v1/sessions/{id}`; curate keeps the session, returns per-shot JSON, forwards progress and cancel | **yes (below)** |
| P3.0b | Engine | Runners: `classify`, `analyze_soundtrack`, `preflight`, `assemble` (from session + include/exclude) | **yes (below)** |
| P3.0d | Engine | Runners: `deliver` (DeliverSettings), `agent` (thought/tool events) | **yes (below)** |
| P3.0c | Engine | FreeCut writer carries real take data: per file `data.takes[{shot_id, window, composite, sharpness, aesthetic, content_role, role_confidence, defects, included}]`; remove the placeholder fields | **yes (below)** |
| P3.1 | Studio | TeamTrau sidebar tab skeleton, engine connection status, job list with SSE/poll progress and cancel, i18n en + vi | **yes (below)** |
| P4.3 | Studio + Engine | Auto-proxy on import (preload `webUtils.getPathForFile` → engine `proxies` job → adopt into `content/proxies`), heavy-media heuristics from `needs_proxy` | later |
| P3.2 | Studio | Ingest & roles panel: folder picker (A-roll / B-roll / Music), classify job, role table with override (PATCH) | later |
| P3.3 | Studio | Curation panel: golden-takes grid (thumbnail, score, defects, role), include/exclude with P/X/F hotkeys, "Send to timeline" as ONE undo step | later |
| P3.4 | Studio + timeline hook | Sequence panel: genre/cadence presets, soundtrack analysis, beat/bar/section ruler overlay, assemble job → timeline | later |
| P3.5 | Studio | Deliver panel: DeliverSettings form, pre-flight report, deliver job with progress, open output folder | later |
| P3.6 | Studio | Agent console: prompt box, streamed thoughts and tool calls, cancel | later |
| G3 | Owner | Feature-parity checklist against the PySide6 GUI (scene grid badges, inspector settings, export panel, agent console) → freeze PySide6 (spec S7) | — |

Global rules: R1–R15 from Phase 1/2, the Studio verification ladder, and isolation rules S3/S8. Studio pushes must pass the pre-push hook; `--no-verify` is never allowed.

---

## P3.0a — Curation sessions over the engine API (Engine repo, branch `feature/studio-engine`)

**API**
```python
# src/engine_api/sessions.py
def shot_to_dict(shot_id: str, sc: ScoredScene) -> dict[str, Any]: ...
def session_to_dict(session: CurationSession) -> dict[str, Any]: ...
class SessionStore:
    def __init__(self, max_sessions: int = 8) -> None: ...
    def put(self, session: CurationSession) -> str: ...
    def get(self, session_id: str) -> CurationSession | None: ...
    def patch(self, session_id: str, updates: Mapping[str, Mapping[str, Any]]) -> CurationSession: ...

# src/core/curation.py
def run_curation(adapter, input_dir, genre, output_dir, top_n=TOP_N_DEFAULT, timeout=None,
                 callbacks: JobCallbacks | None = None,
                 cancel_event: threading.Event | None = None) -> tuple[CurationSession, dict]: ...

# src/engine_api/runners.py
def make_curate_runner(sessions: SessionStore,
                       adapter_factory: Callable[[], Any] = StudioBackendAdapter) -> JobRunner: ...
def default_runners(sessions: SessionStore | None = None) -> dict[str, JobRunner]: ...

# src/engine_api/app.py
def create_app(token, *, runners=None, caps_fn=get_caps, registry=None,
               max_body_bytes=1_000_000, sessions: SessionStore | None = None) -> Starlette: ...
```

**Rules**
- **`shot_to_dict`:** returns exactly the keys `shot_id, clip_path, start, end, duration, composite, sharpness, aesthetic, motion_energy, content_role, role_confidence, included, golden, defects`.
  - `included` defaults to `True`.
  - `golden` is a plain copy of `metadata["golden"]`, or `None`.
  - `defects` is `golden["defects"]`, or `[]`.
  - The result must be JSON-serialisable.
- **`session_to_dict`:** returns `{session_id, input_dir, takes, shots}`, with shots in natural id order (A01, A02, …, B01, …).
- **`SessionStore`:**
  - Thread-safe LRU: `get` refreshes recency, and `put` evicts the least recently used session beyond `max_sessions`.
  - **`patch` validation:** only the fields `included` (must be a bool) and `content_role` (must be `a_roll`, `b_roll` or `ambient_b_roll`) may be patched. Raise `KeyError` for an unknown session or shot, and `ValueError` for any other field or a bad value. Validate everything before mutating anything.
  - **`patch` effect:** it writes into `ScoredScene.metadata`, so the Sequence stage honours it.
- **`run_curation`:**
  - Forwards `callbacks` to `submit_job`.
  - When `cancel_event` is given, it submits with `run_async=True` and polls `handle.wait(0.2)`. When `cancel_event` is set, it calls `handle.cancel()` and raises `InterruptedError("Curation cancelled")`. The existing `timeout` behaviour is kept.
  - Without `cancel_event`, the behaviour is unchanged. The existing curation tests must stay green unedited.
- **`make_curate_runner`:**
  - **Params:** `input_dir`, `workspace`, and the optional `genre`, `top_n`, `output_dir`.
  - **Wiring:** it maps `JobCallbacks.on_progress(stage_pct, total_pct, msg)` to `emit({"type": "progress", "pct": total_pct, "msg": msg})`, and passes the registry `cancel_event` through.
  - **Output:** it stores the session in `sessions` and writes the FreeCut project from `edit_timelines["rough_cut"]` (or `"master"`). It returns `{"session_id", "project_id", "summary", "shots"}`, where `shots` is `session_to_dict(...)["shots"]`.
  - `default_runners(sessions)` uses it for `"curate"`, and `main.py serve` shares one `SessionStore` between the app and the runners.
- **`GET /v1/presets`** (auth required):
  - `{"genres": [{"key", "display_name"}] for every key in GENRE_KEYS, "cadences": list(CADENCE_NAMES), "deliver": {"master", "fit_mode", "subtitles", "color": lists from the DeliverSettings Literal types}}`.
  - For the `rough_cut_assembly` key, `display_name` comes from its preset object.
- **`GET /v1/sessions/{id}`** returns `session_to_dict`, or 404.
- **`PATCH /v1/sessions/{id}`:**
  - Body: `{"shots": {shot_id: {field: value}}}`.
  - Errors: 400 for invalid JSON, a non-object body, or a top-level key other than `shots`; 404 for an unknown session or shot; 400 for an invalid field or value.
  - Success: 200 with the updated session dict.

**Files:**
- `src/engine_api/sessions.py` (new)
- `src/engine_api/app.py`
- `src/engine_api/runners.py`
- `src/core/curation.py`
- `main.py` (serve wiring only)
- `tests/test_engine_sessions.py` (LOCKED)

**Related suites:** `tests/test_engine_api.py`, `tests/test_curation_core.py`, `tests/test_curation_polish.py`, `tests/test_macro_mcp_tools.py`.

**Contract test — `tests/test_engine_sessions.py`:**
```python
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
```
**Expected red:** `ImportError: cannot import name 'make_curate_runner' from 'src.engine_api.runners'`. The `src.engine_api.sessions` module does not exist yet either.

**Note for R1:** `ruff check --fix --select I tests/test_engine_sessions.py` is allowed once `sessions.py` exists. Ruff sorts the new first-party module differently until it exists.

**Commit:** `feat(engine-api): curation sessions, presets endpoint, curate progress and cancel`.


---

## P3.0b — Panel job runners: classify, analyze_soundtrack, preflight, assemble (Engine repo, branch `feature/studio-engine`)

Builds on P3.0a: `SessionStore`, `make_curate_runner`, and `run_curation` with callbacks and cancel. `deliver` and `agent` follow in P3.0d.

**API** (all in `src/engine_api/runners.py`; a factory returns a `JobRunner`)
```python
def make_classify_runner(classify_fn: Callable[..., list[ClassificationResult]] | None = None,
                         chunk_size: int = 4) -> JobRunner: ...
def make_soundtrack_runner(analyze_fn: Callable[[Path], Any] | None = None) -> JobRunner: ...
def make_preflight_runner() -> JobRunner: ...
def make_assemble_runner(sessions: SessionStore,
                         adapter_factory: Callable[[], Any] = StudioBackendAdapter) -> JobRunner: ...

# src/core/curation.py
def assemble_sequence(adapter, session, soundtrack=None, selected_ids=None, cadence=None, timeout=None,
                      callbacks: JobCallbacks | None = None,
                      cancel_event: threading.Event | None = None) -> dict[str, Any]: ...
```

**Rules**
- **classify**
  - **Params:** `{"input_dir"}`. Raise `FileNotFoundError` if it is not a directory.
  - **Inputs:** collect files with `collect_video_files`, **sorted by path**, then classify them in chunks of `chunk_size` with `classify_fn(chunk, max_workers=chunk_size)`. The default `classify_fn` is `media_classifier.classify_batch`.
  - **Cancel and progress:** check `cancel_event` before each chunk and raise `InterruptedError` if it is set. After each chunk, emit `{"type": "progress", "pct": 100 * done / total}`, so the last event is `100.0`.
  - **Result:** `{"files": [...], "skipped": [str], "soundtrack": str | None}`.
    - Each file entry is `{path, role (value string), confidence, has_speech, reasons, error, signals (dataclasses.asdict or None)}`.
    - `soundtrack` comes from `find_soundtrack(input_dir)`.
- **analyze_soundtrack**
  - **Params:** `{"path"}`. Raise `FileNotFoundError` if it is not a file.
  - **Result:** `{"path": str, **audio_state_to_dict(analyze_fn(path))}`.
  - The default `analyze_fn` is `StudioBackendAdapter().quick_analyze_audio`.
- **preflight**
  - **Params:** `{"workspace", "project_id"}`.
  - **Result:** `preflight_timeline(read_freecut_project(...).timeline).to_dict()`.
- **assemble**
  - **Params:** `{"session_id", "workspace"}` plus the optional `soundtrack`, `cadence` and `selected_ids`.
  - **Errors:**
    - An unknown session raises `KeyError`.
    - Validation errors from `assemble_sequence` propagate: an unknown shot id, a missing soundtrack, or a bad cadence raise `ValueError` **before** any adapter call.
  - **Behaviour:**
    - Progress and cancel are wired exactly as in curate. `assemble_sequence` gets the same callbacks / `run_async` / poll / `handle.cancel()` treatment that `run_curation` got in P3.0a.
    - Shots keep their session metadata, so `included: false` from a PATCH reaches the Sequence stage.
  - **Result:** writes the assembled timeline (`session.sequence.edit_timelines[<key from timeline_id>]`) as a new FreeCut project, then returns `{"project_id", "timeline_id", "total_cuts", "duration_sec"}`.
- **Wiring:** `default_runners(sessions)` registers the new kinds `classify`, `analyze_soundtrack`, `preflight` and `assemble`.

**Files:**
- `src/engine_api/runners.py`
- `src/core/curation.py`
- `tests/test_engine_runners_p30b.py` (LOCKED)

**Related suites:**
- `tests/test_engine_sessions.py`
- `tests/test_engine_api.py`
- `tests/test_curation_core.py`
- `tests/test_macro_mcp_tools.py`

**Contract test — `tests/test_engine_runners_p30b.py`:**
```python
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
```
**Expected red:** `ImportError: cannot import name 'make_assemble_runner' (or make_classify_runner) from 'src.engine_api.runners'`.

**Commit:** `feat(engine-api): classify, soundtrack, preflight and assemble job runners`.


---

## P3.0c — The FreeCut writer carries real take data (Engine repo, branch `feature/studio-engine`)

**Problem.** `write_freecut_project` fills the `media/{id}/cache/ai/teamtrau.json` sidecar from `EditClip` attributes that do not exist (`camera_role`, `motion_energy`). As a result:
- every clip gets `role="b_roll"`, `score=0.7` and `defects=[]`;
- the curated takes never reach the Studio.

**API**
```python
def write_freecut_project(timeline, workspace, project_name, proxies=None,
                          shots: Mapping[str, ScoredScene] | None = None) -> str: ...
```

**Rules**
- **Sidecar `data` keys:** exactly `{sourcePath, role, score, is_golden, takes}`. The envelope (`schemaVersion`, `kind`, `mediaId`, `service`, `model`, `params`, `createdAt`, `updatedAt`) is unchanged.
- **Assigning takes to sources:** a take belongs to the source `Path(sc.scene.original_path or sc.scene.clip_path).resolve()`. Takes are listed in natural shot-id order (A01, A02, …, B01, …).
- **Each take:** `{shot_id, start, end, composite, sharpness, aesthetic, content_role, role_confidence, included (default True), golden, defects}`.
  - `golden` is the `metadata["golden"]` dict without its `defects` key, or `None`.
  - `defects` is `golden["defects"]`, or `[]`.
- **Source summary:**
  - `role` is the soundtrack's fixed role `"music"`, or otherwise the `content_role` of the highest-`composite` take, or `None`.
  - `score` is the best take's `composite`, or `None`.
  - `is_golden` is true when any take has `golden`.
  - No placeholder values: without shots, use `role=None`, `score=None`, `is_golden=False`, `takes=[]`.
- **Callers:** `make_curate_runner` and `make_assemble_runner` (P3.0b) pass `shots=session.shots`.
- **Compatibility:** the existing tests in `tests/test_freecut_writer.py` and `tests/integration/test_freecut_writer_real.py` stay green unedited.

**Files:**
- `src/integrations/freecut_writer.py`
- `src/engine_api/runners.py`
- `tests/test_freecut_writer_takes.py` (LOCKED)

**Related suites:**
- `tests/test_freecut_writer.py`
- `tests/test_freecut_writer_index.py`
- `tests/integration/test_freecut_writer_real.py`
- `tests/test_engine_sessions.py`
- `tests/test_engine_runners_p30b.py`

**Contract test — `tests/test_freecut_writer_takes.py`:**
```python
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
```
**Expected red:** `TypeError: write_freecut_project() got an unexpected keyword argument 'shots'`.

**Commit:** `feat(integrations): FreeCut writer carries curated take data (no placeholder sidecar values)`.


---

## P3.0d — Deliver and agent job runners (Engine repo, branch `feature/studio-engine`)

**API** (`src/engine_api/runners.py`)
```python
def make_deliver_runner(adapter_factory: Callable[[], Any] = StudioBackendAdapter) -> JobRunner: ...
def make_agent_runner(controller_factory: Callable[[], Any] | None = None) -> JobRunner: ...
```

**Rules**

**deliver**
- **Params:** `{"workspace", "project_id", "output_dir"}` plus the optional `formats` (list), `settings` (dict), `preview` and `force_cpu`.
- **Timeline:** read it from the FreeCut project with `read_freecut_project`. This is the timeline the user edited in the Studio.
- **Validation, before any adapter call:**
  - Every entry in `formats` must be in `ALL_DELIVER_FORMATS`. The default `None` means all formats.
  - `settings` keys must be among the keyword names of `build_deliver_settings`: `preset, canvas, fps, fit, color, master, subtitles, audio_rate, audio_channels, variants, relink, watermark_in_nle`.
  - Any violation raises `ValueError`.
- **Settings:** `build_deliver_settings(DeliverSettings.from_timeline(tl), **settings)`. Its `ValueError` propagates.
- **Submit:** call `submit_deliver_job(edit_timeline=tl, output_dir, config=PipelineConfig(), formats=set(...) or None, callbacks, run_async=True, preview, force_cpu, settings)`.
- **Progress and cancel:** progress is wired as in curate. Polling and cancel work as in curate: `handle.cancel()`, then `InterruptedError`.
- **Result handling:**
  - `result.cancelled` raises `InterruptedError`.
  - `not result.success` raises `RuntimeError(result.error_message)`.
  - Otherwise return exactly the keys `{success, output_dir, render_path, encoder_used, fcpxml_path, premiere_xml_path, otio_path, capcut_draft_path, manifest_path, preflight_path, preflight_issues}`. Paths become `str` or `None`.

**agent**
- **Params:** `{"prompt"}` and the optional `system_prompt`. A blank prompt raises `ValueError`.
- **Run:** `controller.run(prompt, callbacks=AgentCallbacks(...), cancel_event=<registry event>, system_prompt=...)`.
- **Events emitted:**
  - `{"type": "thought", "text"}`
  - `{"type": "tool_call", "name", "args"}`
  - `{"type": "tool_result", "name", "result": str(result)[:2000]}`
  - `{"type": "response", "text"}`
  - `{"type": "error", "message"}`
- **Return:** `{success, response, provider, model, steps (count), error}`.

**Wiring:** `default_runners(sessions)` registers `deliver` and `agent`.

**Files:**
- `src/engine_api/runners.py`
- `tests/test_engine_runners_p30d.py` (LOCKED)

**Related suites:**
- `tests/test_engine_api.py`
- `tests/test_engine_runners_p30b.py`
- `tests/test_deliver_cli.py`
- `tests/test_agent_controller*.py`

**Contract test — `tests/test_engine_runners_p30d.py`:**
```python
"""P3.0d: deliver and agent job runners for the Studio panels."""

import threading
import time
from pathlib import Path

import pytest

from src.core.types import DeliverResult
from src.engine_api.runners import make_agent_runner, make_deliver_runner
from src.integrations.freecut_writer import write_freecut_project
from src.modules.agent_controller import AgentResult, AgentStep
from tests.curation_fakes import timeline

RESULT_KEYS = {"success", "output_dir", "render_path", "encoder_used", "fcpxml_path",
               "premiere_xml_path", "otio_path", "capcut_draft_path", "manifest_path",
               "preflight_path", "preflight_issues"}


class _Handle:
    def __init__(self, result: DeliverResult, slow: bool = False, cb=None):
        self.cancelled = False
        self._r = result
        self._done = threading.Event()
        self._cancel = threading.Event()

        def work() -> None:
            for i in range(50 if slow else 3):
                if self._cancel.is_set():
                    break
                if cb is not None and cb.on_progress is not None:
                    cb.on_progress(i * 2.0, i * 2.0, "render")
                time.sleep(0.02 if slow else 0)
            self._done.set()

        threading.Thread(target=work, daemon=True).start()

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def cancel(self) -> bool:
        self.cancelled = True
        self._cancel.set()
        return True

    @property
    def result(self):
        return self._r


class _Adapter:
    def __init__(self, result: DeliverResult, slow: bool = False):
        self.result = result
        self.slow = slow
        self.kw: dict = {}
        self.handle: _Handle | None = None

    def submit_deliver_job(self, **kw):
        self.kw = kw
        self.handle = _Handle(self.result, self.slow, kw.get("callbacks"))
        return self.handle


def _project(tmp_path: Path) -> tuple[Path, str]:
    ws = tmp_path / "ws"
    return ws, write_freecut_project(timeline(), ws, "Deliver")


def test_deliver_runner_reads_project_and_builds_settings(tmp_path: Path):
    ws, pid = _project(tmp_path)
    out_dir = tmp_path / "out"
    res = DeliverResult(success=True, output_dir=out_dir, render_path=out_dir / "m.mp4",
                        encoder_used="h264_nvenc", fcpxml_path=out_dir / "m.fcpxml",
                        preflight_issues=["warning:AUDIO_MONO:c.mp4"])
    adapter = _Adapter(res)
    events: list[dict] = []
    out = make_deliver_runner(adapter_factory=lambda: adapter)(
        {"workspace": str(ws), "project_id": pid, "output_dir": str(out_dir),
         "formats": ["mp4", "fcpxml"], "settings": {"master": "h264", "fit": "fill"}},
        events.append, threading.Event())
    kw = adapter.kw
    assert len(kw["edit_timeline"].clips) == 1
    assert Path(kw["output_dir"]) == out_dir
    assert kw["formats"] == {"mp4", "fcpxml"}
    assert kw["settings"].master == "h264" and kw["settings"].fit_mode == "fill"
    assert kw["run_async"] is True and kw["preview"] is False
    assert set(out) == RESULT_KEYS
    assert out["success"] is True and out["encoder_used"] == "h264_nvenc"
    assert out["render_path"] == str(out_dir / "m.mp4") and out["premiere_xml_path"] is None
    assert out["preflight_issues"] == ["warning:AUDIO_MONO:c.mp4"]
    assert [e for e in events if e.get("type") == "progress"]


def test_deliver_runner_validates_before_submitting(tmp_path: Path):
    ws, pid = _project(tmp_path)
    adapter = _Adapter(DeliverResult(success=True))
    run = make_deliver_runner(adapter_factory=lambda: adapter)
    base = {"workspace": str(ws), "project_id": pid, "output_dir": str(tmp_path / "o")}
    with pytest.raises(ValueError):
        run({**base, "formats": ["mp4", "gif"]}, lambda e: None, threading.Event())
    with pytest.raises(ValueError):
        run({**base, "settings": {"master": "xyz"}}, lambda e: None, threading.Event())
    with pytest.raises(ValueError):
        run({**base, "settings": {"bitrate": 5}}, lambda e: None, threading.Event())
    assert adapter.kw == {}


def test_deliver_runner_raises_on_failed_result(tmp_path: Path):
    ws, pid = _project(tmp_path)
    adapter = _Adapter(DeliverResult(success=False, error_message="NVENC exploded"))
    with pytest.raises(RuntimeError, match="NVENC exploded"):
        make_deliver_runner(adapter_factory=lambda: adapter)(
            {"workspace": str(ws), "project_id": pid, "output_dir": str(tmp_path / "o")},
            lambda e: None, threading.Event())


def test_deliver_runner_cancels(tmp_path: Path):
    ws, pid = _project(tmp_path)
    adapter = _Adapter(DeliverResult(success=True), slow=True)
    ev = threading.Event()
    box: dict = {}

    def go() -> None:
        try:
            make_deliver_runner(adapter_factory=lambda: adapter)(
                {"workspace": str(ws), "project_id": pid, "output_dir": str(tmp_path / "o")},
                lambda e: None, ev)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            box["exc"] = exc

    th = threading.Thread(target=go)
    t0 = time.monotonic()
    th.start()
    time.sleep(0.1)
    ev.set()
    th.join(5)
    assert isinstance(box.get("exc"), InterruptedError)
    assert adapter.handle is not None and adapter.handle.cancelled
    assert time.monotonic() - t0 < 2.0


class _Controller:
    def __init__(self):
        self.seen: dict = {}

    def run(self, prompt, callbacks=None, cancel_event=None, system_prompt=None):
        self.seen = {"prompt": prompt, "cancel_event": cancel_event, "system_prompt": system_prompt}
        callbacks.on_thought("plan the edit")
        callbacks.on_tool_call("curate_footage", {"input_dir": "D:/f"})
        callbacks.on_tool_result("curate_footage", {"takes": 3, "blob": "x" * 5000})
        callbacks.on_agent_response("done")
        return AgentResult(success=True, response="done", steps=[AgentStep(step_number=1)],
                           provider_used="heuristic", model_used="heuristic")


def test_agent_runner_streams_events_and_returns_summary():
    ctl = _Controller()
    ev = threading.Event()
    events: list[dict] = []
    out = make_agent_runner(controller_factory=lambda: ctl)(
        {"prompt": "Dựng video dọc 9:16", "system_prompt": "be brief"}, events.append, ev)
    assert ctl.seen == {"prompt": "Dựng video dọc 9:16", "cancel_event": ev, "system_prompt": "be brief"}
    assert [e["type"] for e in events] == ["thought", "tool_call", "tool_result", "response"]
    assert events[0] == {"type": "thought", "text": "plan the edit"}
    assert events[1] == {"type": "tool_call", "name": "curate_footage", "args": {"input_dir": "D:/f"}}
    assert events[2]["name"] == "curate_footage" and isinstance(events[2]["result"], str)
    assert len(events[2]["result"]) <= 2000
    assert events[3] == {"type": "response", "text": "done"}
    assert out == {"success": True, "response": "done", "provider": "heuristic",
                   "model": "heuristic", "steps": 1, "error": None}


def test_agent_runner_rejects_empty_prompt():
    with pytest.raises(ValueError):
        make_agent_runner(controller_factory=_Controller)({"prompt": "  "}, lambda e: None,
                                                          threading.Event())
```
**Expected red:** `ImportError: cannot import name 'make_agent_runner' (or make_deliver_runner) from 'src.engine_api.runners'`.

**Commit:** `feat(engine-api): deliver and agent job runners`.


---

## P3.1 — TeamTrau sidebar tab: engine status and jobs (Studio repo, branch `teamtrau/main`)

**API**
```ts
// src/features/teamtrau/engine/engine-client.ts (extend)
getEvents(id: string): Promise<{ events: Record<string, unknown>[] }>   // GET {base}/v1/jobs/{id}/events?format=json

// src/features/teamtrau/jobs/jobs-store.ts (new, zustand/vanilla — already a dependency)
export interface EngineJobsClient { submitJob; getJob; getEvents; cancel }   // structural subset of the engine client
export type JobStatus = 'running' | 'done' | 'failed' | 'cancelled'
export interface EngineJobView { id; kind; status; pct; message; result; error; startedAt }
export function createJobsStore(client: EngineJobsClient, opts?: { pollMs?: number; now?: () => number })
// state: { jobs: Record<string, EngineJobView>; order: string[] (newest first); lastError: string | null;
//          start(kind, params): Promise<string>; refresh(id): Promise<void>; cancel(id): Promise<void>; dispose(): void }
```

**Rules**
- **`start`**
  - Calls `client.submitJob`.
  - On failure: sets `lastError` to the error message, rethrows, and adds no job.
  - On success: adds a view with `{status 'running', pct 0, message '', result null, error null, startedAt: now()}` at the front of `order`.
  - When `pollMs > 0` (default 500), it schedules polling: `refresh` every `pollMs` until the status is not `running`. `dispose` clears every timer.
- **`refresh(id)`**
  - Fetches the job and its events.
  - `pct` comes from the last `progress` event, or stays the same when there is none. It is `100` when the status is `done`.
  - `message` comes from the last progress `msg`.
  - Copies `status`, `result` and `error`.
- **`cancel(id)`:** calls `client.cancel(id)`, then `refresh(id)`.

**Panel**
- New file `src/features/teamtrau/components/teamtrau-panel.tsx`.
- **Engine section:**
  - Health dot; shows `capabilities.h264_encoders[0]` (the best encoder) and `torch_device`.
  - A Retry button.
  - Outside Electron (`window.teamtrauHost?.isElectron` false), it shows a "requires the desktop app" note.
- **Jobs section:** newest first. Each row shows the kind, status badge, a progress bar (`src/components/ui/progress`), the message, the error in red, and a Cancel button while running.
- Use one module-level store, created with the default engine client.
- Sub-tab shell for the later panels (Ingest, Curation, Sequence, Deliver, Agent):
  - Render only the tabs that exist.
  - P3.1 ships **Engine & Jobs**.
- Use `src/components/ui/*` and lucide icons. No new dependencies.

**Sidebar hook** (the FreeCut edits allowed for this task)
- `src/config/editor-workspaces.ts`: add `'teamtrau'` to `EditorSidebarTab` and to `SIDEBAR_TABS`.
- `src/features/editor/components/media-sidebar.tsx`:
  - Add a lazy panel and a category with icon `Clapperboard` and label `t('teamtrau.tabLabel')`.
  - Add the activate-once block, following the AI tab exactly.
- New `src/features/editor/deps/teamtrau-contract.ts`, which exports `importTeamTrauPanel = () => import('@/features/teamtrau/components/teamtrau-panel')`, plus `deps/teamtrau.ts` (`export * from './teamtrau-contract'`).
- Edge budgets must still pass.

**i18n**
- `src/i18n/locales/partials/<lang>/teamtrau.json` for **every** language directory. The lazy-locale test needs the same partial files for all languages.
- Translate `en` and `vi` properly. Other languages may reuse the English strings.
- The Vietnamese locked test still holds: same keys, same placeholders, and at least 85% of strings translated overall.

**Tests**
- The LOCKED store test below.
- Your own component test, using the mocked-store style of `media-card.test.tsx`: it renders the jobs list, the Cancel button calls `cancel`, and the engine-down state shows the error.

**Manual acceptance (paste in the HANDOFF):**
- In `npm run electron:dev`, the TeamTrau tab shows the engine connected, with the GPU encoder and `torch_device=cuda`.
- Start a classify job on `E:\antigravity\perf-samples` from the devtools console (`teamtrau` store API) or a temporary debug button. A temporary button must be removed before commit.
- Progress and the done state appear in the list.
- Save a screenshot under the engine repo at `docs/superpowers/handoffs/p31_teamtrau_tab.png`.

**Ladder**
- the Studio ladder
- `check:deps-contracts`
- `check:deps-wrapper-health`
- `check:edge-budgets`
- `check-fallow-changed-health`
- `vp build`
- push WITH the hook

**Files:**
- `src/features/teamtrau/engine/engine-client.ts`
- `src/features/teamtrau/jobs/*`
- `src/features/teamtrau/components/teamtrau-panel.tsx` (+ test)
- `src/features/editor/deps/teamtrau*.ts`
- `src/config/editor-workspaces.ts`
- `src/features/editor/components/media-sidebar.tsx`
- `src/i18n/locales/partials/*/teamtrau.json`
- `src/features/teamtrau/jobs/__tests__/jobs-store.test.ts` (LOCKED)

**Contract test — `src/features/teamtrau/jobs/__tests__/jobs-store.test.ts`:**
```ts
import { describe, expect, it, vi } from 'vitest'

import { createJobsStore, type EngineJobsClient } from '../jobs-store'

type Job = { job_id: string; status: string; result: unknown; error: string | null }

function fakeClient() {
  const jobs = new Map<string, Job>()
  const events = new Map<string, Record<string, unknown>[]>()
  let next = 0
  const client: EngineJobsClient = {
    submitJob: vi.fn(async (kind: string) => {
      if (kind === 'down') throw new Error('engine not running')
      const id = `j${++next}`
      jobs.set(id, { job_id: id, status: 'running', result: null, error: null })
      events.set(id, [])
      return id
    }),
    getJob: vi.fn(async (id: string) => jobs.get(id)! as never),
    getEvents: vi.fn(async (id: string) => ({ events: events.get(id) ?? [] })),
    cancel: vi.fn(async (id: string) => {
      jobs.get(id)!.status = 'cancelled'
      return { cancelled: true }
    }),
  }
  return { client, jobs, events }
}

describe('engine jobs store', () => {
  it('starts a job, newest first', async () => {
    const { client } = fakeClient()
    const store = createJobsStore(client, { pollMs: 0, now: () => 1000 })
    const a = await store.getState().start('classify', { input_dir: 'D:/f' })
    const b = await store.getState().start('curate', { input_dir: 'D:/f' })
    expect(client.submitJob).toHaveBeenCalledWith('classify', { input_dir: 'D:/f' })
    expect(store.getState().order).toEqual([b, a])
    expect(store.getState().jobs[a]).toMatchObject({
      id: a,
      kind: 'classify',
      status: 'running',
      pct: 0,
      message: '',
      result: null,
      error: null,
      startedAt: 1000,
    })
  })

  it('applies progress events and the final result on refresh', async () => {
    const { client, jobs, events } = fakeClient()
    const store = createJobsStore(client, { pollMs: 0 })
    const id = await store.getState().start('curate', {})
    events.get(id)!.push({ type: 'progress', pct: 40, msg: 'scoring' })
    await store.getState().refresh(id)
    expect(store.getState().jobs[id]).toMatchObject({ status: 'running', pct: 40, message: 'scoring' })

    jobs.get(id)!.status = 'done'
    jobs.get(id)!.result = { session_id: 's1' }
    events.get(id)!.push({ type: 'done', result: { session_id: 's1' } })
    await store.getState().refresh(id)
    expect(store.getState().jobs[id]).toMatchObject({
      status: 'done',
      pct: 100,
      result: { session_id: 's1' },
    })
  })

  it('records failures and cancellation', async () => {
    const { client, jobs } = fakeClient()
    const store = createJobsStore(client, { pollMs: 0 })
    const bad = await store.getState().start('deliver', {})
    jobs.get(bad)!.status = 'failed'
    jobs.get(bad)!.error = 'NVENC exploded'
    await store.getState().refresh(bad)
    expect(store.getState().jobs[bad]).toMatchObject({ status: 'failed', error: 'NVENC exploded' })

    const c = await store.getState().start('assemble', {})
    await store.getState().cancel(c)
    expect(client.cancel).toHaveBeenCalledWith(c)
    expect(store.getState().jobs[c]?.status).toBe('cancelled')
  })

  it('does not add a job when the engine is unreachable', async () => {
    const { client } = fakeClient()
    const store = createJobsStore(client, { pollMs: 0 })
    await expect(store.getState().start('down', {})).rejects.toThrow('engine not running')
    expect(store.getState().order).toEqual([])
    expect(store.getState().lastError).toBe('engine not running')
  })

  it('polls running jobs until they finish when pollMs > 0', async () => {
    vi.useFakeTimers()
    try {
      const { client, jobs } = fakeClient()
      const store = createJobsStore(client, { pollMs: 100 })
      const id = await store.getState().start('classify', {})
      await vi.advanceTimersByTimeAsync(250)
      expect((client.getJob as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThanOrEqual(2)
      jobs.get(id)!.status = 'done'
      await vi.advanceTimersByTimeAsync(150)
      const calls = (client.getJob as ReturnType<typeof vi.fn>).mock.calls.length
      await vi.advanceTimersByTimeAsync(500)
      expect((client.getJob as ReturnType<typeof vi.fn>).mock.calls.length).toBe(calls)
      expect(store.getState().jobs[id]?.status).toBe('done')
      store.getState().dispose()
    } finally {
      vi.useRealTimers()
    }
  })
})
```
**Expected red:** `Failed to resolve import "../jobs-store"`.

**Commit:** `feat(teamtrau): TeamTrau sidebar tab with engine status and job progress`.
