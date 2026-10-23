# Auto-Curation & Selects Stringout — Implementation Plan v2

> **Supersedes:** `docs/superpowers/plans/2026-10-23-auto-curation-rough-cut.md` (v1). Do not execute v1.
> **Spec:** `docs/superpowers/specs/2026-10-23-auto-curation-rough-cut-design.md`, amended by the decisions in §1 below (the decisions win where they disagree with the spec).
> **Author / reviewer:** Claude Opus (orchestrator). **Implementer:** Antigravity agent, one task per session, using `docs/superpowers/plans/2026-10-23-auto-curation-v2-antigravity-prompts.md`.
> **Baseline:** `981 passed` (full suite, 5m29s, 2026-10-23).

---

## 0. Why v2 exists (short)

v1 was written without reading the codebase. Most of what it planned to "create" already exists and is tested:

| Spec concept | Already implemented in |
|---|---|
| Stage 1 defect excision (head/tail, shake, whip pan, focus, ground, exposure, occlusion) | `src/modules/rough_cut_cleaner.py` (`RoughCutCleaner`, `DefectType` in `src/core/types.py`) |
| 4 Hz quality curve + sliding window | `src/modules/shot_windows.py` (`QualityCurve`, `compute_quality_curve`, `score_window`, `extract_optimal_subwindow`) |
| dHash + Hamming dedupe | `src/modules/visual_scorer.py` (`compute_dhash`, `hamming_distance`, `VisualScorer.deduplicate_scenes`) |
| NTSC rational timebase | `src/core/timebase.py` (`frame_rate_fraction`) |
| Media probe (VFR, channels, rate, timecode) | `src/core/media_info.py` (`probe_media`, `MediaInfo`) |
| VFR → CFR cache | `IngestDetector.normalize_to_cfr` |
| CapCut draft, FCPXML 1.8, Premiere XML | `capcut_exporter.py`, `fcpxml_exporter.py`, `timeline_builder.py` / `premiere_xml_fix.py` |
| BGM ducking −14 dB | `PipelineConfig.ducking_attenuation_db` |
| 540p preview MP4 | `StudioBackendAdapter.submit_deliver_job(preview=True)` |
| Staged rough-cut pipeline | `submit_job(stage="rough_cut")` (commit `a219edd`) |

v2 builds **only the missing pieces**, on top of those modules, as a vertical slice that a user can run end to end.

---

## 1. Decisions (binding for v2)

| # | Topic | Decision | Reason |
|---|---|---|---|
| D1 | File URIs | Keep `Path.as_uri()` percent-encoding (RFC 3986). "Unicode intact" means **lossless round trip** path → URI → path, enforced by preflight + tests. | Already verified on DaVinci Resolve (commit `268cb95`). Do not touch exporters' URI code. |
| D2 | Audio rate | Default 48 kHz; keep `DeliverSettings.audio_rate` choices (44.1/48/96 kHz). Preflight **warns** on mismatch, never rewrites. | Spec "force 48 kHz" contradicts the shipped Deliver settings. |
| D3 | Smart-cut / lossless slicing | **Not in v2.** Rough cuts stay non-destructive (NLE files reference originals with in/out). Stream-copy "export selects" and the GOP smart-cut are Milestone B (B2/B3, spike first). | Slicing new files breaks relink-to-original (`EditClip.original_path`), and NVENC cannot re-encode Sony 10-bit 4:2:2 heads/tails. |
| D4 | FreeCut + local streaming server | **Out of scope** (Milestone C). | Spec §8.2 marks it "Future"; there is no FreeCut schema in the repo to test against. |
| D5 | NVENC proxies + 120 fps voice-safety guard | Milestone B (B1, B4), after the realtime-playback work is merged. | Proxies serve GUI playback, which is a different work stream. |
| D6 | A-roll without a visible face | Speech-dominant clips are **A-roll even without a face** ("offscreen speech", e.g. vlog narration behind the camera). A face only raises confidence. | Cutting speech by visual windows chops sentences. Deliberate deviation from spec §3.2. |
| D7 | MCP tool style | Plain **sync** functions decorated with `@mcp_server.tool()`, same as the existing 30 tools. No `pytest-asyncio`. | Matches `src/modules/mcp_server.py`; avoids a new dependency. |
| D8 | Token budget | Curation summary JSON ≤ **2000 characters** (~500 tokens) for any number of takes: aggregates + top-N rows `ID|role|dur|q`, never per-frame arrays. | Spec's per-scene list grows linearly (50 scenes ≈ 1800 tokens). |
| D9 | Architecture | All new behaviour goes through `StudioBackendAdapter` / `src/core/curation.py`. MCP and CLI are thin wrappers over the same functions. | One code path for GUI, CLI and MCP. |
| D10 | Golden windows | Applied to **non-A-roll** takes only; A-roll keeps the whole cleaned span (sentence snapping = Milestone B5). | Speech continuity. |

---

## 2. Global rules (every task)

- Python 3.11, explicit types (`list[str]`, `X | None`), ruff clean (`E,F,I,W`, line length 100).
- Match the surrounding code: comment density, naming, dataclasses, `logging.getLogger(__name__)`.
- Reuse the modules in §0. Never re-implement them.
- Subprocess: ffmpeg/ffprobe via `src/core/ffmpeg_utils.py` (`run_tool`, `ffprobe_json`) or the new `run_tool_bytes` (Task 2). Binaries via `shutil.which` (as elsewhere).
- **Contract tests** in this plan are copied **verbatim** and are **LOCKED**: never edited to pass. Extra tests may be added. The only allowed change to a locked file is `ruff check --fix --select I <file>` (import order), once the imported modules exist.
- Red must fail **for the stated reason** (missing name / wrong behaviour), not for a typo or a fixture error.
- Characterisation tests marked *(may start green)* lock existing behaviour; that is allowed.
- After each task: task tests → listed related suites → full suite → ruff → one commit + `CHANGELOG.md` entry under `## [Unreleased]`.
- Commands (Windows): `.venv\Scripts\python.exe -m pytest ...`, `.venv\Scripts\python.exe -m ruff check ...`. Never `pip install`.

Task order and dependencies:

```
T0 hygiene ─► T1 role folders ─► T2 content classifier ─┐
                                 T3 golden windows ──────┼─► T4 rough-cut wiring ─┐
                                 T5 NLE preflight ───────┴────────────────────────┼─► T6 curation core + MCP ─► T7 CLI + E2E + release
```

---

## Task 0 — Hygiene & baseline (no production code)

**Files:** `task.md`, `docs/superpowers/plans/2026-10-23-auto-curation-rough-cut.md` (header only), git.

1. On `main`, commit the existing realtime-playback work as its own commit (user-approved):
   `src/ui/widgets/player_widget.py`, `src/ui/widgets/settings_dialog.py`, `src/ui/widgets/timeline_view.py`, `tests/test_realtime_playback.py`, `task.md`.
   Message: `feat(ui): realtime playback improvements (pre-curation snapshot)`.
   Do **not** commit `.claude/`, `docs/plan/`, `docs/superpowers/` in that commit.
2. Commit the docs: `docs/superpowers/` and `docs/plan/` → `docs: auto-curation spec, plan v1 (superseded) and plan v2`.
3. Create the branch `feature/auto-curation-v2` from `main` and work only there from now on.
4. Prepend to v1 plan: `> **SUPERSEDED by 2026-10-23-auto-curation-v2.md — do not execute.**`
5. In `task.md`, replace the Phase 24 block with a v2 checklist (T0..T7 as `[ ]`, T0 `[x]` when done).
6. Run the full suite and record the count in the handoff (expected `981 passed`).
7. Commit: `chore: start auto-curation v2 (branch, superseded v1 plan, task list)`.

**Done when:** branch exists, working tree clean, baseline recorded.

---

## Task 1 — Role folders (Mode B ingestion)

**Problem (real bugs):** `infer_camera_role("…/B-roll/x.mp4")` returns `CAM_A` (only `broll`/`b_roll` are matched, not `B-roll`). `collect_video_files` scans one level, so `input/A-roll/*.mp4` is never ingested. There is no `Music/` soundtrack discovery.

**Files:**
- Modify: `src/core/ontology.py` (add `role_folder_of`; use it first in `infer_camera_role`)
- Modify: `src/core/service.py` (`collect_video_files` descends into role folders; add `AUDIO_EXTENSIONS`, `find_soundtrack`)
- Test (LOCKED): `tests/test_role_folders.py`
- Related suites: `tests/test_multifolder_ingest.py tests/test_service_adapter.py tests/test_media_browser.py`

**Behaviour:**
- `role_folder_of(path: str | Path) -> Literal["a_roll", "b_roll"] | None`: looks at **directory components only** (never the file name), any ancestor. A component matches when `name.lower()` with `-`, `_` and spaces removed equals `"aroll"` or `"broll"`. Exact match only (no substrings).
- `infer_camera_role`: if `role_folder_of` gives `"a_roll"` → `CAM_A`; `"b_roll"` → `B_ROLL`. Otherwise the existing keyword rules run unchanged.
- `collect_video_files(dir)`: unchanged top-level scan, **plus** one level inside subfolders whose name is a role folder (`role_folder_of(sub / "x")` is not `None`). Other subfolders (including `.cache`, `Music`) are not scanned. Return order: top-level files first, then role folders sorted by name, files in OS order (tests compare sorted lists).
- `AUDIO_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"})` in `service.py`.
- `find_soundtrack(input_dir: Path) -> Path | None`: the **single** audio file inside a direct subfolder named `music` (case-insensitive). Zero or several files → `None` (the user must choose).

