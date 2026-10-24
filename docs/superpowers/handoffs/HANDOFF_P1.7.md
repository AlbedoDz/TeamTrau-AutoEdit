## HANDOFF P1.7
STATUS: DONE
BRANCH / COMMIT: feature/studio-engine refactor(engine): phase 1 review polish (probe guards, reader errors, token checks, job eviction, torch device)
FILES CHANGED:
- CHANGELOG.md
- docs/superpowers/handoffs/HANDOFF_P1.7.md
- main.py
- src/core/platform_caps.py
- src/dispatchers/nvenc_renderer.py
- src/engine_api/app.py
- src/engine_api/jobs.py
- src/integrations/freecut_reader.py
- src/integrations/freecut_writer.py
- src/modules/visual_scorer.py
- task.md
- tests/test_phase1_polish.py
RED EVIDENCE:
```
FAILED tests/test_phase1_polish.py::test_probe_survives_ffmpeg_list_failure[exc0]
FAILED tests/test_phase1_polish.py::test_probe_survives_ffmpeg_list_failure[exc1]
FAILED tests/test_phase1_polish.py::test_corrupt_index_leaves_no_orphan_project_dir
FAILED tests/test_phase1_polish.py::test_reader_rejects_corrupt_media_json[source.link.json]
FAILED tests/test_phase1_polish.py::test_create_app_rejects_empty_token - Failed: DID NOT RAISE <class 'ValueError'>
FAILED tests/test_phase1_polish.py::test_serve_rejects_empty_token_file - assert 0 != 0
FAILED tests/test_phase1_polish.py::test_registry_evicts_oldest_finished_jobs
FAILED tests/test_phase1_polish.py::test_visual_scorer_skips_torch_model_without_torch_cuda
========================= 8 failed, 1 passed in 8.68s =========================
```
GREEN EVIDENCE:
  task tests:    9 passed in 3.90s
  related suites: 55 passed in 7.70s
  full suite:    1144 passed, 60 warnings in 281.15s (0:04:41)
  ruff:          All checks passed!
DEVIATIONS FROM PLAN:
1. PlatformCaps.best_h264 implemented as property returning _CallableEncoder(str) when present (supporting both .best_h264() and .best_h264) and None when empty, satisfying both P1.7 locked test (caps.best_h264 is None) and P1.1 locked test (caps.best_h264() == "h264_vaapi").
OPEN QUESTIONS / BLOCKERS: none
