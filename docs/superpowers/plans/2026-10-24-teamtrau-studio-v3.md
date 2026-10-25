# TeamTrau Studio v3 — Implementation Plan

> **Spec:** `docs/superpowers/specs/2026-10-24-teamtrau-studio-v3-design.md` (decisions S1–S9).
> **Process:** same orchestration as auto-curation v2 — architect (Claude) writes tasks + locked tests and reviews every commit; Antigravity 2 implements one task per conversation; prompts in `.claude/orchestrator/`; HANDOFF files in `docs/superpowers/handoffs/`.
> **Rule learned from v1:** no detailed task is written for code nobody has run. Phase 0 spikes measure reality first; Phases 1–6 are detailed only after the Phase 0 gate.

## Repositories

| Repo | Path | Content |
|---|---|---|
| Engine | `E:\antigravity\TeamTrau-AutoEdit` (this repo) | Python engine, CLI, MCP, tests. New code under `src/engine_api/`, `src/integrations/`, `src/core/platform_caps.py`, `src/modules/proxy_generator.py`. |
| Studio | `E:\antigravity\teamtrau-studio` (new) | Fork of `github.com/walterlow/freecut` (remote `upstream`), branch `teamtrau/main`. TeamTrau code only in `src/features/teamtrau/**`, `src/features/editor/deps/teamtrau.ts`, `electron/**`, plus minimal hooks. |

Global rules for every task (in addition to the MASTER PROMPT R1–R12):
- **License (S8):** never copy code from Concat (AGPL). FilmCraft code may be ported only with an entry in Studio/Engine `NOTICE`. Keep FreeCut `LICENSE` and add `NOTICE`.
- **Studio isolation (S3):** do not rename `freecut` internal ids; edits to existing FreeCut files are limited to the one-line hooks a task names. Run FreeCut's `npm run verify` (or the subset the task names) before commit.
- **No new Python dependencies** unless a task names them (starlette/uvicorn are already installed).

---

## Phase 0 — Spikes (measure before building)

Each spike ends with a report `docs/spikes/S0.x-*.md` containing commands run, raw numbers, screenshots paths, problems hit, and a GO / NO-GO recommendation. Spike code must be small, committed on a `spike/*` branch, and is allowed to be thrown away.

