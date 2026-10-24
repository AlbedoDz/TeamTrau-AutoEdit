# TeamTrau Studio v3 — GUI, Playback & Cross-Platform Design

- **Status:** Proposed (needs owner sign-off on §2 decisions before Phase 1)
- **Date:** 2026-10-24
- **Author:** Claude Opus (architect / orchestrator); implementation by Antigravity agents per plan v3
- **Inputs:** five read-only studies of FreeCut (architecture + playback), FilmCraft, Concat and the TeamTrau codebase (summaries in §8)

---

## 1. Problem

Owner requirements (2026-10-24):
1. The current PySide6 GUI is ugly and unfriendly; the product needs a **professional, complete editor GUI**.
2. **Real-time, stutter-free preview/playback** of 4K, 60 fps and 120 fps footage while editing.
3. Run on **Windows, macOS and Linux**.
4. Do **not** depend on deep coupling to an upstream project; **cherry-pick** the best features of FreeCut, FilmCraft and Concat that TeamTrau lacks.

What the inventory found in the current GUI (`src/ui/**`, ~11k lines):
- The player is **silent** (no audio playback at all); decode is OpenCV with wall-clock sync and no proxies.
- The timeline has **no manual editing**: no drag/move, trim, split/razor, roll/slip/slide, user markers or extra tracks (fixed V2/V1/A1). Only select, ripple-delete and copy/paste exist.
- Inspector parameter changes are not undoable; the GUI has never been manually verified.

Conclusion: "prettier Qt" is not enough. Reaching the bar means writing a full NLE front end (timeline editing, audio-synced player, keyframes, effects, source monitor). Writing that from scratch in Qt is XL.

What is strong and must be kept: the **Python engine** (`StudioBackendAdapter`, Qt-free, with job/progress/cancel model), its pipelines (curation, golden takes, cadence, reframe, Whisper, NLE export, NVENC deliver), the CLI and 26 MCP tools, and ~1070 tests.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| S1 | **GUI = fork of FreeCut (MIT), rebranded "TeamTrau Studio".** FreeCut's in-browser engine (WebCodecs/WebGPU preview, timeline, keyframes, effects, export) is kept **untouched**. | FreeCut already ships every editor feature we lack (multi-track timeline with ripple/roll/slip/slide/rate-stretch, source monitor, keyframe graph, 100+ effects, audio mixer, transitions, i18n, proxies, undo). React/Tailwind/shadcn is the easiest stack to make beautiful. Concat is AGPL (cannot be cloned); FilmCraft is Rust/egui (would force a language switch). |
| S2 | **Engine = existing Python, run as a local service** (`teamtrau-engine`), using `starlette` + `uvicorn` (already in the venv through `mcp`; no new dependency). | One engine for Studio, CLI and MCP (same principle as FilmCraft/Concat: one engine path). |
| S3 | **All TeamTrau UI lives in new files** under `src/features/teamtrau/` plus one `deps/teamtrau.ts` adapter, one sidebar tab and minimal one-line hooks in shared files. FreeCut internal ids (`freecut-*` storage keys, `.freecut-workspace.json`, `window.freecut`) are **not renamed**; only display strings/branding change. | Keeps upstream merges at "L" instead of "XL" (FreeCut ships weekly). |
| S4 | **Data hand-off by files, not live mutation.** Python writes FreeCut workspace files: `projects/{id}/project.json` (schema v15, frame-integer items, `mediaId` refs), `media/{id}/metadata.json` (+ link, no copy), and analysis sidecars `media/{id}/cache/ai/{scenes,beats,golden,roles}.json`. Studio then loads/reloads through an `execute()`-wrapped command (one undo step). | Project JSON and the AI-sidecar envelope are FreeCut's existing seams; avoids the autosave race of writing into an open project. |
| S5 | **Desktop shell = Electron** (bundled Chromium on all three OSes); the Python engine is a sidecar process started by Electron. Tauri rejected: Linux WebKitGTK lacks WebCodecs/WebGPU. | Identical behaviour on Windows/macOS/Linux; FreeCut requires Chromium ≥ 113. |
| S6 | **Playback: proxies are produced by the Python engine automatically**, not by the browser. 1080p (and 720p) **all-intra or short-GOP H.264 8-bit 4:2:0, CFR at project fps, with audio**, using the OS hardware encoder (NVENC / VideoToolbox / QSV / AMF / VAAPI, x264 fallback); log→Rec.709 LUT option. Originals are always used for export / NLE relink. | Chrome cannot hardware-decode Sony 10-bit 4:2:2 or (without the extension) HEVC on Windows; FreeCut's own proxies are manual, 960×540, long-GOP, video-only; Concat/FilmCraft never validated 60/120 fps. H.264 8-bit is the one format every Chromium decodes in hardware. |
| S7 | **PySide6 GUI is frozen, then retired** once Studio reaches parity (Phase 3 gate). CLI and MCP stay forever. | No big-bang rewrite; users keep a working tool. |
| S8 | **License rules:** FreeCut MIT (fork, keep LICENSE + NOTICE); FilmCraft Apache-2.0 (may port code with attribution in NOTICE); **Concat AGPL-3.0: ideas only, never copy code** (clean-room; reviewers reject Concat-derived code). | Avoid forcing AGPL on TeamTrau. |
| S9 | **Measured performance targets** (§5) are acceptance criteria, measured on the reference machine (RTX 3070 Laptop, Windows 11) and on one macOS + one Linux machine before release. | FilmCraft/Concat publish no 60/120 fps numbers; we must prove ours. |

