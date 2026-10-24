## HANDOFF P4.6-fix
STATUS: DONE
BRANCH / COMMITS:
  Studio (`E:\antigravity\teamtrau-studio`, branch `teamtrau/main`):
    - `537edbc2 fix(teamtrau): expose bench hooks only in bench mode`
  Engine (`E:\antigravity\TeamTrau-AutoEdit`, branch `feature/studio-engine`):
    - `docs: HANDOFF P4.6-fix`

FIX SUMMARY:
1. Removed global window debug handles from production store and player modules:
   - `src/features/timeline/stores/items-store.ts`: removed `window.__itemsStore` assignment.
   - `src/features/timeline/stores/timeline-settings-store.ts`: removed `window.__timelineSettingsStore` assignment.
   - `src/runtime/player/clock/Clock.ts`: removed direct `window.__freecutClock` assignment and deletion.
2. Created `src/features/teamtrau/bench/bench-hooks.ts`:
   - Detects bench mode strictly via `teamtrauBench=1` query param or `import.meta.env.MODE === 'perf'`.
   - Single point of registration (`registerBenchClock`) that binds `window.__freecutClock`, `window.__itemsStore`, and `window.__timelineSettingsStore` ONLY when bench mode is active.
   - Completely no-op outside bench mode.
   - Clean teardown on `unregisterBenchClock`.
3. Created adapter contracts adhering to strict layer and feature boundaries:
   - `src/features/teamtrau/deps/timeline-contract.ts`: re-exports stores from `src/features/timeline/stores/`.
   - `src/runtime/player/deps/bench-contract.ts`: runtime contract re-exporting `registerBenchClock` and `unregisterBenchClock`.
4. Updated `scripts/teamtrau/measure-render-cache.mjs` to navigate with `?previewPerfPanel&teamtrauBench=1`.
5. Added unit test `src/features/teamtrau/bench/bench-hooks.test.ts`:
   - With no bench flag: after importing stores and creating a clock, `window.__itemsStore`, `window.__timelineSettingsStore`, and `window.__freecutClock` are `undefined`.
   - With bench flag: all three handles are defined.
6. Re-ran T1 render_cache verification:
   - Saved to `docs/teamtrau/p42-runs/p46-T1-render_cache-benchflag.json`.
   - Presented frames: 1,789 / 1,800 (dropped: 0.61%)
   - Max Stall: 70.0 ms | Median Stall: 23.3 ms
   - Decode drop: 0.28%
   - A/V offset: Mean `+3.62 ms`, P95 `44.67 ms`.

LADDER TAILS (teamtrau-studio):
- `npm run test:electron`:
```text
ℹ tests 16
ℹ suites 0
ℹ pass 16
ℹ fail 0
ℹ cancelled 0
ℹ skipped 0
ℹ todo 0
ℹ duration_ms 3019.7549
```
- `npx vp test run` (full vitest):
```text
Test Files  706 passed (706)
     Tests  5141 passed (5141)
  Start at  02:22:43
  Duration  160.16s (import 29%, environment 27%, setup 19%, tests 15%, transform 9%, worker 1%)
```
- `npx tsc --noEmit -p tsconfig.json`:
```text
clean exit 0 (0 errors)
```
- `npx vp lint src`:
```text
Found 443 warnings and 0 errors.
Finished in 7.6s on 2606 files with 92 rules using 16 threads.
```
- Boundaries & Contracts:
```text
Boundary check passed (1752 files scanned): no direct cross-feature imports outside deps/* detected.
Deps contract check passed (158 deps files scanned, 293 contract files).
Layer boundary check passed (2606 files scanned): no illegal layer dependencies detected.
Deps wrapper health: 45 pass-through wrappers, 0 unused.
```
- `node scripts/check-fallow-changed-health.mjs` (base `origin/teamtrau/main`):
```text
Using .coverage/headless.json (collected 0.3h ago).
Fallow changed health: pass (10 changed files, base origin/teamtrau/main).
Introduced: dead_code=0, complexity=0, duplication=0.
```
- `npx vp build`:
```text
✓ built in 10.54s
```

REMOTE PUSH EVIDENCE:
```text
To https://github.com/AlbedoDz/TeamTrau-Studio.git
   ec633395..537edbc2  teamtrau/main -> teamtrau/main
```
All pre-push quality checks passed (boundaries, deps-contracts, deps-wrapper-health, changed-health, edge-budgets, check).