**Contract test — `tests/test_role_folders.py`:**
```python
"""Mode B ingestion: A-roll / B-roll / Music folders."""

from pathlib import Path

from src.core.ontology import CameraRole, infer_camera_role, role_folder_of
from src.core.service import collect_video_files, find_soundtrack


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_role_folder_of_matches_directory_components_only():
    assert role_folder_of("D:/W/A-roll/x.mp4") == "a_roll"
    assert role_folder_of(Path("D:/W/b_roll/sub/x.mp4")) == "b_roll"
    assert role_folder_of("D:/W/B Roll/x.mp4") == "b_roll"
    assert role_folder_of("D:/W/BROLL/x.mp4") == "b_roll"
    assert role_folder_of("D:/W/Barollo/x.mp4") is None
    assert role_folder_of("D:/W/broll.mp4") is None
    assert role_folder_of("D:/W/x.mp4") is None


def test_hyphenated_role_folders_drive_camera_role():
    assert infer_camera_role("D:/Wedding/B-roll/detail_rings.mp4") == CameraRole.B_ROLL
    assert infer_camera_role("D:/Wedding/A-roll/vows_bride.mov") == CameraRole.CAM_A
    # An explicit folder beats file-name keywords.
    assert infer_camera_role("D:/Shoot/A-roll/DJI_0001.MP4") == CameraRole.CAM_A
    assert infer_camera_role("D:/Shoot/B-roll/GOPRO_0001.MP4") == CameraRole.B_ROLL


def test_existing_keyword_rules_still_apply():
    assert infer_camera_role("C:/Footage/DJI_Drone_Aerials/clip1.mp4") == CameraRole.DRONE
    assert infer_camera_role("D:/Shoot/B_Roll_Inserts/coffee.mp4") == CameraRole.B_ROLL
    assert infer_camera_role("D:/Footage/Main_Host/interview.mp4") == CameraRole.CAM_A


def test_collect_video_files_descends_into_role_folders_only(tmp_path: Path):
    a = _touch(tmp_path / "A-roll" / "interview_01.mp4")
    b = _touch(tmp_path / "B-roll" / "walking.mov")
    top = _touch(tmp_path / "loose.mp4")
    _touch(tmp_path / "Music" / "theme.mp3")
    _touch(tmp_path / ".cache" / "proxy.mp4")
    _touch(tmp_path / "random_sub" / "ignored.mp4")
    found, skipped = collect_video_files(tmp_path)
    assert sorted(found) == sorted([a, b, top])
    assert skipped == []


def test_find_soundtrack_single_file_in_music_folder(tmp_path: Path):
    track = _touch(tmp_path / "Music" / "theme.mp3")
    _touch(tmp_path / "Music" / "notes.txt")
    assert find_soundtrack(tmp_path) == track


def test_find_soundtrack_none_or_ambiguous(tmp_path: Path):
    assert find_soundtrack(tmp_path) is None
    _touch(tmp_path / "music" / "a.mp3")
    _touch(tmp_path / "music" / "b.wav")
    assert find_soundtrack(tmp_path) is None
```

**Expected red:** `ImportError: cannot import name 'role_folder_of'`.
**Commit:** `feat(ingest): A-roll/B-roll/Music role folders (Mode B)`.

---

## Task 2 — Content role classifier (Mode A, single folder)

**Files:**
- Modify: `src/core/ontology.py` (add `ContentRole`)
- Modify: `src/core/ffmpeg_utils.py` (add `run_tool_bytes`)
- Create: `src/modules/media_classifier.py`
- Test (LOCKED): `tests/test_media_classifier.py`, `tests/integration/test_media_classifier_real.py`
- Related suites: `tests/test_multifolder_ingest.py tests/test_robustness.py`

**API (exact names):**
```python
# src/core/ontology.py
class ContentRole(str, Enum):
    A_ROLL = "a_roll"                  # speech is the anchor
    B_ROLL = "b_roll"                  # visual cutaway
    AMBIENT_B_ROLL = "ambient_b_roll"  # some / unclear speech, usable as cutaway or soundbite

# src/core/ffmpeg_utils.py
def run_tool_bytes(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[bytes]: ...

# src/modules/media_classifier.py
@dataclass(frozen=True)
class ClipSignals:
    has_audio: bool
    voice_ratio: float   # 0..1 share of active (speech-like) audio frames
    snr_db: float        # median active level minus noise floor
    face_ratio: float    # largest face area / frame area over the sampled keyframes
    motion: float        # mean abs grey difference between sampled frames, 0..255

@dataclass(frozen=True)
class RoleDecision:
    role: ContentRole
    confidence: float     # 0..1
    has_speech: bool
    reasons: list[str]    # never empty, e.g. ["voice_ratio=0.45>=0.30", "snr_db=14.5>=10", "offscreen_speech"]

@dataclass(frozen=True)
class ClassificationResult:
    path: Path
    role: ContentRole
    confidence: float
    has_speech: bool
    reasons: list[str]
    signals: ClipSignals | None = None
    error: str | None = None

def decide_role(signals: ClipSignals) -> RoleDecision: ...
def voice_activity(samples: np.ndarray, sample_rate: int) -> tuple[float, float]: ...  # (voice_ratio, snr_db)
def face_area_ratio(frames: list[np.ndarray]) -> float: ...
def motion_score(frames: list[np.ndarray]) -> float: ...
def extract_signals(path: Path, max_audio_sec: float = 120.0) -> ClipSignals: ...
def classify_clip(path: Path, extractor: Callable[[Path], ClipSignals] = extract_signals) -> ClassificationResult: ...
def classify_batch(paths: list[Path], max_workers: int = 4,
                   extractor: Callable[[Path], ClipSignals] = extract_signals) -> list[ClassificationResult]: ...
```

**Algorithms (do not invent others):**
- `decide_role`, evaluated in this order (total function):
  1. `not has_audio` → `B_ROLL`, confidence `0.95`, `has_speech=False`.
  2. `voice_ratio < 0.15` → `B_ROLL`, confidence `0.70 + 0.25 * (1 - voice_ratio / 0.15)`, `has_speech=False`.
  3. `voice_ratio >= 0.30 and snr_db >= 10.0` → `A_ROLL`, confidence `0.70 + 0.15 * min(1, (voice_ratio - 0.30) / 0.40) + (0.15 if face_ratio >= 0.08 else 0.0)`, `has_speech=True`; add reason `"offscreen_speech"` when `face_ratio < 0.08` (D6).
  4. Otherwise → `AMBIENT_B_ROLL`, confidence `0.60`, `has_speech=True`.
  Confidence is clamped to `[0, 1]`.
- `voice_activity`: 30 ms non-overlapping frames (drop the remainder); `db = 10*log10(mean(x**2) + 1e-12)`; `floor = 10th percentile of db`; `active = (db > floor + 10) & (db > -50)`; `voice_ratio = mean(active)`; `snr_db = median(db[active]) - floor` if any active else `0.0`. Empty input → `(0.0, 0.0)`.
- `face_area_ratio`: OpenCV Haar `cv2.data.haarcascades + "haarcascade_frontalface_default.xml"`, loaded once lazily (module-level); grey frames, `detectMultiScale(scaleFactor=1.1, minNeighbors=5)`; max over frames of `w*h / (frame_w*frame_h)`; empty list → `0.0`.
- `motion_score`: grey, resize to width 160 (keep aspect), mean abs difference between consecutive frames, averaged; fewer than 2 frames → `0.0`.
- `extract_signals`: audio = `ffmpeg -v error -i <path> -vn -ac 1 -ar 16000 -t <max_audio_sec> -f f32le -` via `run_tool_bytes` → `np.frombuffer(..., np.float32)`; `has_audio` from `src.core.ffmpeg_utils.has_audio_stream`. Frames: 3 frames at 20 %, 50 %, 80 % of the duration via `cv2.VideoCapture` (open it the same way `shot_windows.compute_quality_curve` does).
- `classify_clip`: if `role_folder_of(path)` is not `None` → result from the folder, confidence `1.0`, reasons `["folder:a_roll"]` / `["folder:b_roll"]`, **extractor not called**, `has_speech = role is A_ROLL`. Otherwise `decide_role(extractor(path))`. Any exception → `B_ROLL`, confidence `0.0`, `has_speech=False`, `reasons=["probe_failed"]`, `error=str(exc)`; never raises.
- `classify_batch`: `ThreadPoolExecutor(max_workers)`, results in input order.

