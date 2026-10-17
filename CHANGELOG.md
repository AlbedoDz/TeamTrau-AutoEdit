# CHANGELOG

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
