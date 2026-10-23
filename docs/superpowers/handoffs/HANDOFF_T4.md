## HANDOFF T4
STATUS: DONE
BRANCH / COMMIT: feature/auto-curation-v2 feat(curation): roles and golden windows in the rough-cut stage
FILES CHANGED:
- CHANGELOG.md
- docs/superpowers/handoffs/HANDOFF_T4.md
- src/core/config.py
- src/core/service.py
- src/modules/golden_takes.py
- task.md
- tests/integration/test_rough_cut_golden_real.py
- tests/test_golden_takes.py
RED EVIDENCE:
```
tests\test_golden_takes.py:10: in <module>
    from src.modules.golden_takes import window_cleaned_segments
E   ModuleNotFoundError: No module named 'src.modules.golden_takes'
=========================== short test summary info ===========================
ERROR tests/test_golden_takes.py
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
============================== 1 error in 0.46s ===============================

tests\integration\test_rough_cut_golden_real.py:51: in test_mode_b_role_folders
>       assert {"a_roll", "b_roll"} <= _check_windows(cfg, res)
E       AssertionError: assert {'a_roll', 'b_roll'} <= {None}
E         
E         Extra items in the left set:
E         'a_roll'
E         'b_roll'
```
GREEN EVIDENCE:
  task tests:    8 passed in 27.48s
  related suites: 94 passed, 41 warnings in 114.72s (0:01:54)
  full suite:    1040 passed, 60 warnings in 285.04s (0:04:45)
  ruff:          All checks passed!
DEVIATIONS FROM PLAN: none
OPEN QUESTIONS / BLOCKERS: none