## 3. Architecture

```
┌──────────────────────── Electron (Win / macOS / Linux) ────────────────────────┐
│  TeamTrau Studio = FreeCut fork (React, WebCodecs, WebGPU, Zustand)             │
│   ├─ FreeCut editor (timeline, monitors, keyframes, effects, mixer, export)     │
│   └─ features/teamtrau/  (Ingest & Roles, Curation, Sequence/Cadence,           │
│        Deliver/NLE, Proxies, Agent) ──── typed client ───┐                      │
│  main process: workspace path, COOP/COEP, sidecar lifecycle, packaging          │
└──────────────────────────────────────────────────────────┼──────────────────────┘
                                                           │ HTTP + SSE on 127.0.0.1 (token)
┌──────────────────────────────────────────────────────────▼──────────────────────┐
│  teamtrau-engine (Python, starlette/uvicorn)  — wraps StudioBackendAdapter      │
│   jobs: curate | sequence | deliver | proxies | transcribe   (progress via SSE)  │
│   writers: FreeCut project.json + media metadata + cache/ai sidecars            │
│   platform_caps: OS / GPU / hw encoder + decoder / Resolve paths                │
│   same engine behind CLI (main.py) and MCP (mcp_server.py)                      │
└─────────────────────────────────────────────────────────────────────────────────┘
```

Engine API (v1, JSON, token in header, loopback only — hardening rules borrowed from FilmCraft/Concat: reject non-JSON, cap body size, cap connections):
- `GET /v1/health`, `GET /v1/capabilities` (platform_caps: encoders, decoders, GPU, versions).
- `POST /v1/jobs/{curate|sequence|deliver|proxies|transcribe}` → `{job_id}`; `GET /v1/jobs/{id}`; `GET /v1/jobs/{id}/events` (SSE: stage, progress, log, done); `POST /v1/jobs/{id}/cancel`.
- `POST /v1/freecut/import` → writes project/media/sidecars into a given FreeCut workspace path, returns `{project_id, revision}`.

## 4. Feature plan (cherry-pick map)

Already provided by the FreeCut fork (no work beyond theming): multi-track timeline, ripple/roll/slip/slide/rate-stretch, razor/split, source monitor, keyframes + graph editor, effects/transitions/blend modes, audio mixer + EQ, captions/text, proxies (browser), undo/redo, i18n, scopes, silence/filler removal.

TeamTrau-native (port into `features/teamtrau`): role folders + classifier, defect pruning/golden takes, beat/section/bar grid overlay, cadence sequence + genre presets, 9:16 reframe, Whisper subtitles via engine, NLE export (FCPXML/Premiere XML/CapCut), NVENC master deliver + Deliver Settings, pre-flight, MCP agent console.

Cherry-picked from FilmCraft / Concat (backlog, ordered by value ÷ cost; ideas only for Concat):
1. Engine auto-proxy (S6) + perf meter (shown/repeated/dropped frames, like FilmCraft `PlaybackMeter`, Concat `Cadence`).
2. Wire FreeCut's adaptive preview quality to the real render scale (it is a stub today) + Full/½/¼ playback resolution (FilmCraft/Concat).
3. Render cache with green/yellow/red bar, segment content-hash file names (FilmCraft) — rendered by NVENC in the engine.
4. Loudness: EBU R128 meters + auto-ducking that writes volume keyframes (FilmCraft Essential Sound).
5. One command registry serving menus, shortcuts and the MCP agent; UI automation ids so the agent can drive and screenshot Studio (FilmCraft control channel).
6. LUT (.cube) + log→Rec.709 on proxies; scopes already exist.
7. Shortcut presets (Premiere / CapCut / Avid) and conflict detection (FilmCraft).
8. Templates with fillable slots (Concat idea).
9. Interchange breadth: OTIO and EDL export (FilmCraft notes: `pproTicks`, `out = in + (end-start)`).

