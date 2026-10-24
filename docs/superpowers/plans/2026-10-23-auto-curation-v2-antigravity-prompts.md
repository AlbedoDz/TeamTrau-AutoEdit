# Bộ prompt điều phối Antigravity — Auto-Curation v2

Plan: `docs/superpowers/plans/2026-10-23-auto-curation-v2.md`

## Cách vận hành (dành cho bạn)

```
 ┌──────────────┐  1. dán MASTER + TASK Tn   ┌─────────────────────┐
 │     Bạn      │ ─────────────────────────► │ Antigravity (Gemini)│
 │ (chuyển tin) │ ◄───────────────────────── │  làm đúng 1 task,   │
 └──────┬───────┘  2. HANDOFF REPORT Tn      │  commit, dừng       │
        │                                    └─────────────────────┘
        │ 3. "review T<n>" + dán HANDOFF
        ▼
 ┌──────────────┐  4a. GO  → sang task tiếp theo
 │ Claude Opus  │  4b. FIX → prompt sửa lỗi, dán lại cho Antigravity
 │ (điều phối)  │      (tự chạy test, đọc diff, không tin báo cáo suông)
 └──────────────┘
```

Quy tắc vận hành:
1. **Mỗi task = một phiên Antigravity mới.** Dán `MASTER PROMPT`, rồi dán `TASK Tn`. Không gộp nhiều task vào một phiên vì ngữ cảnh dài làm model nhỏ trôi khỏi plan.
2. Antigravity xong việc sẽ in khối `HANDOFF T<n>`. Bạn quay lại đây, gửi tôi: `review T<n>` kèm khối đó.
3. Tôi tự chạy test và đọc `git diff`, rồi trả về **GO** hoặc một **FIX prompt**. Chỉ sang task tiếp theo khi đã có GO.
4. Nếu Antigravity báo `STATUS: BLOCKED` (test khoá có vẻ sai, cần sửa file ngoài danh sách...) thì **đừng tự cho phép**. Chuyển nguyên văn cho tôi quyết.
5. Model gợi ý: T0, T1, T5 dùng Gemini Flash được. T2, T3, T4, T6, T7 nên dùng mức reasoning cao nhất bạn có, vì các task này dính tới thuật toán và wiring service.

---

## MASTER PROMPT (dán đầu mỗi phiên Antigravity)

```text
You are the IMPLEMENTER on TeamTrau-AutoEdit. An external architect (Claude Opus) wrote the plan and
the contract tests and will review your work. Your job: execute EXACTLY ONE task of the plan, test-first,
then stop and report.

READ FIRST (in this order):
1. AGENTS.md  (follow §1 bootstrap: task.md, memory_tool status, memory_tool search <keywords of the task>)
2. docs/superpowers/plans/2026-10-23-auto-curation-v2.md  -> §1 Decisions, §2 Global rules, and ONLY your task section.
   Do NOT read or execute docs/superpowers/plans/2026-10-23-auto-curation-rough-cut.md (v1, superseded).

HARD RULES (breaking any one = task rejected):
R1  Contract tests/helpers in the plan are copied VERBATIM into the listed files. They are LOCKED.
    Never edit, weaken, skip, xfail, or delete them. Only allowed change: `ruff check --fix --select I <file>`.
    You MAY add extra tests in new files.
R2  TDD order: (a) write the locked tests, (b) run them and paste the RED output, and confirm the failure
    reason matches the plan's "Expected red", (c) implement, (d) GREEN. If red fails for a different
    reason (fixture error, typo, wrong existing API), STOP with STATUS: BLOCKED and show the evidence.
R3  Touch ONLY the files listed under "Files" of your task (+ CHANGELOG.md, task.md). Need another
    file? STOP with STATUS: BLOCKED and explain why.
R4  Reuse existing code named in the plan (§0 table). Do not re-implement probe, dHash, timebase,
    quality curves, exporters. Do not rename or remove existing public functions.
R5  No new dependencies. Never run pip/uv add. Use .venv\Scripts\python.exe only.
R6  No hiding failures: no broad try/except to make a test pass, no mocking the unit under test,
    no sleeps, no changing thresholds or constants given in the plan.
R7  Exact names, signatures, constants and algorithms from the plan. Where the plan is silent,
    choose the simplest option and list it under DEVIATIONS.
R8  Code style: Python 3.11 types, match surrounding comment density and naming, no emojis in code,
    ruff clean (line length 100).
R9  Verification ladder, all must be green before commit:
      .venv\Scripts\python.exe -m pytest <task tests> -v
      .venv\Scripts\python.exe -m pytest <related suites listed in the task> -q
      .venv\Scripts\python.exe -m pytest tests -q          (full suite, ~6 min; run once at the end)
      .venv\Scripts\python.exe -m ruff check src tests main.py
R10 Git: work on branch feature/auto-curation-v2 (Task 0 creates it). One commit per task with the
    message given in the plan, plus a CHANGELOG.md bullet under "## [Unreleased]". Never push,
    never merge, never rebase, never amend earlier commits.
R11 Never claim a result you did not paste. Every PASS claim needs the pytest tail in the report.
R12 Do ONLY your task. When done, print the HANDOFF block and STOP. Do not start the next task.

HANDOFF FORMAT (print exactly this block at the end):
## HANDOFF T<n>
STATUS: DONE | BLOCKED
BRANCH / COMMIT: <branch> <short sha> <message>
FILES CHANGED: <list>
RED EVIDENCE: <last ~15 lines of the failing run, showing the expected error>
GREEN EVIDENCE:
  task tests:    <tail line, e.g. "14 passed in 2.1s">
  related suites:<tail line>
  full suite:    <tail line, e.g. "1012 passed, 54 warnings in 341s">
  ruff:          <"All checks passed!" or output>
DEVIATIONS FROM PLAN: none | <numbered list: what, why>
OPEN QUESTIONS / BLOCKERS: none | <list>
```

