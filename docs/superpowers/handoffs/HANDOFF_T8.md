## HANDOFF T8
STATUS: DONE
BRANCH / COMMIT: feature/studio-engine refactor(curation): review polish (batch classify, export guard, top_n clamp, docs)
FILES CHANGED:
- CHANGELOG.md
- docs/superpowers/handoffs/HANDOFF_T8.md
- docs/user_guide.md
- main.py
- src/core/curation.py
- src/core/nle_preflight.py
- src/core/service.py
- src/modules/golden_takes.py
- src/modules/mcp_server.py
- src/modules/media_classifier.py
- task.md
- tests/test_curation_polish.py
RED EVIDENCE:
```
tests/test_curation_polish.py::test_export_session_raises_when_no_timeline FAILED [ 50%]
tests/test_curation_polish.py::test_curate_footage_clamps_top_n FAILED   [100%]

================================== FAILURES ===================================
_________________ test_export_session_raises_when_no_timeline _________________
    def submit_deliver_job(self, **kw):
        self.calls.append(("deliver", kw))
>       return FakeHandle(self.results.pop(0))
                          ^^^^^^^^^^^^^^^^^^^
E       IndexError: pop from empty list

tests\curation_fakes.py:31: IndexError
______________________ test_curate_footage_clamps_top_n _______________________
        # Test values below range (< 1)
        mcp_server.curate_footage(input_dir=str(tmp_path), top_n=0)
>       assert recorded_top_n[-1] == 1
E       assert 0 == 1

tests\test_curation_polish.py:57: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_curation_polish.py::test_export_session_raises_when_no_timeline
FAILED tests/test_curation_polish.py::test_curate_footage_clamps_top_n - asse...
============================== 2 failed in 2.23s ==============================
```
GREEN EVIDENCE:
  task tests:    48 passed in 2.37s
  related suites:10 passed in 8.02s
  full suite:    1135 passed, 60 warnings in 249.98s (0:04:09)
  ruff:          All checks passed!
DEVIATIONS FROM PLAN: none
OPEN QUESTIONS / BLOCKERS: none
