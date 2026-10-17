"""Unit tests for GPU VRAM sequential eviction and memory headroom guards."""

from unittest.mock import MagicMock, patch

import pytest

from src.core.config import PipelineConfig
from src.core.service import _enforce_vram_headroom
from src.modules.smart_reframer import SmartReframer
from src.modules.transcription_engine import TranscriptionEngine
from src.modules.visual_scorer import VisualScorer


@pytest.fixture
def sample_config() -> PipelineConfig:
    return PipelineConfig()


def test_visual_scorer_unload_model(sample_config: PipelineConfig) -> None:
    """Test that VisualScorer.unload_model releases model references."""
    scorer = VisualScorer(sample_config)
    fake_model = MagicMock()
    fake_transforms = MagicMock()
    scorer._torch_model = fake_model
    scorer._torch_transforms = fake_transforms

    scorer.unload_model()
    assert scorer._torch_model is None
    assert not hasattr(scorer, "_torch_transforms")


def test_smart_reframer_unload_model(sample_config: PipelineConfig) -> None:
    """Test that SmartReframer.unload_model releases YOLO model reference."""
    reframer = SmartReframer(sample_config, model=MagicMock())
    assert reframer._model is not None

    reframer.unload_model()
    assert reframer._model is None


def test_transcription_engine_unload_model(sample_config: PipelineConfig) -> None:
    """Test that TranscriptionEngine.unload_model releases whisper model reference."""
    engine = TranscriptionEngine(sample_config, model=MagicMock())
    assert engine._model is not None

    engine.unload_model()
    assert engine._model is None


def test_enforce_vram_headroom_execution() -> None:
    """Test that _enforce_vram_headroom runs cleanly and invokes GC."""
    with patch("gc.collect") as mock_gc:
        _enforce_vram_headroom(max_allowed_gb=2.0)
        mock_gc.assert_called()