**Contract test — `tests/test_media_classifier.py`:**
```python
"""Content role classifier: decision matrix and signal extractors (no media files)."""

from pathlib import Path

import numpy as np
import pytest

from src.core.ontology import ContentRole
from src.modules.media_classifier import (
    ClipSignals,
    classify_batch,
    classify_clip,
    decide_role,
    face_area_ratio,
    motion_score,
    voice_activity,
)


def sig(**kw) -> ClipSignals:
    base = dict(has_audio=True, voice_ratio=0.0, snr_db=0.0, face_ratio=0.0, motion=0.0)
    base.update(kw)
    return ClipSignals(**base)


def test_content_role_values():
    assert ContentRole.A_ROLL.value == "a_roll"
    assert ContentRole.B_ROLL.value == "b_roll"
    assert ContentRole.AMBIENT_B_ROLL.value == "ambient_b_roll"


@pytest.mark.parametrize(
    "signals, role",
    [
        (sig(voice_ratio=0.45, snr_db=14.5, face_ratio=0.30, motion=2.0), ContentRole.A_ROLL),
        (sig(voice_ratio=0.45, snr_db=14.5, face_ratio=0.0, motion=2.0), ContentRole.A_ROLL),
        (sig(voice_ratio=0.45, snr_db=6.0, face_ratio=0.30), ContentRole.AMBIENT_B_ROLL),
        (sig(voice_ratio=0.20, snr_db=20.0, face_ratio=0.30), ContentRole.AMBIENT_B_ROLL),
        (sig(voice_ratio=0.10, snr_db=20.0, face_ratio=0.50), ContentRole.B_ROLL),
        (sig(has_audio=False, face_ratio=0.5), ContentRole.B_ROLL),
    ],
)
def test_decision_matrix(signals, role):
    assert decide_role(signals).role == role


def test_threshold_edges():
    assert decide_role(sig(voice_ratio=0.30, snr_db=10.0)).role == ContentRole.A_ROLL
    assert decide_role(sig(voice_ratio=0.15, snr_db=10.0)).role == ContentRole.AMBIENT_B_ROLL
    assert decide_role(sig(voice_ratio=0.1499, snr_db=10.0)).role == ContentRole.B_ROLL


def test_face_raises_confidence_and_speech_flags():
    with_face = decide_role(sig(voice_ratio=0.45, snr_db=14.5, face_ratio=0.30))
    no_face = decide_role(sig(voice_ratio=0.45, snr_db=14.5, face_ratio=0.0))
    assert with_face.confidence >= 0.85
    assert with_face.confidence > no_face.confidence
    assert with_face.has_speech and no_face.has_speech
    assert "offscreen_speech" in no_face.reasons
    assert not decide_role(sig(has_audio=False)).has_speech


def test_decision_is_total_and_explained():
    for v in np.linspace(0.0, 1.0, 11):
        for s in (-5.0, 0.0, 9.99, 10.0, 40.0):
            for f in (0.0, 0.08, 0.5):
                d = decide_role(sig(voice_ratio=float(v), snr_db=s, face_ratio=f))
                assert isinstance(d.role, ContentRole)
                assert 0.0 <= d.confidence <= 1.0
                assert d.reasons


def test_voice_activity_silence_and_empty():
    assert voice_activity(np.zeros(16000 * 2, dtype=np.float32), 16000)[0] == 0.0
    assert voice_activity(np.zeros(0, dtype=np.float32), 16000) == (0.0, 0.0)


def test_voice_activity_gated_bursts():
    sr = 16000
    rng = np.random.default_rng(0)
    x = 0.001 * rng.standard_normal(sr * 4).astype(np.float32)
    for k in range(4):  # 0.5 s bursts every second -> 50 % activity
        x[k * sr : k * sr + sr // 2] += 0.3 * rng.standard_normal(sr // 2).astype(np.float32)
    ratio, snr = voice_activity(x, sr)
    assert 0.40 <= ratio <= 0.60
    assert snr >= 20.0


def test_voice_activity_stationary_tone_is_not_speech():
    sr = 16000
    t = np.arange(sr * 3) / sr
    ratio, _ = voice_activity((0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), sr)
    assert ratio <= 0.05


def test_motion_score():
    still = [np.full((90, 160, 3), 128, np.uint8)] * 3
    assert motion_score(still) == 0.0
    rng = np.random.default_rng(1)
    noisy = [rng.integers(0, 256, (90, 160, 3), dtype=np.uint8) for _ in range(3)]
    assert motion_score(noisy) > 30.0
    assert motion_score(still[:1]) == 0.0


def test_face_area_ratio_without_faces():
    assert face_area_ratio([]) == 0.0
    assert face_area_ratio([np.full((180, 320, 3), 127, np.uint8)] * 3) == 0.0


def test_classify_clip_uses_role_folder_without_probing(tmp_path: Path):
    def boom(_p: Path) -> ClipSignals:
        raise AssertionError("extractor must not run for role folders")

    r = classify_clip(tmp_path / "A-roll" / "x.mp4", extractor=boom)
    assert r.role == ContentRole.A_ROLL and r.confidence == 1.0 and r.has_speech
    r = classify_clip(tmp_path / "B-roll" / "y.mp4", extractor=boom)
    assert r.role == ContentRole.B_ROLL and r.confidence == 1.0


def test_classify_clip_never_raises(tmp_path: Path):
    def broken(_p: Path) -> ClipSignals:
        raise RuntimeError("decode failed")

    r = classify_clip(tmp_path / "x.mp4", extractor=broken)
    assert r.role == ContentRole.B_ROLL
    assert r.confidence == 0.0
    assert r.error == "decode failed"
    assert r.reasons == ["probe_failed"]


def test_classify_batch_keeps_input_order(tmp_path: Path):
    paths = [tmp_path / f"c{i}.mp4" for i in range(6)]

    def fake(p: Path) -> ClipSignals:
        speech = int(p.stem[1:]) % 2 == 0
        return sig(voice_ratio=0.6 if speech else 0.0, snr_db=20.0)

    out = classify_batch(paths, max_workers=3, extractor=fake)
    assert [r.path for r in out] == paths
    assert [r.role for r in out] == [ContentRole.A_ROLL, ContentRole.B_ROLL] * 3
```

**Contract test — `tests/integration/test_media_classifier_real.py`:**
```python
"""Classifier on real ffmpeg-generated clips."""

import shutil
import subprocess
import time
from pathlib import Path

from src.core.ontology import ContentRole
from src.modules.media_classifier import classify_batch, classify_clip

FFMPEG = shutil.which("ffmpeg")


def _clip(out: Path, seconds: float, audio: str | None) -> Path:
    """``audio``: None (no stream), "silence" or "gated" (0.5 s tone every second)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds}"]
    if audio == "silence":
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={seconds}"]
    elif audio == "gated":
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=48000:duration={seconds}",
                "-af", "volume='if(lt(mod(t,1),0.5),1,0)':eval=frame"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(out))
    subprocess.run(cmd, check=True)
    return out


def test_no_audio_and_silent_clips_are_b_roll(tmp_path: Path):
    for name, audio in (("mute.mp4", None), ("silent.mp4", "silence")):
        r = classify_clip(_clip(tmp_path / name, 4.0, audio))
        assert r.error is None, r.error
        assert r.role == ContentRole.B_ROLL
        assert not r.has_speech


def test_gated_voice_like_audio_is_a_roll(tmp_path: Path):
    r = classify_clip(_clip(tmp_path / "talk.mp4", 4.0, "gated"))
    assert r.error is None, r.error
    assert r.role == ContentRole.A_ROLL
    assert r.signals is not None and 0.35 <= r.signals.voice_ratio <= 0.65


def test_unicode_path_and_missing_file(tmp_path: Path):
    ok = _clip(tmp_path / "Lễ Cưới" / "cảnh 01.mp4", 3.0, None)
    res = classify_batch([ok, tmp_path / "missing.mp4"])
    assert res[0].error is None and res[0].role == ContentRole.B_ROLL
    assert res[1].error is not None and res[1].confidence == 0.0


def test_classification_is_fast(tmp_path: Path):
    clip = _clip(tmp_path / "speed.mp4", 4.0, "gated")
    t0 = time.perf_counter()
    classify_clip(clip)
    assert time.perf_counter() - t0 < 2.0
```

**Expected red:** `ImportError` / `ModuleNotFoundError` for `ContentRole` / `src.modules.media_classifier`.
**Commit:** `feat(curation): content role classifier (A-roll / B-roll / ambient)`.

---

## Task 3 — Golden window selection (Stage 2)

**Files:**
- Modify: `src/core/config.py` (`golden_window_min_sec: float = Field(default=3.0)`, `golden_window_max_sec: float = Field(default=8.0)`)
- Modify: `src/core/genre_presets.py` (`SubGenrePreset.golden_window_sec: tuple[float, float] = (3.0, 8.0)`, a `_with_golden_windows` pass, `apply()` sets the two config fields)
- Modify: `src/modules/shot_windows.py` (add `select_golden_windows`)
- Test (LOCKED): `tests/test_golden_windows.py`
- Related suites: `tests/test_genre_presets.py tests/test_extended_shots_and_cadence.py tests/test_config.py tests/test_gui_config_precedence.py`

**Genre ranges:**
- By major genre: `ACTION_SPORTS`, `TRAVEL_LIFESTYLE`, `COMMERCIAL_SOCIAL` → `(2.5, 4.5)`; `DOCUMENTARY_NARRATIVE` → `(3.0, 8.0)`.
- Cinematic overrides `(4.0, 8.0)`: `wedding_highlight`, `travel_cinematic`, `travel_luxury`, `narrative_shortfilm`, `social_reels_aesthetic`, `commercial_realestate`.
- `ROUGH_CUT_PRESET` → `(3.0, 8.0)`.

**`select_golden_windows(curve, start, end, min_len, max_len, max_takes=1, tolerance=0.05) -> list[tuple[float, float]]`:**
1. `span = end - start`. If `span <= max_len` → `[(start, end)]`.
2. If `curve is None` → earliest windows of length `max_len` (the next one starts `min_len` after the previous end), up to `max_takes`, all inside `[start, end]`.
3. Otherwise candidate lengths `L ∈ {max_len, (min_len + max_len) / 2, min_len}`. For each `L`, slide `t0` from `start` in steps of `1 / curve.hz` (plus `end - L` as the last start), score with the existing `score_window(curve, t0, t0 + L)`, keep the best (ties → earliest). Let `s*` be the best score over all `L`; choose the **longest** `L` whose best score `>= s* - tolerance`.
4. Further takes (up to `max_takes`) use the same `L`. A candidate must not overlap any earlier take widened by `min_len` on each side (overlap is strict: `a0 < b1 and b0 < a1`; touching is allowed). Same scoring and tie-breaking. Stop early when nothing fits.
5. Return windows sorted by start.

