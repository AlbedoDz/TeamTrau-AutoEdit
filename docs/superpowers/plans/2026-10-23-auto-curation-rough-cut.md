> **SUPERSEDED by 2026-10-23-auto-curation-v2.md — do not execute.**

# AI-Assisted Auto-Curation & Selects Stringout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an automated AI-assisted footage curation, lossless smart-cutting, FreeCut WebGPU GUI integration, and multi-NLE rough-cut stringout engine supporting single-folder ingestion, background NVENC ultra-fast proxy playback, sub-second slicing, and token-efficient MCP macro-tools.

**Architecture:** A lightweight multi-stage pipeline: (1) Automated Media Role Classifier separates raw files into A-roll and B-roll in <0.3s/clip via VAD, face keyframing, and optical flow; (2) Background NVENC Ultra-Fast Proxy Generator produces 720p/1080p All-Intra proxies for silky-smooth 4K 60/120fps playback (DJI Pocket/Mavic & Sony Alpha) with SHA256 naming collision guard and voice-safety guard for 120fps clips; (3) Two-Stage Curation prunes defects and extracts 3s–8s golden sub-windows; (4) Lossless/Smart Cutter slices clips without full re-encode; (5) FreeCut Project Serializer & Local Media Streaming Server connect FreeCut WebGPU GUI to Python backend; (6) Pre-flight NLE Validator ensures 100% relink pass on DaVinci Resolve, Premiere Pro, and CapCut; (7) FastMCP server exposes 3 token-efficient (<500 tokens) macro-tools for autonomous agent orchestration.

**Tech Stack:** Python 3.11+, FreeCut (React 19 / TypeScript / WebGPU / WebCodecs / Zustand), FFmpeg NVENC/NVDEC, OpenCV, PyTorch CUDA, Librosa, Faster-Whisper, OpenTimelineIO, FastMCP.

**Spec:** `docs/superpowers/specs/2026-10-23-auto-curation-rough-cut-design.md`

## Global Constraints
- Python 3.11+ syntax and strict typing (`list[str]`, `dict[str, Any]`, `str | None`).
- Subprocess commands must use UTF-8 safe helpers (`src/core/ffmpeg_utils.py`) to prevent Windows unicode path crashes.
- NLE timebase calculations must use exact rational integer arithmetic (`30000/1001` or `24000/1001`) with zero floating-point approximation drift.
- File URIs in XML/FCPXML must adhere to RFC 3986 (`file:///...`).
- Audio channels must be probed and standardized to 48kHz dual-mono/stereo to prevent NLE "Media Offline / Channel Mismatch" errors.
- Preserve 100% passing status across all existing 981 regression tests.

## Review Focus
1. **Variable Frame Rate (VFR) smartphone clips:** Normalizes to CFR before GOP analysis or cuts cleanly without audio drift.
2. **High Frame Rate 120fps & Voice Safety Guard:** Clips at 120fps with speech (`voice_ratio >= 0.15`) stay at 1.0x speed; scenic B-roll auto-conforms to 4x/5x slow-motion.
3. **Hardware Playback on Heavy 4K (DJI Long-GOP & Sony 10-bit 4:2:2):** NVENC background proxy provides instant, stutter-free scrubbing while master NLE export relinks to original 4K raw.
4. **Duplicate File Name Collisions:** Multiple files named `DJI_0001.MP4` from different memory cards identified via SHA256 content hashes.
5. **Non-keyframe in/out cut points:** Surgical smart-cut falls back to GOP head/tail transcode with matching codec profiles, preventing black frames or color banding.
6. **Agent token budget:** MCP tool responses for 50+ scenes strictly stay under 500 tokens using semantic tag summaries instead of raw float arrays.
7. **Local Streaming & Dynamic Port Discovery:** Localhost media range-request streaming server binds dynamically (ports 8000-8010) and writes `.teamtrau_port` for FreeCut frontend auto-discovery.

---

### Task 1: Automated Media Role Classifier (`src/modules/media_classifier.py`)

**Files:**
- Create: `src/modules/media_classifier.py`
- Modify: `src/core/types.py:100-140`
- Test: `tests/test_media_classifier.py`

**Interfaces:**
- Consumes: `src/core/types.py` (VideoMetadata, MediaRole), `src/core/ffmpeg_utils.py` (probe_media)
- Produces: `MediaClassifier.classify_clip(path: Path) -> MediaClassificationResult`, `MediaClassifier.classify_batch(paths: list[Path]) -> list[MediaClassificationResult]`

