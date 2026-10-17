## Commits 4b2de02..7ea75d2 (2026-10-17)
Itemized by commit. Summary for the unlisted range; Phase 17-19 below covers the same work at feature level.

- `4b2de02` **fix: real-footage robustness** for render, ingest, audio, NLE export. Adds `src/core/ffmpeg_utils.py`, real-footage integration tests (`tests/integration/`), robustness tests. Renderer rewritten (`nvenc_renderer.py`).
- `e86c80d` **feat: split Edit and Deliver.** Analysis stops at "Timeline ready". New `src/modules/edit_timeline.py`, `src/core/project_io.py`. `main.py deliver` command. MCP `render_master` / `export_timeline` take `project_path`.
- `51f824d` **perf(ingest):** single-pass dead-air detection, sequential motion sampling, metadata reuse. Adds `scripts/benchmark_pipeline.py`.
- `d2dcbfd` **perf(render):** NVENC preset p5 default, optional NVDEC hwaccel with software fallback.
- `ccce241` **feat(ui): Edit/Deliver workflow** in Desktop Studio. Main window, timeline view, export panel and dialog updated; undo commands; new GUI tests.
- `b13cd2f` **perf(pipeline):** shared keyframe sweep (`keyframe_cache.py`), analysis disk cache (`analysis_cache.py`), GPU batching in visual scorer and reframer, concurrent audio/whisper stages.
- `1ab4fa4` **test(pipeline):** concurrency tests for audio/whisper overlap and cancellation.
- `7ea75d2` **Merge** `perf/pipeline-speed`.

---

## Phase 17-20 (2026-10-17)
- **FCPXML**: real FCPXML 1.8 for DaVinci Resolve (own writer; previously an FCP7 XML was written into a `.fcpxml` file). Verified by importing into DaVinci Resolve Studio 21; speed ramps fixed after pixel comparison.
- **Resolve dispatch**: imports into a new project (never your open one), keeps 29.97/23.976 rates, auto-uses a Python version Resolve supports.
- **Edit/Deliver split**: analysis ends at "Timeline ready"; nothing is rendered or exported until the user asks (GUI Export button, `main.py deliver`, MCP `render_master`/`export_timeline` with `project_path`). Exports match the edited timeline exactly. New 540p preview render.
- **Fixes**: unicode filenames, speed-ramp audio sync, clips without audio, native-quality music, rotated phone footage, NLE source ranges, beat alignment, failed-clip reporting.
- **Speed**: ~2.3x faster cold, ~2.5x cached overall (analysis 33.6s -> 1.8s cached): shared keyframes, analysis cache, merged dead-air pass, GPU batching, concurrent stages.

# CHANGELOG

---

## [Push 6] — 2026-10-17
**feat: Phase 7 hardening - rough cut cleaner, VFR normalization, VRAM cleanup, VLM semaphore**

### Hardening & Production Reliability
- `src/modules/rough_cut_cleaner.py`: Loại bỏ clip rung lắc, mờ tối, micro-cuts, dead-air trước khi vào sequencer.
- `src/modules/ingest_detector.py`: Tự động phát hiện và chuẩn hóa Variable Frame Rate (VFR) sang Constant Frame Rate (CFR) để tránh lệch tiếng.
- `src/modules/visual_scorer.py`: Cơ chế dọn dẹp bộ nhớ PyTorch CUDA VRAM (`torch.cuda.empty_cache()`, giải phóng model handler).
- `src/modules/vlm_assistant.py`: Bổ sung `asyncio.Semaphore` giới hạn số lượng request đồng thời tránh tràn rate-limit API.
- `docs/TECHNICAL_HANDOFF.md`: Tài liệu bàn giao kỹ thuật kiến trúc, quy trình deploy và vận hành pipeline.

### Tests
- 228/228 test pass 100% (+4 test files mới: `test_rough_cut_cleaner`, `test_vfr_normalization`, `test_vram_cleanup`, `test_vlm_concurrency`).

---

## [Push 5] — 2026-10-17
**feat: Phase 6 multi-variant synthesizer, genre recommender, security guardrails**

### Security & Environment
- Thêm bảo mật `.gitignore`: chặn `.env`, `.env.*`, `*.pem`, `*.key`, `token.json`, `credentials.json`, `secrets.json`.
- Thêm `.env.example` template sạch không lộ API key.

### Core Modules Mới
- `src/core/ontology.py`: Video semantic ontology, scene taxonomy, pacing attributes.
- `src/core/genre_presets.py`: Presets đạo diễn 6 phong cách (cinematic, montage, docu, vlog, tech, trailer).
- `src/modules/genre_recommender.py`: Gợi ý genre tự động theo phân tích footage và soundtrack.
- `src/modules/variant_synthesizer.py`: Tổng hợp đa biến thể timeline (A/B/C testing).

