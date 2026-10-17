"""Unit tests for SceneGridWidget (Interactive Scene Grid & Visual Quality Dashboard)."""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QMessageBox

from src.core.types import CropWindow, SceneCut, SceneVLMAnalysis, ScoredScene
from src.ui.app import create_app
from src.ui.main_window import StudioMainWindow
from src.ui.widgets.scene_grid import SceneGridWidget, ThumbnailRunnable, ThumbnailWorkerSignals

# Ensure headless Qt execution in CI / test environments
os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


@pytest.fixture
def sample_scenes() -> list[ScoredScene]:
    """Generate mock ScoredScene instances for dashboard testing."""
    cut1 = SceneCut(
        clip_path=Path("footage/drone_beach.mp4"),
        start_time=0.0,
        end_time=4.5,
        start_frame=0,
        end_frame=135,
        duration=4.5,
    )
    vlm1 = SceneVLMAnalysis(
        scene_index=0,
        description="Aerial drone shot of tropical beach",
        tags=["drone", "beach", "ocean"],
        mood="Cinematic",
        suggested_pacing="medium",
        is_key_moment=True,
    )
    scene1 = ScoredScene(
        scene=cut1,
        sharpness_score=85.0,
        aesthetic_score=0.88,
        composite_score=0.86,
        motion_energy=0.35,
        crop_window=CropWindow(100, 0, 1080, 1920, 0.95, "person"),
        vlm_analysis=vlm1,
        metadata={"has_dead_air": False},
    )

    cut2 = SceneCut(
        clip_path=Path("footage/action_chase.mp4"),
        start_time=10.0,
        end_time=13.2,
        start_frame=300,
        end_frame=396,
        duration=3.2,
    )
    scene2 = ScoredScene(
        scene=cut2,
        sharpness_score=72.0,
        aesthetic_score=0.65,
        composite_score=0.68,
        motion_energy=0.82,  # Action drop candidate
        vlm_analysis=None,
        metadata={"has_dead_air": False},
    )

    cut3 = SceneCut(
        clip_path=Path("footage/static_room.mp4"),
        start_time=20.0,
        end_time=28.0,
        start_frame=600,
        end_frame=840,
        duration=8.0,
    )
    scene3 = ScoredScene(
        scene=cut3,
        sharpness_score=35.0,
        aesthetic_score=0.30,
        composite_score=0.32,
        motion_energy=0.05,
        vlm_analysis=None,
        metadata={"has_dead_air": True},  # Dead-air flagged
    )

    return [scene1, scene2, scene3]


def test_scene_grid_initial_state(qapp):
    """Verify SceneGridWidget initializes with clean empty default state."""
    widget = SceneGridWidget()
    assert widget.table.rowCount() == 0
    assert len(widget.get_all_scenes()) == 0
    assert len(widget.get_included_scenes()) == 0
    assert len(widget.get_excluded_scenes()) == 0
    assert "0 total" in widget.lbl_stats.text()
    assert "Select a Scene" in widget.inspector_thumb.text() or widget.inspector_thumb.pixmap() is not None


def test_scene_grid_set_scenes(qapp, sample_scenes):
    """Verify set_scenes populates table rows, stats, and default inclusions."""
    widget = SceneGridWidget()
    widget.set_scenes(sample_scenes)

    assert widget.table.rowCount() == 3
    assert len(widget.get_all_scenes()) == 3
    assert len(widget.get_included_scenes()) == 3
    assert len(widget.get_excluded_scenes()) == 0
    assert "3 total | 3 included" in widget.lbl_stats.text()

    # Verify column 0 checkbox is checked by default
    for row in range(3):
        item = widget.table.item(row, 0)
        assert item is not None
        assert item.checkState() == Qt.CheckState.Checked

    # Verify column 2 text contains clip names
    assert "drone_beach.mp4" in widget.table.item(0, 2).text()
    assert "action_chase.mp4" in widget.table.item(1, 2).text()
    assert "static_room.mp4" in widget.table.item(2, 2).text()

    # Verify Dead-Air status text in col 7
    assert widget.table.item(0, 7).text() == "Active"
    assert widget.table.item(2, 7).text() == "Dead-Air"