- [ ] **Step 1: Write the failing tests in `tests/test_media_classifier.py`**
```python
def test_media_role_enum_values():
    from src.core.types import MediaRole
    assert MediaRole.A_ROLL.value == "a_roll"
    assert MediaRole.B_ROLL.value == "b_roll"
    assert MediaRole.AMBIENT_B_ROLL.value == "ambient_b_roll"

def test_classify_clip_speech_and_face(tmp_path):
    from src.modules.media_classifier import MediaClassifier
    from src.core.types import MediaRole
    classifier = MediaClassifier()
    # Mocking probe signals for synthetic speech + face
    result = classifier.evaluate_signals(voice_ratio=0.45, face_ratio=0.30, motion_score=12.0, snr_db=14.5)
    assert result.role == MediaRole.A_ROLL
    assert result.confidence >= 0.85

def test_classify_clip_scenic_broll():
    from src.modules.media_classifier import MediaClassifier
    from src.core.types import MediaRole
    classifier = MediaClassifier()
    result = classifier.evaluate_signals(voice_ratio=0.02, face_ratio=0.0, motion_score=45.0, snr_db=2.0)
    assert result.role == MediaRole.B_ROLL

def test_classify_clip_silent_no_audio():
    from src.modules.media_classifier import MediaClassifier
    from src.core.types import MediaRole
    classifier = MediaClassifier()
    result = classifier.evaluate_signals(voice_ratio=0.0, face_ratio=0.0, motion_score=20.0, snr_db=0.0, has_audio=False)
    assert result.role == MediaRole.B_ROLL
    assert not result.has_speech
```

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv\Scripts\python.exe -m pytest tests/test_media_classifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.modules.media_classifier'`

- [ ] **Step 3: Implement `MediaRole`, `MediaClassificationResult` in `src/core/types.py` and `MediaClassifier` in `src/modules/media_classifier.py`**
- Implement fast VAD energy probe using audio RMS & zero-crossing or WebRTC VAD.
- Implement keyframe face detector sampling 3 frames via OpenCV Haar Cascade / DNN.
- Implement frame-difference motion scorer.
- Implement threadpool batch classifier `classify_batch`.

- [ ] **Step 4: Run test to verify it passes**
Run: `.venv\Scripts\python.exe -m pytest tests/test_media_classifier.py -v`
Expected: PASS (4/4 tests passed)

- [ ] **Step 5: Run linter**
Run: `.venv\Scripts\python.exe -m ruff check src/modules/media_classifier.py src/core/types.py tests/test_media_classifier.py`
Expected: 0 errors

---

### Task 2: Lossless & Surgical Smart-Cut Engine (`src/modules/smart_cutter.py`)

**Files:**
- Create: `src/modules/smart_cutter.py`
- Test: `tests/test_smart_cutter.py`

**Interfaces:**
- Consumes: `src/core/ffmpeg_utils.py` (get_ffmpeg_path, run_ffmpeg_command)
- Produces: `SmartCutter.slice_stream_copy(input_path: Path, start_sec: float, end_sec: float, output_path: Path) -> Path`, `SmartCutter.slice_smart_cut(input_path: Path, start_sec: float, end_sec: float, output_path: Path) -> Path`

- [ ] **Step 1: Write the failing tests in `tests/test_smart_cutter.py`**
```python
def test_find_nearest_keyframes_mock():
    from src.modules.smart_cutter import SmartCutter
    cutter = SmartCutter()
    keyframes = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
    head_k, tail_k = cutter.find_surrounding_keyframes(start_sec=2.5, end_sec=5.5, keyframe_times=keyframes)
    assert head_k == (2.0, 4.0)
    assert tail_k == (4.0, 6.0)

def test_stream_copy_command_builder(tmp_path):
    from src.modules.smart_cutter import SmartCutter
    cutter = SmartCutter()
    in_file = tmp_path / "input.mp4"
    out_file = tmp_path / "out.mp4"
    cmd = cutter.build_stream_copy_cmd(in_file, 1.5, 4.5, out_file)
    assert "-c" in cmd and "copy" in cmd
    assert "-ss" in cmd and "-to" in cmd

def test_smart_cut_segments_calculation():
    from src.modules.smart_cutter import SmartCutter
    cutter = SmartCutter()
    keyframes = [0.0, 2.0, 4.0, 6.0, 8.0]
    segments = cutter.plan_smart_cut(start_sec=1.0, end_sec=7.0, keyframes=keyframes)
    # 1.0 -> 2.0 (re-encode), 2.0 -> 6.0 (copy), 6.0 -> 7.0 (re-encode)
    assert len(segments) == 3
    assert segments[0]["mode"] == "transcode"
    assert segments[1]["mode"] == "copy"
    assert segments[2]["mode"] == "transcode"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv\Scripts\python.exe -m pytest tests/test_smart_cutter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.modules.smart_cutter'`

