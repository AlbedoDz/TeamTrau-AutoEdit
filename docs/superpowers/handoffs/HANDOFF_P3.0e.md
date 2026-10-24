## HANDOFF P3.0e
STATUS: DONE
BRANCH / COMMIT: feature/studio-engine fix(scoring): normalise composites without zeroing single or uniform batches
FILES CHANGED:
- CHANGELOG.md
- docs/superpowers/handoffs/HANDOFF_P3.0e.md
- docs/superpowers/handoffs/P3.0e-shortclip-diagnosis.md
- src/core/service.py
- src/modules/visual_scorer.py
- task.md
- tests/test_composite_normalization.py
RED EVIDENCE:
```
ImportError while importing test module 'E:\antigravity\TeamTrau-AutoEdit\tests\test_composite_normalization.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
C:\Users\firef\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\Lib\importlib\__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests\test_composite_normalization.py:8: in <module>
    from src.modules.visual_scorer import normalize_composites
E   ImportError: cannot import name 'normalize_composites' from 'src.modules.visual_scorer' (E:\antigravity\TeamTrau-AutoEdit\src\modules\visual_scorer.py)
=========================== short test summary info ===========================
ERROR tests/test_composite_normalization.py
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
============================== 1 error in 0.74s ===============================
```
GREEN EVIDENCE:
  task tests:    4 passed in 0.50s
  related suites:26 passed in 33.58s
  full suite:    1176 passed, 60 warnings in 511.85s (0:08:31)
  ruff:          All checks passed!
DEVIATIONS FROM PLAN: none
OPEN QUESTIONS / BLOCKERS: none
