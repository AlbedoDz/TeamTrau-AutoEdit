## HANDOFF T3
STATUS: DONE
BRANCH / COMMIT: feature/auto-curation-v2 feat(curation): genre-aware golden window selection
FILES CHANGED:
- CHANGELOG.md
- docs/superpowers/handoffs/HANDOFF_T3.md
- src/core/config.py
- src/core/genre_presets.py
- src/modules/shot_windows.py
- task.md
- tests/test_golden_windows.py
RED EVIDENCE:
```
tests\test_golden_windows.py:9: in <module>
    from src.modules.shot_windows import QualityCurve, select_golden_windows
E   ImportError: cannot import name 'select_golden_windows' from 'src.modules.shot_windows' (E:\antigravity\TeamTrau-AutoEdit\src\modules\shot_windows.py)
=========================== short test summary info ===========================
ERROR tests/test_golden_windows.py
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
============================== 1 error in 0.67s ===============================
```
GREEN EVIDENCE:
  task tests:    14 passed in 0.77s
  related suites: 50 passed in 25.52s
  full suite:    1032 passed, 60 warnings in 296.88s (0:04:56)
  ruff:          All checks passed!
DEVIATIONS FROM PLAN: none
OPEN QUESTIONS / BLOCKERS: none