**Contract test — `tests/test_golden_windows.py`:**
```python
"""Stage 2 golden windows: genre ranges and window selection on quality curves."""

import numpy as np
import pytest

from src.core.config import PipelineConfig
from src.core.genre_presets import GENRE_PRESETS, ROUGH_CUT_PRESET
from src.core.stage_options import apply_staged_options
from src.modules.shot_windows import QualityCurve, select_golden_windows


def make_curve(values, hz: float = 4.0, t0: float = 0.0, bad=None) -> QualityCurve:
    n = len(values)
    return QualityCurve(
        times=t0 + np.arange(n) / hz,
        sharpness=np.asarray(values, dtype=float),
        motion=np.zeros(n),
        jerk=np.zeros(n),
        exposure_bad=np.zeros(n, dtype=bool) if bad is None else np.asarray(bad, dtype=bool),
        hz=hz,
    )


def test_default_golden_window_range():
    cfg = PipelineConfig()
    assert (cfg.golden_window_min_sec, cfg.golden_window_max_sec) == (3.0, 8.0)
    assert ROUGH_CUT_PRESET.golden_window_sec == (3.0, 8.0)


@pytest.mark.parametrize(
    "key, expected",
    [
        ("wedding_highlight", (4.0, 8.0)),
        ("travel_cinematic", (4.0, 8.0)),
        ("travel_daily_vlog", (2.5, 4.5)),
        ("sports_extreme", (2.5, 4.5)),
        ("social_tiktok_fast", (2.5, 4.5)),
        ("doc_interview", (3.0, 8.0)),
    ],
)
def test_genre_sets_golden_window(key, expected):
    cfg = PipelineConfig()
    apply_staged_options(cfg, genre=key)
    assert (cfg.golden_window_min_sec, cfg.golden_window_max_sec) == expected


def test_every_preset_has_a_valid_range():
    for preset in GENRE_PRESETS.values():
        lo, hi = preset.golden_window_sec
        assert 0.0 < lo <= hi


def test_short_span_is_kept_whole():
    curve = make_curve([0.5] * 16)
    assert select_golden_windows(curve, 0.0, 4.0, 2.5, 4.5) == [(0.0, 4.0)]


def test_without_curve_takes_earliest_window():
    assert select_golden_windows(None, 10.0, 40.0, 3.0, 8.0) == [(10.0, 18.0)]


def test_peak_prefers_longest_window_within_tolerance():
    curve = make_curve([0.2] * 12 + [0.9] * 16 + [0.2] * 12)  # peak 3.0 .. 6.75 s
    (w,) = select_golden_windows(curve, 0.0, 10.0, 2.5, 4.5)
    assert w == pytest.approx((3.0, 6.5))


def test_exposure_bad_region_is_avoided():
    bad = [True] * 20 + [False] * 20
    curve = make_curve([0.8] * 40, bad=bad)
    (w,) = select_golden_windows(curve, 0.0, 10.0, 2.5, 4.5)
    assert w[0] >= 5.0 - 1e-6


def test_two_takes_do_not_overlap_and_keep_a_gap():
    curve = make_curve([0.7] * 160)  # 40 s flat
    takes = select_golden_windows(curve, 0.0, 40.0, 3.0, 8.0, max_takes=2)
    assert len(takes) == 2
    (a0, a1), (b0, b1) = takes
    assert a1 - a0 == pytest.approx(8.0) and b1 - b0 == pytest.approx(8.0)
    assert b0 >= a1 + 3.0 - 1e-6


def test_windows_stay_inside_span_and_length_range():
    rng = np.random.default_rng(7)
    for _ in range(25):
        n = int(rng.integers(60, 200))
        curve = make_curve(rng.random(n), t0=100.0)
        end = 100.0 + (n - 1) / 4.0
        for w0, w1 in select_golden_windows(curve, 100.0, end, 2.5, 4.5, max_takes=2):
            assert 100.0 - 1e-6 <= w0 < w1 <= end + 1e-6
            assert 2.5 - 1e-6 <= w1 - w0 <= 4.5 + 1e-6
```

**Expected red:** `ImportError: cannot import name 'select_golden_windows'`. The genre tests fail with `AttributeError: golden_window_min_sec`.
**Commit:** `feat(curation): genre-aware golden window selection`.

---

## Task 4 — Wire roles and golden windows into the rough-cut stage

**Files:**
- Create: `src/modules/golden_takes.py` (`window_cleaned_segments`)
- Modify: `src/core/config.py` (`auto_classify_roles: bool = Field(default=True)`, `enable_golden_windows: bool = Field(default=True)`)
- Modify: `src/core/service.py` (golden path of `_execute_pipeline`; `_golden_scenes` copies role/window metadata)
- Test (LOCKED): `tests/test_golden_takes.py`, `tests/integration/test_rough_cut_golden_real.py`
- Related suites: `tests/test_staged_pipeline.py tests/test_rough_cut_cleaner.py tests/test_gui_staged_workflow.py tests/test_scene_curation_and_cancel.py tests/test_pipeline_e2e.py`

**`window_cleaned_segments(segments, roles, min_len, max_len, curve_fn) -> list[CleanedSegment]`:**
- `roles: Mapping[Path, ContentRole]` keyed by `seg.clip_path`. `curve_fn(path, start, end) -> QualityCurve | None`.
- A segment whose clip has **no role** → returned unchanged (no metadata added).
- `A_ROLL` → unchanged; `metadata["content_role"] = "a_roll"`.
- Other roles → `select_golden_windows(curve_fn(clip, start, end), start, end, min_len, max_len, max_takes=2 if seg.duration >= MULTI_TAKE_SPAN_SEC else 1)` with `MULTI_TAKE_SPAN_SEC = 30.0`. One new segment per window via `dataclasses.replace`: `start_time = w0`, `end_time = w1`, `duration = w1 - w0`, and `source_start_time` / `source_end_time` shifted by the same deltas. `metadata` is a **new** dict (copy of the original) plus `content_role` and `golden_window = {"span": [start, end], "window": [w0, w1]}`. Note: `curve_fn` is only called when the span is longer than `max_len`.
- Output order is chronological per input order.
- Never mutate the input segments.

**Service wiring (golden path only, after `cleaner.clean_batch(...)`):**
1. `if config.auto_classify_roles`: classify each **unique** `seg.clip_path` with `classify_batch` (paths ordered by first appearance). Use the original media for the classifier (`seg.scene.original_path or seg.clip_path`) and key the result by `seg.clip_path`. Store `role_confidence` too.
2. `if config.enable_golden_windows`: `cleaned_segments = window_cleaned_segments(cleaned_segments, roles, config.golden_window_min_sec, config.golden_window_max_sec, curve_fn)`, with `curve_fn` = `compute_quality_curve(path, s, e, hz=4.0, cache=AnalysisCache(config))` (one `AnalysisCache` instance per job).
   If windows are disabled but roles exist, still set `metadata["content_role"]` (no windowing).
3. `_golden_scenes`: copy `content_role`, `role_confidence` and `golden_window` from `seg.metadata` (when present) into the per-scene meta dict.
4. Report progress for classification as one stage message, e.g. `"Classifying media roles (n clips)"`. Respect `cancel_event` between clips.

**Contract test — `tests/test_golden_takes.py`:**
```python
"""window_cleaned_segments: roles decide which takes are windowed."""

from pathlib import Path

import numpy as np
import pytest

from src.core.ontology import ContentRole
from src.core.types import CleanedSegment, SceneCut
from src.modules.golden_takes import window_cleaned_segments
from src.modules.shot_windows import QualityCurve

CLIP_A = Path("A.mp4")
CLIP_B = Path("B.mp4")


def seg(path: Path, s: float, e: float) -> CleanedSegment:
    sc = SceneCut(clip_path=path, start_time=s, end_time=e, start_frame=int(s * 30),
                  end_frame=int(e * 30), duration=e - s)
    return CleanedSegment(scene=sc, clip_path=path, start_time=s, end_time=e, duration=e - s,
                          source_start_time=s, source_end_time=e)


def peak_curve(path: Path, start: float, end: float) -> QualityCurve:
    n = int(round((end - start) * 4))
    t = start + np.arange(n) / 4.0
    vals = np.where((t >= start + 8.0) & (t < start + 14.0), 0.9, 0.2)
    return QualityCurve(times=t, sharpness=vals, motion=np.zeros(n), jerk=np.zeros(n),
                        exposure_bad=np.zeros(n, dtype=bool), hz=4.0)


def no_curve(path, start, end):
    raise AssertionError("curve_fn must not be called")


def test_a_roll_is_kept_whole_and_tagged():
    s = seg(CLIP_A, 0.0, 20.0)
    (out,) = window_cleaned_segments([s], {CLIP_A: ContentRole.A_ROLL}, 2.5, 4.5, no_curve)
    assert (out.start_time, out.end_time) == (0.0, 20.0)
    assert out.metadata["content_role"] == "a_roll"
    assert s.metadata == {}


def test_b_roll_is_cut_to_its_best_window():
    s = seg(CLIP_B, 100.0, 120.0)
    (out,) = window_cleaned_segments([s], {CLIP_B: ContentRole.B_ROLL}, 2.5, 4.5, peak_curve)
    assert (out.start_time, out.end_time) == pytest.approx((108.0, 112.5))
    assert out.duration == pytest.approx(4.5)
    assert (out.source_start_time, out.source_end_time) == pytest.approx((108.0, 112.5))
    assert out.metadata["content_role"] == "b_roll"
    assert out.metadata["golden_window"] == {"span": [100.0, 120.0], "window": [108.0, 112.5]}
    assert (s.start_time, s.end_time) == (100.0, 120.0)


def test_short_b_roll_is_tagged_but_not_cut():
    s = seg(CLIP_B, 5.0, 8.0)
    (out,) = window_cleaned_segments([s], {CLIP_B: ContentRole.B_ROLL}, 2.5, 4.5, no_curve)
    assert (out.start_time, out.end_time) == (5.0, 8.0)
    assert out.metadata["content_role"] == "b_roll"


def test_unknown_role_is_untouched():
    s = seg(CLIP_B, 0.0, 20.0)
    (out,) = window_cleaned_segments([s], {}, 2.5, 4.5, no_curve)
    assert out is s


def test_long_ambient_take_gives_two_windows_in_order():
    s = seg(CLIP_B, 0.0, 40.0)

    def flat(path, start, end):
        n = int(round((end - start) * 4))
        return QualityCurve(times=start + np.arange(n) / 4.0, sharpness=np.full(n, 0.7),
                            motion=np.zeros(n), jerk=np.zeros(n),
                            exposure_bad=np.zeros(n, dtype=bool), hz=4.0)

    out = window_cleaned_segments([s], {CLIP_B: ContentRole.AMBIENT_B_ROLL}, 3.0, 8.0, flat)
    assert len(out) == 2
    assert out[0].end_time <= out[1].start_time
    assert all(o.metadata["content_role"] == "ambient_b_roll" for o in out)


def test_mixed_segments_keep_input_order():
    segs = [seg(CLIP_A, 0.0, 10.0), seg(CLIP_B, 100.0, 120.0), seg(CLIP_A, 30.0, 35.0)]
    roles = {CLIP_A: ContentRole.A_ROLL, CLIP_B: ContentRole.B_ROLL}
    out = window_cleaned_segments(segs, roles, 2.5, 4.5, peak_curve)
    assert [o.clip_path for o in out] == [CLIP_A, CLIP_B, CLIP_A]
```

