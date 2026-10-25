## HANDOFF P5.2
STATUS: DONE
BRANCH / COMMIT: teamtrau/main 3231dea7 feat(teamtrau): fixed app:// origin, packaged-engine launch, platform GPU default, shell CI matrix
FILES CHANGED:
  teamtrau-studio (branch teamtrau/main, commit 3231dea7):
    .github/workflows/ci.yml
    docs/teamtrau/CHANGELOG.md
    electron/__tests__/app-protocol.test.cjs (new, LOCKED)
    electron/__tests__/engine-launch.test.cjs (new, LOCKED)
    electron/__tests__/gpu-preference.test.cjs (replaced, LOCKED)
    electron/__tests__/static-server.test.cjs (deleted)
    electron/app-protocol.cjs (new)
    electron/engine-launch.cjs (new)
    electron/gpu-preference.cjs
    electron/main.cjs
    electron/static-server.cjs (deleted)
  TeamTrau-AutoEdit (branch feature/studio-engine):
    docs/superpowers/handoffs/HANDOFF_P5.2.md
    docs/superpowers/handoffs/p52_run1_editor.png
    docs/superpowers/handoffs/p52_run2_projects.png
    docs/superpowers/handoffs/p52_run2_job.png
    docs/superpowers/handoffs/p52_run2_agent_playback.png
    docs/superpowers/handoffs/p52_offline.png
    docs/superpowers/handoffs/p52_missing_engine_dialog.png
RED EVIDENCE:
  ✖ electron\__tests__\app-protocol.test.cjs (99.0211ms)
    Error: Cannot find module '../app-protocol.cjs'
    Require stack:
    - E:\antigravity\teamtrau-studio\electron\__tests__\app-protocol.test.cjs
  ✖ electron\__tests__\engine-launch.test.cjs (86.1705ms)
    Error: Cannot find module '../engine-launch.cjs'
    Require stack:
    - E:\antigravity\teamtrau-studio\electron\__tests__\engine-launch.test.cjs
  ✖ defaults to high-performance on Windows and auto elsewhere (1.9904ms)
    AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:
    + actual - expected
    + 'high-performance'
    - 'auto'
        at TestContext.<anonymous> (E:\antigravity\teamtrau-studio\electron\__tests__\gpu-preference.test.cjs:10:10)
  ✖ unknown values fall back to the platform default (0.3113ms)
    AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:
    + actual - expected
    + 'high-performance'
    - 'auto'
        at TestContext.<anonymous> (E:\antigravity\teamtrau-studio\electron\__tests__\gpu-preference.test.cjs:30:10)
GREEN EVIDENCE:
  task tests:    node --test electron/__tests__/app-protocol.test.cjs electron/__tests__/engine-launch.test.cjs electron/__tests__/gpu-preference.test.cjs -> 16 passed, 0 failed (3272.8ms)
  related suites:npm run test:electron -> 25 passed, 0 failed in 5 files (3312.6ms) ; npx vp test run src/features/teamtrau -> Test Files 28 passed (28) | Tests 115 passed (115) (14.22s)
  full suite:    npx vp test run -> Test Files 727 passed (727) | Tests 5216 passed (5216) (205.38s) ; Baseline comparison: 0 new failures (baseline 699 files / 5096 tests), 0 tsc errors (baseline 0), 0 lint errors / 457 warnings
  ruff:          vp check --no-fmt src headless vite.config.ts -> 0 errors, 457 warnings (2707 files) ; tsc --noEmit -p tsconfig.json -> 0 errors ; check:boundaries -> passed (1809 files) ; check:layer-boundaries -> passed (2663 files) ; check:unused-exports -> 0 new ; check:changed-health -> pass (11 changed files, dead_code=0, complexity=0, duplication=0) ; vp build -> ✓ built in 10.63s ; git push pre-push hook [1/6..6/6] -> all passed
