## HANDOFF P5.4
STATUS: DONE
BRANCH / COMMIT: teamtrau/main fc8ae30b feat(teamtrau): local Windows installer with the bundled engine sidecar (electron-builder NSIS)
FILES CHANGED:
  teamtrau-studio (branch teamtrau/main, commit fc8ae30b):
    docs/teamtrau/CHANGELOG.md
    docs/teamtrau/packaging.md (new)
    electron-builder.config.cjs (new)
    electron/__tests__/builder-config.test.cjs (new, LOCKED)
    electron/builder-config.cjs (new)
    package-lock.json
    package.json
  TeamTrau-AutoEdit (branch feature/studio-engine):
    docs/superpowers/handoffs/HANDOFF_P5.4.md
    docs/superpowers/handoffs/p54_installed_editor.png
    docs/superpowers/handoffs/p54_installed_job.png
RED EVIDENCE:
  node:internal/modules/cjs/loader:1572
    throw err;
    ^

  Error: Cannot find module '../builder-config.cjs'
  Require stack:
  - E:\antigravity\teamtrau-studio\electron\__tests__\builder-config.test.cjs
      at Module._resolveFilename (node:internal/modules/cjs/loader:1568:15)
      at defaultResolveImplForCJSLoading (node:internal/modules/cjs/loader:1146:10)
      at resolveForCJSWithHooks (node:internal/modules/cjs/loader:1173:12)
      at Module._load (node:internal/modules/cjs/loader:1345:5)
      at TracingChannel.traceSync (node:diagnostics_channel:328:14)
      at wrapModuleLoad (node:internal/modules/cjs/loader:260:19)
      at Module.require (node:internal/modules/cjs/loader:1689:12)
      at require (node:internal/modules/helpers:191:16)
      at Object.<anonymous> (E:\antigravity\teamtrau-studio\electron\__tests__\builder-config.test.cjs:6:46)
  ✖ electron\__tests__\builder-config.test.cjs (82.9379ms)
  ℹ tests 1 | pass 0 | fail 1 | duration_ms 93.1713
GREEN EVIDENCE:
  task tests:    node --test electron/__tests__/builder-config.test.cjs -> 6 passed, 0 failed (117.07ms)
  related suites:npm run test:electron -> 31 passed, 0 failed (3424.16ms) ; npx vp test run src/features/teamtrau -> Test Files 28 passed (28) | Tests 115 passed (115) (37.35s)
  full suite:    npx vp test run -> Test Files 727 passed (727) | Tests 5216 passed (5216) (199.49s) ; Baseline comparison: 0 new failures (baseline 699 files / 5096 tests), 0 tsc errors (baseline 0), 0 lint errors / 457 warnings
  ruff:          vp check --no-fmt src headless vite.config.ts -> Found 0 errors and 457 warnings in 2707 files (8.4s) ; tsc --noEmit -p tsconfig.json -> 0 errors ; check:boundaries -> passed (1809 files) ; check:layer-boundaries -> passed (2663 files) ; check:unused-exports -> 0 new ; check:changed-health -> pass (7 changed files, dead_code=0, complexity=0, duplication=0) ; vp build -> ✓ built in 10.47s ; git push pre-push hook [1/6..6/6] -> all passed