**Contract test — `tests/integration/test_rough_cut_golden_real.py`:**
```python
"""Rough-cut stage on real clips: roles (folder + classifier) and golden windows."""

import shutil
import subprocess
from pathlib import Path

from src.core.config import PipelineConfig
from src.core.service import StudioBackendAdapter

FFMPEG = shutil.which("ffmpeg")


def _clip(out: Path, seconds: float, gated_audio: bool) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds}"]
    if gated_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=48000:duration={seconds}",
                "-af", "volume='if(lt(mod(t,1),0.5),1,0)':eval=frame", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15", str(out)]
    subprocess.run(cmd, check=True)
    return out


def _run_rough_cut(input_dir: Path, tmp_path: Path):
    cfg = PipelineConfig()
    cfg.cache_dir = tmp_path / "cache"
    handle = StudioBackendAdapter().submit_job(
        input_dir=input_dir, soundtrack=None, output_dir=tmp_path / "out", config=cfg,
        stage="rough_cut",
    )
    assert handle.wait(timeout=600)
    res = handle.result
    assert res.success, res.error_message
    return cfg, res


def _check_windows(cfg, res) -> set[str]:
    roles = {s.metadata.get("content_role") for s in res.scored_scenes}
    for s in res.scored_scenes:
        if s.metadata.get("content_role") in ("b_roll", "ambient_b_roll"):
            assert s.scene.duration <= cfg.golden_window_max_sec + 1.0 / cfg.fps
    assert "rough_cut" in res.edit_timelines
    return roles


def test_mode_b_role_folders(tmp_path: Path):
    _clip(tmp_path / "footage" / "A-roll" / "talk.mp4", 20.0, True)
    _clip(tmp_path / "footage" / "B-roll" / "scenic.mp4", 20.0, False)
    cfg, res = _run_rough_cut(tmp_path / "footage", tmp_path)
    assert {"a_roll", "b_roll"} <= _check_windows(cfg, res)


def test_mode_a_single_folder_classifier(tmp_path: Path):
    _clip(tmp_path / "footage" / "talk.mp4", 20.0, True)
    _clip(tmp_path / "footage" / "scenic.mp4", 20.0, False)
    cfg, res = _run_rough_cut(tmp_path / "footage", tmp_path)
    by_clip = {Path(s.scene.clip_path).stem: s.metadata.get("content_role")
               for s in res.scored_scenes}
    assert by_clip.get("talk") == "a_roll"
    assert by_clip.get("scenic") == "b_roll"
    _check_windows(cfg, res)
```
*Fixture note:* if `testsrc2` footage gets fully pruned by the cleaner, the implementer may change only the **video source** in `_clip` (e.g. `testsrc`, `smptebars`, `mandelbrot`) and must report it. Assertions stay as written. `clip_path` may be a CFR cache file; use `.stem` only after confirming the cache keeps the stem (`_cfr_cache_path` does: `{stem}_{hash}_cfr..`). If it differs, compare with `startswith`. Report this as a deviation.

**Expected red:** `ModuleNotFoundError: src.modules.golden_takes`; integration: `content_role` missing.
**Commit:** `feat(curation): roles and golden windows in the rough-cut stage`.

---

## Task 5 — NLE pre-flight report (no exporter rewrites)

**Files:**
- Create: `src/core/nle_preflight.py`
- Modify: `src/core/types.py` (`DeliverResult.preflight_path: Path | None = None`, `DeliverResult.preflight_issues: list[str] = field(default_factory=list)`)
- Modify: `src/core/service.py` (`_execute_deliver`, non-preview path: run preflight before the exporters, write `<out_dir>/preflight.json`, fill both fields; never block the export)
- Test (LOCKED): `tests/test_nle_preflight.py`, `tests/integration/test_preflight_deliver_real.py`
- Related suites: `tests/test_deliver_settings.py tests/test_deliver_cli.py tests/test_fcpxml_exporter.py tests/test_fcpxml_matrix.py tests/test_capcut_exporter.py tests/integration`

**API:**
```python
IssueCode = Literal["MEDIA_MISSING", "MEDIA_UNREADABLE", "VFR_SOURCE", "FPS_MISMATCH",
                    "AUDIO_MONO", "AUDIO_MULTICHANNEL", "AUDIO_RATE_MISMATCH", "URI_ROUNDTRIP"]

@dataclass(frozen=True)
class PreflightIssue:
    code: IssueCode
    severity: Literal["error", "warning"]
    path: Path
    detail: str

@dataclass
class PreflightReport:
    issues: list[PreflightIssue]
    checked_files: int
    @property
    def ok(self) -> bool: ...          # no "error" issues
    def to_dict(self) -> dict[str, Any]: ...  # JSON-safe

def preflight_timeline(timeline: EditTimeline, audio_rate: int = 48000,
                       probe: Callable[[Path], MediaInfo] = probe_media) -> PreflightReport: ...
```

**Rules:**
- Media = unique `clip.media_path()` (relinks to originals, like the exporters) **plus** `timeline.soundtrack_path` if set. `probe` is called once per unique path, with that path unchanged. `checked_files` = number of unique media paths considered (missing ones included).
- Missing file → `MEDIA_MISSING` (error). Probe skipped.
- Clip with `info.has_video is False` → `MEDIA_UNREADABLE` (error). Soundtrack with `has_audio is False` → `MEDIA_UNREADABLE` (error).
- `info.is_vfr` (clips only) → `VFR_SOURCE` (warning).
- Clips only: `frame_rate_fraction(info.fps) != frame_rate_fraction(timeline.fps)` (when `info.fps` known) → `FPS_MISMATCH` (warning). Use `src.core.timebase.frame_rate_fraction`.
- If `has_audio`: channels `== 1` → `AUDIO_MONO`; `> 2` → `AUDIO_MULTICHANNEL`; `audio_rate` known and `!= audio_rate` → `AUDIO_RATE_MISMATCH` (all warnings).
- URI round trip: `_uri_to_path(path.resolve().as_uri())` from `src.modules.fcpxml_exporter` must equal `path.resolve()`, else `URI_ROUNDTRIP` (error).
- Deliver wiring passes `audio_rate=settings.audio_rate` when settings exist, else 48000. `preflight_issues` entries are `"<severity>:<code>:<file name>"`.

