"""Unit tests for InspectorPanelWidget, PlayerWidget, and ExportDialog."""

import os
from pathlib import Path

import pytest

from src.core.config import PipelineConfig
from src.ui.app import create_app
from src.ui.widgets.export_dialog import ExportDialog
from src.ui.widgets.inspector_panel import InspectorPanelWidget
from src.ui.widgets.player_widget import PlayerWidget, format_smpte_timecode

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


def test_format_smpte_timecode():
    """Verify SMPTE timecode calculation."""
    assert format_smpte_timecode(0.0, 30.0) == "00:00:00:00"
    assert format_smpte_timecode(1.0, 30.0) == "00:00:01:00"
    assert format_smpte_timecode(65.5, 30.0) == "00:01:05:15"
    assert format_smpte_timecode(3600.0, 30.0) == "01:00:00:00"


def test_player_widget_transport_and_shuttle(qapp):
    """Verify PlayerWidget transport controls, stepping, and shuttle rates."""
    player = PlayerWidget()
    player.set_duration(120.0)
    player.set_fps(30.0)

    time_updates = []
    player.time_changed.connect(lambda t: time_updates.append(t))

    # Seek
    player.seek_time(10.0)
    assert player.current_time == 10.0
    assert len(time_updates) == 1

    # Step frame
    player.step_frame(1)
    assert abs(player.current_time - (10.0 + (1.0 / 30.0))) < 1e-4

    player.step_frame(-1)
    assert abs(player.current_time - 10.0) < 1e-4

    # Step seconds
    player.step_seconds(5.0)
    assert abs(player.current_time - 15.0) < 1e-4

    # Play/Pause toggle
    assert not player.is_playing
    player.toggle_play_pause()
    assert player.is_playing
    assert player.btn_play_pause.text() == "⏸"

    player.toggle_play_pause()
    assert not player.is_playing
    assert player.btn_play_pause.text() == "▶"

    # Shuttle rate
    player.set_shuttle_rate(4.0)
    assert player.shuttle_rate == 4.0
    assert player.is_playing
    assert "4.0x" in player.lbl_rate.text()

    player.set_shuttle_rate(0.0)
    assert not player.is_playing
    assert "1.0x" in player.lbl_rate.text()


def test_player_widget_vertical_hud_toggle(qapp):
    """Verify vertical 9:16 guide overlay mode."""
    player = PlayerWidget()
    assert player.canvas.is_vertical is True

    player.set_vertical_mode(False)
    assert player.canvas.is_vertical is False

    player.set_vertical_mode(True)
    assert player.canvas.is_vertical is True