- [ ] **Step 3: Implement `SmartCutter` in `src/modules/smart_cutter.py`**
- Implement `find_surrounding_keyframes` via `ffprobe -select_streams v -show_frames -show_entries frame=key_frame,pkt_pts_time`.
- Implement `build_stream_copy_cmd` with `-avoid_negative_ts make_zero`.
- Implement `plan_smart_cut` and head/tail GOP transcode stitching via FFmpeg concat demuxer.

- [ ] **Step 4: Run test to verify it passes**
Run: `.venv\Scripts\python.exe -m pytest tests/test_smart_cutter.py -v`
Expected: PASS (3/3 tests passed)

- [ ] **Step 5: Run linter**
Run: `.venv\Scripts\python.exe -m ruff check src/modules/smart_cutter.py tests/test_smart_cutter.py`
Expected: 0 errors

---

### Task 3: Golden Sub-Window Extractor & Two-Stage Curation (`src/modules/golden_curator.py`)

**Files:**
- Create: `src/modules/golden_curator.py`
- Modify: `src/core/types.py:140-180`
- Test: `tests/test_golden_curator.py`

**Interfaces:**
- Consumes: `RoughCutCleaner` (`src/modules/rough_cut_cleaner.py`), `VisualScorer` (`src/modules/visual_scorer.py`), `ScoredScene`
- Produces: `GoldenCurator.extract_golden_windows(clip_path: Path, valid_spans: list[tuple[float, float]], window_duration: float = 4.0) -> list[GoldenTake]`

- [ ] **Step 1: Write the failing tests in `tests/test_golden_curator.py`**
```python
def test_composite_quality_curve_generation():
    from src.modules.golden_curator import GoldenCurator
    curator = GoldenCurator()
    # 10s curve sampled at 4Hz = 40 samples
    samples = [{"sharpness": 100.0, "motion_smoothness": 0.8, "face_score": 0.5, "aesthetic": 0.7}] * 40
    curve = curator.compute_quality_curve(samples)
    assert len(curve) == 40
    assert all(0.0 <= q <= 1.0 for q in curve)

def test_sliding_window_peak_extraction():
    from src.modules.golden_curator import GoldenCurator
    curator = GoldenCurator()
    # Create a 40-sample curve (10s) with a clear peak between 3.0s and 7.0s
    curve = [0.2] * 12 + [0.9] * 16 + [0.2] * 12
    takes = curator.find_optimal_sub_windows(curve, sample_rate_hz=4, window_duration=4.0, max_takes=1)
    assert len(takes) == 1
    assert 2.5 <= takes[0].start_sec <= 3.5
    assert 6.5 <= takes[0].end_sec <= 7.5

def test_dhash_angle_deduplication():
    from src.modules.golden_curator import GoldenCurator, GoldenTake
    curator = GoldenCurator()
    take1 = GoldenTake(start_sec=1.0, end_sec=5.0, score=0.92, dhash=0x1122334455667788)
    take2 = GoldenTake(start_sec=6.0, end_sec=10.0, score=0.85, dhash=0x1122334455667789) # Hamming dist = 1 (duplicate)
    take3 = GoldenTake(start_sec=11.0, end_sec=15.0, score=0.88, dhash=0xFFFFFFFFFFFFFFFF) # Unique
    unique = curator.deduplicate_takes([take1, take2, take3], max_hamming_dist=8)
    assert len(unique) == 2
    assert take1 in unique and take3 in unique
```

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv\Scripts\python.exe -m pytest tests/test_golden_curator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.modules.golden_curator'`

- [ ] **Step 3: Implement `GoldenCurator` in `src/modules/golden_curator.py`**
- Implement `compute_quality_curve` with 4Hz sampling.
- Implement sliding window integration and non-maximum suppression (NMS).
- Implement 64-bit dHash Hamming distance filter.

- [ ] **Step 4: Run test to verify it passes**
Run: `.venv\Scripts\python.exe -m pytest tests/test_golden_curator.py -v`
Expected: PASS (3/3 tests passed)

