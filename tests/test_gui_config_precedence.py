"""Regression tests: UI state -> PipelineConfig must be consistent across panels."""

from unittest.mock import MagicMock

import pytest

from src.core.genre_presets import get_preset_by_key
from src.ui.main_window import StudioMainWindow


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp):
    w = StudioMainWindow()
    yield w
    w.close()


def _select(combo, data) -> None:
    idx = combo.findData(data)
    assert idx >= 0, data
    combo.setCurrentIndex(idx)


def test_inspector_rough_cut_is_not_overridden_by_export_panel(window):
    _select(window.inspector_panel.combo_workflow, "rough_cut")
    cfg = window._build_config()
    assert cfg.workflow_mode == "rough_cut"
    assert cfg.keep_video_audio is True


def test_inspector_build_timelapse_applies_mode_side_effects(window):
    _select(window.inspector_panel.combo_workflow, "build_timelapse")
    cfg = window._build_config()
    assert cfg.workflow_mode == "build_timelapse"
    assert cfg.keep_video_audio is False


def test_vlm_needs_both_inspector_and_preferences(window):
    window.preferences_dialog.chk_enable_vlm.setChecked(True)
    window.inspector_panel.chk_ai_color.setChecked(False)
    assert window._build_config().enable_vlm_tagging is False
    window.inspector_panel.chk_ai_color.setChecked(True)
    window.preferences_dialog.chk_enable_vlm.setChecked(False)
    assert window._build_config().enable_vlm_tagging is False
    window.preferences_dialog.chk_enable_vlm.setChecked(True)
    assert window._build_config().enable_vlm_tagging is True


def test_inspector_watermark_choice_wins(window):
    window.inspector_panel.chk_watermark.setChecked(True)
    window.export_panel.chk_watermark.setChecked(False)
    assert window._build_config().watermark_enabled is True


def test_variants_always_generated_regardless_of_export_scope(window):
    window.export_panel.combo_variant_target.setCurrentIndex(0)
    assert window._build_config().generate_variants == 3


def test_apply_recommended_preset_persists_into_panels(window):
    _select(window.inspector_panel.combo_workflow, "standard")
    preset = get_preset_by_key("rough_cut_assembly")
    window._on_preset_recommended(preset)
    assert window.inspector_panel.combo_workflow.currentData() == "rough_cut"
    assert window._build_config().workflow_mode == "rough_cut"


def test_deliver_uses_full_ui_config_not_defaults(window, tmp_path):
    """Export must honour watermark / NVENC overrides chosen in the panels."""
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    window.inspector_panel.chk_watermark.setChecked(True)
    window.inspector_panel.edit_wm_path.setText(str(logo))
    window.export_panel.grp_nvenc_adv.setChecked(True)
    window.export_panel.spin_nvenc_bitrate.setValue(25)

    from pathlib import Path

    from src.core.types import EditClip, EditTimeline

    tl = EditTimeline("t", 30.0, 1920, 1080, [EditClip(Path("a.mp4"), 0.0, 30, 1.0)])
    window._edit_timelines = {"master": tl}
    window.timeline_view.set_edit_timelines({"master": tl}, active_key="master", tempo_bpm=0.0)
    adapter = MagicMock()
    handle = MagicMock()
    handle.is_running.return_value = False
    handle.result = None
    adapter.submit_deliver_job.return_value = handle
    window.adapter = adapter

    window._on_start_deliver()
    cfg = adapter.submit_deliver_job.call_args.args[2]
    assert cfg.watermark_enabled is True
    assert cfg.watermark_path == logo
    assert cfg.nvenc_bitrate == "25M"