ACCEPTANCE (Windows, installed app on REAL workspace E:\antigravity\demo-footage\.workspace, no OPFS):
  1. Engine sidecar check (`E:\antigravity\TeamTrau-AutoEdit\dist\teamtrau-engine`):
     - Verified existing sidecar with shared FFmpeg build (`teamtrau-engine.exe` + `tools/ffmpeg.exe` + `tools/avcodec-62.dll` + `tools/FFMPEG-LICENSE.txt`).
  2. `npm run dist:win` build log tail, sizes, and `app.asar` inspection:
     - Log tail:
       • updating asar integrity executable resource  executablePath=out\installer\win-unpacked\TeamTrau Studio.exe
       • no code signing certificate configured, signing is skipped  path=out\installer\win-unpacked\resources\engine\teamtrau-engine.exe
       • no code signing certificate configured, signing is skipped  path=out\installer\win-unpacked\resources\engine\tools\ffprobe.exe
       • no code signing certificate configured, signing is skipped  path=out\installer\win-unpacked\resources\engine\tools\ffmpeg.exe
       • no code signing certificate configured, signing is skipped  path=out\installer\win-unpacked\TeamTrau Studio.exe
       • building        target=nsis file=out\installer\TeamTrau-Studio-Setup-0.1.0.exe archs=x64 oneClick=false perMachine=false
       • no code signing certificate configured, signing is skipped  path=out\installer\TeamTrau-Studio-Setup-0.1.0.__uninstaller.exe
       • no code signing certificate configured, signing is skipped  path=out\installer\TeamTrau-Studio-Setup-0.1.0.exe
       • building block map  blockMapFile=out\installer\TeamTrau-Studio-Setup-0.1.0.exe.blockmap
     - Installer file: `out\installer\TeamTrau-Studio-Setup-0.1.0.exe` — 345,071,008 bytes (345.1 MB decimal / 329.1 MiB)
     - `out\installer\win-unpacked` size: 1,188,842,879 bytes (1.19 GB decimal / 1133.8 MiB)
     - `npx asar list out\installer\win-unpacked\resources\app.asar`:
       Total entries: 383
       node_modules entries: 0
       .map entries: 0
       __tests__ entries: 0
  3. Silent per-user install into scratch folder:
     - Command: `TeamTrau-Studio-Setup-0.1.0.exe /S /D=E:\antigravity\teamtrau-studio\out\scratch-install\TeamTrauStudio`
     - Verified `TeamTrau Studio.exe`, `Uninstall TeamTrau Studio.exe`, and `resources\engine\teamtrau-engine.exe` in `E:\antigravity\teamtrau-studio\out\scratch-install\TeamTrauStudio`.
  4. Installed exe launched with `--remote-debugging-port=9333`:
     - GPU log: `[gpu] ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Laptop GPU (0x0000249D) Direct3D11 vs_5_0 ps_5_0, D3D11-32.0.16.1742)`
     - `location.origin`: `"app://teamtrau"`
     - `window.crossOriginIsolated`: `true`
     - `/engine/v1/health`: `200 {"status": "ok", "version": "1"}`
     - `/engine/v1/capabilities.ffmpeg`: `"E:\\antigravity\\teamtrau-studio\\out\\scratch-install\\TeamTrauStudio\\resources\\engine\\tools\\ffmpeg.EXE"`
  5. Real workspace connection, 5s timeline playback, and soundtrack analysis (`docs/superpowers/handoffs/p54_installed_editor.png`, `docs/superpowers/handoffs/p54_installed_job.png`):
     - Picked `E:\antigravity\demo-footage\.workspace` via native folder picker (`FileSystemDirectoryHandle`, no OPFS) and opened project `Assembled Sequence` (`p-5345cb0d-b694-4f3c-8849-795b16578767`, `1920x1080 | 30fps | 32s | 33 clips | 6 media | 0 missing`).
     - Played timeline for 5s (`00:05:06 / 00:31:29`) and captured `docs/superpowers/handoffs/p54_installed_editor.png`.
     - Opened `TeamTrau -> Sequence`, selected `E:/antigravity/demo-footage/Music/soundtrack.mp3`, ran `Analyze soundtrack` (`BPM: 117`, `DURATION: 32.0s`, `BARS: 16`, sections `INTRO`, `BUILDUP`, `DROP Climax`, `OUTRO`), and captured `docs/superpowers/handoffs/p54_installed_job.png`.
  6. Clean engine termination on quit:
     - Closed installed app and ran `tasklist /FI "IMAGENAME eq teamtrau-engine.exe"`:
       `INFO: No tasks are running which match the specified criteria.`
  7. Silent uninstall and `%APPDATA%\TeamTrau Studio` preservation:
     - Ran `E:\antigravity\teamtrau-studio\out\scratch-install\TeamTrauStudio\Uninstall TeamTrau Studio.exe /S`:
       `Install folder exists after uninstall: False`
       `APPDATA TeamTrau Studio exists: True`
  8. Verification ladder:
     - `npm run test:electron`: 31 passed, 0 failed
     - `vp check --no-fmt src headless vite.config.ts`: 0 errors, 457 warnings
     - `npx vp test run src/features/teamtrau`: 28 files passed, 115 tests passed
     - `npx vp test run`: 727 files passed, 5216 tests passed
     - `npx vp build`: ✓ built in 10.47s
DEVIATIONS FROM PLAN:
  1. Added `'!node_modules/**'` to `files` in `electron/builder-config.cjs` so `electron-builder`'s `getNodeModuleFileMatcher` excludes production `node_modules` from `app.asar` (producing the 345.1 MB installer and 1.19 GB `win-unpacked` with 0 `.map` files and 0 `__tests__` entries in `app.asar`).
OPEN QUESTIONS / BLOCKERS: none