ACCEPTANCE (Electron on REAL workspace E:\antigravity\demo-footage\.workspace, no OPFS):
  1. Launch 1 (`docs/superpowers/handoffs/p52_run1_editor.png`):
     - Connected native `FileSystemDirectoryHandle` directly to `E:\antigravity\demo-footage\.workspace` via native folder picker (`id: "demo-workspace"`).
     - Observed `location.origin === "app://teamtrau"`, `location.href === "app://teamtrau/editor/p-5345cb0d-b694-4f3c-8849-795b16578767"`, `window.crossOriginIsolated === true`, `typeof SharedArrayBuffer === "function"`.
     - Opened project `Assembled Sequence` (`p-5345cb0d-b694-4f3c-8849-795b16578767`, `1920x1080 | 30fps | 32s | 33 clips | 6 media | 0 missing`), then quit Electron completely.
  2. Launch 2 — Persistence (`docs/superpowers/handoffs/p52_run2_projects.png`):
     - Started a fresh Electron process and navigated to `app://teamtrau/projects`.
     - Observed `location.origin === "app://teamtrau"` and `window.crossOriginIsolated === true`.
     - `.workspace` and all `15 projects` (`Assembled Sequence`, `Curated Rough Cut`, etc.) loaded immediately from persisted `freecut-handles-db` on `app://teamtrau` without asking for the workspace folder again (`askedForFolder: false`).
  3. Launch 2 — Engine JSON Proxy, SSE Streaming & 10s Playback (`docs/superpowers/handoffs/p52_run2_job.png`, `docs/superpowers/handoffs/p52_run2_agent_playback.png`):
     - Opened `p-5345cb0d-b694-4f3c-8849-795b16578767` in Launch 2, switched to `TeamTrau -> Sequence`, selected `E:/antigravity/demo-footage/Music/soundtrack.mp3`, and clicked `Analyze soundtrack`: job completed through `app://teamtrau/engine/*` proxy showing `BPM: 117`, `DURATION: 32.0s`, `BARS: 16`, and 4 sections (`INTRO`, `BUILDUP`, `DROP Climax`, `OUTRO`) (`p52_run2_job.png`).
     - Switched to `TeamTrau -> Agent` (`Heuristic Mode`), ran prompt `"Phân tích thư mục E:/antigravity/demo-footage và cho biết có bao nhiêu take tốt"`: streamed thought (`Chế độ Heuristic Planner nội bộ...`), tool card (`generate_mcp_config DONE`), and markdown `AGENT RESPONSE` rendered unbuffered over SSE.
     - Played the timeline for 10 seconds (`00:10:00 / 00:31:29`): video decoded via `blob:app://teamtrau/...` with `readyState: 4` and active stereo audio meters (`p52_run2_agent_playback.png`).
  4. Launch 3 (`--offline`) & Launch 4 (`--engine-dir=<nonexistent>`) (`docs/superpowers/handoffs/p52_offline.png`, `docs/superpowers/handoffs/p52_missing_engine_dialog.png`):
     - Launched with `--offline`: `location.origin === "app://teamtrau"`, `crossOriginIsolated === true`, `/engine/v1/health` returned `200 {"status":"ok","version":"1"}`, and editor opened normally (`p52_offline.png`).
     - Launched with `--engine-dir=E:/antigravity/nonexistent-engine`: `/engine/v1/health` returned `503 {"error":"engine not running"}`, non-blocking native warning dialog `TeamTrau Engine Unavailable` (`The TeamTrau Python engine could not be started. Engine files not found: E:\antigravity\nonexistent-engine\.venv\Scripts\python.exe, E:\antigravity\nonexistent-engine\main.py`) appeared while the `Assembled Sequence` editor still opened and functioned (`p52_missing_engine_dialog.png`).
DEVIATIONS FROM PLAN:
  1. Added `setupPermissions(ses)` in `electron/main.cjs` (`setPermissionCheckHandler` and `setPermissionRequestHandler` allowing `app://teamtrau`) so Chromium's persisted `FileSystemDirectoryHandle.queryPermission({ mode: 'readwrite' })` returns `'granted'` automatically on subsequent Electron launches without showing a Reconnect prompt.
OPEN QUESTIONS / BLOCKERS: none
