"""Unit tests for pipeline configuration, presets, and hardware detection helpers."""

import pytest

from src.core.config import (
    PRESETS,
    PipelineConfig,
    check_cuda_available,
    check_nvenc_available,
)


def test_default_preset_is_1080p30():
    """Verify default preset is 1080p30 with 1920x1080 geometry at 30 fps."""
    config = PipelineConfig()
    assert config.preset_name == "1080p30"
    assert config.canvas_width == 1920
    assert config.canvas_height == 1080
    assert config.fps == 30.0


def test_apply_valid_presets():
    """Test switching across all defined presets."""
    for name, preset in PRESETS.items():
        cfg = PipelineConfig.from_preset(name)
        assert cfg.preset_name == name
        assert cfg.canvas_width == preset.width
        assert cfg.canvas_height == preset.height
        assert cfg.fps == preset.fps


def test_apply_invalid_preset_raises_error():
    """Verify that applying an unknown preset raises ValueError with available list."""
    with pytest.raises(ValueError, match="Unknown preset 'invalid_preset'"):
        PipelineConfig.from_preset("invalid_preset")


def test_hardware_detection_helpers():
    """Check hardware detection functions return boolean types without crashing."""
    cuda_status = check_cuda_available()
    nvenc_status = check_nvenc_available()
    assert isinstance(cuda_status, bool)
    assert isinstance(nvenc_status, bool)
