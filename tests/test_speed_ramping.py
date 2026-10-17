"""Unit tests for Dynamic Speed Ramping (1x - 8x) across visual scorer and timeline."""

from pathlib import Path
from unittest.mock import MagicMock

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
    """Verify NvencRenderer writes setpts=(PTS-STARTPTS)/speed into filter complex."""
    cfg = PipelineConfig(fps=30.0, enable_teaser=False)
    renderer = NvencRenderer(cfg)

    fake_video = tmp_path / "scene.mp4"
    fake_video.write_bytes(b"\x00" * 100)
    fake_audio = tmp_path / "track.wav"
    fake_audio.write_bytes(b"\x00" * 100)

    scene = ScoredScene(
        scene=SceneCut(
            clip_path=fake_video,
            start_time=1.0,
            end_time=10.0,
            start_frame=30,
            end_frame=300,
            duration=9.0,
        ),
        sharpness_score=0.3,
        aesthetic_score=0.3,
        composite_score=0.3,
        speed=3.0,
    )

    out_mp4 = tmp_path / "rendered.mp4"

    # Mock subprocess to capture filter complex script
    captured_filter: list[str] = []

    def mock_popen(cmd, *args, **kwargs):
        script_idx = cmd.index("-filter_complex_script") + 1
        script_path = Path(cmd[script_idx])
        if script_path.exists():
            captured_filter.append(script_path.read_text(encoding="utf-8"))
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.stderr.readline.return_value = ""
        out_mp4.write_bytes(b"MP4_DATA")
        return proc

    from unittest.mock import patch
    with patch("subprocess.Popen", side_effect=mock_popen):
        renderer.render_sequence(
            scored_scenes=[scene],
            cut_timestamps=[0.0, 2.0],
            soundtrack_path=fake_audio,
            output_file=out_mp4,
            include_teaser=False,
            force_cpu=True,
        )

    assert len(captured_filter) == 1
    content = captured_filter[0]
    assert "setpts=(PTS-STARTPTS)/3.0000" in content
