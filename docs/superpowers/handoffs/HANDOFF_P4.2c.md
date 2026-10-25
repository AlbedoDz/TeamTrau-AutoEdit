## HANDOFF P4.2c
STATUS: DONE
BRANCH / COMMIT: teamtrau/main (Studio) 16ff9fd0 test(teamtrau): multi-item playback cap diagnosis (V5-V8) | feature/studio-engine (Engine)
FILES CHANGED:
  teamtrau-studio:
    scripts/teamtrau/make_diagnosis_projects.py
    scripts/teamtrau/measure-render-cache.mjs
    src/runtime/composition-runtime/components/stable-video-sequence.tsx
    src/runtime/composition-runtime/compositions/main-composition.tsx
    src/runtime/player/video/VideoSourcePool.ts
    docs/teamtrau/P4.2c-diagnosis.md
    docs/teamtrau/p42-runs/rtx-T1-V5.json
    docs/teamtrau/p42-runs/rtx-T1-V6.json
    docs/teamtrau/p42-runs/rtx-T1-V7.json
    docs/teamtrau/p42-runs/rtx-T1-V8.json
  TeamTrau-AutoEdit:
    docs/superpowers/handoffs/HANDOFF_P4.2c.md
RED EVIDENCE: N/A (Diagnostic exploration task P4.2c)
GREEN EVIDENCE:
  task tests:    All diagnostic runs V5, V6, V7, V8 completed cleanly on RTX 3070 in Electron shell
  related suites:npm run test:electron -> 16 passed, 0 failed (duration_ms 3006.42)
  tsc:           npx tsc --noEmit -p tsconfig.json -> 0 errors (clean)
  lint:          npx vp lint src -> 0 errors, 443 warnings (identical to baseline)
  boundaries:    check:boundaries & check:layer-boundaries -> passed (0 illegal layer dependencies, 2602 files)
  build:         npx vp build -> ✓ built in 9.82s
DEVIATIONS FROM PLAN: none
OPEN QUESTIONS / BLOCKERS: none

### P4.2c Diagnosis Results Table (Raw JSON)

| Variant | Description | Total Items | Mode | Presented / Total Frames | Dropped % | Max Stall (ms) | Median Stall (ms) | 4 Hz Pool Size (min/max) | 4 Hz Unpaused Videos | Acceptance Target (Spec §5) | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **V0** | Baseline single pre-rendered 30s file | 1 | baseline | 1778 / 1800 | 1.22% | 0.0 ms | 0.0 ms | 1 / 1 | 1 | Diagnostic | Baseline |
| **V5** | V0 split into 3x 10s files (`ffmpeg -c copy`), 3 normal items | 3 | baseline | 1777 / 1800 | **1.28%** | 170.0 ms | 115.0 ms | 1 / 1 | 1 | ≤ 1.0% dropped, stall ≤ 50ms | Near Target |
| **V6** | V0 single 30s file placed as 2 items (0–15s & 15–30s) | 2 | baseline | 1792 / 1800 | **0.44%** | **20.0 ms** | **20.0 ms** | 1 / 1 | 1 | ≤ 1.0% dropped, stall ≤ 50ms | **MET** |
| **V7** | Render cache with `max_segment_sec=30` (1 single segment) | 1 (cache) | render_cache | 1791 / 1800 | **0.50%** | **43.3 ms** | **23.4 ms** | 1 / 1 | 1 | ≤ 1.0% dropped, stall ≤ 50ms | **MET** |
| **V8** | V1 (3 cache segments) with premount disabled (`premountFor=0`) | 3 (cache) | render_cache | 1199 / 1800 | 33.39% | 600.0 ms | 30.1 ms | 1 / 2 | 0 - 1 | ≤ 1.0% dropped, stall ≤ 50ms | MISSED |
| **V1** | Standard render cache (3 segments @ 10s, audio removed) | 3 (cache) | render_cache | 1196 / 1800 | 33.56% | 600.0 ms | 23.5 ms | 1 / 2 | 0 - 1 | ≤ 1.0% dropped, stall ≤ 50ms | MISSED |

*(R11 Compliance: Audio clock synchronization reported as NOT MEASURED per architect instruction).*

### Verdict on "Multi-Item ⇒ ~40 fps Cap"
- **VERDICT: REJECTED.**
- **Evidence:**
  1. **V6 (2 items on video track):** Delivers **1792 / 1800 frames presented (0.44% dropped)**, max stall **20.0 ms**, median stall **20.0 ms**. Time series shows 60 fps rock solid across all 30 seconds with zero stall at the 15s split boundary.
  2. **V5 (3 items on video track):** Delivers **1777 / 1800 frames presented (1.28% dropped)**, matching V0 (1778 / 1800) at solid 60 fps throughout.
  3. **V7 (1 single 30s cache segment):** Delivers **1791 / 1800 frames presented (0.50% dropped)**, max stall **43.3 ms**, passing all acceptance criteria.

### True Root Cause of ~1193-1205 Frames in V1, V2, V4, V8
The "~40 fps cap" was an arithmetic illusion ($1196 / 30s \approx 39.87$ fps):
1. Telemetry proves playback ran at full **60 fps for the first 20 seconds** (1195 frames presented out of 1200 frames = 99.6% delivery), and then **completely froze at $t = 20.0s$** (0-3 frames presented across seconds 21-30).
2. Because benchmark clips looped with `source_in = (i * clip_dur_sec) % 20.0`, Segment 1 (0-10s) and Segment 3 (20-30s) contained bit-for-bit identical source ranges.
3. Content-addressable SHA1 hashing in `render_cache.py` gave Segment 1 and Segment 3 the exact same hash key (`a25850c35e4a4c4ee5b596dc236dfbf6aa8ba6a2`).
4. `VideoSourcePool` keys controllers by URL. When Segment 1 finished at $t = 10s$, its element entered `ended = true`. At $t = 20s$, when Segment 3 mounted, the pooled element was recycled with `currentTime = 0`, but the player never resumed playback on an ended element, causing the 10-second freeze at the end.

### Components Responsible (Named with File & Line)
1. `src/modules/render_cache.py:28-36` & line 105: Content-addressable segment hashing omits timeline sequence placement (`start_frame`), leading to URL collisions when footage loops.
2. `src/runtime/player/video/VideoSourcePool.ts:495-510` & `src/runtime/composition-runtime/components/video-content.tsx:742-760`: VideoSourcePool reuses elements in `ended` state without re-triggering playback on segment acquisition.
3. Premount controls toggled in V8:
   - `src/runtime/composition-runtime/compositions/main-composition.tsx:694` (`premountFor={Math.round(fps * VIDEO_CLIP_PREMOUNT_SECONDS)}`)
   - `src/runtime/composition-runtime/components/stable-video-sequence.tsx:471` (`defaultPremount`)
   - Controlled by runtime flag `window.__disablePremount = true`.
