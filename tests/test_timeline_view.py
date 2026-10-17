"""Unit tests for TimelineViewWidget (Multi-Track Visual Timeline & Beat-Grid Inspector)."""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QMessageBox

from src.core.types import BeatMarker, SceneCut, ScoredScene, SongSection, SongSectionType
from src.modules.audio_analyzer import AudioAnalysisResult
from src.ui.app import create_app
from src.ui.main_window import StudioMainWindow
from src.ui.widgets.timeline_view import TimelineViewWidget, format_timecode

# Ensure headless Qt execution in CI / test environments
os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


@pytest.fixture
def sample_timeline_assets():
    """Create sample scored scenes and audio analysis for timeline tests."""
    cut1 = SceneCut(Path("v1.mp4"), 0.0, 5.0, 0, 150, 5.0)
    cut2 = SceneCut(Path("v2.mp4"), 2.0, 8.0, 60, 240, 6.0)
    cut3 = SceneCut(Path("v3.mp4"), 0.0, 4.0, 0, 120, 4.0)

    scene1 = ScoredScene(cut1, 80.0, 0.85, 0.82, motion_energy=0.30)
    scene2 = ScoredScene(cut2, 90.0, 0.92, 0.91, motion_energy=0.85)
    scene3 = ScoredScene(cut3, 60.0, 0.60, 0.60, motion_energy=0.50)
    scenes = [scene1, scene2, scene3]

    sections = [
        SongSection(SongSectionType.INTRO, 0.0, 4.0, 4.0, avg_energy=0.25),
        SongSection(SongSectionType.BUILDUP, 4.0, 8.0, 4.0, avg_energy=0.55),
        SongSection(SongSectionType.DROP, 8.0, 12.0, 4.0, avg_energy=0.88, is_climax=True),
        SongSection(SongSectionType.OUTRO, 12.0, 15.0, 3.0, avg_energy=0.20),
    ]

    beats = [
        BeatMarker(0.0, 0, 1.0, is_downbeat=True),
        BeatMarker(1.0, 30, 0.8, is_downbeat=False),
        BeatMarker(2.0, 60, 0.7, is_downbeat=False),
        BeatMarker(3.0, 90, 0.7, is_downbeat=False),
        BeatMarker(4.0, 120, 1.0, is_downbeat=True),
        BeatMarker(8.0, 240, 1.0, is_downbeat=True),
    ]

    cuts = [0.0, 3.0, 6.0, 9.0, 12.0, 15.0]

    audio_res = AudioAnalysisResult(
        tempo_bpm=120.0,
        duration=15.0,
        beats=beats,
        cut_timestamps=cuts,
        sample_rate=44100,
        sections=sections,
    )

    return {
        "scenes": scenes,
        "audio_res": audio_res,
    }


def test_format_timecode():
    """Verify timecode string formatting."""
    assert format_timecode(0.0) == "00:00.000"
    assert format_timecode(5.5) == "00:05.500"
    assert format_timecode(65.123) == "01:05.123"
    assert format_timecode(3661.050) == "61:01.050"


def test_timeline_view_initial_state(qapp):
    """Verify clean initial state of TimelineViewWidget."""
    widget = TimelineViewWidget()
    assert widget.canvas.total_duration == 0.0
    assert widget.canvas.playhead_time == 0.0
    assert len(widget.canvas.clips) == 0
    assert widget.canvas.audio_analysis is None
    assert "00:00.000 / 00:00.000" in widget.lbl_timecode.text()


def test_timeline_view_load_timeline_data(qapp, sample_timeline_assets):
    """Verify loading multi-track data builds clips, teaser track, and audio lanes."""
    widget = TimelineViewWidget()
    scenes = sample_timeline_assets["scenes"]
    audio_res = sample_timeline_assets["audio_res"]

    widget.load_timeline_data(
        scored_scenes=scenes,
        audio_result=audio_res,
        include_teaser=True,
        teaser_duration=3.0,
    )

    assert widget.canvas.has_teaser_track is True
    assert widget.canvas.total_duration >= 15.0
    assert len(widget.canvas.clips) >= 6  # 3 teaser clips + 5 slot cuts

    # Verify tracks
    v2_clips = [c for c in widget.canvas.clips if c.track == "V2"]
    v1_clips = [c for c in widget.canvas.clips if c.track == "V1"]
    assert len(v2_clips) == 3
    assert len(v1_clips) == 5

    # Verify timecode label contains BPM
    assert "BPM: 120.0" in widget.lbl_timecode.text()


