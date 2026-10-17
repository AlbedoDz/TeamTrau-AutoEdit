"""Unit tests for headless NVENC video renderer and dynamic filter graph construction."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene
from src.dispatchers.nvenc_renderer import NvencRenderer


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
    cfg = PipelineConfig()
    renderer = NvencRenderer(cfg)

    with pytest.raises(ValueError, match="No scenes provided"):
        renderer.render_sequence([], [0.0, 2.0], Path("bgm.wav"), Path("out.mp4"))

    with pytest.raises(ValueError, match="at least 2 boundary"):
        renderer.render_sequence([MagicMock()], [0.0], Path("bgm.wav"), Path("out.mp4"))


def test_nvenc_renderer_command_construction(tmp_path, sample_scenes):
    """Verify FFmpeg arguments, encoder flags, and filter_complex_script generation."""
    cfg = PipelineConfig(
        canvas_width=1920,
        canvas_height=1080,
        fps=30.0,
        nvenc_preset="p7",
        nvenc_cq=18,
        nvenc_bitrate="60M",
    )
    renderer = NvencRenderer(cfg)
    renderer.nvenc_available = True

    cut_timestamps = [0.0, 1.5, 3.0]
    soundtrack = Path("e:/music/bgm.wav")
    output_mp4 = tmp_path / "master.mp4"

    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = []
    mock_process.stderr = []

    with patch("subprocess.Popen", return_value=mock_process) as mock_run:
        # Create dummy file to simulate output
        output_mp4.touch()
        res = renderer.render_sequence(
            scored_scenes=sample_scenes,
            cut_timestamps=cut_timestamps,
            soundtrack_path=soundtrack,
            output_file=output_mp4,
        )

        assert res.encoder_used == "hevc_nvenc"
        assert res.return_code == 0

        # Inspect executed command
        called_cmd = mock_run.call_args[0][0]
        assert "-filter_complex_script" in called_cmd
        assert "hevc_nvenc" in called_cmd
        assert "-preset" in called_cmd
        assert "p7" in called_cmd
        assert "-cq" in called_cmd
        assert "18" in called_cmd
        assert "-b:v" in called_cmd
        assert "60M" in called_cmd


def test_nvenc_renderer_fallback_to_libx265(tmp_path, sample_scenes):
    """Verify automatic fallback to libx265 when NVENC is unavailable or errors."""
    cfg = PipelineConfig()
    renderer = NvencRenderer(cfg)
    renderer.nvenc_available = False  # Simulating hardware unavailable

    cut_timestamps = [0.0, 1.5, 3.0]
    soundtrack = Path("e:/music/bgm.wav")
    output_mp4 = tmp_path / "master_cpu.mp4"

    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = []
    mock_process.stderr = []

    with patch("subprocess.Popen", return_value=mock_process) as mock_run:
        output_mp4.touch()
        res = renderer.render_sequence(
            scored_scenes=sample_scenes,
            cut_timestamps=cut_timestamps,
            soundtrack_path=soundtrack,
            output_file=output_mp4,
        )

        assert res.encoder_used == "libx265"
        called_cmd = mock_run.call_args[0][0]
        assert "libx265" in called_cmd


def test_nvenc_renderer_with_teaser(tmp_path, sample_scenes):
    """Verify teaser clips are included in the filter complex script when teaser is enabled."""
    cfg = PipelineConfig(enable_teaser=True, teaser_duration=4.0)
    renderer = NvencRenderer(cfg)

    # 15s timeline, long enough for teaser
    cut_timestamps = [0.0, 3.0, 6.0, 9.0, 12.0, 15.0]
    soundtrack = Path("e:/music/bgm.wav")
    output_mp4 = tmp_path / "teaser_master.mp4"

    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = []
    mock_process.stderr = []

    captured_content: list[str] = []

    def fake_popen(cmd, **kwargs):
        script_idx = cmd.index("-filter_complex_script") + 1
        script_file = Path(cmd[script_idx])
        if script_file.is_file():
            captured_content.append(script_file.read_text(encoding="utf-8"))
        return mock_process

    with patch("subprocess.Popen", side_effect=fake_popen):
        output_mp4.touch()
        res = renderer.render_sequence(
            scored_scenes=sample_scenes,
            cut_timestamps=cut_timestamps,
            soundtrack_path=soundtrack,
            output_file=output_mp4,
            include_teaser=True,
        )

        assert res.return_code == 0
        assert len(captured_content) == 1
        # Should have concat with multiple clips (teaser + main cuts)
        assert "concat=n=" in captured_content[0]


def test_nvenc_renderer_with_sections(tmp_path, sample_scenes):
    """Verify render_sequence properly utilizes sections for structural alignment."""
    from src.core.types import SongSection, SongSectionType

    cfg = PipelineConfig()
    renderer = NvencRenderer(cfg)

    cut_timestamps = [0.0, 2.0, 4.0]
    soundtrack = Path("e:/music/bgm.wav")
    output_mp4 = tmp_path / "structured_master.mp4"
    sections = [
        SongSection(SongSectionType.INTRO, 0.0, 2.0, 2.0, 0.2, is_climax=False),
        SongSection(SongSectionType.DROP, 2.0, 4.0, 2.0, 0.9, is_climax=True),
    ]

    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = []
    mock_process.stderr = []

    with patch("subprocess.Popen", return_value=mock_process):
        output_mp4.touch()
        res = renderer.render_sequence(
            scored_scenes=sample_scenes,
            cut_timestamps=cut_timestamps,
            soundtrack_path=soundtrack,
            output_file=output_mp4,
            sections=sections,
        )

        assert res.return_code == 0
        assert res.output_path == output_mp4

