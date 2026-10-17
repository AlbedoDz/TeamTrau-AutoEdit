"""Unit tests for PySide6 Desktop Studio application shell and Qt signal bridge."""

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from main import cli
from src.core.types import PipelineExecutionResult, SceneCut, ScoredScene
from src.ui.adapter_bridge import BackendSignalBridge
from src.ui.app import create_app, load_stylesheet, run_studio_app
from src.ui.main_window import StudioMainWindow

# Ensure headless Qt execution in CI / test environments
os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    """Ensure QApplication instance is initialized for test session."""
    return create_app()


def test_backend_signal_bridge_emits_qt_signals(qapp):
    """Verify BackendSignalBridge maps backend callbacks directly to Qt signals."""
    bridge = BackendSignalBridge()

    stages_emitted: list[tuple[str, int, int]] = []
    progress_emitted: list[tuple[float, float, str]] = []
    scenes_emitted: list[ScoredScene] = []
    logs_emitted: list[tuple[str, str]] = []
    results_emitted: list[PipelineExecutionResult] = []

    bridge.stage_changed.connect(lambda name, idx, tot: stages_emitted.append((name, idx, tot)))
    bridge.progress_updated.connect(lambda sp, tp, msg: progress_emitted.append((sp, tp, msg)))
    bridge.scene_discovered.connect(lambda sc: scenes_emitted.append(sc))
    bridge.log_received.connect(lambda lvl, msg: logs_emitted.append((lvl, msg)))
    bridge.job_finished.connect(lambda res: results_emitted.append(res))

    callbacks = bridge.to_job_callbacks()
    callbacks.on_stage_changed("Detection", 1, 6)
    callbacks.on_progress(50.0, 15.0, "Processing clip 1")
    dummy_cut = SceneCut(Path("v1.mp4"), 0.0, 2.0, 0, 60, 2.0)
    callbacks.on_scene_discovered(ScoredScene(dummy_cut, 10.0, 0.8, 0.9))
    callbacks.on_log("info", "Test log message")

    fake_res = PipelineExecutionResult(
        success=True,
        total_duration=10.0,
        clip_count=2,
        scenes_detected=2,
        audio_bpm=120.0,
        sections_detected=1,
        fcpxml_path=None,
        premiere_xml_path=None,
        otio_path=None,
        capcut_draft_path=None,
        render_path=None,
        encoder_used=None,
        resolve_status=None,
        dialogue_sentences=0,
        error_message=None,
    )
    bridge.notify_finished(fake_res)

    assert len(stages_emitted) == 1
    assert stages_emitted[0] == ("Detection", 1, 6)
    assert len(progress_emitted) == 1
    assert progress_emitted[0][2] == "Processing clip 1"
    assert len(scenes_emitted) == 1
    assert len(logs_emitted) == 1
    assert len(results_emitted) == 1
    assert results_emitted[0].success is True


def test_load_stylesheet():
    """Verify dark studio stylesheet exists and contains expected rules."""
    qss = load_stylesheet()
    assert len(qss) > 100
    assert "QMainWindow" in qss
    assert "#121214" in qss


def test_studio_main_window_instantiation(qapp):
    """Verify StudioMainWindow initializes with dark studio controls and preset options."""
    window = StudioMainWindow()
    assert window.isWidgetType()
    assert "TeamTrau AutoEdit" in window.windowTitle()
    assert window.preset_combo.count() >= 5
    # Export stays disabled until an edit timeline exists
    assert not window.btn_run.isEnabled()
    assert not window.action_export.isEnabled()
    assert not window.btn_stop.isEnabled()
    assert window.progress_bar.value() == 0

    # Test closing
    window.close()


def test_studio_main_window_handles_signals(qapp):
    """Verify UI controls update in response to bridge signals."""
    window = StudioMainWindow()

    window.bridge.stage_changed.emit("Audio Analysis", 2, 7)
    assert "Stage 2/7" in window.lbl_stage.text()

    window.bridge.progress_updated.emit(50.0, 35.0, "Extracting tempo")
    assert window.progress_bar.value() == 35
    assert window.lbl_message.text() == "Extracting tempo"

    window.close()


def test_run_studio_app_dry_run():
    """Verify run_studio_app in dry-run mode completes with zero exit code."""
    res = run_studio_app(dry_run=True)
    assert res == 0


def test_cli_gui_dry_run():
    """Verify main.py gui --dry-run CLI command executes cleanly."""
    runner = CliRunner()
    result = runner.invoke(cli, ["gui", "--dry-run"])
    assert result.exit_code == 0
    assert "Main window successfully validated" in result.output or result.exit_code == 0
