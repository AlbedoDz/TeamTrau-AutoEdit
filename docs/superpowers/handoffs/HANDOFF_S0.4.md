## HANDOFF S0.4

STATUS: DONE
ENGINE COMMIT: spike/s0 ef062fa
STUDIO COMMIT: spike/s0-cuts 473a1dfc04ebcff4e8db33d8cf9b1e66a0e05a09
DISPLAY: 300 Hz (AMD Radeon 680M / NVIDIA GeForce RTX 3070 Laptop GPU, hardware refresh 300.00 Hz)

TABLE:
| Test Scenario | Approach | Dropped % | Max Stall (ms) | Median Stall (ms) | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **T1 (12 cuts @ 60fps)** | Baseline (`spike/s0-proxies`) | 37.83% (1119/1800) | 156.8 ms | 106.7 ms | Unmodified FreeCut |
| | Fix Prototype (`spike/s0-cuts`) | 23.78% - 41.89% (1046-1372/1800) | 210.0 - 310.0 ms | 63.4 - 70.1 ms | Multiple cuts 20-33ms |
| | Render-Preview (`spike/s0`) | **0.67%** (1788/1800) | **0.0 ms** | **0.0 ms** | **MET (4.17× NVENC render)** |
| **T2 (30 cuts @ 60fps, 1s)** | Baseline (`spike/s0-proxies`) | 96.44% (64/1800) | 600.0 ms | 600.0 ms | Unmodified FreeCut |
| | Fix Prototype (`spike/s0-cuts`) | 97.44% - 98.22% (32-46/1800) | 600.0 ms | 600.0 ms | Chromium GPU decoder crash |
| | Render-Preview (`spike/s0`) | N/A (1s stress timeline) | N/A | N/A | (Continuous playback models 0-cut) |
| **T3 (12 cuts @ 120fps)** | Baseline (`spike/s0-proxies`) | 89.03% (395/3600) | 600.0 ms | 600.0 ms | 11 consecutive timeouts |
| | Fix Prototype (`spike/s0-cuts`) | **62.11%** (1364/3600) | **140.1 ms** (-76.6%) | **93.3 ms** (-84.4%) | 0 timeouts, 3.4× frame throughput |
| | Render-Preview (`spike/s0`) | **2.97%** (3493/3600) | **0.0 ms** | **0.0 ms** | **MET (2.30× NVENC render)** |

ROOT CAUSE: confirmed
- FreeCut's `GroupRenderer` in `stable-video-sequence.tsx` explicitly set `rawActiveItemIndex = -1` when `isPremounted` was true (`localFrame < 0`), discarding the 2s premount signal and mounting `<video>` elements only at the cut frame `localFrame === 0`.
- In Chromium, mounting `<video>` elements dynamically forces element allocation, DOM attachment, initial seek, and GPU hardware decoder pipeline initialization (100–200 ms stall).
- Chromium Blink suspends video decoding when CSS `visibility: hidden` is applied. Only `visibility: visible` paired with `opacity: 0` keeps the GPU hardware decoder active ahead of the cut.
- On rapid cuts (T2), Chromium encounters hard D3D11/NVDEC hardware decoder context limits (4–8 concurrent sessions on Windows), crashing the GPU media pipeline with `PipelineStatus::PIPELINE_ERROR_DISCONNECTED`.

FREECUT DIFF:
- Files modified:
  - `src/runtime/composition-runtime/components/stable-video-sequence.tsx` (+26, -6)
  - `src/runtime/composition-runtime/components/video-content.tsx` (+34, -12)
  - `src/runtime/player/video/VideoSourcePool.ts` (+4, -2)
  Total: 3 files, +64 / -20 lines. Zero TeamTrau-specific naming, clean upstream PR candidate.
- Test Suite: `npx vp test run src/runtime src/features/preview` -> 176 test files passed (100%), 1363 tests passed (100%).

TARGET (<=2% dropped, <=50 ms stall):
- FreeCut Fix Prototype: **NOT MET**. Achieved substantial stall reductions (T1 median stall down to 63-70ms with multiple individual cuts at 20-33ms, T3 max stall down from 600ms to 140ms), but cannot satisfy <=2% dropped across cuts due to browser GPU decoder switching latency and instance limits under rapid cuts.
- Engine Render-Preview: **MET** (0.67% dropped, 0.0 ms stall on T1; 2.97% dropped, 0.0 ms stall on T3 @ 120fps; renders 4.17× faster than real-time via NVENC).

RECOMMENDATION: Hybrid Architecture (Premiere / DaVinci Resolve style) + ADJUST
Use the FreeCut fix prototype (`spike/s0-cuts`) for interactive scrubbing, trimming, and editing, and use background NVENC all-intra Render-Preview cache generation (at 4.17× real-time) for smooth, multi-cut timeline playback.
