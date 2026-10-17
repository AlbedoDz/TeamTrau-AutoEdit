"""Unit tests for MediaBrowserWidget (PySide6 Asset Ingestion and Library)."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from src.core.service import StudioBackendAdapter
from src.core.types import VideoMetadata
from src.modules.audio_analyzer import AudioAnalysisResult
from src.ui.app import create_app
from src.ui.widgets.media_browser import MediaBrowserWidget

# Ensure headless Qt execution in CI / test environments
os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


@pytest.fixture
def sample_media_dir(tmp_path: Path):
    """Create directory containing sample video and audio files."""
    footage_dir = tmp_path / "footage"
    footage_dir.mkdir()

    (footage_dir / "clip1.mp4").write_bytes(b"\x00" * 1024)
    (footage_dir / "clip2.mov").write_bytes(b"\x00" * 2048)
    (footage_dir / "readme.txt").write_text("not video", encoding="utf-8")

    soundtrack = tmp_path / "bgm.mp3"
    soundtrack.write_bytes(b"\x00" * 4096)

    return {
        "footage_dir": footage_dir,
        "soundtrack": soundtrack,
    }


def test_media_browser_initial_state(qapp):
    """Verify MediaBrowserWidget initializes with clean default properties."""
    widget = MediaBrowserWidget()
    assert widget.get_input_dir() is None
    assert widget.get_soundtrack_path() is None
    assert widget.get_output_dir() == Path("./output")
    assert len(widget.get_video_files()) == 0
    assert widget.table.rowCount() == 0

    valid, reason = widget.is_valid_setup()
    assert valid is False
    assert "No valid source video" in reason


def test_media_browser_set_input_dir(qapp, sample_media_dir):
    """Verify set_input_dir populates video files in table."""
    widget = MediaBrowserWidget()
    footage = sample_media_dir["footage_dir"]

    widget.set_input_dir(footage)

    assert widget.get_input_dir() == footage
    # Should discover clip1.mp4 and clip2.mov, ignoring readme.txt
    assert len(widget.get_video_files()) == 2
    assert widget.table.rowCount() == 2
    assert widget.table.item(0, 1).text() == "clip1.mp4"
    assert widget.table.item(1, 1).text() == "clip2.mov"


def test_media_browser_set_soundtrack(qapp, sample_media_dir):
    """Verify set_soundtrack_path updates path and probes audio."""
    mock_adapter = MagicMock(spec=StudioBackendAdapter)
    mock_analysis = AudioAnalysisResult(
        tempo_bpm=125.0,
        duration=45.0,
        beats=[],
        cut_timestamps=[0.0, 1.0],
        sample_rate=44100,
        sections=[],
    )
    mock_adapter.quick_analyze_audio.return_value = mock_analysis

    widget = MediaBrowserWidget(backend_adapter=mock_adapter)
    bgm = sample_media_dir["soundtrack"]
    widget.set_soundtrack_path(bgm)

    assert widget.get_soundtrack_path() == bgm
    assert "125.0" in widget.lbl_audio_info.text()
    assert "45.0s" in widget.lbl_audio_info.text()


def test_media_browser_validation_flow(qapp, sample_media_dir):
    """Verify is_valid_setup validates both video clips and audio soundtrack presence."""
    widget = MediaBrowserWidget()
    footage = sample_media_dir["footage_dir"]
    bgm = sample_media_dir["soundtrack"]

    # Only footage
    widget.set_input_dir(footage)
    valid, msg = widget.is_valid_setup()
    assert valid is False
    assert "Soundtrack" in msg

    # Both footage and soundtrack
    widget.set_soundtrack_path(bgm)
    valid, msg = widget.is_valid_setup()
    assert valid is True
    assert msg == "Ready"


def test_media_browser_metadata_probed_slot(qapp, sample_media_dir):
    """Verify _on_metadata_probed slot updates table row with probed specs."""
    widget = MediaBrowserWidget()
    footage = sample_media_dir["footage_dir"]
    clip1 = footage / "clip1.mp4"
    widget.set_input_dir(footage)

    meta = VideoMetadata(
        path=clip1,
        width=3840,
        height=2160,
        fps=60.0,
        duration=8.5,
        total_frames=510,
        codec="hevc",
    )
    widget._on_metadata_probed(meta)

    # clip1 row
    assert widget.table.item(0, 2).text() == "3840x2160"
    assert widget.table.item(0, 3).text() == "60.0"
    assert widget.table.item(0, 4).text() == "8.50s"
    assert widget.table.item(0, 5).text() == "510"
    assert widget.table.item(0, 6).text() == "hevc"
    assert "8.5s" in widget.lbl_status.text()


def test_media_browser_clear_all(qapp, sample_media_dir):
    """Verify clear_all clears table and resets all inputs."""
    widget = MediaBrowserWidget()
    widget.set_input_dir(sample_media_dir["footage_dir"])
    widget.set_soundtrack_path(sample_media_dir["soundtrack"])

    assert widget.table.rowCount() == 2
    widget.clear_all()

    assert widget.table.rowCount() == 0
    assert widget.get_input_dir() is None
    assert widget.get_soundtrack_path() is None
    assert len(widget.get_video_files()) == 0


def test_media_browser_drag_and_drop_folder(qapp, sample_media_dir):
    """Verify drag and drop of a directory URL sets input directory."""
    widget = MediaBrowserWidget()
    footage = sample_media_dir["footage_dir"]

    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(footage))])

    enter_event = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime_data, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget.dragEnterEvent(enter_event)
    assert enter_event.isAccepted()

    drop_event = QDropEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime_data, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(drop_event)

    assert widget.get_input_dir() == footage
    assert len(widget.get_video_files()) == 2
