"""Unit tests for StudioMainWindow professional NLE File & Edit menus, UndoStack, and Kaizen workflow."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from src.core.types import SceneCut, ScoredScene
from src.ui.app import create_app
from src.ui.main_window import StudioMainWindow
from src.ui.widgets.timeline_view import TimelineClipItem

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


@pytest.fixture
def isolated_studio(qapp, tmp_path, monkeypatch):
    """Create StudioMainWindow isolated from real disk paths."""
    recent_path = tmp_path / "isolated_recents.json"
    monkeypatch.setattr(
        "src.ui.controllers.project_manager.DEFAULT_RECENT_PROJECTS_PATH",
        recent_path,
    )
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *a, **k: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *a, **k: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.critical", lambda *a, **k: None)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.Discard,
    )

    window = StudioMainWindow()
    yield window, tmp_path
    window.close()


def test_file_menu_actions_and_shortcuts(isolated_studio):
    """Verify standard NLE File Menu actions and shortcuts exist."""
    window, _ = isolated_studio
    assert window.action_new.shortcut().toString() == "Ctrl+N"
    assert window.action_open.shortcut().toString() == "Ctrl+O"
    assert window.action_save.shortcut().toString() == "Ctrl+S"
    assert window.action_save_as.shortcut().toString() == "Ctrl+Shift+S"
    assert window.action_import_folder.shortcut().toString() == "Ctrl+I"
    assert window.action_import_audio.shortcut().toString() == "Ctrl+Shift+I"
    assert window.action_export.shortcut().toString() == "Ctrl+M"
    assert window.action_exit.shortcut().toString() == "Ctrl+Q"


def test_edit_menu_actions_and_undo_stack(isolated_studio):
    """Verify standard NLE Edit Menu actions, UndoStack, and clip editing."""
    window, _ = isolated_studio
    assert window.action_undo.shortcut().toString() == "Ctrl+Z"
    assert window.action_redo.shortcut().toString() == "Ctrl+Y"
    assert window.action_cut.shortcut().toString() == "Ctrl+X"
    assert window.action_copy.shortcut().toString() == "Ctrl+C"
    assert window.action_paste.shortcut().toString() == "Ctrl+V"
    assert window.action_delete.shortcut().toString() == "Del"

    # Initially empty undo stack
    assert window.undo_stack.canUndo() is False
    assert window.undo_stack.canRedo() is False

    # Add dummy clip to timeline
    clip = TimelineClipItem(
        clip_id="TEST_CLIP_1",
        name="Clip 1",
        track="V1",
        start_time=0.0,
        end_time=5.0,
        duration=5.0,
        source_path=Path("clip1.mp4"),
        source_start=0.0,
        source_end=5.0,
    )
    window.timeline_view.canvas.clips = [clip]
    window.timeline_view.canvas.selected_clip = clip

    # 1. Copy clip
    window._on_copy_clip()
    assert window._clipboard_clip is not None
    assert window._clipboard_clip.name == "Clip 1"

    # 2. Delete clip with Undo
    window._on_delete_clip()
    assert len(window.timeline_view.canvas.clips) == 0
    assert window.undo_stack.canUndo() is True

    # 3. Undo delete
    window.undo_stack.undo()
    assert len(window.timeline_view.canvas.clips) == 1
    assert window.undo_stack.canRedo() is True

    # 4. Redo delete
    window.undo_stack.redo()
    assert len(window.timeline_view.canvas.clips) == 0

    # 5. Paste clip with Undo
    window.player_widget.current_time = 10.0
    window._on_paste_clip()
    assert len(window.timeline_view.canvas.clips) == 1
    assert window.timeline_view.canvas.clips[0].start_time == 10.0

    window.undo_stack.undo()
    assert len(window.timeline_view.canvas.clips) == 0


def test_scene_toggle_undo_redo(isolated_studio):
    """Verify SceneToggleCommand pushes to undo stack and reverts curation accurately."""
    window, _ = isolated_studio
    sc = SceneCut(Path("v1.mp4"), 0.0, 4.0, 0, 120, 4.0)
    scored = ScoredScene(sc, 80.0, 0.8, 0.8)
    window.scene_grid.add_scene(scored)

    assert window.scene_grid.is_scene_included(0) is True

    # Exclude scene #0 via hotkey/command
    window.scene_grid.table.setCurrentCell(0, 0)
    window._on_shortcut_scene_action("reject")
    assert window.scene_grid.is_scene_included(0) is False
    assert window.undo_stack.canUndo() is True

    # Undo
    window.undo_stack.undo()
    assert window.scene_grid.is_scene_included(0) is True

    # Redo
    window.undo_stack.redo()
    assert window.scene_grid.is_scene_included(0) is False


def test_project_save_open_and_recent_workflow(isolated_studio, tmp_path):
    """Verify saving a complete project session to .tteproj and fully restoring workspace state."""
    from src.core.types import (
        BeatMarker,
        SceneCut,
        ScoredScene,
        SongSection,
        SongSectionType,
        TimelineVariant,
    )
    from src.modules.audio_analyzer import AudioAnalysisResult

    window, _ = isolated_studio

    window.edit_project_name.setText("Summer Travel Vlog")
    window.media_browser.edit_input_dir.setText(str(tmp_path))

    # Setup scored scene in review grid
    cut = SceneCut(tmp_path / "clip_a.mp4", 0.0, 4.0, 0, 120, 4.0)
    scored = ScoredScene(cut, sharpness_score=88.0, aesthetic_score=0.85, composite_score=0.86, camera_role="drone")
    window.scene_grid.set_scenes([scored])

    # Setup audio analysis and variant on timeline
    audio_res = AudioAnalysisResult(
        tempo_bpm=124.0,
        duration=30.0,
        sample_rate=44100,
        cut_timestamps=[0.0, 2.0, 4.0],
        beats=[BeatMarker(2.0, 60, 0.9, True)],
        sections=[SongSection(SongSectionType.INTRO, 0.0, 10.0, 10.0, 0.4)],
    )
    window.timeline_view.canvas.audio_analysis = audio_res

    var = TimelineVariant(
        name="beat_driven",
        display_name="Variant 1: Beat-Driven",
        total_duration=4.0,
        clip_count=1,
        sequence_scenes=[scored],
    )
    window.timeline_view.set_variants({"beat_driven": var}, active_key="beat_driven")

    # Mark in / out and playhead
    window.player_widget.current_time = 2.5
    window._in_point = 1.0
    window._out_point = 3.5

    save_path = tmp_path / "vlog.tteproj"
    ok = window._do_save(save_path)
    assert ok is True
    assert save_path.exists()
    assert window.project_manager.is_dirty is False
    assert "vlog.tteproj" in [p.name for p in window.project_manager.get_recent_projects()]

    # Reset with new project
    window._on_new_project()
    assert window.edit_project_name.text() == "Untitled Project"
    assert window.project_manager.current_project_path is None
    assert len(window.scene_grid.get_all_scenes()) == 0

    # Load back
    loaded = window._do_open(save_path)
    assert loaded is True
    assert window.edit_project_name.text() == "Summer Travel Vlog"
    assert window.project_manager.current_project_path == save_path.resolve()

    # Verify complete session state restored
    assert len(window.scene_grid.get_all_scenes()) == 1
    assert window.scene_grid.get_all_scenes()[0].scene.clip_path == tmp_path / "clip_a.mp4"
    assert window.scene_grid.get_all_scenes()[0].camera_role == "drone"
    assert window._in_point == 1.0
    assert window._out_point == 3.5
    assert abs(window.player_widget.current_time - 2.5) < 0.1
    assert "beat_driven" in window.timeline_view.get_variants()
    assert window.timeline_view.get_active_variant_key() == "beat_driven"
    assert window.timeline_view.canvas.audio_analysis is not None
    assert window.timeline_view.canvas.audio_analysis.tempo_bpm == 124.0


def test_kaizen_workflow_stepper_and_quick_buttons(isolated_studio):
    """Verify Kaizen 4-step workflow indicator updates through lifecycle."""
    window, _ = isolated_studio

    # Step 1: Ingest active
    window._update_workflow_stepper(1)
    assert "● 1. Ingest" in window.lbl_workflow_steps.text()

    # Step 2: Media loaded -> AI Edit active
    window._update_workflow_stepper(2)
    assert "✔ 1. Ingest" in window.lbl_workflow_steps.text()
    assert "● 2. AI Edit" in window.lbl_workflow_steps.text()

    # Step 3: Pipeline completed -> NLE Refine active
    window._update_workflow_stepper(3)
    assert "✔ 2. AI Edit" in window.lbl_workflow_steps.text()
    assert "● 3. NLE Refine" in window.lbl_workflow_steps.text()

    # Step 4: Export dialog active
    window._update_workflow_stepper(4)
    assert "✔ 3. NLE Refine" in window.lbl_workflow_steps.text()
    assert "● 4. Export" in window.lbl_workflow_steps.text()

    # Quick Topbar buttons
    assert window.btn_quick_open.text() == "📁 Open"
    assert window.btn_quick_save.text() == "💾 Save"