### S0.1 — Studio boots on this machine, in Chrome and in Electron
1. `git clone https://github.com/walterlow/freecut E:\antigravity\teamtrau-studio`; `git remote rename origin upstream`; branch `spike/s0-boot`.
2. `npm ci`, `npm run build`, `npm run test` (record pass/fail counts and duration).
3. Generate perf samples in the Engine repo with a new script `scripts/make_perf_samples.py` (ffmpeg lavfi `testsrc2` with motion + `sine` audio, 30 s each, into `E:\antigravity\perf-samples\`):
   `4k60_h264_8bit.mp4`, `4k60_hevc_10bit.mp4` (`-pix_fmt yuv420p10le`), `4k60_hevc_422_10bit.mov` (`yuv422p10le`), `4k120_h264.mp4`, `phone_vfr.mp4` (VFR via `-vsync vfr` with dropped-frame filter), `1080p60_h264_allintra.mp4` (`-g 1`).
4. Run `npm run perf` (port 4173), open in Chrome with `?previewPerfPanel`, create a workspace, import every sample, play 30 s each at Full and at the lowest available preview quality; record dropped frames / frame-time from the perf panel and Chrome `chrome://media-internals` decoder type (hardware or software).
5. Minimal Electron shell (`electron/main.cjs`, dev dependency `electron` pinned): loads the built `dist/`, sets COOP `same-origin` + COEP `require-corp`, disables the service worker; repeat step 4 inside Electron for 4k60 H.264 and the 1080p all-intra sample.
6. Report `docs/spikes/S0.1-studio-boot.md` (in the Studio repo) with a table: sample × (Chrome, Electron) × (decoder hw/sw, dropped %, notes).

**Done when:** report committed on `spike/s0-boot` with real numbers; nothing in the Engine repo changes except `scripts/make_perf_samples.py` (committed on Engine branch `spike/s0`).

### S0.2 — Engine → Studio project hand-off
1. Read FreeCut `src/types/project.ts`, `src/types/timeline.ts`, `src/shared/projects/migrations/*`, `src/features/project-bundle/schemas/project-schema.ts`, `src/infrastructure/storage/workspace-fs/paths.ts`, `src/features/timeline/stores/timeline-persistence.ts` and `src/types/storage.ts`. Write `docs/spikes/S0.2-freecut-contract.md` listing the exact minimum fields for: workspace marker, `index.json`, `projects/{id}/project.json` (schema v15), video/audio items, tracks, markers, and `media/{id}/metadata.json` in **link mode** (no copy) — with the evidence lines.
2. Engine: spike module `src/integrations/freecut_writer.py` with `write_freecut_project(timeline: EditTimeline, workspace: Path, project_name: str) -> str` (returns project id): one video track + one audio track, beats/sections as markers, golden/role info into `media/{id}/cache/ai/teamtrau.json` sidecars.
3. Run it on the rough-cut timeline from `main.py curate` on the perf samples, open the workspace in Studio (S0.1 build), and verify: clips at the right frames, correct durations, audio plays, markers visible.
4. Studio: one Vitest test `src/features/teamtrau/__tests__/engine-project-fixture.test.ts` that loads the Python-generated `project.json` fixture through FreeCut's project zod schema + normalize and asserts item frames.

**Done when:** contract doc + spike writer + fixture test + screenshot of the opened project, with a list of mismatches found.

### S0.3 — Engine proxies drive smooth playback
1. Spike `src/modules/proxy_generator.py`: `make_proxy(src, out_dir, height=1080, fps=None, intra=True) -> Path` using `platform_caps`-like probing inline for the spike: try `h264_nvenc` (`-g 1` or `-g 6`, `-bf 0`, CFR at the source fps or `fps`), else `libx264 -preset veryfast -g 6`; keep audio (AAC 48 kHz stereo); name by `sha1(path|size|mtime|params)`.
2. Measure encode speed (× real time) for each perf sample with NVENC and with libx264.
3. Find how FreeCut attaches a proxy (`features/media-library/services/proxy-service.ts`, `proxy-constants.ts`, workspace `content/proxies/{key}/`): can the engine write a proxy the app adopts (meta.json schema v4)? If yes, attach and measure playback (perf panel) for 4k60 HEVC 10-bit, 4:2:2 and 4k120 via proxy; if not, note the smallest hook needed.
4. Report `docs/spikes/S0.3-proxies.md`: encode speed table, playback table (original vs proxy, all-intra vs GOP 6), recommendation for proxy parameters.

**Done when:** report with numbers; spike code on Engine branch `spike/s0`.

### Gate G0 (owner decision)
Architect reviews the three reports and proposes GO / adjust / NO-GO for S1 (FreeCut fork). Owner signs off. Only then Phase 1+ tasks are written in detail (with locked tests).

---

## Phase 1 — Engine service (Python, TDD with locked tests)
**G0 passed 2026-10-24 (hybrid). Detailed plan with locked tests: `2026-10-24-teamtrau-studio-v3-phase1.md` (P1.0–P1.6: hygiene, platform_caps, proxy generator, FreeCut writer, FreeCut reader, render cache + intra-only renderer, engine API). The bullets below are the original outline.**
- 1.1 `src/core/platform_caps.py`: OS, GPU, hardware encoders/decoders (probe ffmpeg `-encoders`/`-hwaccels`, test-encode 1 frame), torch device, Resolve/CapCut paths per OS. Replace hard-coded NVENC/CUDA/`C:\ProgramData` lookups.
- 1.2 `src/modules/proxy_generator.py` production version (parameters from S0.3), background queue, cache key, cancel; CLI `main.py proxies` + MCP tool.
- 1.3 `src/integrations/freecut_writer.py` production version + contract tests generated from S0.2.
- 1.4 `src/engine_api/` starlette app: endpoints of spec §3, token auth, loopback, body/connection caps, SSE job events wrapping `JobCallbacks`; CLI `main.py serve --port --token`.
- 1.5 Render-preview cache: NVENC segment renders named by content hash (FilmCraft idea) for the Studio render bar.

## Phase 2 — Studio shell
**Detailed plan with locked tests: `2026-10-24-teamtrau-studio-v3-phase2-4.md` (P2.0–P2.5).**
- 2.1 Electron main process: COOP/COEP, workspace path instead of `showDirectoryPicker`, no service worker, single-instance, engine sidecar start/stop/health, token handshake.
- 2.2 Offline assets: bundle fonts and the CDN-loaded model workers (or disable them in favour of engine services).
- 2.3 Rebrand display strings, logo, theme tokens (`src/index.css`), landing page; keep internal ids.
- 2.4 Vietnamese locale `vi` (+ English stays fallback).

## Phase 3 — TeamTrau panels (parity with the PySide6 GUI)
**Rolling-wave plan: `2026-10-25-teamtrau-studio-v3-phase3.md` (P3.0a detailed; P3.0b–P3.6 detailed just in time).**
- 3.1 `features/teamtrau` sidebar tab + typed engine client + job progress UI (SSE).
- 3.2 Ingest & Roles (A-roll/B-roll/Music, classifier results, role override).
- 3.3 Curation (golden takes grid with scores/defects, include/exclude, P/X/F hotkeys) → "Send to timeline" (S4 hand-off, one undo step).
- 3.4 Sequence: genre/cadence presets, soundtrack analysis overlay (beats, bars, sections) on the timeline ruler.
- 3.5 Deliver: NLE export (FCPXML/Premiere/CapCut), NVENC master with Deliver Settings, pre-flight report.
- 3.6 Agent console (MCP agent) inside Studio.
- **Gate G3:** feature parity checklist signed → PySide6 GUI frozen (S7).

## Phase 4 — Playback excellence
**P4.0–P4.2 (render-cache playback) are detailed in `2026-10-24-teamtrau-studio-v3-phase2-4.md`; the rest after gate G4.**
- 4.1 Auto-proxy on import (heavy-media heuristics: > 1080p, HEVC, 10-bit, 4:2:2, > 60 fps, VFR, log).
- 4.2 Perf meter (shown / repeated / dropped, worst wait) + wire adaptive preview quality to real render scale; Full/½/¼ playback resolution.
- 4.3 Meet spec §5 targets; benchmark script in CI-able form.

## Phase 5 — Cross-platform release
**Rolling-wave plan: `2026-10-25-teamtrau-studio-v3-phase5.md` (P5.0 detailed).**
- 5.1 CI matrix windows/macos/ubuntu: pytest (CPU), Studio vitest, packaged smoke start.
- 5.2 Packaging: electron-builder (NSIS/MSI, DMG signed+notarized, AppImage/deb) + PyInstaller engine sidecar + ffmpeg per OS.

## Phase 6 — Cherry-pick backlog (spec §4 list, in order)
R128 meters + auto-ducking keyframes · command registry + UI automation for the agent · LUT + log→709 proxies · shortcut presets (Premiere/CapCut/Avid) · templates · OTIO/EDL export.
Deferred from G3 (2026-10-25): multi-variant timelines + variant deliver list · VLM tagging UI · genre recommender banner · camera role · snap-error badge · probe columns in Ingest · curation card thumbnails · AI endpoint ping · auto-proxy on import (P4.3).
Out of scope (owner, 2026-10-24): AI background removal / cutout, TTS voice cloning.

---

## Phase 0 prompts
Prepared as `.claude/orchestrator/S0.1.prompt.md`, `S0.2.prompt.md`, `S0.3.prompt.md` (MASTER rules + spike task). S0.1 must finish before S0.2/S0.3 (they need the Studio build); S0.2 and S0.3 can run in parallel in two Antigravity conversations because they touch different files.