### UI & Pipeline Enhancements
- Tích hợp đa folder ingest và filter scenes theo folder nguồn (`src/modules/ingest_detector.py`, `src/ui/widgets/media_browser.py`).
- Cập nhật GUI Project Manager, Timeline View, Export Panel hỗ trợ rendering đa biến thể song song.

### Tests
- 208/208 test pass 100% (+4 test files mới: `test_genre_presets`, `test_genre_recommender`, `test_multifolder_ingest`, `test_variant_synthesizer`).

---

## [Push 4] `44b4f7b` — 2026-10-17
**feat: TeamTrau-AutoEdit v1 - automated video editing pipeline**

> Squash commit (orphan branch). Fix HTTP 500 chunked push. Pack size tối thiểu.

### Modules mới
- `src/dispatchers/watermark_helper.py` — watermark overlay
- `src/modules/agent_controller.py` — MCP agent controller
- `src/modules/audio_normalizer.py` — loudness normalization LUFS
- `src/modules/kinetic_subtitle.py` — kinetic typography subtitles
- `src/modules/subtitle_formatter.py` — SRT/ASS formatter
- `src/modules/timeline_qa.py` — timeline QA checks
- `src/modules/visual_flow.py` — transition scorer

### UI mới
- `src/ui/controllers/project_manager.py`
- `src/ui/controllers/shortcut_manager.py`
- `src/ui/controllers/undo_commands.py`
- `src/ui/widgets/agent_console_dialog.py`
- `src/ui/widgets/export_dialog.py`
- `src/ui/widgets/inspector_panel.py`
- `src/ui/widgets/player_widget.py`
- `src/ui/widgets/settings_dialog.py`

### Tests: +13 file mới

---

## [Push 3] `ccf3624` — 2026-10-17
**chore: exclude .agent/, .codegraph/ - keep only source code**

- Loại `.agent/` (skills, subagents, memory scripts, font TTF) khỏi tracking
- Loại `.codegraph/` khỏi tracking
- `.gitignore` cập nhật: block agent infra, local tooling
- Repo size: ~7MB → **~559KB**

---

## [Push 2] `7477608` — 2026-10-17
**chore: add .gitignore, remove test footage/output/pycache from tracking**

- Thêm `.gitignore` toàn diện
- Untrack: `test/*.MP4` (7 DJI clip), `test/audio/*.mp3`, `output/` (mp4, fcpxml, otio, edl, xml, csv), `__pycache__/`, `yolov8n.pt`
- 76 files changed

---

## [Push 1] `b4bfeb3` — 2026-10-17
**feat: initial commit - TeamTrau-AutoEdit pipeline v1**

### Source code
- `main.py` — CLI entrypoint (`run`, `check-env`, `list-presets`)
- `src/core/config.py` — Preset system: `1080p30/60/24`, `4k30/60/24`, `social_vertical`, `cinema_4k`
- `src/core/service.py` — Pipeline orchestrator
- `src/core/types.py` — Typed dataclasses (`SceneInfo`, `AudioAnalysis`, `PipelineResult`)

### Modules
- `src/modules/ingest_detector.py` — PySceneDetect adaptive, FFmpeg silencedetect/freezedetect, EDL/CSV export
- `src/modules/audio_analyzer.py` — Librosa beat tracking, 4/4 downbeat, onset envelope, min 0.4s filter
- `src/modules/visual_scorer.py` — MobileNetV3 CUDA, Tenengrad sharpness, dHash dedup (Hamming)
- `src/modules/timeline_builder.py` — OpenTimelineIO multi-track, Retention Teaser Hook, FCPXML/XML/OTIO export
- `src/modules/capcut_exporter.py` — CapCut draft JSON export
- `src/modules/duration_packer.py` — Bin-packing scene selection by target duration
- `src/modules/smart_reframer.py` — Auto-reframe vertical 9:16
- `src/modules/transcription_engine.py` — Whisper transcription, SRT/VTT export
- `src/modules/vlm_assistant.py` — VLM scene description
- `src/modules/mcp_server.py` — MCP tool server

### Dispatchers
- `src/dispatchers/nvenc_renderer.py` — `hevc_nvenc` p7 CQ18 60M, filter_complex_script (bypass 8191 limit), CPU libx265 fallback
- `src/dispatchers/resolve_dispatcher.py` — DaVinci Resolve Studio API, Windows scripting

### UI
- `src/ui/main_window.py` — PyQt6 dark studio window
- `src/ui/widgets/` — `media_browser`, `scene_grid`, `timeline_view`, `export_panel`

### Tests
- 21 test files covering tất cả modules

### Docs
- `AGENTS.md`, `README.md`, `docs/user_guide.md`, `docs/ADR_memory_upgrade.md`