---

## TASK T0 — Hygiene & baseline

```text
Execute Task 0 of docs/superpowers/plans/2026-10-23-auto-curation-v2.md.

Notes:
- The user approves committing the existing realtime-playback work on main as its own commit
  (exact file list in the plan). Before committing, run `git status` and `git diff --stat` and paste them.
  If ANY other modified/untracked file appears that is not in the plan's lists, STOP (BLOCKED).
- Never commit .claude/ (local tool state). Add `.claude/` to .gitignore only if it is not ignored
  already, in the docs commit.
- No production code changes in this task. "Expected red" does not apply; RED EVIDENCE = "n/a (T0)".
- Baseline must be "981 passed". If it is not, STOP (BLOCKED) with the failures.
```

## TASK T1 — Role folders (Mode B)

```text
Execute Task 1 of docs/superpowers/plans/2026-10-23-auto-curation-v2.md.

Traps to avoid:
- role_folder_of must look at DIRECTORY components only (Path(p).parent.parts), never the file name.
  Normalise each component: lower(), then remove "-", "_" and " ". Exact equality with "aroll" / "broll".
- Keep every existing keyword rule of infer_camera_role untouched; only add the folder check first.
- collect_video_files: only descend ONE level into role folders. Do not use rglob.
- find_soundtrack: only a direct subfolder whose name.lower() == "music"; exactly one audio file -> return it.
- Run tests/test_multifolder_ingest.py: the existing role assertions must stay green.
```

## TASK T1b — Media Browser follows Mode B folder rules (review follow-up)

```text
ADDENDUM from the architect (do this BEFORE the T1b tests, as a separate first commit):
Commit the architect's pending edits to docs/superpowers/plans/2026-10-23-auto-curation-v2.md and
docs/superpowers/plans/2026-10-23-auto-curation-v2-antigravity-prompts.md (new Task 1b) and add
"  - [ ] Task 1b: Media Browser follows Mode B folder rules" under Task 1 in task.md, with message:
"docs: plan v2 task 1b (media browser role folders)".

Execute Task 1b of docs/superpowers/plans/2026-10-23-auto-curation-v2.md (its own commit).

Traps to avoid:
- role_folder_of: iterate reversed(Path(p).parent.parts) so the NEAREST role folder wins.
  Nothing else changes in ontology.py.
- media_browser.py: import collect_video_files and find_soundtrack from src.core.service and use them
  in _scan_directory, add_footage_folder and is_valid_setup. Do not write a new scanner.
- add_footage_folder: role per file = default_role or infer_camera_role(f).value.
- set_input_dir: auto-fill the Music soundtrack ONLY when get_soundtrack_path() is None.
- Do not touch main_window.py or any other GUI file.
- GUI tests run headless (QT_QPA_PLATFORM=offscreen is set inside the tests).
```