def test_timeline_view_playhead_and_zoom(qapp, sample_timeline_assets):
    """Verify playhead scrubbing and zoom scale controls."""
    widget = TimelineViewWidget()
    widget.load_timeline_data(sample_timeline_assets["scenes"], sample_timeline_assets["audio_res"])

    emitted_times: list[float] = []
    widget.playhead_changed.connect(lambda t: emitted_times.append(t))

    # Test playhead position
    widget.set_playhead_time(6.5)
    assert widget.canvas.playhead_time == 6.5
    assert "00:06.500" in widget.lbl_timecode.text()

    # Test Zoom
    orig_zoom = widget.canvas.px_per_sec
    widget._on_zoom_in()
    assert widget.canvas.px_per_sec > orig_zoom

    widget._on_zoom_out()
    widget._on_zoom_out()
    assert widget.canvas.px_per_sec < orig_zoom + 10

    # Test Fit to window
    widget.fit_to_window()
    assert widget.canvas.px_per_sec >= 5.0


def test_timeline_view_mouse_interactions(qapp, sample_timeline_assets):
    """Verify mouse clicks select clips and update inspector info."""
    widget = TimelineViewWidget()
    widget.load_timeline_data(sample_timeline_assets["scenes"], sample_timeline_assets["audio_res"])

    selected_clips = []
    widget.clip_selected.connect(lambda c: selected_clips.append(c))

    # Click on the first V1 clip at x = LEFT_MARGIN + 100, y = RULER_HEIGHT + V2_HEIGHT + 20
    canvas = widget.canvas
    click_x = canvas.LEFT_MARGIN + 120
    click_y = canvas.RULER_HEIGHT + canvas.V2_HEIGHT + 20

    event_press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(click_x, click_y),
        QPointF(click_x, click_y),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    canvas.mousePressEvent(event_press)

    assert canvas.selected_clip is not None
    assert "Selected Clip:" in widget.lbl_selected_info.text()
    assert len(selected_clips) == 1

    # Click on A1 track to inspect song section
    click_y_audio = canvas.RULER_HEIGHT + canvas.V2_HEIGHT + canvas.V1_HEIGHT + 20
    event_press_audio = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(click_x, click_y_audio),
        QPointF(click_x, click_y_audio),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    canvas.mousePressEvent(event_press_audio)

    assert canvas.selected_section is not None
    assert "Song Section:" in widget.lbl_selected_info.text()

    # Mouse release
    event_release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        QPointF(click_x, click_y),
        QPointF(click_x, click_y),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    canvas.mouseReleaseEvent(event_release)
    assert not canvas._dragging_playhead


def test_timeline_view_clear(qapp, sample_timeline_assets):
    """Verify clear_timeline cleans canvas and inspector."""
    widget = TimelineViewWidget()
    widget.load_timeline_data(sample_timeline_assets["scenes"], sample_timeline_assets["audio_res"])
    assert len(widget.canvas.clips) > 0

    widget.clear_timeline()
    assert len(widget.canvas.clips) == 0
    assert widget.canvas.total_duration == 0.0
    assert widget.canvas.audio_analysis is None
    assert "Select a clip" in widget.lbl_selected_info.text()


def test_main_window_timeline_view_integration(qapp, sample_timeline_assets, monkeypatch):
    """Verify StudioMainWindow integrates TimelineViewWidget across tabs and reset hooks."""
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *args, **kwargs: None)
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )

    window = StudioMainWindow()
    assert hasattr(window, "timeline_view")
    assert isinstance(window.timeline_view, TimelineViewWidget)
    assert window.tab_widget.count() >= 4

    # Load sample into timeline
    window.timeline_view.load_timeline_data(
        sample_timeline_assets["scenes"],
        sample_timeline_assets["audio_res"],
    )
    assert len(window.timeline_view.canvas.clips) > 0

    # Clear cache hook should clear timeline view
    window._on_clear_cache()
    assert len(window.timeline_view.canvas.clips) == 0

    window._set_dirty(False)
    window.close()
