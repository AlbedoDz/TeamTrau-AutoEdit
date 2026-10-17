"""Media Ingestion and Asset Library Widget for the Desktop Studio."""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.service import StudioBackendAdapter
from src.core.types import VideoMetadata

logger = logging.getLogger(__name__)

SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v"}
SUPPORTED_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac"}


class ProbeSignals(QObject):
    """Signals emitted by background media probing workers."""

    metadata_ready = Signal(object)
    probe_failed = Signal(str, str)


class ProbeRunnable(QRunnable):
    """Background worker probing video file metadata without blocking UI event loop."""

    def __init__(self, file_path: Path, adapter: StudioBackendAdapter) -> None:
        super().__init__()
        self.file_path = file_path
        self.adapter = adapter
        self.signals = ProbeSignals()

    def run(self) -> None:
        try:
            meta = self.adapter.probe_media(self.file_path)
            self.signals.metadata_ready.emit(meta)
        except Exception as e:
            logger.warning("Failed probing %s: %s", self.file_path.name, e)
            self.signals.probe_failed.emit(str(self.file_path), str(e))


class MediaBrowserWidget(QWidget):
    """Media Ingestion & Asset Library Widget with asynchronous metadata probing."""

    media_changed = Signal()

    def __init__(
        self,
        backend_adapter: StudioBackendAdapter | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.adapter = backend_adapter or StudioBackendAdapter()
        self._video_files: list[Path] = []
        self._probed_metadata: dict[Path, VideoMetadata] = {}

        self.setAcceptDrops(True)
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 1. Project Paths GroupBox
        paths_group = QGroupBox("Project Setup & Media Paths", self)
        paths_layout = QVBoxLayout(paths_group)
        paths_layout.setSpacing(8)

        # Input Directory Row
        row_in = QHBoxLayout()
        row_in.addWidget(QLabel("Footage Directory:"))
        self.edit_input_dir = QLineEdit(self)
        self.edit_input_dir.setPlaceholderText("Select folder containing raw camera clips (.mp4, .mov, .mkv)...")
        self.edit_input_dir.textChanged.connect(self._on_input_dir_text_changed)
        row_in.addWidget(self.edit_input_dir, stretch=1)
        btn_browse_in = QPushButton("Browse...", self)
        btn_browse_in.setObjectName("secondaryButton")
        btn_browse_in.clicked.connect(self._browse_input_dir)
        row_in.addWidget(btn_browse_in)
        paths_layout.addLayout(row_in)

        # Soundtrack File Row
        row_audio = QHBoxLayout()
        row_audio.addWidget(QLabel("Soundtrack Audio:"))
        self.edit_soundtrack = QLineEdit(self)
        self.edit_soundtrack.setPlaceholderText("Select background music file (.wav, .mp3, .flac)...")
        self.edit_soundtrack.textChanged.connect(self._on_soundtrack_text_changed)
        row_audio.addWidget(self.edit_soundtrack, stretch=1)
        btn_browse_audio = QPushButton("Browse...", self)
        btn_browse_audio.setObjectName("secondaryButton")
        btn_browse_audio.clicked.connect(self._browse_soundtrack)
        row_audio.addWidget(btn_browse_audio)
        paths_layout.addLayout(row_audio)

        # Output Directory Row
        row_out = QHBoxLayout()
        row_out.addWidget(QLabel("Output Directory:"))
        self.edit_output_dir = QLineEdit(self)
        self.edit_output_dir.setText("./output")
        self.edit_output_dir.textChanged.connect(lambda: self.media_changed.emit())
        row_out.addWidget(self.edit_output_dir, stretch=1)
        btn_browse_out = QPushButton("Browse...", self)
        btn_browse_out.setObjectName("secondaryButton")
        btn_browse_out.clicked.connect(self._browse_output_dir)
        row_out.addWidget(btn_browse_out)
        paths_layout.addLayout(row_out)

        # Soundtrack details summary label
        self.lbl_audio_info = QLabel("Soundtrack: None selected", self)
        self.lbl_audio_info.setStyleSheet("color: #a1a1aa; font-style: italic;")
        paths_layout.addWidget(self.lbl_audio_info)

        layout.addWidget(paths_group)

        # 2. Source Media Library GroupBox
        media_group = QGroupBox("Source Media Library (Drag & Drop Supported)", self)
        media_layout = QVBoxLayout(media_group)
        media_layout.setSpacing(8)

        # Asset Table
        self.table = QTableWidget(self)
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            "#", "Filename", "Resolution", "FPS", "Duration", "Frames", "Codec", "File Size"
        ])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        media_layout.addWidget(self.table)

        # Controls below table
        btn_layout = QHBoxLayout()
        self.lbl_status = QLabel("0 clips loaded (0.0s total footage)", self)
        self.lbl_status.setStyleSheet("color: #a1a1aa; font-weight: bold;")
        btn_layout.addWidget(self.lbl_status)
        btn_layout.addStretch()

        btn_refresh = QPushButton("Refresh Metadata", self)
        btn_refresh.setObjectName("secondaryButton")
        btn_refresh.clicked.connect(self._refresh_library)
        btn_layout.addWidget(btn_refresh)

        btn_remove = QPushButton("Remove Selected", self)
        btn_remove.setObjectName("secondaryButton")
        btn_remove.clicked.connect(self._remove_selected)
        btn_layout.addWidget(btn_remove)

        btn_clear = QPushButton("Clear All", self)
        btn_clear.setObjectName("dangerButton")
        btn_clear.clicked.connect(self.clear_all)
        btn_layout.addWidget(btn_clear)

        media_layout.addLayout(btn_layout)
        layout.addWidget(media_group, stretch=1)

    # -------------------------------------------------------------
    # Drag & Drop Support
    # -------------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        if not urls:
            return

        added_videos: list[Path] = []
        for url in urls:
            path = Path(url.toLocalFile())
            if path.is_dir():
                self.set_input_dir(path)
                return
            elif path.is_file():
                ext = path.suffix.lower()
                if ext in SUPPORTED_AUDIO_EXTS:
                    self.set_soundtrack_path(path)
                elif ext in SUPPORTED_VIDEO_EXTS:
                    added_videos.append(path)

        if added_videos:
            self._add_video_files(added_videos)

        event.acceptProposedAction()

    # -------------------------------------------------------------
    # Path Pickers & Handlers
    # -------------------------------------------------------------
    def _browse_input_dir(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(self, "Select Source Footage Directory")
        if dir_path:
            self.set_input_dir(Path(dir_path))

    def _browse_soundtrack(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Soundtrack Audio File",
            "",
            "Audio Files (*.wav *.mp3 *.flac *.m4a *.aac);;All Files (*.*)",
        )
        if file_path:
            self.set_soundtrack_path(Path(file_path))

    def _browse_output_dir(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if dir_path:
            self.set_output_dir(Path(dir_path))

    def _on_input_dir_text_changed(self, text: str) -> None:
        p = Path(text.strip())
        if p.is_dir():
            self._scan_directory(p)
        self.media_changed.emit()

    def _on_soundtrack_text_changed(self, text: str) -> None:
        p = Path(text.strip())
        if p.is_file():
            self._inspect_audio(p)
        else:
            self.lbl_audio_info.setText("Soundtrack: None selected")
        self.media_changed.emit()

    def set_input_dir(self, path: Path | str) -> None:
        """Set the input directory and automatically scan for video files."""
        p = Path(path)
        self.edit_input_dir.setText(str(p.resolve()))
        self._scan_directory(p)

    def set_soundtrack_path(self, path: Path | str) -> None:
        """Set the soundtrack path and update summary label."""
        p = Path(path)
        self.edit_soundtrack.setText(str(p.resolve()))
        self._inspect_audio(p)

    def set_output_dir(self, path: Path | str) -> None:
        """Set the output directory."""
        p = Path(path)
        self.edit_output_dir.setText(str(p.resolve()))
        self.media_changed.emit()

    def _scan_directory(self, directory: Path) -> None:
        """Scan directory for video files and populate library table."""
        if not directory.is_dir():
            return
        files = [
            f for f in directory.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_VIDEO_EXTS
        ]
        self._video_files = sorted(files, key=lambda f: f.name)
        self._rebuild_table()
        self._start_probing_queue()
        self._update_status()
        self.media_changed.emit()

    def _add_video_files(self, files: list[Path]) -> None:
        """Add individual video files to the library."""
        for f in files:
            if f not in self._video_files:
                self._video_files.append(f)
        self._rebuild_table()
        self._start_probing_queue()
        self._update_status()
        self.media_changed.emit()

    def _inspect_audio(self, audio_path: Path) -> None:
        """Probe audio file metadata and update label."""
        try:
            analysis = self.adapter.quick_analyze_audio(audio_path)
            self.lbl_audio_info.setText(
                f"Soundtrack: {audio_path.name} | "
                f"BPM: {analysis.tempo_bpm:.1f} | "
                f"Duration: {analysis.duration:.1f}s | "
                f"Beats: {len(analysis.beats)} | "
                f"Sections: {len(analysis.sections)}"
            )
        except Exception:
            file_size_mb = audio_path.stat().st_size / (1024 * 1024)
            self.lbl_audio_info.setText(f"Soundtrack: {audio_path.name} ({file_size_mb:.2f} MB)")

    # -------------------------------------------------------------
    # Table & Probing Operations
    # -------------------------------------------------------------
    def _rebuild_table(self) -> None:
        """Rebuild the table rows based on _video_files."""
        self.table.setRowCount(len(self._video_files))
        for idx, file_path in enumerate(self._video_files):
            self.table.setItem(idx, 0, QTableWidgetItem(str(idx + 1)))
            self.table.setItem(idx, 1, QTableWidgetItem(file_path.name))

            meta = self._probed_metadata.get(file_path)
            if meta:
                self.table.setItem(idx, 2, QTableWidgetItem(f"{meta.width}x{meta.height}"))
                self.table.setItem(idx, 3, QTableWidgetItem(f"{meta.fps:.1f}"))
                self.table.setItem(idx, 4, QTableWidgetItem(f"{meta.duration:.2f}s"))
                self.table.setItem(idx, 5, QTableWidgetItem(str(meta.total_frames)))
                self.table.setItem(idx, 6, QTableWidgetItem(meta.codec))
            else:
                self.table.setItem(idx, 2, QTableWidgetItem("Probing..."))
                self.table.setItem(idx, 3, QTableWidgetItem("-"))
                self.table.setItem(idx, 4, QTableWidgetItem("-"))
                self.table.setItem(idx, 5, QTableWidgetItem("-"))
                self.table.setItem(idx, 6, QTableWidgetItem("-"))

            size_mb = file_path.stat().st_size / (1024 * 1024) if file_path.is_file() else 0.0
            self.table.setItem(idx, 7, QTableWidgetItem(f"{size_mb:.2f} MB"))

    def _start_probing_queue(self) -> None:
        """Enqueue background probing tasks for unprobed files."""
        pool = QThreadPool.globalInstance()
        for f in self._video_files:
            if f not in self._probed_metadata:
                task = ProbeRunnable(f, self.adapter)
                task.signals.metadata_ready.connect(self._on_metadata_probed)
                task.signals.probe_failed.connect(self._on_probe_failed)
                pool.start(task)

    @Slot(object)
    def _on_metadata_probed(self, meta: VideoMetadata) -> None:
        """Update table row once metadata probe completes."""
        self._probed_metadata[meta.path] = meta
        if meta.path in self._video_files:
            idx = self._video_files.index(meta.path)
            self.table.setItem(idx, 2, QTableWidgetItem(f"{meta.width}x{meta.height}"))
            self.table.setItem(idx, 3, QTableWidgetItem(f"{meta.fps:.1f}"))
            self.table.setItem(idx, 4, QTableWidgetItem(f"{meta.duration:.2f}s"))
            self.table.setItem(idx, 5, QTableWidgetItem(str(meta.total_frames)))
            self.table.setItem(idx, 6, QTableWidgetItem(meta.codec))
        self._update_status()

    @Slot(str, str)
    def _on_probe_failed(self, path_str: str, err: str) -> None:
        p = Path(path_str)
        if p in self._video_files:
            idx = self._video_files.index(p)
            self.table.setItem(idx, 2, QTableWidgetItem("Error"))

    def _refresh_library(self) -> None:
        self._probed_metadata.clear()
        self._rebuild_table()
        self._start_probing_queue()

    def _remove_selected(self) -> None:
        selected_rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()), reverse=True)
        for row in selected_rows:
            if 0 <= row < len(self._video_files):
                del self._video_files[row]
        self._rebuild_table()
        self._update_status()
        self.media_changed.emit()

    def clear_all(self) -> None:
        """Clear all loaded assets and inputs."""
        self._video_files.clear()
        self._probed_metadata.clear()
        self.edit_input_dir.clear()
        self.edit_soundtrack.clear()
        self.table.setRowCount(0)
        self.lbl_audio_info.setText("Soundtrack: None selected")
        self._update_status()
        self.media_changed.emit()

    def _update_status(self) -> None:
        total_dur = sum(
            self._probed_metadata[f].duration
            for f in self._video_files
            if f in self._probed_metadata
        )
        self.lbl_status.setText(
            f"{len(self._video_files)} clips loaded ({total_dur:.1f}s total probed footage)"
        )

    # -------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------
    def get_input_dir(self) -> Path | None:
        text = self.edit_input_dir.text().strip()
        return Path(text) if text and Path(text).is_dir() else None

    def get_soundtrack_path(self) -> Path | None:
        text = self.edit_soundtrack.text().strip()
        return Path(text) if text and Path(text).is_file() else None

    def get_output_dir(self) -> Path:
        text = self.edit_output_dir.text().strip()
        return Path(text) if text else Path("./output")

    def get_video_files(self) -> list[Path]:
        return list(self._video_files)

    def is_valid_setup(self) -> tuple[bool, str]:
        """Validate if current selections are sufficient to run the editing pipeline."""
        if not self.get_input_dir() and not self._video_files:
            return False, "No valid source video directory or clips loaded."
        if not self._video_files and self.get_input_dir():
            files = [
                f for f in self.get_input_dir().iterdir()
                if f.is_file() and f.suffix.lower() in SUPPORTED_VIDEO_EXTS
            ]
            if not files:
                return False, f"No supported video clips found in '{self.get_input_dir()}'."
        if not self.get_soundtrack_path():
            return False, "Soundtrack audio file must be selected."
        return True, "Ready"