def test_scene_grid_manual_inclusion_toggle(qapp, sample_scenes):
    """Verify toggling scene inclusion updates internal state and emits signal."""
    widget = SceneGridWidget()
    widget.set_scenes(sample_scenes)

    toggled_signals: list[tuple[int, bool]] = []
    widget.scene_toggled.connect(lambda idx, inc: toggled_signals.append((idx, inc)))

    # Exclude scene 1
    widget.set_scene_included(1, False)
    assert not widget.is_scene_included(1)
    assert len(widget.get_included_scenes()) == 2
    assert len(widget.get_excluded_scenes()) == 1
    assert widget.get_excluded_scenes()[0] == sample_scenes[1]
    assert len(toggled_signals) == 1
    assert toggled_signals[0] == (1, False)
    assert widget.table.item(1, 0).checkState() == Qt.CheckState.Unchecked

    # Re-include scene 1
    widget.set_scene_included(1, True)
    assert widget.is_scene_included(1)
    assert len(widget.get_included_scenes()) == 3
    assert len(toggled_signals) == 2
    assert toggled_signals[1] == (1, True)


def test_scene_grid_include_exclude_all(qapp, sample_scenes):
    """Verify include_all and exclude_all batch actions."""
    widget = SceneGridWidget()
    widget.set_scenes(sample_scenes)

    widget.exclude_all()
    assert len(widget.get_included_scenes()) == 0
    assert len(widget.get_excluded_scenes()) == 3
    assert "0 included" in widget.lbl_stats.text()

    widget.include_all()
    assert len(widget.get_included_scenes()) == 3
    assert len(widget.get_excluded_scenes()) == 0
    assert "3 included" in widget.lbl_stats.text()


def test_scene_grid_exclude_dead_air(qapp, sample_scenes):
    """Verify exclude_dead_air_scenes flags and excludes dead-air items only."""
    widget = SceneGridWidget()
    widget.set_scenes(sample_scenes)

    widget.exclude_dead_air_scenes()
    # scene 2 (static_room.mp4) has dead-air
    assert widget.is_scene_included(0) is True
    assert widget.is_scene_included(1) is True
    assert widget.is_scene_included(2) is False
    assert len(widget.get_included_scenes()) == 2
    assert len(widget.get_excluded_scenes()) == 1


def test_scene_grid_filtering(qapp, sample_scenes):
    """Verify dynamic filtering by min score, search text, dead-air, and inclusion."""
    widget = SceneGridWidget()
    widget.set_scenes(sample_scenes)

    # 1. Filter by composite score >= 0.70 (only scene 0 should be visible)
    widget.spin_min_score.setValue(0.70)
    assert not widget.table.isRowHidden(0)
    assert widget.table.isRowHidden(1)
    assert widget.table.isRowHidden(2)

    # 2. Reset score filter
    widget.spin_min_score.setValue(0.0)
    assert not widget.table.isRowHidden(1)
    assert not widget.table.isRowHidden(2)

    # 3. Filter by Dead-Air exclusion
    widget.chk_hide_dead_air.setChecked(True)
    assert not widget.table.isRowHidden(0)
    assert not widget.table.isRowHidden(1)
    assert widget.table.isRowHidden(2)
    widget.chk_hide_dead_air.setChecked(False)

    # 4. Search query filter
    widget.edit_search.setText("chase")
    assert widget.table.isRowHidden(0)
    assert not widget.table.isRowHidden(1)
    assert widget.table.isRowHidden(2)

    widget.edit_search.setText("tropical")  # Match VLM description
    assert not widget.table.isRowHidden(0)
    assert widget.table.isRowHidden(1)

    # 5. Reset all filters
    widget.reset_filters()
    assert not widget.table.isRowHidden(0)
    assert not widget.table.isRowHidden(1)
    assert not widget.table.isRowHidden(2)


