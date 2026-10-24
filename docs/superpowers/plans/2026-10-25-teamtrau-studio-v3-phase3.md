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
| P3.0b | Engine | Runners: `classify`, `analyze_soundtrack`, `assemble` (from session + include/exclude), `preflight`, `deliver` (DeliverSettings), `agent` (with thought/tool events) | next |
| P3.0c | Engine | FreeCut writer carries real take data: per file `data.takes[{shot_id, window, composite, sharpness, aesthetic, content_role, role_confidence, defects, included}]`; remove the placeholder fields | later |
| P3.1 | Studio | TeamTrau sidebar tab skeleton, engine connection status, job list with SSE/poll progress and cancel, i18n en + vi | later |
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