def test_inspector_panel_config_sync(qapp):
    """Verify InspectorPanelWidget transfers and reflects configuration parameters."""
    panel = InspectorPanelWidget()

    cfg = PipelineConfig()
    cfg.workflow_mode = "build_timelapse"
    cfg.duration_mode = "footage_driven"
    cfg.target_duration = 90.0
    cfg.enable_speed_ramping = True
    cfg.max_speed_multiplier = 4.0
    cfg.watermark_enabled = True
    cfg.watermark_path = Path("test_logo.png")
    cfg.watermark_position = "bottom-right"
    cfg.watermark_opacity = 0.70
    cfg.watermark_margin = 25
    cfg.enable_teaser = False
    cfg.teaser_duration = 4.0

    panel.load_from_config(cfg)

    assert panel.combo_workflow.currentData() == "build_timelapse"
    assert panel.combo_dur_mode.currentData() == "footage_driven"
    assert panel.slider_duration.value() == 90
    assert panel.chk_speed_ramping.isChecked() is True
    assert panel.combo_max_speed.currentData() == 4.0
    assert panel.chk_watermark.isChecked() is True
    assert panel.edit_wm_path.text() == "test_logo.png"
    assert panel.combo_wm_pos.currentData() == "bottom-right"
    assert panel.slider_wm_opacity.value() == 70
    assert panel.spin_wm_margin.value() == 25
    assert panel.chk_teaser.isChecked() is False
    assert panel.spin_teaser_dur.value() == 4

    # Apply back
    target_cfg = PipelineConfig()
    panel.apply_to_config(target_cfg)

    assert target_cfg.workflow_mode == "build_timelapse"
    assert target_cfg.duration_mode == "footage_driven"
    assert target_cfg.target_duration == 90.0
    assert target_cfg.enable_speed_ramping is True
    assert target_cfg.max_speed_multiplier == 4.0
    assert target_cfg.watermark_enabled is True
    assert str(target_cfg.watermark_path) == "test_logo.png"
    assert target_cfg.watermark_position == "bottom-right"
    assert abs(target_cfg.watermark_opacity - 0.70) < 1e-4
    assert target_cfg.watermark_margin == 25
    assert target_cfg.enable_teaser is False
    assert target_cfg.teaser_duration == 4.0

    # Retention controls
    panel.combo_reframing.setCurrentIndex(panel.combo_reframing.findData("blur_backdrop"))
    panel.combo_subtitles.setCurrentIndex(panel.combo_subtitles.findData("clean_srt"))
    panel.chk_exposure_guard.setChecked(False)
    panel.chk_ken_burns.setChecked(False)

    retention_cfg = PipelineConfig()
    panel.apply_to_config(retention_cfg)
    assert retention_cfg.reframing_mode == "blur_backdrop"
    assert retention_cfg.subtitle_style == "clean_srt"
    assert retention_cfg.enable_exposure_guard is False
    assert retention_cfg.enable_ken_burns is False


def test_export_dialog_instantiation(qapp):
    """Verify ExportDialog wraps ExportPanelWidget correctly."""
    dialog = ExportDialog()
    assert dialog.isWidgetType()
    assert dialog.export_panel is not None
    assert dialog.export_panel.chk_fcpxml.isChecked() is True

    started = []
    dialog.start_export_requested.connect(lambda: started.append(True))
    dialog.btn_run_export.click()
    assert len(started) == 1
    assert dialog.btn_run_export.isEnabled() is False


def test_timeline_seek_playhead_alias(qapp):
    """Verify seek_playhead alias methods on canvas and view widget."""
    from src.ui.widgets.timeline_view import TimelineCanvas, TimelineViewWidget

    canvas = TimelineCanvas()
    canvas.total_duration = 100.0
    canvas.seek_playhead(35.5)
    assert abs(canvas.playhead_time - 35.5) < 1e-4

    view = TimelineViewWidget()
    view.canvas.total_duration = 100.0
    view.seek_playhead(42.0)
    assert abs(view.canvas.playhead_time - 42.0) < 1e-4
    assert "00:42.000" in view.lbl_timecode.text()


def test_main_window_playhead_bidirectional_sync(qapp, monkeypatch):
    """Verify bidirectional synchronization between player and timeline without AttributeError."""
    from src.ui.main_window import StudioMainWindow

    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *args, **kwargs: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *args, **kwargs: None)

    window = StudioMainWindow()
    window.timeline_view.canvas.total_duration = 120.0
    window.player_widget.set_duration(120.0)

    # 1. Player time tick/seek -> Timeline playhead
    window.player_widget.seek_time(25.0)
    assert abs(window.timeline_view.canvas.playhead_time - 25.0) < 1e-4

    # 2. Timeline scrub -> Player time
    window.timeline_view.canvas.playhead_moved.emit(60.0)
    assert abs(window.player_widget.current_time - 60.0) < 1e-4

    # 3. Direct slot call must not raise AttributeError
    window._on_player_time_changed(15.0)
    assert abs(window.timeline_view.canvas.playhead_time - 15.0) < 1e-4

    window.close()