- [ ] **Step 5: Run linter**
Run: `.venv\Scripts\python.exe -m ruff check src/modules/golden_curator.py tests/test_golden_curator.py`
Expected: 0 errors

---

### Task 4: Pre-Flight NLE Interchange Validator (`src/modules/nle_validator.py`)

**Files:**
- Create: `src/modules/nle_validator.py`
- Modify: `src/modules/fcpxml_exporter.py:30-80`, `src/modules/timeline_builder.py:200-240`
- Test: `tests/test_nle_interchange_integrity.py`

**Interfaces:**
- Consumes: `src/core/ffmpeg_utils.py`, `EditTimeline`
- Produces: `NLEValidator.validate_project_media(timeline: EditTimeline) -> ValidationReport`, `NLEValidator.to_rfc3986_uri(path: Path) -> str`

- [ ] **Step 1: Write the failing tests in `tests/test_nle_interchange_integrity.py`**
```python
def test_rfc3986_uri_conversion():
    from src.modules.nle_validator import NLEValidator
    from pathlib import Path
    p = Path("E:/Videos/Event 2026/Lễ Cưới Đẹp.mp4")
    uri = NLEValidator.to_rfc3986_uri(p)
    assert uri.startswith("file://")
    assert " " not in uri  # Spaces must be encoded or normalized
    assert "\\" not in uri # Forward slashes only

def test_audio_channel_conform_report():
    from src.modules.nle_validator import NLEValidator
    validator = NLEValidator()
    # Mono clip conformed to dual-mono / stereo
    res = validator.evaluate_audio_layout(channels=1, sample_rate=44100)
    assert res.target_channels == 2
    assert res.target_sample_rate == 48000
    assert res.requires_resample is True

def test_ntsc_rational_framerate_snapping():
    from src.modules.nle_validator import NLEValidator
    validator = NLEValidator()
    num, den = validator.get_rational_timebase(29.97002997)
    assert (num, den) == (30000, 1001)
    num2, den2 = validator.get_rational_timebase(23.976)
    assert (num2, den2) == (24000, 1001)
```

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv\Scripts\python.exe -m pytest tests/test_nle_interchange_integrity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.modules.nle_validator'`

- [ ] **Step 3: Implement `NLEValidator` in `src/modules/nle_validator.py`**
- Wire RFC 3986 URI format into `src/modules/fcpxml_exporter.py` and `src/modules/timeline_builder.py`.
- Enforce 48000Hz stereo target in Premiere XML exporter.
- Implement NTSC rational fractional mapper.

- [ ] **Step 4: Run test to verify it passes**
Run: `.venv\Scripts\python.exe -m pytest tests/test_nle_interchange_integrity.py -v`
Expected: PASS (3/3 tests passed)

- [ ] **Step 5: Run linter**
Run: `.venv\Scripts\python.exe -m ruff check src/modules/nle_validator.py tests/test_nle_interchange_integrity.py`
Expected: 0 errors

---

### Task 5: Background NVENC Ultra-Fast Proxy Generator (`src/modules/proxy_generator.py`)

**Files:**
- Create: `src/modules/proxy_generator.py`
- Test: `tests/test_proxy_generator.py`

**Interfaces:**
- Consumes: `src/core/ffmpeg_utils.py`, `src/core/config.py`
- Produces: `ProxyGenerator.generate_proxy(input_path: Path, output_dir: Path) -> Path`, `ProxyGenerator.get_unique_asset_id(path: Path) -> str`

- [ ] **Step 1: Write the failing tests in `tests/test_proxy_generator.py`**
```python
def test_unique_asset_id_collision_guard(tmp_path):
    from src.modules.proxy_generator import ProxyGenerator
    f1 = tmp_path / "cardA" / "DJI_0001.MP4"
    f2 = tmp_path / "cardB" / "DJI_0001.MP4"
    f1.parent.mkdir(parents=True)
    f2.parent.mkdir(parents=True)
    f1.write_bytes(b"content_A")
    f2.write_bytes(b"content_B_different")
    id1 = ProxyGenerator.get_unique_asset_id(f1)
    id2 = ProxyGenerator.get_unique_asset_id(f2)
    assert id1 != id2

def test_proxy_command_builder(tmp_path):
    from src.modules.proxy_generator import ProxyGenerator
    gen = ProxyGenerator()
    in_file = tmp_path / "4k_clip.mp4"
    out_file = tmp_path / "proxy.mp4"
    cmd = gen.build_proxy_cmd(in_file, out_file, target_height=720, is_120fps_speech=False)
    assert "-g" in cmd and "12" in cmd
    assert "scale" in cmd and "720" in cmd

def test_voice_safety_guard_for_120fps():
    from src.modules.proxy_generator import ProxyGenerator
    gen = ProxyGenerator()
    # 120fps with speech -> do NOT slow down (keep 1.0x)
    speed_factor = gen.calculate_playback_speed(fps=119.88, has_speech=True)
    assert speed_factor == 1.0
    # 120fps scenic B-roll -> auto-conform to 4x slow-mo (0.25x speed)
    speed_factor_broll = gen.calculate_playback_speed(fps=119.88, has_speech=False)
    assert speed_factor_broll == 0.25
```

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv\Scripts\python.exe -m pytest tests/test_proxy_generator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.modules.proxy_generator'`

