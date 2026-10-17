"""Unit tests for AudioNormalizer and workflow mode configurations."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene
from src.modules.audio_normalizer import AudioNormalizationError, AudioNormalizer


def test_inspect_audio_clean_file(tmp_path: Path):
    """Test inspecting a clean 24-bit WAV file with no BWF offset."""
    fake_wav = tmp_path / "soundtrack.wav"
    fake_wav.write_bytes(b"RIFF" + b"\x00" * 40)

    ffprobe_output = {
        "streams": [
            {
                "codec_name": "pcm_s24le",
                "channels": 2,
                "sample_rate": "48000",
                "bits_per_raw_sample": "24",
            }
        ],
        "format": {"tags": {"time_reference": "0"}},
    }

    normalizer = AudioNormalizer(cache_dir=tmp_path / "cache")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout=json.dumps(ffprobe_output),
            returncode=0,
        )
        info = normalizer.inspect_audio(fake_wav)

        assert info["codec"] == "pcm_s24le"
        assert info["bit_depth"] == 24
        assert info["channels"] == 2
        assert info["sample_rate"] == 48000
        assert info["has_bwf_offset"] is False
        assert info["requires_cleaning"] is False


def test_inspect_audio_dirty_bwf_and_16bit(tmp_path: Path):
    """Test inspecting a dirty 16-bit WAV with BWF time_reference offset."""
    fake_wav = tmp_path / "dirty.wav"
    fake_wav.write_bytes(b"RIFF" + b"\x00" * 40)

    ffprobe_output = {
        "streams": [
            {
                "codec_name": "pcm_s16le",
                "channels": 2,
                "sample_rate": "44100",
                "bits_per_raw_sample": "16",
            }
        ],
        "format": {"tags": {"time_reference": "144000"}},
    }

    normalizer = AudioNormalizer(cache_dir=tmp_path / "cache")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout=json.dumps(ffprobe_output),
            returncode=0,
        )
        info = normalizer.inspect_audio(fake_wav)

        assert info["codec"] == "pcm_s16le"
        assert info["bit_depth"] == 16
        assert info["has_bwf_offset"] is True
        assert info["requires_cleaning"] is True


def test_clean_audio_not_required(tmp_path: Path):
    """If cleaning is not required, returns original file path."""
    fake_wav = tmp_path / "clean.wav"
    fake_wav.write_bytes(b"RIFF" + b"\x00" * 40)

    normalizer = AudioNormalizer(cache_dir=tmp_path / "cache")

    with patch.object(normalizer, "inspect_audio") as mock_inspect:
        mock_inspect.return_value = {
            "requires_cleaning": False,
            "time_reference": None,
            "bit_depth": 24,
        }
        res = normalizer.clean_audio(fake_wav, force_clean=False)
        assert res == fake_wav.resolve()


def test_clean_audio_executes_ffmpeg(tmp_path: Path):
    """If cleaning is required, invokes ffmpeg to clean BWF tags and normalize to 24-bit."""
    fake_wav = tmp_path / "dirty.wav"
    fake_wav.write_bytes(b"RIFF" + b"\x00" * 40)

    cache_dir = tmp_path / "cache"
    normalizer = AudioNormalizer(cache_dir=cache_dir)

    with patch.object(normalizer, "inspect_audio") as mock_inspect, patch("subprocess.run") as mock_run:
        mock_inspect.return_value = {
            "requires_cleaning": True,
            "time_reference": "144000",
            "bit_depth": 16,
        }
        mock_run.return_value = MagicMock(returncode=0)

        cleaned_file = normalizer.clean_audio(fake_wav)

        assert cleaned_file == cache_dir / "dirty_clean_24bit.wav"
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "-map_metadata" in cmd
        assert "-1" in cmd
        assert "+bitexact" in cmd
        assert "pcm_s24le" in cmd


def test_clean_audio_reuses_existing_cache(tmp_path: Path):
    """If cached clean file already exists and is fresh, reuse it without calling ffmpeg."""
    fake_wav = tmp_path / "dirty.wav"
    fake_wav.write_bytes(b"RIFF" + b"\x00" * 40)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached_clean = cache_dir / "dirty_clean_24bit.wav"
    cached_clean.write_bytes(b"RIFF_CLEAN")

    normalizer = AudioNormalizer(cache_dir=cache_dir)

    with patch.object(normalizer, "inspect_audio") as mock_inspect, patch("subprocess.run") as mock_run:
        mock_inspect.return_value = {
            "requires_cleaning": True,
            "time_reference": "144000",
            "bit_depth": 16,
        }
        res = normalizer.clean_audio(fake_wav)
        assert res == cached_clean
        mock_run.assert_not_called()


def test_clean_audio_failure_raises_exception(tmp_path: Path):
    """If ffmpeg fails, raise AudioNormalizationError."""
    fake_wav = tmp_path / "broken.wav"
    fake_wav.write_bytes(b"RIFF" + b"\x00" * 40)

    normalizer = AudioNormalizer(cache_dir=tmp_path / "cache")

    with patch.object(normalizer, "inspect_audio") as mock_inspect, patch("subprocess.run") as mock_run:
        mock_inspect.return_value = {"requires_cleaning": True}
        mock_run.side_effect = subprocess.SubprocessError("FFmpeg error")

        with pytest.raises(AudioNormalizationError):
            normalizer.clean_audio(fake_wav)


def test_workflow_presets():
    """Verify workflow modes apply expected speed ramping, dead air, and audio flags."""
    cfg = PipelineConfig()
    assert cfg.workflow_mode == "standard"
    assert cfg.keep_video_audio is True

    # Build / Timelapse mode
    cfg.apply_workflow_mode("build_timelapse")
    assert cfg.workflow_mode == "build_timelapse"
    assert cfg.enable_speed_ramping is True
    assert cfg.keep_video_audio is False

    # Unboxing / Narration mode
    cfg.apply_workflow_mode("unboxing_narration")
    assert cfg.workflow_mode == "unboxing_narration"
    assert cfg.enable_speed_ramping is False
    assert cfg.keep_video_audio is True
    assert cfg.enable_dead_air_detection is True

    # Standard mode
    cfg.apply_workflow_mode("standard")
    assert cfg.workflow_mode == "standard"
    assert cfg.keep_video_audio is True

    with pytest.raises(ValueError, match="Unknown workflow mode"):
        cfg.apply_workflow_mode("unknown_mode")  # type: ignore


def test_scored_scene_speed_default():
    """Verify ScoredScene has default speed of 1.0."""
    scene = ScoredScene(
        scene=SceneCut(
            clip_path=Path("test.mp4"),
            start_time=0.0,
            end_time=5.0,
            start_frame=0,
            end_frame=150,
            duration=5.0,
        ),
        sharpness_score=0.8,
        aesthetic_score=0.9,
        composite_score=0.85,
    )
    assert scene.speed == 1.0
