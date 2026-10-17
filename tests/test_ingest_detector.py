"""Unit tests for ingest detector, video probing, and non-destructive metadata export."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from src.core.config import PipelineConfig
from src.core.types import ActiveSegment, SceneCut
from src.modules.ingest_detector import IngestDetector


@pytest.fixture
def synthetic_video_path(tmp_path):
    """Generate a 2-second synthetic MP4 video file with two distinct colored scenes."""
    video_path = tmp_path / "test_scenes.mp4"
    width, height = 320, 240
    fps = 30.0

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(video_path), fourcc, fps, (width, height))

    # Frame 0 to 29: Red
    for _ in range(30):
        frame = np.full((height, width, 3), (0, 0, 255), dtype=np.uint8)
        out.write(frame)

    # Frame 30 to 59: Blue (Hard cut transition)
    for _ in range(30):
        frame = np.full((height, width, 3), (255, 0, 0), dtype=np.uint8)
        out.write(frame)

    out.release()
    return video_path


def test_probe_video(synthetic_video_path):
    """Verify video dimension, framerate, and duration probing."""
    cfg = PipelineConfig()
    detector = IngestDetector(cfg)
    meta = detector.probe_video(synthetic_video_path)

    assert meta.width == 320
    assert meta.height == 240
    assert meta.fps == pytest.approx(30.0, abs=0.5)
    assert meta.total_frames == 60
    assert meta.duration == pytest.approx(2.0, abs=0.1)


def test_detect_scenes_returns_valid_cuts(synthetic_video_path):
    """Verify scene detection creates non-destructive SceneCut intervals."""
    cfg = PipelineConfig(adaptive_threshold=3.0, min_scene_len_sec=0.4)
    detector = IngestDetector(cfg)
    cuts = detector.detect_scenes(synthetic_video_path)

    assert len(cuts) >= 1
    for c in cuts:
        assert c.clip_path == synthetic_video_path
        assert c.end_time >= c.start_time
        assert c.duration > 0


def test_export_metadata_csv_and_edl(tmp_path):
    """Verify export of scene timecodes into valid CSV and CMX 3600 EDL files."""
    cfg = PipelineConfig()
    detector = IngestDetector(cfg)

    scenes = [
        SceneCut(
            clip_path=Path("sample1.mp4"),
            start_time=0.0,
            end_time=3.5,
            start_frame=0,
            end_frame=105,
            duration=3.5,
        ),
        SceneCut(
            clip_path=Path("sample2.mp4"),
            start_time=1.0,
            end_time=5.0,
            start_frame=30,
            end_frame=150,
            duration=4.0,
        ),
    ]

    csv_file = tmp_path / "scenes.csv"
    edl_file = tmp_path / "scenes.edl"

    detector.export_metadata_csv(scenes, csv_file)
    detector.export_metadata_edl(scenes, edl_file, fps=30.0)

    assert csv_file.is_file()
    assert edl_file.is_file()

    edl_content = edl_file.read_text(encoding="utf-8")
    assert "TITLE: AUTOEDIT_NON_DESTRUCTIVE_SCENES" in edl_content
    assert "sample1.mp4" in edl_content
    assert "sample2.mp4" in edl_content


def test_filter_scenes_by_activity():
    """Verify pruning of static scenes based on activity segments."""
    cfg = PipelineConfig()
    detector = IngestDetector(cfg)

    scenes = [
        SceneCut(
            clip_path=Path("active.mp4"),
            start_time=0.0,
            end_time=4.0,
            start_frame=0,
            end_frame=120,
            duration=4.0,
        ),
        SceneCut(
            clip_path=Path("static.mp4"),
            start_time=0.0,
            end_time=4.0,
            start_frame=0,
            end_frame=120,
            duration=4.0,
        ),
    ]

    # Map activity: first scene is active, second scene is not
    filtered = detector.filter_scenes_by_activity(
        scenes,
        [
            ActiveSegment(start_time=0.0, end_time=4.0, motion_score=0.8, is_active=True),
            ActiveSegment(start_time=4.0, end_time=8.0, motion_score=0.01, is_active=False),
        ],
    )
    assert len(filtered) >= 1


def test_batch_process_videos_and_cache(tmp_path, synthetic_video_path):
    """Verify batch processing produces scenes and reuses disk cache on subsequent runs."""
    cache_dir = tmp_path / "cache"
    cfg = PipelineConfig(cache_dir=cache_dir, enable_cache=True, max_workers=2)
    detector = IngestDetector(cfg)

    # First pass: cache miss, compute
    scenes_pass1 = detector.batch_process_videos([synthetic_video_path])
    assert len(scenes_pass1) >= 1
    cache_files = list(cache_dir.glob("scene_*.json"))
    assert len(cache_files) == 1

    # Second pass: cache hit
    scenes_pass2 = detector.batch_process_videos([synthetic_video_path])
    assert len(scenes_pass2) == len(scenes_pass1)

    # Test clearing cache
    cleared = detector.clear_cache()
    assert cleared == 1
    assert len(list(cache_dir.glob("scene_*.json"))) == 0


def test_find_dead_air_intervals():
    """Verify overlapping silence and freeze ranges are identified as dead air."""
    cfg = PipelineConfig(dead_air_min_duration=2.0)
    detector = IngestDetector(cfg)

    silence = [(1.0, 5.0), (10.0, 15.0)]
    freeze = [(2.0, 6.0), (12.0, 13.0)]

    # Overlap 1: max(1, 2) to min(5, 6) = (2.0, 5.0) -> dur = 3.0 >= 2.0 -> kept
    # Overlap 2: max(10, 12) to min(15, 13) = (12.0, 13.0) -> dur = 1.0 < 2.0 -> dropped
    dead_air = detector.find_dead_air_intervals(silence, freeze)
    assert len(dead_air) == 1
    assert dead_air[0] == (2.0, 5.0)


def test_filter_scenes_with_dead_air():
    """Verify scenes covered by dead air are pruned."""
    cfg = PipelineConfig(dead_air_min_duration=2.0)
    detector = IngestDetector(cfg)

    scenes = [
        SceneCut(
            clip_path=Path("good.mp4"),
            start_time=0.0,
            end_time=4.0,
            start_frame=0,
            end_frame=120,
            duration=4.0,
        ),
        SceneCut(
            clip_path=Path("dead.mp4"),
            start_time=5.0,
            end_time=9.0,
            start_frame=150,
            end_frame=270,
            duration=4.0,
        ),
    ]

    # Dead air covers [5.0, 9.0]
    dead_air = [(5.0, 9.0)]
    filtered = detector.filter_scenes_by_activity(
        scenes=scenes,
        activity_segments=[],
        dead_air_intervals=dead_air,
    )
    assert len(filtered) == 1
    assert filtered[0].clip_path.name == "good.mp4"




# --- Speed optimisations -----------------------------------------------------------------


def test_find_dead_air_two_pointer_matches_bruteforce():
    import random

    rng = random.Random(7)
    det = IngestDetector(PipelineConfig(dead_air_min_duration=1.0))

    def intervals(n: int) -> list[tuple[float, float]]:
        t, out = 0.0, []
        for _ in range(n):
            t += rng.uniform(0.1, 4.0)
            end = t + rng.uniform(0.5, 6.0)
            out.append((t, end))
            t = end
        return out

    for _ in range(50):
        sil, frz = intervals(8), intervals(8)
        expected = []
        for s0, s1 in sil:
            for f0, f1 in frz:
                a, b = max(s0, f0), min(s1, f1)
                if b > a and b - a >= 1.0:
                    expected.append((a, b))
        assert sorted(det.find_dead_air_intervals(sil, frz)) == sorted(expected)


def test_filter_scenes_overlap_matches_bruteforce():
    rng = np.random.default_rng(3)
    det = IngestDetector(PipelineConfig())
    for _ in range(30):
        segs = [
            ActiveSegment(start_time=i * 0.5, end_time=(i + 1) * 0.5, motion_score=0.0,
                          is_active=bool(rng.integers(0, 2)))
            for i in range(40)
        ]
        scenes = []
        t = 0.0
        while t < 18:
            d = float(rng.uniform(0.5, 4.0))
            scenes.append(SceneCut(clip_path=Path("a.mp4"), start_time=t, end_time=t + d,
                                   start_frame=0, end_frame=1, duration=d))
            t += d
        expected = []
        for sc in scenes:
            act = sum(
                max(0.0, min(sc.end_time, s.end_time) - max(sc.start_time, s.start_time))
                for s in segs if s.is_active
            )
            if act / sc.duration >= 0.25:
                expected.append(sc)
        assert det.filter_scenes_by_activity(scenes, segs) == (expected or scenes)


def test_activity_opencv_reads_sequentially(synthetic_video_path, monkeypatch):
    """No random-access seeks: the sampler must grab() frames in order."""
    seeks: list[int] = []
    real_cls = cv2.VideoCapture

    class Spy:
        def __init__(self, *a, **k):
            self._c = real_cls(*a, **k)

        def set(self, prop, val):
            seeks.append(prop)
            return self._c.set(prop, val)

        def __getattr__(self, name):
            return getattr(self._c, name)

    monkeypatch.setattr(cv2, "VideoCapture", Spy)
    det = IngestDetector(PipelineConfig())
    segs = det._analyze_activity_opencv(synthetic_video_path)
    assert not seeks
    assert len(segs) >= 3
    # Hard cut red->blue at 1.0 s must register as motion in the sample spanning the cut.
    assert any(s.motion_score > 0.1 for s in segs)


def test_detect_dead_air_ranges_single_pass(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    clip = tmp_path / "dead.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x240:r=10:d=5",
         "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t", "5", "-c:v", "libx264",
         "-c:a", "aac", "-shortest", str(clip)],
        check=True,
    )
    det = IngestDetector(PipelineConfig(dead_air_min_duration=1.0))
    calls: list[list[str]] = []
    import src.modules.ingest_detector as mod

    real = mod.run_tool

    def spy(cmd, *a, **k):
        calls.append(cmd)
        return real(cmd, *a, **k)

    mod.run_tool, orig = spy, mod.run_tool
    try:
        sil, frz = det.detect_dead_air_ranges(clip)
    finally:
        mod.run_tool = orig
    assert len(calls) == 1
    assert "-af" in calls[0] and "-vf" in calls[0]
    assert sil and frz
    assert det.find_dead_air_intervals(sil, frz)


def test_process_single_video_reuses_probed_metadata(tmp_path, synthetic_video_path):
    from unittest.mock import patch

    cfg = PipelineConfig(cache_dir=tmp_path / "c", enable_cache=False)
    det = IngestDetector(cfg)
    meta = det.probe_video(synthetic_video_path)
    with patch.object(det, "probe_video", side_effect=AssertionError("re-probed")):
        scenes = det.process_single_video(synthetic_video_path, metadata=meta)
    assert scenes
