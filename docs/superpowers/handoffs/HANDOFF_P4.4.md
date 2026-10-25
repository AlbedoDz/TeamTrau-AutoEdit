## HANDOFF P4.4
STATUS: DONE
BRANCH / COMMIT: teamtrau/main (Studio) 509eaa68 feat(electron): prefer the NVIDIA GPU by default (--gpu / TEAMTRAU_GPU opt-out) | 9f9cd97a test(teamtrau): playback measurements on the RTX in the Electron shell
FILES CHANGED:
  teamtrau-studio:
    electron/gpu-preference.cjs
    electron/__tests__/gpu-preference.test.cjs
    electron/main.cjs
    docs/teamtrau/CHANGELOG.md
    src/features/teamtrau/render-cache/store.ts
    src/features/preview/hooks/use-preview-composition-model.ts
    src/features/preview/components/video-preview.tsx
    scripts/teamtrau/make_diagnosis_projects.py
    scripts/teamtrau/measure-render-cache.mjs
    docs/teamtrau/P4.4-rtx-playback.md
    docs/teamtrau/p42-runs/rtx-T1-baseline.json
    docs/teamtrau/p42-runs/rtx-T1-render_cache.json
    docs/teamtrau/p42-runs/rtx-T1-render_cache-power-saving.json
    docs/teamtrau/p42-runs/rtx-T1-V1.json
    docs/teamtrau/p42-runs/rtx-T1-V2.json
    docs/teamtrau/p42-runs/rtx-T1-V4.json
    docs/teamtrau/p42-runs/rtx-T2-baseline.json
    docs/teamtrau/p42-runs/rtx-T2-render_cache.json
    docs/teamtrau/p42-runs/rtx-T3-baseline.json
    docs/teamtrau/p42-runs/rtx-T3-render_cache.json
    docs/teamtrau/p42-runs/rtx-T3-V4.json
  TeamTrau-AutoEdit:
    docs/superpowers/handoffs/HANDOFF_P4.4.md
RED EVIDENCE:
  ✖ defaults to high-performance (0.9702ms)
    Error: Cannot find module '../gpu-preference.cjs'
    Require stack:
    - E:\antigravity\teamtrau-studio\electron\__tests__\gpu-preference.test.cjs
        at Function._resolveFilename (node:internal/modules/cjs/loader:1393:15)
        at defaultResolveFilename (node:internal/modules/esm/translators:606:40)
        at Function._load (node:internal/modules/cjs/loader:1232:27)
        at TracingChannel.traceSync (node:diagnostics_channel:322:14)
        at wrapModuleLoad (node:internal/modules/cjs/loader:234:24)
        at Module.require (node:internal/modules/cjs/loader:1463:12)
        at require (node:internal/modules/helpers:145:16)
        at Object.<anonymous> (E:\antigravity\teamtrau-studio\electron\__tests__\gpu-preference.test.cjs:5:43)
GREEN EVIDENCE:
  task tests:    ✔ defaults to high-performance, --gpu flag selects, TEAMTRAU_GPU applies, unknown values fallback, maps switches (5/5 passed)
  related suites:npm run test:electron -> 16 passed, 0 failed (duration_ms 3326.7)
  full suite:    npx vp test run -> 705 test files passed, 5137 tests passed (0 failures, 0 regressions)
  tsc:           npx tsc --noEmit -p tsconfig.json -> 0 errors (clean)
  lint:          npx vp lint src -> 0 errors, 443 warnings (identical to baseline)
  boundaries:    check:boundaries & check:layer-boundaries -> passed (0 illegal layer dependencies, 2602 files)
  build:         npx vp build -> ✓ built in 16.05s
DEVIATIONS FROM PLAN: none
OPEN QUESTIONS / BLOCKERS: none

### P4.4 Playback Measurement Comparison Table (Raw JSON)

