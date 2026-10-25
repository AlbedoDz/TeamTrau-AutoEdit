## HANDOFF P3.4-fix
STATUS: DONE
BRANCH / COMMIT: feature/studio-engine fix(teamtrau): engine-written projects open with their media
FILES CHANGED:
- CHANGELOG.md
- docs/superpowers/handoffs/HANDOFF_P3.4-fix.md
- docs/superpowers/handoffs/p34_sequence.png
- docs/superpowers/handoffs/p34_sequence_fixed.png
- src/integrations/freecut_writer.py
- task.md
- tests/test_freecut_writer.py
RED EVIDENCE:
```
_________________ test_freecut_writer_reuses_existing_workspace_media _________________
tmp_path = WindowsPath('C:/Users/firef/AppData/Local/Temp/pytest-of-firef/pytest-current/test_freecut_writer_reuses_ex0')

    def test_freecut_writer_reuses_existing_workspace_media(tmp_path: Path):
        ...
        p2 = write_freecut_project(timeline2, workspace, "Project 2")
    
        # MUST reuse existing media ID, NOT create a second m-* folder
        media_folders_p2 = list((workspace / "media").glob("m-*"))
>       assert len(media_folders_p2) == 1
E       AssertionError: assert 2 == 1
E        +  where 2 = len([WindowsPath('.../media/m-uuid1'), WindowsPath('.../media/m-uuid2')])
tests/test_freecut_writer.py:307: AssertionError
```
GREEN EVIDENCE:
  task tests:    5 passed in 0.90s (tests/test_freecut_writer.py)
  related suites:15 passed in 3.46s (tests/test_freecut_writer.py tests/test_freecut_writer_index.py tests/test_freecut_writer_takes.py tests/test_engine_runners_p30d.py)
  full suite:    1177 passed, 60 warnings in 502.67s (0:08:22)
  ruff:          All checks passed!
MANUAL ACCEPTANCE:
Observed live in Electron (verified with Playwright automation in `capture_p34.mjs`):
1. Hydrated OPFS demo workspace from `E:/antigravity/demo-footage/.workspace`.
2. Selected footage folder `E:/antigravity/demo-footage`, ran Ingest -> Analyze roles -> Curate.
3. Switched to TeamTrau panel -> Sequence tab (`[data-value="sequence"]`).
4. Soundtrack path defaulted to `soundtrack.mp3` (`E:/antigravity/demo-footage/Music/soundtrack.mp3`).
5. Clicked "Analyze soundtrack" (`[data-testid="analyze-soundtrack-button"]`): job `analyze_soundtrack` completed and rendered:
   - BPM: `117`
   - Duration: `32.0s`
   - Bars: `16`
   - Sections list (4 sections):
     - `INTRO` (0.0s – 1.0s)
     - `BUILDUP` (1.0s – 29.4s)
     - `DROP` with Climax flame badge (29.4s – 31.0s)
     - `OUTRO` (31.0s – 32.0s)
6. Clicked "Show beat grid" (`[data-testid="toggle-beat-grid"]`):
   - Ruler overlay rendered on timeline ruler (`[data-testid="teamtrau-ruler-overlay"]`)
   - 34 beat ticks (`[data-testid="beat-tick"]`), including 16 downbeats (`[data-downbeat="true"]`)
   - 16 bar markers (`[data-testid="bar-marker"]`) with cyan index numerals (1 to 16)
   - 4 translucent section bands (`[data-testid="section-band"]`), with `DROP` band styled in amber (`[data-climax="true"]`)
7. Clicked "Add bars as markers" (`[data-testid="add-bars-markers-button"]`):
   - Executed atomic timeline command `TEAMTRAU_BAR_MARKERS` in exactly one undo step
   - Success notification displayed: `16 bar markers added` (`[data-testid="marker-message"]`)
8. Cadence dropdown defaulted to `balanced_narrative`.
9. Clicked "Assemble sequence" (`[data-testid="assemble-button"]`): job `assemble` completed, producing sequence project `p-5345cb0d-b694-4f3c-8849-795b16578767`.
10. Synced workspace files (including newly updated media metadata and media links) into OPFS.
11. Clicked "Open sequence project" (`[data-testid="open-sequence-project"]`):
    - Editor navigated to `http://127.0.0.1:20787/editor/p-5345cb0d-b694-4f3c-8849-795b16578767`
    - Timeline loaded 29 assembled clips (Track V1: 14 video clips, Track A1: 14 linked audio clips, Track A2: 1 music soundtrack clip)
    - Beat grid ruler overlay is visible above the assembled timeline clips
    - Orphan dialog check: `{ "hasDialog": false, "hasMissingMedia": false, "dialogText": null }`
    - Missing media dialog did NOT appear (0 orphaned clips)
12. Screenshot captured to `docs/superpowers/handoffs/p34_sequence.png` and `docs/superpowers/handoffs/p34_sequence_fixed.png`.
DEVIATIONS FROM PLAN: none
OPEN QUESTIONS / BLOCKERS: none
