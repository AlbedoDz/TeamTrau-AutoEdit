"""Unit tests for ExportPanelWidget (NLE Export Dispatcher & Live NVENC Render Monitor)."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.config import PipelineConfig
from src.core.types import PipelineExecutionResult
from src.ui.app import create_app
from src.ui.main_window import StudioMainWindow
from src.ui.widgets.export_panel import (
    ExportPanelWidget,
    find_default_capcut_draft_dir,
    open_path_in_os,
)

# Ensure headless Qt execution in CI / test environments
os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


def test_find_default_capcut_draft_dir(monkeypatch, tmp_path):
    """Verify CapCut draft directory discovery logic."""
    fake_localappdata = tmp_path / "AppData" / "Local"
    fake_capcut = fake_localappdata / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    fake_capcut.mkdir(parents=True)

    monkeypatch.setenv("LOCALAPPDATA", str(fake_localappdata))
    detected = find_default_capcut_draft_dir()
    assert detected == fake_capcut


def test_open_path_in_os(monkeypatch, tmp_path):
    """Verify open_path_in_os returns False for missing files and executes safely for valid files."""
    assert open_path_in_os(Path("non_existent_path_xyz")) is False

    valid_file = tmp_path / "test.txt"
    valid_file.write_text("hello", encoding="utf-8")

    # Mock os.startfile on Windows or subprocess.Popen
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", lambda p: None)
    else:
        monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: None)

    assert open_path_in_os(valid_file) is True


def test_export_panel_initial_state(qapp):
    """Verify ExportPanelWidget initializes with correct NLE checkboxes and zero progress."""
    panel = ExportPanelWidget()

    assert panel.chk_fcpxml.isChecked() is True
    assert panel.chk_premiere.isChecked() is True
    assert panel.chk_otio.isChecked() is True
    assert panel.chk_capcut.isChecked() is True
    assert panel.chk_enable_render.isChecked() is True
    assert panel.chk_enable_teaser.isChecked() is True
    assert panel.spin_teaser_dur.value() == 6.0
    assert panel.chk_resolve_dispatch.isChecked() is False
    assert panel.chk_dry_run_resolve.isChecked() is False

    assert panel.render_progress.value() == 0
    assert panel.btn_play_master.isEnabled() is False
    assert panel.btn_open_capcut.isEnabled() is False
    assert "Idle" in panel.lbl_gauge_status.text()


def test_export_panel_options_and_apply_to_config(qapp, tmp_path):
    """Verify toggling export options updates config correctly."""
    panel = ExportPanelWidget()

    # Modify some selections
    panel.chk_capcut.setChecked(False)
    panel.chk_enable_teaser.setChecked(True)
    panel.spin_teaser_dur.setValue(4.5)

    options = panel.get_export_options()
    assert options["enable_capcut"] is False
    assert options["enable_teaser"] is True
    assert options["teaser_duration"] == 4.5
    assert options["enable_render"] is True

    cfg = PipelineConfig()
    panel.apply_to_config(cfg)
    assert cfg.enable_capcut_export is False
    assert cfg.enable_teaser is True
    assert cfg.teaser_duration == 4.5


def test_export_panel_render_progress_parsing(qapp):
    """Verify live message parsing extracts frame numbers and updates gauges."""
    panel = ExportPanelWidget()

    panel.update_render_progress(42.5, "Rendering frame 128/300 (42.5%)")
    assert panel.render_progress.value() == 42
    assert "Frame: 128 / 300" in panel.lbl_gauge_frame.text()
    assert "Rendering" in panel.lbl_gauge_status.text()

    panel.update_render_progress(99.0, "Finalizing output container...")
    assert "Finalizing" in panel.lbl_gauge_status.text()

    panel.update_render_progress(100.0, "Master video render complete")
    assert "Complete" in panel.lbl_gauge_status.text()

    panel.reset_render_monitor()
    assert panel.render_progress.value() == 0
    assert "Starting" in panel.lbl_gauge_status.text()


def test_export_panel_set_deliverables(qapp, tmp_path):
    """Verify set_deliverables updates labels and enables action buttons."""
    panel = ExportPanelWidget()

    fake_render = tmp_path / "master_1080p30.mp4"
    fake_render.write_bytes(b"\x00" * 1024)

    fake_capcut = tmp_path / "capcut_draft"
    fake_capcut.mkdir()

    success_result = PipelineExecutionResult(
        success=True,
        total_duration=12.5,
        clip_count=4,
        scenes_detected=6,
        audio_bpm=120.0,
        sections_detected=2,
        fcpxml_path=tmp_path / "timeline.fcpxml",
        premiere_xml_path=tmp_path / "timeline.xml",
        otio_path=tmp_path / "timeline.otio",
        capcut_draft_path=fake_capcut,
        render_path=fake_render,
        encoder_used="hevc_nvenc",
        resolve_status=None,
    )

    panel.set_deliverables(success_result)
    assert "master_1080p30.mp4" in panel.lbl_deliv_master.text()
    assert "timeline.fcpxml" in panel.lbl_deliv_fcpxml.text()
    assert "capcut_draft" in panel.lbl_deliv_capcut.text()
    assert panel.btn_play_master.isEnabled() is True
    assert panel.btn_open_capcut.isEnabled() is True
    assert panel.render_progress.value() == 100

    # Test failure payload
    fail_result = PipelineExecutionResult(
        success=False,
        total_duration=0.0,
        clip_count=0,
        scenes_detected=0,
        audio_bpm=0.0,
        sections_detected=0,
        fcpxml_path=None,
        premiere_xml_path=None,
        otio_path=None,
        capcut_draft_path=None,
        render_path=None,
        encoder_used=None,
        resolve_status=None,
        error_message="CUDA out of memory",
    )
    panel.set_deliverables(fail_result)
    assert "Failed" in panel.lbl_gauge_status.text()
    assert "CUDA out of memory" in panel.lbl_render_message.text()


def test_export_panel_action_clicks(qapp, tmp_path, monkeypatch):
    """Verify clicking open action buttons invokes system path opener."""
    panel = ExportPanelWidget()
    opened_paths: list[Path] = []
    monkeypatch.setattr("src.ui.widgets.export_panel.open_path_in_os", lambda p: opened_paths.append(p) or True)

    fake_render = tmp_path / "master.mp4"
    fake_render.write_bytes(b"\x00")
    fake_capcut = tmp_path / "draft"
    fake_capcut.mkdir()

    res = PipelineExecutionResult(
        success=True,
        total_duration=5.0,
        clip_count=2,
        scenes_detected=2,
        audio_bpm=120.0,
        sections_detected=1,
        fcpxml_path=tmp_path / "t.fcpxml",
        premiere_xml_path=None,
        otio_path=None,
        capcut_draft_path=fake_capcut,
        render_path=fake_render,
        encoder_used="hevc_nvenc",
        resolve_status=None,
    )
    panel.set_deliverables(res)

    panel.btn_open_folder.click()
    assert len(opened_paths) == 1
    assert opened_paths[0] == tmp_path

    panel.btn_play_master.click()
    assert len(opened_paths) == 2
    assert opened_paths[1] == fake_render

    panel.btn_open_capcut.click()
    assert len(opened_paths) == 3
    assert opened_paths[2] == fake_capcut


def test_main_window_export_panel_integration(qapp, tmp_path, monkeypatch):
    """Verify StudioMainWindow integrates ExportPanelWidget into tabs and pipeline execution."""
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *args, **kwargs: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *args, **kwargs: None)

    window = StudioMainWindow()
    assert hasattr(window, "export_panel")
    assert isinstance(window.export_panel, ExportPanelWidget)
    assert window.tab_widget.count() >= 5

    # Mock adapter.submit_job to test option transfer
    mock_adapter = MagicMock()
    mock_handle = MagicMock()
    mock_handle.is_running.return_value = False
    mock_handle.result = None
    mock_adapter.submit_job.return_value = mock_handle
    window.adapter = mock_adapter

    # Setup dummy footage in media browser
    footage_dir = tmp_path / "footage"
    footage_dir.mkdir()
    (footage_dir / "clip1.mp4").write_bytes(b"\x00" * 1024)
    soundtrack = tmp_path / "bgm.mp3"
    soundtrack.write_bytes(b"\x00" * 2048)

    window.media_browser.edit_input_dir.setText(str(footage_dir))
    window.media_browser.edit_soundtrack.setText(str(soundtrack))
    window.media_browser._video_files = [footage_dir / "clip1.mp4"]

    # Toggle force_cpu on export panel
    window.export_panel.chk_force_cpu.setChecked(True)
    window.export_panel.chk_enable_render.setChecked(True)

    window._on_start_pipeline()

    # Verify submit_job was called with force_cpu=True
    assert mock_adapter.submit_job.called
    call_kwargs = mock_adapter.submit_job.call_args.kwargs
    assert call_kwargs["force_cpu"] is True
    assert call_kwargs["no_render"] is False

    window.close()


def test_export_panel_variant_selection(qapp, tmp_path):
    """Verify variant target selector maps to config and renders variant labels."""
    from src.core.types import TimelineVariant

    panel = ExportPanelWidget()
    assert panel.combo_variant_target.currentIndex() == 0
    assert panel.get_export_options()["variant_export_scope"] == "active"

    panel.combo_variant_target.setCurrentIndex(1)
    assert panel.get_export_options()["variant_export_scope"] == "all"

    cfg = PipelineConfig()
    panel.apply_to_config(cfg)
    assert cfg.generate_variants == 3

    # Test load_from_config
    cfg2 = PipelineConfig(generate_variants=1)
    panel.load_from_config(cfg2)
    assert panel.combo_variant_target.currentIndex() == 0

    # Test set_deliverables with variants
    var_a = TimelineVariant(
        name="beat_driven",
        display_name="Variant 1: Beat-Driven",
        total_duration=10.0,
        clip_count=4,
    )
    var_b = TimelineVariant(
        name="cinematic",
        display_name="Variant 2: Cinematic",
        total_duration=15.0,
        clip_count=3,
    )
    result = PipelineExecutionResult(
        success=True,
        total_duration=10.0,
        clip_count=4,
        scenes_detected=4,
        audio_bpm=128.0,
        sections_detected=2,
        fcpxml_path=None,
        premiere_xml_path=None,
        otio_path=None,
        capcut_draft_path=None,
        render_path=None,
        encoder_used=None,
        resolve_status=None,
        timeline_variants={"beat_driven": var_a, "cinematic": var_b},
        active_variant_name="beat_driven",
    )
    panel.show()
    panel.set_deliverables(result)
    assert not panel.lbl_deliv_variants.isHidden()
    assert "Beat-Driven" in panel.lbl_deliv_variants.text()
    assert "Cinematic" in panel.lbl_deliv_variants.text()