## TASK T2 — Content role classifier

```text
Execute Task 2 of docs/superpowers/plans/2026-10-23-auto-curation-v2.md.

Traps to avoid:
- decide_role: implement the 4 rules IN ORDER with the exact formulas. Do not add rules (e.g. motion),
  do not "improve" thresholds. D6: speech without a face is A_ROLL with reason "offscreen_speech".
- voice_activity: the exact energy algorithm in the plan (30 ms frames, 10th percentile floor,
  +10 dB and > -50 dB). No webrtcvad, no librosa, no scipy.
- run_tool_bytes: same as run_tool but binary stdout (no text mode). Put it right after run_tool.
- Haar cascade: load ONCE lazily (module-level cache), path from cv2.data.haarcascades.
- extract_signals: open video the same way shot_windows.compute_quality_curve does (read that code first);
  unicode paths must work (there is an integration test with Vietnamese file names).
- classify_clip must never raise; classify_batch keeps input order.
- Integration tests: run them explicitly:
  .venv\Scripts\python.exe -m pytest tests/integration/test_media_classifier_real.py -v
```

## TASK T3 — Golden window selection

```text
Execute Task 3 of docs/superpowers/plans/2026-10-23-auto-curation-v2.md.

Traps to avoid:
- Reuse shot_windows.score_window for scoring. Do NOT write a second goodness function.
- Candidate lengths are exactly {max_len, (min_len+max_len)/2, min_len}. Pick the LONGEST length whose best
  score >= best_overall - tolerance. Ties on position -> earliest start.
- Start positions: start + k / curve.hz for k = 0, 1, ... while <= end - L, plus end - L itself
  (deduplicate). Compute as start + k / hz (not by repeated addition) to avoid float drift.
- Overlap between takes is strict (touching allowed); widen earlier takes by min_len on both sides.
- Genre ranges: add the field golden_window_sec to SubGenrePreset (last field, with default), fill it in a
  _with_golden_windows(...) pass exactly like _with_cadence does, and make SubGenrePreset.apply() set
  config.golden_window_min_sec / config.golden_window_max_sec.
- Related suites must stay green: tests/test_genre_presets.py tests/test_extended_shots_and_cadence.py
  tests/test_config.py tests/test_gui_config_precedence.py
```

## TASK T4 — Wire into the rough-cut stage

```text
Execute Task 4 of docs/superpowers/plans/2026-10-23-auto-curation-v2.md.

Before coding, read src/core/service.py _execute_pipeline from the "Stage 3: Rough-cut defect pruning"
comment down to where _golden_scenes(...) is called, and _golden_scenes itself. Paste the line numbers
you will change into the report.

Traps to avoid:
- window_cleaned_segments is a PURE function (no I/O); all I/O goes through curve_fn. Never mutate
  input segments; return the same object only for clips without a role.
- Classification runs ONCE per unique clip, on the original media (seg.scene.original_path or seg.clip_path),
  keyed by seg.clip_path. Respect cancel_event between clips.
- Only touch the golden path (stage "rough_cut", or "all" with run_rough_cut_in_all). Do not change the
  legacy workflow_mode == "rough_cut" path or the sequence stage.
- _golden_scenes: copy content_role / role_confidence / golden_window from seg.metadata into the
  per-scene meta dict. Nothing else changes there.
- Integration tests are slow (real ffmpeg + cleaner). Run them explicitly:
  .venv\Scripts\python.exe -m pytest tests/integration/test_rough_cut_golden_real.py -v
  If the cleaner prunes all testsrc2 footage, you may ONLY change the lavfi video source inside _clip
  (see the plan's fixture note) and must list it under DEVIATIONS.
- Related suites: tests/test_staged_pipeline.py tests/test_rough_cut_cleaner.py
  tests/test_gui_staged_workflow.py tests/test_scene_curation_and_cancel.py tests/test_pipeline_e2e.py
```

## TASK T5 — NLE pre-flight