def test_scene_grid_inspector_selection(qapp, sample_scenes):
    """Verify selecting a row updates inspector card metadata and toggle button."""
    widget = SceneGridWidget()
    widget.set_scenes(sample_scenes)

    selected_scenes: list[ScoredScene | None] = []
    widget.scene_selected.connect(lambda s: selected_scenes.append(s))

    # Select row 0
    widget.table.selectRow(0)
    assert widget._selected_index == 0
    assert len(selected_scenes) == 1
    assert selected_scenes[0] == sample_scenes[0]

    assert "drone_beach.mp4" in widget.lbl_insp_clip.text()
    assert "0.86" in widget.lbl_insp_composite.text()
    assert "KEY MOMENT" in widget.lbl_insp_vlm.text()
    assert "9:16 Crop Window" in widget.lbl_insp_crop.text()
    assert widget.btn_toggle_include.isEnabled()
    assert widget.btn_toggle_include.text() == "Exclude Scene"

    # Click toggle button from inspector
    widget.btn_toggle_include.click()
    assert not widget.is_scene_included(0)
    assert widget.btn_toggle_include.text() == "Include Scene"


def test_scene_grid_add_scene_and_clear(qapp, sample_scenes):
    """Verify add_scene and clear_scenes operations."""
    widget = SceneGridWidget()
    assert widget.table.rowCount() == 0

    widget.add_scene(sample_scenes[0])
    assert widget.table.rowCount() == 1
    assert widget.get_all_scenes()[0] == sample_scenes[0]

    widget.add_scene(sample_scenes[1])
    assert widget.table.rowCount() == 2

    widget.clear_scenes()
    assert widget.table.rowCount() == 0
    assert len(widget.get_all_scenes()) == 0


def test_scene_grid_thumbnail_worker_handling(qapp):
    """Verify ThumbnailRunnable emits failed signal safely when media file is missing."""
    signals = ThumbnailWorkerSignals()
    failed_indices: list[int] = []
    signals.thumbnail_failed.connect(lambda idx: failed_indices.append(idx))

    worker = ThumbnailRunnable(
        scene_idx=42,
        clip_path=Path("non_existent_video.mp4"),
        start_time=0.0,
        end_time=5.0,
        signals=signals,
    )
    worker.run()

    assert len(failed_indices) == 1
    assert failed_indices[0] == 42


def test_scene_grid_thumbnail_ready_slot(qapp, sample_scenes):
    """Verify _on_thumbnail_ready updates cached pixmap and inspector."""
    widget = SceneGridWidget()
    widget.set_scenes(sample_scenes)
    widget.table.selectRow(0)

    pix = QPixmap(96, 54)
    widget._on_thumbnail_ready(0, pix)

    assert 0 in widget._thumbnails
    assert widget._thumbnails[0].width() == 96
    assert widget._thumbnails[0].height() == 54
    assert widget.table.item(0, 1).data(Qt.ItemDataRole.DecorationRole) is not None


def test_main_window_scene_grid_integration(qapp, sample_scenes, monkeypatch):
    """Verify StudioMainWindow integrates SceneGridWidget with discovery and tab views."""
    # Prevent modal QMessageBox from blocking headless test execution
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *args, **kwargs: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )

    window = StudioMainWindow()
    assert hasattr(window, "scene_grid")
    assert isinstance(window.scene_grid, SceneGridWidget)
    assert window.tab_widget.count() >= 3

    # Emit scene_discovered through signal bridge
    window.bridge.scene_discovered.emit(sample_scenes[0])
    assert window.scene_grid.table.rowCount() == 1
    assert "drone_beach.mp4" in window.scene_grid.table.item(0, 2).text()

    # Verify clear cache clears scene grid
    window._on_clear_cache()
    assert window.scene_grid.table.rowCount() == 0

    window._set_dirty(False)
    window.close()