**Contract test — `tests/test_nle_preflight.py`:**
```python
"""NLE pre-flight checks (fake probe, real paths)."""

import json
from fractions import Fraction
from pathlib import Path

from src.core.media_info import MediaInfo
from src.core.nle_preflight import preflight_timeline
from src.core.types import EditClip, EditTimeline


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def info(p: Path, **kw) -> MediaInfo:
    base = dict(path=p, has_video=True, has_audio=True, duration=10.0, width=1920, height=1080,
                fps=Fraction(30), audio_channels=2, audio_rate=48000)
    base.update(kw)
    return MediaInfo(**base)


def tl(*paths: Path, fps: float = 30.0, soundtrack: Path | None = None) -> EditTimeline:
    clips = [EditClip(source_path=p, source_in=0.0, frames=30, available=1.0) for p in paths]
    return EditTimeline(name="t", fps=fps, canvas_width=1920, canvas_height=1080, clips=clips,
                        soundtrack_path=soundtrack)


def probe_from(table: dict[Path, MediaInfo]):
    return lambda q: table[Path(q).resolve()]


def codes(report) -> set[str]:
    return {i.code for i in report.issues}


def test_clean_media_has_no_issues(tmp_path: Path):
    p = _touch(tmp_path / "a.mp4")
    r = preflight_timeline(tl(p), probe=probe_from({p.resolve(): info(p)}))
    assert r.issues == [] and r.ok and r.checked_files == 1


def test_missing_media_is_an_error(tmp_path: Path):
    r = preflight_timeline(tl(tmp_path / "gone.mp4"), probe=probe_from({}))
    assert codes(r) == {"MEDIA_MISSING"}
    assert not r.ok


def test_audio_layout_warnings(tmp_path: Path):
    mono = _touch(tmp_path / "mono.mp4")
    surround = _touch(tmp_path / "surround.mp4")
    table = {mono.resolve(): info(mono, audio_channels=1, audio_rate=44100),
             surround.resolve(): info(surround, audio_channels=6)}
    r = preflight_timeline(tl(mono, surround), probe=probe_from(table))
    assert codes(r) == {"AUDIO_MONO", "AUDIO_RATE_MISMATCH", "AUDIO_MULTICHANNEL"}
    assert r.ok


def test_audio_rate_follows_deliver_setting(tmp_path: Path):
    p = _touch(tmp_path / "a.mp4")
    r = preflight_timeline(tl(p), audio_rate=44100,
                           probe=probe_from({p.resolve(): info(p, audio_rate=44100)}))
    assert r.issues == []


def test_silent_clip_has_no_audio_issues(tmp_path: Path):
    p = _touch(tmp_path / "mute.mp4")
    table = {p.resolve(): info(p, has_audio=False, audio_channels=0, audio_rate=0)}
    assert preflight_timeline(tl(p), probe=probe_from(table)).issues == []


def test_vfr_and_fps_mismatch(tmp_path: Path):
    phone = _touch(tmp_path / "phone.mp4")
    ntsc = _touch(tmp_path / "ntsc.mp4")
    table = {phone.resolve(): info(phone, is_vfr=True),
             ntsc.resolve(): info(ntsc, fps=Fraction(30000, 1001))}
    r = preflight_timeline(tl(phone, ntsc, fps=30.0), probe=probe_from(table))
    assert codes(r) == {"VFR_SOURCE", "FPS_MISMATCH"}


def test_unicode_paths_round_trip(tmp_path: Path):
    p = _touch(tmp_path / "Lễ Cưới Đẹp 2026" / "clip 01 #1.mp4")
    r = preflight_timeline(tl(p), probe=probe_from({p.resolve(): info(p)}))
    assert r.issues == []


def test_each_media_is_probed_once(tmp_path: Path):
    p = _touch(tmp_path / "a.mp4")
    calls: list[Path] = []

    def probe(q):
        calls.append(q)
        return info(p)

    preflight_timeline(tl(p, p, p), probe=probe)
    assert len(calls) == 1


def test_soundtrack_is_checked(tmp_path: Path):
    p = _touch(tmp_path / "a.mp4")
    r = preflight_timeline(tl(p, soundtrack=tmp_path / "missing.mp3"),
                           probe=probe_from({p.resolve(): info(p)}))
    assert codes(r) == {"MEDIA_MISSING"}
    assert r.checked_files == 2


def test_report_is_json_safe(tmp_path: Path):
    p = _touch(tmp_path / "mono.mp4")
    r = preflight_timeline(tl(p), probe=probe_from({p.resolve(): info(p, audio_channels=1)}))
    data = json.loads(json.dumps(r.to_dict()))
    assert data["ok"] is True
    assert data["issues"][0]["code"] == "AUDIO_MONO"
```

**Contract test — `tests/integration/test_preflight_deliver_real.py`:**
```python
"""Deliver writes preflight.json next to the NLE files."""

import json
from pathlib import Path

from src.core.config import PipelineConfig
from src.core.service import StudioBackendAdapter
from src.core.types import EditClip, EditTimeline


def test_deliver_writes_preflight(tmp_path: Path, make_clip):
    mono = make_clip("Lễ mono.mp4", duration=2.0, channels=1, sample_rate=44100)
    tl = EditTimeline(name="pf", fps=30.0, canvas_width=1920, canvas_height=1080,
                      clips=[EditClip(source_path=mono, source_in=0.0, frames=30, available=2.0)])
    cfg = PipelineConfig()
    cfg.cache_dir = tmp_path / "cache"
    handle = StudioBackendAdapter().submit_deliver_job(
        edit_timeline=tl, output_dir=tmp_path / "out", config=cfg, formats={"fcpxml"},
    )
    assert handle.wait(timeout=300)
    res = handle.result
    assert res.success, res.error_message
    assert res.preflight_path is not None and res.preflight_path.exists()
    data = json.loads(res.preflight_path.read_text(encoding="utf-8"))
    assert {i["code"] for i in data["issues"]} >= {"AUDIO_MONO", "AUDIO_RATE_MISMATCH"}
    assert any("AUDIO_MONO" in s for s in res.preflight_issues)
    assert res.fcpxml_path is not None and res.fcpxml_path.exists()
```
*Note:* `make_clip` comes from `tests/integration/conftest.py` (signature `make_clip(name, **kwargs)` → `_make_clip(tmp_path / name, ...)`).

**Expected red:** `ModuleNotFoundError: src.core.nle_preflight`; integration: `AttributeError: preflight_path`.
**Commit:** `feat(deliver): NLE pre-flight report (media, VFR, fps, audio layout, URI round trip)`.

---

## Task 6 — Curation core + MCP macro-tools

**Files:**
- Create: `src/core/curation.py`
- Modify: `src/modules/mcp_server.py` (3 tools + session store; nothing else changes)
- Test (LOCKED): `tests/curation_fakes.py`, `tests/test_curation_core.py`, `tests/test_macro_mcp_tools.py`
- Related suites: `tests/test_mcp_server.py tests/test_mcp_comprehensive.py tests/test_agent_controller.py`

**API (`src/core/curation.py`):**
```python
TOP_N_DEFAULT = 12
NLE_TARGETS: frozenset[str] = frozenset({"fcpxml", "premiere", "otio", "capcut"})

@dataclass
class CurationSession:
    session_id: str
    input_dir: Path
    config: PipelineConfig
    result: PipelineExecutionResult            # rough-cut stage result
    shots: dict[str, ScoredScene]              # "A01" / "B03" -> scene
    sequence: PipelineExecutionResult | None = None

def summarize_curation(result: PipelineExecutionResult, top_n: int = TOP_N_DEFAULT
                       ) -> tuple[dict[str, Any], dict[str, ScoredScene]]: ...
def run_curation(adapter, input_dir: Path, genre: str | None, output_dir: Path,
                 top_n: int = TOP_N_DEFAULT, timeout: float | None = None
                 ) -> tuple[CurationSession, dict[str, Any]]: ...
def assemble_sequence(adapter, session: CurationSession, soundtrack: Path | None = None,
                      selected_ids: list[str] | None = None, cadence: str | None = None,
                      timeout: float | None = None) -> dict[str, Any]: ...
def export_session(adapter, session: CurationSession, output_dir: Path,
                   targets: Iterable[str] = ("fcpxml", "premiere", "capcut"),
                   render_preview: bool = False, timeout: float | None = None) -> dict[str, Any]: ...
```

**Rules:**
- **Adapter calls use keyword arguments only** (`adapter.submit_job(input_dir=..., ...)`, `adapter.submit_deliver_job(edit_timeline=..., ...)`), then `handle.wait(timeout)` and `handle.result`. A falsy `wait` → `TimeoutError`. `result.success is False` → `RuntimeError(result.error_message)`.
- IDs: walk `result.scored_scenes` in order; `content_role == "a_roll"` → `A01, A02…`, everything else → `B01, B02…` (two-digit, three when > 99).
- Summary keys exactly: `takes` (int), `curated_duration_sec` (sum of `scene.duration`, 1 decimal), `by_role` (dict role → count; missing role → `"unknown"`), `top` (list[str]), `truncated` (int). `top` = scenes sorted by `composite_score` desc (ties: ID order), first `top_n`, each `f"{id}|{role}|{duration:.1f}s|q{composite_score:.2f}"`. `truncated = takes - len(top)`. No other keys, no lists of numbers.
- `run_curation`: `FileNotFoundError` if `input_dir` is not a directory. Build `PipelineConfig()`; when `genre` is given, `apply_staged_options(cfg, genre=genre)` **before** any adapter call (unknown genre → `ValueError`, no job). Submit `stage="rough_cut"`, `soundtrack=None`. `session_id = uuid4().hex[:8]`. The returned summary is `summarize_curation(...)[0]` plus `"session_id"`.
- `assemble_sequence`: unknown IDs → `ValueError` listing them. `selected_ids=None` → all shots in ID order. `soundtrack` falls back to `find_soundtrack(session.input_dir)`; still `None` → `ValueError("No soundtrack ...")`. `cadence` → `apply_staged_options(session.config, cadence=cadence)`. Submit `stage="sequence"`, `scenes=[selected...]`, `existing_timelines={"rough_cut": rough}` when the session has one. Timeline = `edit_timelines[result.active_variant_name or "master"]`, else the first timeline whose `kind == "music"`. Store `session.sequence`. Return exactly `{"timeline_id": f"{session_id}:<key>", "total_cuts": len(clips), "duration_sec": round(duration, 1), "status": "TIMELINE_ASSEMBLED_SUCCESS"}`.
- `export_session`: targets must be a subset of `NLE_TARGETS`, else `ValueError`. Timeline = the assembled one if `session.sequence` exists, else `session.result.edit_timelines["rough_cut"]` (the selects stringout). One `submit_deliver_job(edit_timeline=tl, output_dir=output_dir, config=session.config, formats=set(targets))`. If `render_preview`: a second call with `preview=True, formats={"mp4"}`. Return `{"fcpxml_path", "premiere_xml_path", "otio_path", "capcut_draft_path", "preview_mp4"}` (str or None), plus `"preflight_errors"` / `"preflight_warnings"` (counts from `preflight_issues` prefixes) and `"status": "EXPORT_COMPLETE"`.
- MCP (`mcp_server.py`): module-level `_SESSIONS: OrderedDict[str, CurationSession]`, capacity `MAX_SESSIONS = 8` (evict oldest). Tools:
  - `curate_footage(input_dir: str, genre: str | None = None, top_n: int = 12) -> dict` → `run_curation(adapter=adapter, ..., output_dir=Path(cfg.cache_dir) / "curation")`. Use the default `PipelineConfig().cache_dir`.
  - `assemble_cadence_timeline(session_id: str, selected_shot_ids: list[str] | None = None, soundtrack_path: str | None = None, cadence_style: str | None = None) -> dict`
  - `export_nle_project(session_id: str, output_dir: str, targets: list[str] | None = None, render_preview: bool = False) -> dict`
  - Unknown `session_id` → `ValueError("Unknown session ...")`. Tools always call the module attribute `adapter` (so tests can monkeypatch it).

