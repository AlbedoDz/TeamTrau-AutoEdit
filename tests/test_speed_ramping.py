"""Unit tests for Dynamic Speed Ramping (1x - 8x) across visual scorer and timeline."""

from pathlib import Path

import opentimelineio as otio

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene
from src.dispatchers.nvenc_renderer import NvencRenderer
from src.modules.timeline_builder import TimelineBuilder
from src.modules.visual_scorer import VisualScorer


def test_speed_calculation_tiers():
    """Verify high, medium, and low scores map to appropriate speed multipliers."""
    # Speed ramping enabled
    assert VisualScorer.calculate_speed_for_score(0.85, max_speed=8.0, enabled=True) == 1.0
    assert VisualScorer.calculate_speed_for_score(0.75, max_speed=8.0, enabled=True) == 1.0
    assert VisualScorer.calculate_speed_for_score(0.60, max_speed=8.0, enabled=True) == 2.0
    assert VisualScorer.calculate_speed_for_score(0.45, max_speed=8.0, enabled=True) == 2.0
    assert VisualScorer.calculate_speed_for_score(0.30, max_speed=8.0, enabled=True) == 4.0
    assert VisualScorer.calculate_speed_for_score(0.10, max_speed=3.0, enabled=True) == 3.0

    # Speed ramping disabled
    assert VisualScorer.calculate_speed_for_score(0.30, max_speed=8.0, enabled=False) == 1.0


def test_timeline_builder_linear_time_warp():
    """Verify TimelineBuilder attaches LinearTimeWarp effect and records speed metadata."""
    cfg = PipelineConfig(fps=30.0, enable_teaser=False)
    builder = TimelineBuilder(cfg)

    scene1 = ScoredScene(
        scene=SceneCut(
            clip_path=Path("clip_fast.mp4"),
            start_time=0.0,
            end_time=10.0,
            start_frame=0,
            end_frame=300,
            duration=10.0,
        ),
        sharpness_score=0.9,
        aesthetic_score=0.9,
        composite_score=0.9,
        speed=1.0,
    )
    scene2 = ScoredScene(
        scene=SceneCut(
            clip_path=Path("clip_slow.mp4"),
            start_time=0.0,
            end_time=10.0,
            start_frame=0,
            end_frame=300,
            duration=10.0,
        ),
        sharpness_score=0.3,
        aesthetic_score=0.3,
        composite_score=0.3,
        speed=4.0,
    )

    cuts = [0.0, 2.0, 4.0]
    timeline = builder.build_timeline(
        scored_scenes=[scene1, scene2],
        cut_timestamps=cuts,
        soundtrack_path=Path("audio.wav"),
        include_teaser=False,
    )

    video_track = timeline.tracks[0]
    assert len(video_track) == 2

    clip1 = video_track[0]
    clip2 = video_track[1]

    # Clip 1 has speed 1.0 -> no LinearTimeWarp effect
    assert clip1.metadata["AutoEdit"]["speed"] == 1.0
    assert len(clip1.effects) == 0

    # Clip 2 has speed 4.0 -> LinearTimeWarp effect attached
    assert clip2.metadata["AutoEdit"]["speed"] == 4.0
    assert len(clip2.effects) == 1
    warp = clip2.effects[0]
    assert isinstance(warp, otio.schema.LinearTimeWarp)
    assert warp.time_scalar == 4.0


def test_nvenc_renderer_pts_scaling(tmp_path: Path):
    """Verify the segment filter scales video PTS by the speed multiplier."""
    cfg = PipelineConfig(fps=30.0, enable_teaser=False)
    renderer = NvencRenderer(cfg)
    scene = ScoredScene(
        scene=SceneCut(tmp_path / "scene.mp4", 1.0, 10.0, 30, 300, 9.0),
        sharpness_score=0.3,
        aesthetic_score=0.3,
        composite_score=0.3,
        speed=3.0,
    )
    seg = renderer.build_segments([scene], [0.0, 2.0])[0]
    content = renderer.build_segment_filter(seg, (1920, 1080), with_audio=False, source_has_audio=False)
    assert "setpts=(PTS-STARTPTS)/3.0000" in content