| Run Key | Timeline & Mode | GPU & Shell | Presented / Total Frames | Dropped % | Max Stall (ms) | Median Stall (ms) | A/V Offset Status | Acceptance Target (Spec §5) | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `rtx-T1-baseline` | T1 (1080p60) Baseline | NVIDIA RTX 3070 (Electron) | 1205 / 1800 | 33.06% | 136.7 ms | 33.4 ms | NOT MEASURED | Baseline | N/A |
| `rtx-T1-render_cache` | T1 (1080p60) Render Cache | NVIDIA RTX 3070 (Electron) | 1193 / 1800 | 33.72% | 600.0 ms | 26.8 ms | NOT MEASURED | ≤ 1.0% dropped, stall ≤ 50ms | **MISSED** |
| `rtx-T1-render_cache-power-saving` | T1 (1080p60) Render Cache | AMD Radeon 680M iGPU (Electron) | 1196 / 1800 | 33.56% | 600.0 ms | 23.3 ms | NOT MEASURED | ≤ 1.0% dropped, stall ≤ 50ms | **MISSED** |
| `T1-render_cache` (Chrome) | T1 (1080p60) Render Cache | AMD/Chrome (P4.2) | 1196 / 1800 | 33.56% | 600.0 ms | 23.4 ms | NOT MEASURED | ≤ 1.0% dropped, stall ≤ 50ms | **MISSED** |
| `rtx-T1-V1` | T1 (1080p60) V1 (Video Only) | NVIDIA RTX 3070 (Electron) | 1196 / 1800 | 33.56% | 600.0 ms | 23.5 ms | NOT MEASURED | Diagnostic | N/A |
| `rtx-T1-V2` | T1 (1080p60) V2 (Muted Audio) | NVIDIA RTX 3070 (Electron) | 1198 / 1800 | 33.44% | 600.0 ms | 23.3 ms | MEASURED (mean 5659.6ms) | Diagnostic | N/A |
| `rtx-T1-V4` | T1 (1080p60) V4 (Scrub Substituted) | NVIDIA RTX 3070 (Electron) | 1195 / 1800 | 33.61% | 600.0 ms | 30.1 ms | NOT MEASURED | ≤ 1.0% dropped, stall ≤ 50ms | **MISSED** |
| `rtx-T2-baseline` | T2 (30 cuts) Baseline | NVIDIA RTX 3070 (Electron) | 1441 / 1800 | **19.94%** | 216.6 ms | 36.7 ms | NOT MEASURED | Baseline | N/A |
| `rtx-T2-render_cache` | T2 (30 cuts) Render Cache | NVIDIA RTX 3070 (Electron) | 1197 / 1800 | **33.50%** | 600.0 ms | 26.7 ms | NOT MEASURED | ≤ 1.0% dropped, stall ≤ 50ms | **MISSED** |
| `T2-render_cache` (Chrome) | T2 (30 cuts) Render Cache | AMD/Chrome (P4.2) | 770 / 1800 | 57.22% | 313.3 ms | 50.0 ms | NOT MEASURED | ≤ 1.0% dropped, stall ≤ 50ms | **MISSED** |
| `rtx-T3-baseline` | T3 (4K120) Baseline | NVIDIA RTX 3070 (Electron) | 786 / 3600 | 78.17% | 313.1 ms | 110.0 ms | NOT MEASURED | Baseline | N/A |
| `rtx-T3-render_cache` | T3 (4K120) Render Cache | NVIDIA RTX 3070 (Electron) | 2047 / 3600 | **43.14%** | 536.6 ms | 50.0 ms | NOT MEASURED | ≤ 3.0% dropped, stall ≤ 50ms | **MISSED** |
| `T3-render_cache` (Chrome) | T3 (4K120) Render Cache | AMD/Chrome (P4.2) | 47 / 3600 | 98.69% | 600.0 ms | 600.0 ms | NOT MEASURED | ≤ 3.0% dropped, stall ≤ 50ms | **MISSED** |
| `rtx-T3-V4` | T3 (4K120) V4 (Scrub Substituted) | NVIDIA RTX 3070 (Electron) | 974 / 3600 | 72.94% | 106.7 ms | 90.0 ms | NOT MEASURED | ≤ 3.0% dropped, stall ≤ 50ms | **MISSED** |

### Architect's Hypothesis Verdict (Part C)
- **Verdict:** REJECTED.
- **Evidence:**
  1. Active render source sampled at 4 Hz throughout all runs was 100% `player` (0 `fast_scrub_overlay`, 0 `playback_transition_overlay`). Overlays were completely dormant.
  2. HTMLMediaElement writes intercepted at cut boundaries ±200ms showed 0 `currentTime` seeks and 0 `src` mutations at cut points (the only mutation across 30s was at $t = 20.0s$ where segment 3 mounts).
  3. Rebuilding scrub spans in V4 produced identical drop rate on T1 (33.61% vs 33.56%) and worsened T3 (72.94% vs 43.14%). Per-cut stalls are not caused by fast-scrub structures. `substituteScrubToo` remains inert (`default: false`).
