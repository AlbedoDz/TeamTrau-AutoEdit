"""Unit tests for Variable Frame Rate (VFR) detection and CFR normalization."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.config import PipelineConfig
from src.modules.ingest_detector import IngestDetector


@pytest.fixture
def sample_config() -> PipelineConfig:
    return PipelineConfig(fps=30.0, auto_normalize_vfr=True)


def test_vfr_detection_flag_true_when_rates_differ(tmp_path: Path, sample_config: PipelineConfig) -> None:
    """Test that probe_video flags is_vfr=True when r_frame_rate != avg_frame_rate."""
    detector = IngestDetector(sample_config)
    dummy_video = tmp_path / "vfr_clip.mp4"
    dummy_video.write_bytes(b"dummy video content")

    ffprobe_payload = json.dumps({
        "streams": [
            {
                "r_frame_rate": "30/1",
                "avg_frame_rate": "27.42/1",
            }
        ]
    })

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = ffprobe_payload

    with patch("shutil.which", return_value="C:\\fake\\ffprobe.exe"), \
         patch("subprocess.run", return_value=mock_proc), \
         patch("cv2.VideoCapture") as mock_cap_cls:

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.get.side_effect = lambda prop: {
            3: 1920,   # CAP_PROP_FRAME_WIDTH
            4: 1080,   # CAP_PROP_FRAME_HEIGHT
            5: 27.42,  # CAP_PROP_FPS
            7: 300,    # CAP_PROP_FRAME_COUNT
        }.get(prop, 0.0)
        mock_cap_cls.return_value = mock_cap

        meta = detector.probe_video(dummy_video)
        assert meta.is_vfr is True
        assert meta.width == 1920
        assert meta.height == 1080


def test_vfr_detection_flag_false_when_rates_match(tmp_path: Path, sample_config: PipelineConfig) -> None:
    """Test that probe_video flags is_vfr=False when rates match closely."""
    detector = IngestDetector(sample_config)
    dummy_video = tmp_path / "cfr_clip.mp4"
    dummy_video.write_bytes(b"dummy video content")

    ffprobe_payload = json.dumps({
        "streams": [
            {
                "r_frame_rate": "30000/1001",
                "avg_frame_rate": "30000/1001",
            }
        ]
    })

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = ffprobe_payload

    with patch("shutil.which", return_value="C:\\fake\\ffprobe.exe"), \
         patch("subprocess.run", return_value=mock_proc), \
         patch("cv2.VideoCapture") as mock_cap_cls:

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.get.side_effect = lambda prop: {
            3: 1920,
            4: 1080,
            5: 29.97,
            7: 300,
        }.get(prop, 0.0)
        mock_cap_cls.return_value = mock_cap

        meta = detector.probe_video(dummy_video)
        assert meta.is_vfr is False


def test_normalize_to_cfr_command_arguments(tmp_path: Path, sample_config: PipelineConfig) -> None:
    """Test that normalize_to_cfr executes FFmpeg with exact ultrafast CFR flags."""
    detector = IngestDetector(sample_config)
    input_video = tmp_path / "iphone_vfr.mov"
    input_video.write_bytes(b"iphone raw")
    output_video = tmp_path / "normalized" / "iphone_cfr.mp4"

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stderr = ""

    with patch("shutil.which", return_value="C:\\fake\\ffmpeg.exe"), \
         patch("subprocess.run", return_value=mock_proc) as mock_run:

        result_path = detector.normalize_to_cfr(input_video, target_fps=30.0, output_path=output_video)
        assert result_path == output_video
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "-vsync" in cmd
        assert "cfr" in cmd
        assert "-r" in cmd
        assert "30.0" in cmd
        assert "-preset" in cmd
        assert "ultrafast" in cmd
        assert "-crf" in cmd
        assert "17" in cmd


def test_normalize_to_cfr_cache_hit_skips_subprocess(tmp_path: Path, sample_config: PipelineConfig) -> None:
    """Test that existing non-empty output file reuses cache without invoking FFmpeg."""
    detector = IngestDetector(sample_config)
    input_video = tmp_path / "clip.mp4"
    input_video.write_bytes(b"source")
    cached_output = tmp_path / "cached_cfr.mp4"
    cached_output.write_bytes(b"pre-rendered cfr video")

    with patch("shutil.which", return_value="C:\\fake\\ffmpeg.exe"), \
         patch("subprocess.run") as mock_run:

        res = detector.normalize_to_cfr(input_video, target_fps=30.0, output_path=cached_output)
        assert res == cached_output
        mock_run.assert_not_called()