Out of scope by owner decision (2026-10-24): AI background removal / cutout and TTS voice cloning.

## 5. Playback acceptance targets (S9)

Measured with the perf meter over a 30 s playback, timeline at the source fps, monitor at Full unless noted:

| Source | Mode | Target |
|---|---|---|
| 4K60 H.264 8-bit (DJI-like) | engine proxy 1080p | ≤ 0.5 % dropped frames; seek-to-first-frame ≤ 150 ms |
| 4K60 HEVC 10-bit / Sony 4:2:2 | engine proxy 1080p | ≤ 0.5 % dropped; works on all three OSes |
| 4K120 (DJI/Sony high frame rate) | engine proxy 1080p@120 on a 120 Hz display; on 60 Hz, clock-paced every 2nd frame | ≤ 1 % dropped vs display refresh |
| Phone VFR | engine proxy CFR | A/V drift ≤ 1 frame after 10 min |
| 4K60 original (no proxy) | Full | best effort; ½ resolution must hit ≤ 2 % dropped on RTX 3070 |

## 6. Cross-platform (S5, S6)

- Python: `platform_caps` module is the single place that knows OS, GPU and encoder/decoder availability; renderer/proxy/Resolve code asks it instead of hard-coding NVENC/CUDA/`C:\ProgramData`. Encoder ladder: NVENC → VideoToolbox → QSV → AMF → VAAPI → libx264/libx265. Torch: CUDA → MPS → CPU. Whisper: CUDA → CPU int8.
- Resolve paths per OS (Windows `C:\ProgramData\…\fusionscript.dll`, macOS `/Library/Application Support/Blackmagic Design/…/fusionscript.so`, Linux `/opt/resolve/…`). CapCut: Windows/macOS only (export still writes files on Linux).
- Packaging: electron-builder (NSIS/MSI, DMG signed + notarized, AppImage/deb). Python engine shipped as a PyInstaller sidecar per OS; ffmpeg shipped per OS. CI matrix: windows / macos / ubuntu runs pytest (CPU paths) + Studio vitest + a packaged-app smoke start.

## 7. Risks (top 5) and mitigations

1. **Browser-only preview engine** — mitigated by S6 (H.264 8-bit proxies always hardware-decodable) and by keeping NVENC deliver in Python.
2. **Upstream churn (weekly, schema v15 and rising, strict boundary checks)** — S3 isolation; merge upstream monthly on a branch; a FreeCut schema bump is a planned task with tests.
3. **No live sync between engine and an open project** — S4 file hand-off + explicit reload command; never write into a project the editor has open without the reload handshake.
4. **Electron hardening** (COOP/COEP, `showDirectoryPicker`, service worker, CDN-loaded models/fonts) — dedicated Phase 2 tasks; offline asset bundling.
5. **Scope** — phased plan with a hard spike gate (Phase 0) before any large build.

## 8. Study summaries (sources)

- **FreeCut** (MIT, React 19 / Zustand / TanStack / shadcn, ~2.6k TS files): playback through `<video>` elements + a WebCodecs fast-scrub path; manual 960×540 long-GOP video-only proxies; adaptive-quality controller not wired to render scale; no beat detection, no NLE export, no NVENC; data model `project.json` schema v15, integer frames, `mediaId` refs, AI sidecars `cache/ai/*.json`; no plugin registry (hard-coded JSX, `deps/` adapter rule); snapshot undo via `timeline-command-store.execute()`; headless API needs Playwright Chrome; no Vietnamese locale.
- **FilmCraft** (Apache-2.0/MIT, Rust/egui/wgpu): audio-clock master with 200 ms play-ahead ring; prioritised cancellable frame jobs; hold-nearest-frame; preroll gate; GOP cache with continue-through and non-ref skipping; playback/paused resolution tiers; content-hash render cache; HybridDecoder (hardware → software); one command registry for menus/shortcuts/MCP; full NLE tool set; no 60/120 fps benchmarks.
- **Concat** (AGPL-3.0, Rust/Slint/wgpu, ideas only): automatic proxies for >1080p (¼ size, H.264, long-GOP); frame cache keyed by (file, level, frame); decode pyramid; per-codec hardware policy by measurement; audio baked to mmap WAV; 8 ms follower with one in-flight request; 127 data-driven effect packages; AI cutout, TTS cloning, templates, 26 locales; no NLE export, no beat sync.
- **TeamTrau** (Python): strong engine and automation; weak GUI (silent player, no manual editing).
