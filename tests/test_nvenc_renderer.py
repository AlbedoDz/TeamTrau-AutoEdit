"""Unit tests for the segment-based NVENC renderer command and filter builders.

Real end-to-end ffmpeg execution is covered in tests/integration/test_render_real.py.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene, SongSection, SongSectionType
from src.dispatchers.nvenc_renderer import NvencRenderer, RenderSegment


@pytest.fixture
def sample_scenes():
    return [
        ScoredScene(
            scene=SceneCut(Path("e:/raw/clip1.mp4"), 0.0, 3.0, 0, 90, 3.0),
            sharpness_score=100.0,
            aesthetic_score=0.9,
            composite_score=0.95,
        ),
        ScoredScene(
            scene=SceneCut(Path("e:/raw/clip2.mp4"), 1.0, 4.0, 30, 120, 3.0),
            sharpness_score=90.0,
            aesthetic_score=0.8,
            composite_score=0.85,
        ),
    ]


def test_nvenc_renderer_validates_empty_inputs():
    """Verify ValueError is raised on empty scene or cut list."""
    renderer = NvencRenderer(PipelineConfig())

    with pytest.raises(ValueError, match="No scenes provided"):
        renderer.render_sequence([], [0.0, 2.0], Path("bgm.wav"), Path("out.mp4"))

    with pytest.raises(ValueError, match="at least 2 boundary"):
        renderer.render_sequence([MagicMock()], [0.0], Path("bgm.wav"), Path("out.mp4"))


def test_segments_are_frame_exact(sample_scenes):
    """Beat cuts are quantised to whole frames so long timelines never drift."""
    cfg = PipelineConfig(fps=30.0, enable_teaser=False)
    segs = NvencRenderer(cfg).build_segments(sample_scenes, [0.0, 0.517, 1.033, 3.0])
    assert sum(s.frames for s in segs) == 90
    assert [s.frames for s in segs] == [16, 15, 59]


def test_nvenc_encoder_args_use_config():
    cfg = PipelineConfig(nvenc_preset="p7", nvenc_cq=18, nvenc_bitrate="60M")
    enc, args = NvencRenderer(cfg).encoder_args(use_nvenc=True)
    assert enc == "hevc_nvenc"
    assert args[args.index("-preset") + 1] == "p7"
    assert args[args.index("-cq") + 1] == "18"
    assert args[args.index("-b:v") + 1] == "60M"


def test_cpu_fallback_encoder_is_libx265():
    enc, args = NvencRenderer(PipelineConfig()).encoder_args(use_nvenc=False)
    assert enc == "libx265"
    assert "libx265" in args


def test_preview_uses_fast_h264_at_540p():
    cfg = PipelineConfig(canvas_width=1920, canvas_height=1080)
    r = NvencRenderer(cfg)
    assert r.canvas_size(preview=True) == (960, 540)
    enc, _ = r.encoder_args(use_nvenc=True, preview=True)
    assert enc == "h264_nvenc"


def test_segment_command_fast_seeks_each_clip(sample_scenes):
    """Each segment seeks its own input instead of trimming from frame 0."""
    cfg = PipelineConfig(enable_teaser=False)
    r = NvencRenderer(cfg)
    seg = r.build_segments(sample_scenes, [0.0, 1.5, 3.0])[1]
    cmd = r.build_segment_command("ffmpeg", seg, Path("seg.mkv"), True, with_audio=False)
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == f"{seg.source_in:.3f}"
    assert "hevc_nvenc" in cmd
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert f"trim=end_frame={seg.frames}" in graph


def test_teaser_adds_leading_segments(sample_scenes):
    cfg = PipelineConfig(enable_teaser=True, teaser_duration=4.0, fps=30.0)
    segs = NvencRenderer(cfg).build_segments(
        sample_scenes, [0.0, 3.0, 6.0, 9.0, 12.0, 15.0], include_teaser=True
    )
    assert sum(s.frames for s in segs) == 450
    assert len(segs) >= 3 + 4


def test_sections_drive_scene_selection(sample_scenes):
    cfg = PipelineConfig(enable_teaser=False)
    sections = [
        SongSection(SongSectionType.INTRO, 0.0, 2.0, 2.0, 0.2, is_climax=False),
        SongSection(SongSectionType.DROP, 2.0, 4.0, 2.0, 0.9, is_climax=True),
    ]
    segs = NvencRenderer(cfg).build_segments(sample_scenes, [0.0, 2.0, 4.0], sections=sections)
    assert len(segs) == 2


def test_audio_filter_speed_ramp_and_silent_sources():
    cfg = PipelineConfig(fps=30.0)
    r = NvencRenderer(cfg)
    seg = RenderSegment(Path("a.mp4"), 0.0, frames=30, available=5.0, speed=3.0)
    graph = r.build_segment_filter(seg, (320, 180), with_audio=True, source_has_audio=True)
    assert "setpts=(PTS-STARTPTS)/3.0000" in graph
    assert "atempo=2.000000,atempo=1.500000" in graph
    assert "aresample=48000" in graph
    silent = r.build_segment_filter(seg, (320, 180), with_audio=True, source_has_audio=False)
    assert "anullsrc=r=48000:cl=stereo" in silent


def test_concat_command_mixes_soundtrack_and_copies_video():
    r = NvencRenderer(PipelineConfig(workflow_mode="standard"))
    cmd = r.build_concat_command(
        "ffmpeg", Path("list.txt"), Path("out.mp4.part"), 10.0, True, Path("music.wav")
    )
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in graph
    assert cmd[cmd.index("-f", cmd.index("-t")) + 1] == "mp4"


def test_default_nvenc_preset_is_p5_and_p7_selectable():
    assert PipelineConfig().nvenc_preset == "p5"
    _, args = NvencRenderer(PipelineConfig(nvenc_preset="p7")).encoder_args(use_nvenc=True)
    assert args[args.index("-preset") + 1] == "p7"


def _one_segment(sample_scenes):
    cfg = PipelineConfig(fps=30.0, enable_teaser=False)
    seg = NvencRenderer(cfg).build_segments(sample_scenes, [0.0, 2.0])[0]
    return cfg, seg


def test_hwdecode_flag_only_on_nvenc_path(sample_scenes):
    cfg, seg = _one_segment(sample_scenes)
    r = NvencRenderer(cfg)
    hw = r.build_segment_command("ffmpeg", seg, Path("o.mkv"), True, False, hwdecode=True)
    assert hw[hw.index("-hwaccel") + 1] == "cuda"
    # Frames must stay in system memory: CPU filters (zoompan/overlay/...) rely on it.
    assert "-hwaccel_output_format" not in hw
    assert hw.index("-hwaccel") < hw.index("-i")
    assert "-hwaccel" not in r.build_segment_command("ffmpeg", seg, Path("o.mkv"), True, False)
    assert "-hwaccel" not in r.build_segment_command("ffmpeg", seg, Path("o.mkv"), False, False, hwdecode=True)