**Contract helper — `tests/curation_fakes.py`** (shared by both test files, LOCKED):
```python
"""Fakes for curation tests: an adapter that records keyword calls and returns canned results."""

from pathlib import Path

from src.core.types import EditClip, EditTimeline, PipelineExecutionResult, SceneCut, ScoredScene


class FakeHandle:
    def __init__(self, result):
        self._r = result

    def wait(self, timeout=None):
        return True

    @property
    def result(self):
        return self._r


class FakeAdapter:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[tuple[str, dict]] = []

    def submit_job(self, **kw):
        self.calls.append(("job", kw))
        return FakeHandle(self.results.pop(0))

    def submit_deliver_job(self, **kw):
        self.calls.append(("deliver", kw))
        return FakeHandle(self.results.pop(0))


def scene(i: int, role: str | None, dur: float = 4.0, q: float = 0.5) -> ScoredScene:
    s = float(i * 10)
    sc = SceneCut(clip_path=Path("c.mp4"), start_time=s, end_time=s + dur,
                  start_frame=int(s * 30), end_frame=int((s + dur) * 30), duration=dur)
    md = {"content_role": role} if role else {}
    return ScoredScene(scene=sc, sharpness_score=q, aesthetic_score=q, composite_score=q,
                       metadata=md)


def timeline(name: str = "m", kind: str = "music") -> EditTimeline:
    return EditTimeline(name=name, fps=30.0, canvas_width=1920, canvas_height=1080,
                        clips=[EditClip(source_path=Path("c.mp4"), source_in=0.0, frames=60,
                                        available=2.0)], kind=kind)


def pipeline_result(scenes, timelines=None, success=True, error=None):
    return PipelineExecutionResult(
        success=success, total_duration=0.0, clip_count=len(scenes),
        scenes_detected=len(scenes), audio_bpm=0.0, sections_detected=0, fcpxml_path=None,
        premiere_xml_path=None, otio_path=None, capcut_draft_path=None, render_path=None,
        encoder_used=None, resolve_status=None, error_message=error, scored_scenes=scenes,
        edit_timelines=timelines or {},
    )
```

**Contract test — `tests/test_curation_core.py`:**
```python
"""Curation core with a fake adapter (no media, no ffmpeg)."""

import json
from pathlib import Path

import pytest

from src.core import curation
from src.core.types import DeliverResult
from tests.curation_fakes import FakeAdapter, pipeline_result, scene, timeline


def test_ids_roles_and_top_format():
    res = pipeline_result([scene(0, "a_roll", q=0.3), scene(1, "b_roll", q=0.9),
                           scene(2, "ambient_b_roll", q=0.6), scene(3, None, q=0.1)])
    summary, shots = curation.summarize_curation(res)
    assert list(shots) == ["A01", "B01", "B02", "B03"]
    assert summary["by_role"] == {"a_roll": 1, "b_roll": 1, "ambient_b_roll": 1, "unknown": 1}
    assert summary["top"][0] == "B01|b_roll|4.0s|q0.90"
    assert summary["takes"] == 4 and summary["truncated"] == 0
    assert summary["curated_duration_sec"] == 16.0
    assert set(summary) == {"takes", "curated_duration_sec", "by_role", "top", "truncated"}


def test_summary_stays_within_token_budget():
    scenes = [scene(i, "b_roll" if i % 3 else "a_roll", q=(i % 10) / 10) for i in range(60)]
    summary, shots = curation.summarize_curation(pipeline_result(scenes))
    assert len(json.dumps(summary)) <= 2000
    assert len(summary["top"]) == 12 and summary["truncated"] == 48
    assert len(shots) == 60
    assert curation.summarize_curation(pipeline_result(scenes))[0] == summary


def test_run_curation_submits_rough_cut(tmp_path: Path):
    rough = timeline("r", "rough_cut")
    fake = FakeAdapter(pipeline_result([scene(0, "b_roll")], {"rough_cut": rough}))
    session, summary = curation.run_curation(fake, tmp_path, "travel_daily_vlog", tmp_path / "o")
    kind, kw = fake.calls[0]
    assert kind == "job" and kw["stage"] == "rough_cut" and kw["soundtrack"] is None
    assert summary["session_id"] == session.session_id
    assert list(session.shots) == ["B01"]
    assert session.config.golden_window_max_sec == 4.5


def test_run_curation_validates_before_submitting(tmp_path: Path):
    fake = FakeAdapter()
    with pytest.raises(FileNotFoundError):
        curation.run_curation(fake, tmp_path / "nope", None, tmp_path / "o")
    with pytest.raises(ValueError):
        curation.run_curation(fake, tmp_path, "not_a_genre", tmp_path / "o")
    assert fake.calls == []


def test_run_curation_surfaces_pipeline_failure(tmp_path: Path):
    fake = FakeAdapter(pipeline_result([], success=False, error="every scene was pruned"))
    with pytest.raises(RuntimeError, match="pruned"):
        curation.run_curation(fake, tmp_path, None, tmp_path / "o")


def _session(tmp_path: Path, fake: FakeAdapter):
    rough = timeline("r", "rough_cut")
    fake.results.insert(0, pipeline_result([scene(0, "a_roll"), scene(1, "b_roll")],
                                           {"rough_cut": rough}))
    session, _ = curation.run_curation(fake, tmp_path, None, tmp_path / "o")
    return session, rough


def test_assemble_selected_shots(tmp_path: Path):
    music = tmp_path / "Music" / "theme.mp3"
    music.parent.mkdir()
    music.write_bytes(b"x")
    fake = FakeAdapter(pipeline_result([], {"master": timeline()}))
    session, rough = _session(tmp_path, fake)
    out = curation.assemble_sequence(fake, session, selected_ids=["B01"])
    kind, kw = fake.calls[-1]
    assert kind == "job" and kw["stage"] == "sequence"
    assert kw["scenes"] == [session.shots["B01"]]
    assert kw["soundtrack"] == music
    assert kw["existing_timelines"] == {"rough_cut": rough}
    assert out == {"timeline_id": f"{session.session_id}:master", "total_cuts": 1,
                   "duration_sec": 2.0, "status": "TIMELINE_ASSEMBLED_SUCCESS"}


def test_assemble_rejects_unknown_ids_and_missing_soundtrack(tmp_path: Path):
    fake = FakeAdapter()
    session, _ = _session(tmp_path, fake)
    with pytest.raises(ValueError, match="Z99"):
        curation.assemble_sequence(fake, session, soundtrack=tmp_path / "m.mp3",
                                   selected_ids=["A01", "Z99"])
    with pytest.raises(ValueError, match="soundtrack"):
        curation.assemble_sequence(fake, session)


def test_export_rough_cut_selects_and_preview(tmp_path: Path):
    fake = FakeAdapter()
    session, rough = _session(tmp_path, fake)
    fake.results += [
        DeliverResult(success=True, fcpxml_path=tmp_path / "t.fcpxml",
                      preflight_issues=["warning:AUDIO_MONO:a.mp4"]),
        DeliverResult(success=True, render_path=tmp_path / "p.mp4", is_preview=True),
    ]
    out = curation.export_session(fake, session, tmp_path / "exp", render_preview=True)
    (k1, kw1), (k2, kw2) = fake.calls[-2:]
    assert k1 == "deliver" and kw1["edit_timeline"] is rough
    assert kw1["formats"] == {"fcpxml", "premiere", "capcut"}
    assert kw2["preview"] is True
    assert out["status"] == "EXPORT_COMPLETE"
    assert out["fcpxml_path"] == str(tmp_path / "t.fcpxml")
    assert out["preview_mp4"] == str(tmp_path / "p.mp4")
    assert (out["preflight_errors"], out["preflight_warnings"]) == (0, 1)


def test_export_rejects_unknown_targets(tmp_path: Path):
    fake = FakeAdapter()
    session, _ = _session(tmp_path, fake)
    with pytest.raises(ValueError):
        curation.export_session(fake, session, tmp_path / "exp", targets=["avid"])
```

**Contract test — `tests/test_macro_mcp_tools.py`:**
```python
"""MCP macro-tools delegate to src.core.curation and keep sessions."""

import json
from pathlib import Path

import pytest

from src.core.types import DeliverResult
from src.modules import mcp_server
from tests.curation_fakes import FakeAdapter, pipeline_result, scene, timeline


def test_three_tools_end_to_end_with_fake_adapter(tmp_path: Path, monkeypatch):
    fake = FakeAdapter(
        pipeline_result([scene(0, "b_roll", q=0.8)], {"rough_cut": timeline("r", "rough_cut")}),
        pipeline_result([], {"master": timeline()}),
    )
    monkeypatch.setattr(mcp_server, "adapter", fake)
    cur = mcp_server.curate_footage(input_dir=str(tmp_path))
    assert len(json.dumps(cur)) <= 2000
    sid = cur["session_id"]
    music = tmp_path / "song.mp3"
    music.write_bytes(b"x")
    asm = mcp_server.assemble_cadence_timeline(session_id=sid, selected_shot_ids=["B01"],
                                               soundtrack_path=str(music))
    assert asm["status"] == "TIMELINE_ASSEMBLED_SUCCESS"
    fake.results.append(DeliverResult(success=True, fcpxml_path=tmp_path / "t.fcpxml"))
    exp = mcp_server.export_nle_project(session_id=sid, output_dir=str(tmp_path / "out"),
                                        targets=["fcpxml"])
    assert exp["status"] == "EXPORT_COMPLETE"
    assert fake.calls[-1][1]["formats"] == {"fcpxml"}


def test_unknown_session(monkeypatch):
    with pytest.raises(ValueError, match="Unknown session"):
        mcp_server.assemble_cadence_timeline(session_id="nope")


def test_session_store_is_bounded(tmp_path: Path, monkeypatch):
    results = [pipeline_result([scene(0, "b_roll")], {"rough_cut": timeline("r", "rough_cut")})
               for _ in range(mcp_server.MAX_SESSIONS + 1)]
    monkeypatch.setattr(mcp_server, "adapter", FakeAdapter(*results))
    ids = [mcp_server.curate_footage(input_dir=str(tmp_path))["session_id"]
           for _ in range(mcp_server.MAX_SESSIONS + 1)]
    with pytest.raises(ValueError, match="Unknown session"):
        mcp_server.export_nle_project(session_id=ids[0], output_dir=str(tmp_path / "o"))
    assert ids[-1] in mcp_server._SESSIONS
```
*Note:* `from tests.curation_fakes import ...` works like the existing `from tests.integration.conftest import ...` (`pythonpath = ["."]`). Do **not** add `tests/__init__.py`.

