## HANDOFF T2-fix
STATUS: DONE
BRANCH / COMMIT: feature/auto-curation-v2 fix(curation): report missing face detector; timeout classifier audio decode
FILES CHANGED:
- CHANGELOG.md
- docs/superpowers/handoffs/HANDOFF_T2-fix.md
- src/modules/media_classifier.py
- task.md
- tests/test_media_classifier_face_fallback.py
RED EVIDENCE:
```
tests/test_media_classifier_face_fallback.py:43: AttributeError
E   AttributeError: module 'src.modules.media_classifier' has no attribute 'face_detector_available'
=========================== short test summary info ===========================
FAILED tests/test_media_classifier_face_fallback.py::test_missing_face_detector_is_a_reason
FAILED tests/test_media_classifier_face_fallback.py::test_available_face_detector_adds_no_reason
FAILED tests/test_media_classifier_face_fallback.py::test_folder_and_failure_results_are_unchanged
FAILED tests/test_media_classifier_face_fallback.py::test_unavailable_cascade_warns_once
============================== 4 failed in 0.46s ==============================
```
GREEN EVIDENCE:
  task tests:    26 passed in 1.58s
  related suites: 9 passed in 7.35s
  full suite:    1018 passed, 60 warnings in 308.00s (0:05:08)
  ruff:          All checks passed!
DEVIATIONS FROM PLAN: none
OPEN QUESTIONS / BLOCKERS: none
