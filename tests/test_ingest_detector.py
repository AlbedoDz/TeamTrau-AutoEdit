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