def test_pipeline_concurrency_guard(qapp, monkeypatch):
    """Verify StudioMainWindow prevents duplicate concurrent pipeline jobs."""
    import threading

    from src.core.service import JobHandle
    from src.ui.main_window import StudioMainWindow

    info_messages = []
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.information",
        lambda parent, title, text: info_messages.append(text),
    )
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.warning",
        lambda parent, title, text: None,
    )

    window = StudioMainWindow()

    # Simulate an active running job
    active_handle = JobHandle(job_id="job_active_test", cancel_event=threading.Event())
    window._current_job = active_handle
    assert active_handle.is_running() is True

    # Attempting to start pipeline must be blocked by guard
    window._on_start_pipeline()
    assert len(info_messages) == 1
    assert "already actively running" in info_messages[0]

    # Stop cancellation halts cleanly
    window._on_stop_pipeline()
    assert window.btn_stop.isEnabled() is False

    window.close()


def test_player_widget_live_video_preview(qapp, tmp_path):
    """Verify PlayerWidget loads and renders real video frames from source media."""
    import cv2
    import numpy as np

    v_path = tmp_path / "test_preview_source.mp4"
    w, h, fps = 640, 360, 30.0
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(v_path), fourcc, fps, (w, h))
    for f in range(30):
        frame = np.full((h, w, 3), (f * 8, 100, 200), dtype=np.uint8)
        out.write(frame)
    out.release()

    player = PlayerWidget()
    ok = player.load_source_video(v_path)
    assert ok is True
    assert player._mode == "source"
    assert player.canvas.current_pixmap is not None
    assert not player.canvas.current_pixmap.isNull()

    # Seek and verify frame update
    player.seek_time(0.5)
    assert abs(player.current_time - 0.5) < 1e-4
    assert player.canvas.current_pixmap is not None

    # Zoom mode changes
    player.canvas.set_zoom("50%")
    assert player.canvas.zoom_mode == "50%"
    player.canvas.set_zoom("fit")
    assert player.canvas.zoom_mode == "fit"

    # Theater mode toggle
    events = []
    player.theater_mode_toggled.connect(lambda state: events.append(state))
    player._on_toggle_theater_mode()
    assert len(events) == 1
    assert events[0] is True
    assert "Normal" in player.btn_fullscreen.text()

    player.close()


def test_player_widget_timeline_sequence_preview(qapp, tmp_path):
    """Verify PlayerWidget renders across multiple scene cuts on master timeline."""
    import cv2
    import numpy as np

    from src.core.types import SceneCut, ScoredScene

    clip_paths = []
    for idx, col in enumerate([(255, 0, 0), (0, 255, 0)], start=1):
        p = tmp_path / f"clip_{idx}.mp4"
        out = cv2.VideoWriter(str(p), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (320, 240))
        for _ in range(30):
            frame = np.full((240, 320, 3), col, dtype=np.uint8)
            out.write(frame)
        out.release()
        clip_paths.append(p)

    scene1 = ScoredScene(
        scene=SceneCut(clip_path=clip_paths[0], start_frame=0, end_frame=30, start_time=0.0, end_time=1.0, duration=1.0),
        sharpness_score=80.0,
        aesthetic_score=0.8,
        composite_score=0.8,
    )
    scene2 = ScoredScene(
        scene=SceneCut(clip_path=clip_paths[1], start_frame=0, end_frame=30, start_time=0.0, end_time=1.0, duration=1.0),
        sharpness_score=85.0,
        aesthetic_score=0.85,
        composite_score=0.85,
    )

    player = PlayerWidget()
    player.load_timeline_sequence([scene1, scene2], total_duration=2.0)
    assert player._mode == "timeline"
    assert player.duration == 2.0
    assert player.canvas.current_pixmap is not None

    # Scrub to Clip 1
    player.seek_time(0.4)
    assert player.canvas.current_pixmap is not None

    # Scrub to Clip 2
    player.seek_time(1.5)
    assert player.canvas.current_pixmap is not None

    player.close()