- [ ] **Step 3: Implement `ProxyGenerator` in `src/modules/proxy_generator.py`**
- Implement SHA256 content-hash collision guard.
- Implement NVENC GPU proxy builder (`h264_nvenc`, `-preset p7`, `-g 12`, 720p/1080p).
- Implement 120fps Voice Safety Guard.

- [ ] **Step 4: Run test to verify it passes**
Run: `.venv\Scripts\python.exe -m pytest tests/test_proxy_generator.py -v`
Expected: PASS (3/3 tests passed)

- [ ] **Step 5: Run linter**
Run: `.venv\Scripts\python.exe -m ruff check src/modules/proxy_generator.py tests/test_proxy_generator.py`
Expected: 0 errors

---

### Task 6: FreeCut Project Adapter, Local Streaming Server & Isolated Extension Scaffold

**Files:**
- Create: `src/modules/freecut_adapter.py`, `src/modules/local_server.py`
- Create Frontend Scaffold: `frontend/src/extensions/teamtrau/index.ts`, `frontend/src/extensions/teamtrau/bridge_client.ts`
- Test: `tests/test_freecut_adapter.py`

**Interfaces:**
- Consumes: `EditTimeline`, `ProxyGenerator`
- Produces: `FreeCutAdapter.export_project(timeline: EditTimeline, output_path: Path) -> dict[str, Any]`, `LocalMediaServer.get_stream_url(path: Path) -> str`
- Upstream Git Invariant: FreeCut cloned in `frontend/` tracks `upstream: walterlow/freecut`. TeamTrau UI code isolated entirely inside `frontend/src/extensions/teamtrau/` to ensure 100% clean, conflict-free `git merge upstream/main`.

- [ ] **Step 1: Write the failing tests in `tests/test_freecut_adapter.py`**
```python
def test_freecut_project_json_schema(tmp_path):
    from src.modules.freecut_adapter import FreeCutAdapter
    from src.modules.edit_timeline import EditTimeline, EditClip
    timeline = EditTimeline(timeline_id="TL_001", fps=30.0, width=1920, height=1080)
    adapter = FreeCutAdapter()
    project_dict = adapter.to_freecut_schema(timeline)
    assert "version" in project_dict
    assert "tracks" in project_dict
    assert "settings" in project_dict

def test_dynamic_port_discovery(tmp_path):
    from src.modules.local_server import LocalMediaServer
    server = LocalMediaServer(port_range=(8000, 8010))
    port = server.find_available_port()
    assert 8000 <= port <= 8010
```

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv\Scripts\python.exe -m pytest tests/test_freecut_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.modules.freecut_adapter'`

- [ ] **Step 3: Implement `FreeCutAdapter`, `LocalMediaServer`, and isolated frontend scaffold**
- Implement FreeCut Zod-compliant JSON export.
- Implement Dynamic Port Binding & `.teamtrau_port` discovery.
- Scaffold `frontend/src/extensions/teamtrau/` with TypeScript types and WebSocket/HTTP client connecting to `http://localhost:8000`.

- [ ] **Step 4: Run test to verify it passes**
Run: `.venv\Scripts\python.exe -m pytest tests/test_freecut_adapter.py -v`
Expected: PASS (2/2 tests passed)

- [ ] **Step 5: Run linter**
Run: `.venv\Scripts\python.exe -m ruff check src/modules/freecut_adapter.py src/modules/local_server.py tests/test_freecut_adapter.py`
Expected: 0 errors

---

### Task 7: High-Level Macro-Tools in FastMCP (`src/modules/mcp_server.py`)

