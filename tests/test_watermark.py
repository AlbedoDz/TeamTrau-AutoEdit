"""Unit tests for Watermark & Branding Engine overlay and positioning."""

import os
from pathlib import Path

import pytest

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene
from src.dispatchers.nvenc_renderer import NvencRenderer
from src.dispatchers.watermark_helper import (
    build_watermark_filter_lines,
    compute_watermark_overlay_expr,
)
from src.ui.app import create_app
from src.ui.widgets.export_panel import ExportPanelWidget

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


def test_compute_watermark_overlay_expr():
    """Verify coordinate expressions across all five standard anchor positions."""
    assert compute_watermark_overlay_expr("top-left", margin=30) == "x=30:y=30"
    assert compute_watermark_overlay_expr("top-right", margin=40) == "x=W-w-40:y=40"
    assert compute_watermark_overlay_expr("bottom-left", margin=25) == "x=25:y=H-h-25"
    assert compute_watermark_overlay_expr("bottom-right", margin=50) == "x=W-w-50:y=H-h-50"
    assert compute_watermark_overlay_expr("center", margin=0) == "x=(W-w)/2:y=(H-h)/2"
    # Fallback to top-right
    assert compute_watermark_overlay_expr("invalid_pos", margin=40) == "x=W-w-40:y=40"


def test_build_watermark_filter_lines():
    """Verify alpha mixer and overlay shortest=1 filter strings."""
    lines = build_watermark_filter_lines(
        wm_input_idx=2,
        base_v_label="vconcat",
        out_v_label="vout",
        position="top-right",
        opacity=0.75,
        margin=40,
    )
    assert len(lines) == 2
    assert "[2:v]format=rgba,colorchannelmixer=aa=0.75[wm_alpha];" in lines[0]
    assert "[vconcat][wm_alpha]overlay=x=W-w-40:y=40:shortest=1[vout];" in lines[1]


def test_nvenc_renderer_watermark_integration(tmp_path: Path):
    """Verify the watermark image is looped as an input and overlaid in each segment."""
    wm_img = tmp_path / "logo.png"
    wm_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

    cfg = PipelineConfig(
        fps=30.0,
        enable_teaser=False,
        watermark_enabled=True,
        watermark_path=wm_img,
        watermark_position="bottom-right",
        watermark_opacity=0.90,
        watermark_margin=50,
    )
    renderer = NvencRenderer(cfg)
    scene = ScoredScene(
        scene=SceneCut(tmp_path / "scene.mp4", 0.0, 5.0, 0, 150, 5.0),
        sharpness_score=0.8,
        aesthetic_score=0.8,
        composite_score=0.8,
    )
    seg = renderer.build_segments([scene], [0.0, 2.0])[0]
    cmd = renderer.build_segment_command("ffmpeg", seg, tmp_path / "s.mkv", False, with_audio=False)

    assert str(wm_img.resolve()) in cmd
    assert cmd[cmd.index(str(wm_img.resolve())) - 3] == "-loop"
    content = cmd[cmd.index("-filter_complex") + 1]
    assert "[vbase]" in content
    assert "colorchannelmixer=aa=0.90" in content
    assert "overlay=x=W-w-50:y=H-h-50" in content


def test_export_panel_watermark_controls(qapp, tmp_path: Path):
    """Verify ExportPanelWidget UI binds watermark and workflow controls into PipelineConfig."""
    panel = ExportPanelWidget()

    # Initially watermark is disabled
    assert panel.chk_watermark.isChecked() is False
    assert panel.edit_watermark_path.isEnabled() is False

    # Toggle watermark on
    panel.chk_watermark.setChecked(True)
    assert panel.edit_watermark_path.isEnabled() is True
    assert panel.combo_wm_pos.isEnabled() is True
    assert panel.spin_wm_opacity.isEnabled() is True

    fake_wm = tmp_path / "logo.png"
    fake_wm.write_bytes(b"PNG")
    panel.edit_watermark_path.setText(str(fake_wm))
    panel.combo_wm_pos.setCurrentText("bottom-left")
    panel.spin_wm_opacity.setValue(0.70)

    # Select Build / Timelapse workflow mode
    panel.combo_workflow.setCurrentIndex(1)

    cfg = PipelineConfig()
    panel.apply_to_config(cfg)

    assert cfg.workflow_mode == "build_timelapse"
    assert cfg.enable_speed_ramping is True
    assert cfg.watermark_enabled is True
    assert cfg.watermark_path == fake_wm.resolve()
    assert cfg.watermark_position == "bottom-left"
    assert cfg.watermark_opacity == 0.70