**Expected red:** `ImportError: cannot import name 'curation'` / missing `curate_footage`.
**Commit:** `feat(mcp): curate_footage / assemble_cadence_timeline / export_nle_project macro-tools`.

---

## Task 7 — CLI `curate`, end-to-end test, release docs

**Files:**
- Modify: `main.py` (new `curate` command; import `from src.core import curation` and call `curation.run_curation(...)` / `curation.export_session(...)` through the module, keyword arguments)
- Test (LOCKED): `tests/test_curate_cli.py`, `tests/integration/test_auto_curation_e2e.py`
- Docs: `CHANGELOG.md`, `docs/user_guide.md` (section "Auto-curation (rough cut selects)"), `docs/TECHNICAL_HANDOFF.md` (rev 2.4.0), `task.md`
- Related suites: `tests/test_deliver_cli.py tests/test_agent_cli_and_gui.py`, then **full suite**

**CLI:** `main.py curate --input-dir DIR [--genre KEY] [--output-dir ./output] [--top N] [--export/--no-export]`
- Calls `curation.run_curation(adapter=StudioBackendAdapter(), input_dir=Path(input_dir), genre=..., output_dir=..., top_n=...)`. Pass the path as given (no `.resolve()`).
- Writes `<output-dir>/curation_summary.json` (UTF-8, indent 2) and prints it.
- `--export` → `curation.export_session(adapter=..., session=..., output_dir=...)` and prints the paths.
- `ValueError` (e.g. unknown genre) → `click.BadParameter` (exit code 2). `FileNotFoundError` → same.

**Contract test — `tests/test_curate_cli.py`:**
```python
"""main.py curate."""

import json
from pathlib import Path

from click.testing import CliRunner

import main
from src.core import curation


def test_curate_writes_summary(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def fake_run(adapter, input_dir, genre, output_dir, top_n=12, timeout=None):
        seen.update(input_dir=Path(input_dir), genre=genre, top_n=top_n)
        return object(), {"session_id": "s1", "takes": 2, "top": ["B01|b_roll|4.0s|q0.90"]}

    monkeypatch.setattr(curation, "run_curation", fake_run)
    (tmp_path / "in").mkdir()
    r = CliRunner().invoke(main.cli, ["curate", "--input-dir", str(tmp_path / "in"),
                                      "--genre", "travel_daily_vlog",
                                      "--output-dir", str(tmp_path / "out"), "--top", "5"])
    assert r.exit_code == 0, r.output
    data = json.loads((tmp_path / "out" / "curation_summary.json").read_text(encoding="utf-8"))
    assert data["takes"] == 2
    assert seen == {"input_dir": tmp_path / "in", "genre": "travel_daily_vlog", "top_n": 5}


def test_curate_export_flag_calls_export(tmp_path: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(curation, "run_curation",
                        lambda adapter, input_dir, genre, output_dir, top_n=12, timeout=None:
                        ("SESSION", {"session_id": "s1", "takes": 1}))
    monkeypatch.setattr(curation, "export_session",
                        lambda adapter, session, output_dir, **kw:
                        calls.append(session) or {"status": "EXPORT_COMPLETE"})
    (tmp_path / "in").mkdir()
    r = CliRunner().invoke(main.cli, ["curate", "--input-dir", str(tmp_path / "in"),
                                      "--output-dir", str(tmp_path / "out"), "--export"])
    assert r.exit_code == 0, r.output
    assert calls == ["SESSION"]


def test_curate_rejects_unknown_genre(tmp_path: Path):
    (tmp_path / "in").mkdir()
    r = CliRunner().invoke(main.cli, ["curate", "--input-dir", str(tmp_path / "in"),
                                      "--genre", "not_a_genre",
                                      "--output-dir", str(tmp_path / "out")])
    assert r.exit_code == 2
    assert "not_a_genre" in r.output
```

**Contract test — `tests/integration/test_auto_curation_e2e.py`:**
```python
"""Folder -> curate -> assemble -> export on real ffmpeg media."""

import re
import shutil
import subprocess
from pathlib import Path

from src.core import curation
from src.core.service import StudioBackendAdapter

FFMPEG = shutil.which("ffmpeg")


def _run(cmd: list[str]) -> None:
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *cmd], check=True)


def _footage(root: Path) -> None:
    (root / "A-roll").mkdir(parents=True)
    (root / "B-roll").mkdir()
    (root / "Music").mkdir()
    _run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=15",
          "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=15",
          "-af", "volume='if(lt(mod(t,1),0.5),1,0)':eval=frame",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15",
          "-c:a", "aac", "-shortest", str(root / "A-roll" / "talk.mp4")])
    _run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=20",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15",
          str(root / "B-roll" / "scenic.mp4")])
    # 120 BPM click track, 30 s.
    _run(["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=30",
          "-af", "volume='if(lt(mod(t,0.5),0.06),1,0)':eval=frame",
          str(root / "Music" / "click.wav")])


def test_folder_to_nle_files(tmp_path: Path):
    root = tmp_path / "Dự án"
    _footage(root)
    adapter = StudioBackendAdapter()
    session, summary = curation.run_curation(adapter, root, "travel_daily_vlog",
                                             tmp_path / "work", timeout=900)
    assert summary["takes"] >= 2
    assert summary["by_role"].get("a_roll", 0) >= 1
    asm = curation.assemble_sequence(adapter, session, timeout=900)
    assert asm["status"] == "TIMELINE_ASSEMBLED_SUCCESS" and asm["total_cuts"] >= 1
    out = curation.export_session(adapter, session, tmp_path / "export", timeout=900)
    assert out["status"] == "EXPORT_COMPLETE"
    assert out["preflight_errors"] == 0
    for key in ("fcpxml_path", "premiere_xml_path", "capcut_draft_path"):
        assert out[key] and Path(out[key]).exists(), key
    fcpxml = Path(out["fcpxml_path"]).read_text(encoding="utf-8")
    srcs = re.findall(r'src="([^"]+)"', fcpxml)
    assert srcs and all(s.startswith("file:///") for s in srcs)
```

**Release steps (after green):**
1. Full suite: expected `>= 981 + new tests passed, 0 failed`; ruff `src tests main.py` clean.
2. Docs: user guide section (folders, `curate` CLI, MCP tools, the decisions D1–D10 in plain words), `TECHNICAL_HANDOFF.md` rev 2.4.0, `CHANGELOG.md` summary for the release.
3. `task.md`: T0–T7 `[x]`, Milestone B listed as `[ ]`.
4. AGENTS.md §2 end-of-session memory steps (`memory_tool.py update-goal / sync-git / add-lesson / compress`).
5. Commit: `feat(cli): main.py curate + auto-curation E2E; docs rev 2.4.0`. **Do not push or merge**; the orchestrator reviews first.

---

## Milestone B / C (not in v2: each needs its own spike + plan)

| ID | Item | Gate before planning |
|---|---|---|
| B1 | NVENC playback proxies (short-GOP, cache key path+size+mtime like `_cfr_cache_path`, CPU fallback) | Realtime-playback work merged; measure scrub latency on real DJI/Sony 4K |
| B2 | "Export selects" with `-c copy`, keyframe-snapped in/out (optional, non-destructive originals untouched) | Real user need confirmed |
| B3 | GOP smart-cut (re-encode head/tail only) | Spike: SPS/PPS match, open-GOP, 10-bit 4:2:2 encoder availability; go/no-go doc |
| B4 | HFR (≥ 100 fps) conform: B-roll slow-mo, A-roll real time (voice-safety guard) | Decide timeline speed model (`EditClip.speed` + speed ramping) |
| B5 | A-roll sentence snapping (use `TranscriptionEngine` sentences) | After T4 is in use |
| B6 | GUI: role colours in the selects canvas + one-click role override | After T4 |
| B7 | Multi-channel camera audio (spec §9.4 / matrix #13): pick the cleanest-SNR channel for A-roll analysis, keep all channels in NLE XML | Real 2–4 channel camera files; check what the exporters emit today |
| B8 | Corrupted media guard (matrix #11): hard probe/decode timeout + quarantine list | Reproduce with a truncated MP4 (missing `moov`) |
| B9 | Cache size cap / LRU (matrix #14) | Measure cache growth on a real project |
| C1 | FreeCut in-app fusion (spec §8, matrix #1, #2, #15) + local media server + 3D LUT preview (§9.2) | FreeCut checked out under `frontend/` with its schema; separate plan |

---

## Self-review (orchestrator)

- Every contract test was checked against the current signatures (`SceneCut`, `CleanedSegment`, `ScoredScene`, `EditClip`, `EditTimeline`, `PipelineExecutionResult`, `DeliverResult`, `DeliverSettings.audio_rate`, `submit_job` / `submit_deliver_job` keywords, `JobHandle.result` property, integration `make_clip` fixture).
- Spec items not built in v2 are listed in Milestone B/C with a gate, not silently dropped.
- Risky areas get integration tests on real ffmpeg media: classifier, rough-cut wiring, preflight, E2E.