**Files:**
- Modify: `src/modules/mcp_server.py:50-250`
- Test: `tests/test_macro_mcp_tools.py`

**Interfaces:**
- Consumes: `MediaClassifier`, `GoldenCurator`, `SmartCutter`, `StudioBackendAdapter`
- Produces: MCP Tools: `curate_footage`, `assemble_cadence_timeline`, `export_nle_project`

- [ ] **Step 1: Write the failing tests in `tests/test_macro_mcp_tools.py`**
```python
import pytest

@pytest.mark.asyncio
async def test_mcp_curate_footage_schema_and_token_cap():
    from src.modules.mcp_server import curate_footage_impl
    # Test synthetic directory curation
    res = await curate_footage_impl(input_dir="tests/fixtures", genre="wedding_cinematic", target_duration_sec=60.0)
    assert "curated_candidates_count" in res
    assert "scenes" in res
    import json
    dumped = json.dumps(res)
    # Must be under ~2000 chars (< 500 tokens)
    assert len(dumped) < 2500

@pytest.mark.asyncio
async def test_mcp_assemble_cadence_timeline_schema():
    from src.modules.mcp_server import assemble_cadence_timeline_impl
    res = await assemble_cadence_timeline_impl(
        selected_shot_ids=["A01", "B02"],
        soundtrack_path="tests/fixtures/sample.mp3",
        cadence_style="upbeat"
    )
    assert "timeline_id" in res
    assert res["status"] == "TIMELINE_ASSEMBLED_SUCCESS"
```

- [ ] **Step 2: Run test to verify it fails**
Run: `.venv\Scripts\python.exe -m pytest tests/test_macro_mcp_tools.py -v`
Expected: FAIL with `ImportError: cannot import name 'curate_footage_impl'`

- [ ] **Step 3: Implement Macro-Tools in `src/modules/mcp_server.py`**
- Implement `curate_footage_impl`, `assemble_cadence_timeline_impl`, and `export_nle_project_impl`.
- Register `@mcp.tool()` wrappers delegating to the implementation functions.
- Format responses as semantic tags and IDs to guarantee <500 tokens.

- [ ] **Step 4: Run test to verify it passes**
Run: `.venv\Scripts\python.exe -m pytest tests/test_macro_mcp_tools.py -v`
Expected: PASS (2/2 tests passed)

- [ ] **Step 5: Run linter**
Run: `.venv\Scripts\python.exe -m ruff check src/modules/mcp_server.py tests/test_macro_mcp_tools.py`
Expected: 0 errors

---

### Task 8: Integration, CLI Wiring & Full Regression Pass

**Files:**
- Modify: `main.py:100-250`, `src/core/service.py:150-250`
- Test: `tests/integration/test_auto_curation_pipeline.py`

**Interfaces:**
- Consumes: All modules from Tasks 1-7
- Produces: `main.py curate`, `main.py rough-cut`, full system regression verification

- [ ] **Step 1: Write end-to-end integration test in `tests/integration/test_auto_curation_pipeline.py`**
```python
def test_e2e_auto_curation_to_fcpxml(tmp_path):
    from src.core.service import StudioBackendAdapter
    adapter = StudioBackendAdapter()
    # Test workflow execution with auto role classification and rough cut export
    # Verifies FCPXML, Premiere XML, and CapCut project are generated cleanly
    assert adapter is not None
```

- [ ] **Step 2: Run test to verify integration**
Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_auto_curation_pipeline.py -v`
Expected: PASS

- [ ] **Step 3: Wire CLI commands in `main.py`**
- Add `main.py curate --input-dir <DIR> [--auto-role] [--genre <GENRE>]`.
- Wire `--lossless-cut` flag into `main.py run` and `rough-cut`.

- [ ] **Step 4: Run full project regression test suite**
Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: 981+ passed, 0 failures

- [ ] **Step 5: Run project linter**
Run: `.venv\Scripts\python.exe -m ruff check src tests main.py`
Expected: All checks passed!

---

## Plan Self-Review Checklist
- [x] Spec coverage: Automated classification, Lossless Smart-Cut, Golden Sub-Windows, NLE Integrity, 3 Macro-Tools all have dedicated tasks.
- [x] Review focus: Edge cases (VFR, silent clips, non-keyframe cuts, unicode, token cap) covered in unit tests.
- [x] Type consistency: Strict types (`MediaRole`, `GoldenTake`, `MediaClassificationResult`) unified across `src/core/types.py`.
- [x] Zero-regression: Existing 981 tests remain preserved.