```text
Execute Task 5 of docs/superpowers/plans/2026-10-23-auto-curation-v2.md.

Traps to avoid:
- Do NOT modify any exporter (fcpxml_exporter, timeline_builder, premiere_xml_fix, capcut_exporter).
  D1: URIs stay as they are; preflight only CHECKS the round trip using fcpxml_exporter._uri_to_path.
- Use src.core.timebase.frame_rate_fraction for FPS_MISMATCH and src.core.media_info.probe_media as the
  default probe (injected via the probe parameter; tests pass a fake).
- Deliver wiring: only the non-preview path of StudioBackendAdapter._execute_deliver. Preflight never
  blocks or fails an export; write <out_dir>/preflight.json (UTF-8, indent 2) and fill
  DeliverResult.preflight_path / preflight_issues ("<severity>:<code>:<file name>").
- Related suites include tests/integration (real ffmpeg) - run the whole folder once:
  .venv\Scripts\python.exe -m pytest tests/integration -q
```

## TASK T6 — Curation core + MCP macro-tools

```text
Execute Task 6 of docs/superpowers/plans/2026-10-23-auto-curation-v2.md.

Traps to avoid:
- All logic lives in src/core/curation.py. mcp_server.py only adds _SESSIONS, MAX_SESSIONS and three thin
  @mcp_server.tool() functions that call curation.* with adapter=adapter (the module attribute, looked up
  at call time so tests can monkeypatch it). Plain sync functions (D7). Do not touch other tools.
- Adapter calls: keyword arguments ONLY. handle.wait(timeout) falsy -> TimeoutError;
  result.success False -> RuntimeError(result.error_message).
- run_curation validates the directory and the genre BEFORE any adapter call.
- assemble_sequence validates ids and resolves the soundtrack BEFORE any adapter call.
- Summary keys are exactly: takes, curated_duration_sec, by_role, top, truncated (+ session_id added by
  run_curation). No numeric arrays anywhere (D8: <= 2000 chars).
- Create tests/curation_fakes.py exactly as in the plan. Do NOT add tests/__init__.py.
- Related suites: tests/test_mcp_server.py tests/test_mcp_comprehensive.py tests/test_agent_controller.py
```

## TASK T7 — CLI, E2E, release docs

```text
Execute Task 7 of docs/superpowers/plans/2026-10-23-auto-curation-v2.md.

Traps to avoid:
- main.py: `from src.core import curation` and call curation.run_curation / curation.export_session
  THROUGH THE MODULE with keyword arguments (tests monkeypatch them). Map ValueError and
  FileNotFoundError to click.BadParameter (exit code 2). Follow the style of the existing commands.
- E2E test is slow (curate + sequence + export on real media). Run it explicitly:
  .venv\Scripts\python.exe -m pytest tests/integration/test_auto_curation_e2e.py -v
  If it fails, debug the product code. Never weaken the test. If the root cause is outside your
  files, STOP (BLOCKED) with the traceback.
- Docs: user guide section in Vietnamese + English short summary; TECHNICAL_HANDOFF rev 2.4.0;
  CHANGELOG release summary; task.md all T0..T7 checked, Milestone B listed unchecked.
- Run AGENTS.md §2 end-of-session memory steps. Do NOT push or merge.
```

---

## FIX PROMPT (mẫu tôi sẽ gửi khi review không đạt)

```text
REVIEW RESULT T<n>: FIX REQUIRED. Stay on branch feature/auto-curation-v2. Same HARD RULES as MASTER PROMPT.

Problems found by the architect (each must be fixed; do not change anything else):
1. <file:line> - <problem> - <expected behaviour>
2. ...

Then: re-run the verification ladder (R9), make ONE new commit "fix(<area>): address review T<n>",
and print a new HANDOFF T<n> block.
```

## Checklist tôi dùng khi review mỗi task (để bạn biết tôi kiểm gì)

1. `git log --oneline` / `git diff main...` có đúng file trong danh sách và đúng message commit không.
2. Test khoá có bị sửa không. So khớp từng byte với plan bằng script trích xuất.
3. Tự chạy lại: test của task, các suite liên quan, toàn bộ suite, ruff.
4. Đọc code: có tái dùng module cũ không, có nuốt lỗi không, có đổi hằng số không, có code thừa không.
5. Các mục DEVIATIONS có chấp nhận được không. Cái nào cần ghi lại thì ghi vào plan.
6. Có `CHANGELOG.md` và `task.md` chưa.
