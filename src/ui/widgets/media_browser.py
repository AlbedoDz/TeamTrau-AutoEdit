"""Media Ingestion and Asset Library Widget for the Desktop Studio."""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QComboBox,
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

from src.core.ontology import CameraRole, get_role_display_name, infer_camera_role
from src.core.service import StudioBackendAdapter
from src.core.types import VIDEO_EXTENSIONS, VideoMetadata
from src.modules.genre_recommender import GenreRecommendation, GenreRecommender

logger = logging.getLogger(__name__)

SUPPORTED_VIDEO_EXTS = VIDEO_EXTENSIONS
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
    video_selected = Signal(object)
    preset_recommended = Signal(object)
    footage_probed = Signal()  # a probe finished: total footage duration changed

    def __init__(
        self,
        backend_adapter: StudioBackendAdapter | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.adapter = backend_adapter or StudioBackendAdapter()
        self._video_files: list[Path] = []
        self._input_dirs: list[Path] = []
        self._clip_roles: dict[Path, str] = {}
        self._probed_metadata: dict[Path, VideoMetadata] = {}
        self._current_recommendation: GenreRecommendation | None = None

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

        # Input Directory Row (Multi-Folder Capable)
        row_in = QHBoxLayout()
        row_in.addWidget(QLabel("Footage Directory:"))
        self.edit_input_dir = QLineEdit(self)
        self.edit_input_dir.setPlaceholderText("Select folder containing raw camera clips (.mp4, .mov, .mkv, .mxf, .avi, ...)...")
        self.edit_input_dir.textChanged.connect(self._on_input_dir_text_changed)
        row_in.addWidget(self.edit_input_dir, stretch=1)
        btn_browse_in = QPushButton("📁 Browse...", self)
        btn_browse_in.setObjectName("secondaryButton")
        btn_browse_in.setToolTip("Select primary folder containing raw camera clips")
        btn_browse_in.clicked.connect(self._browse_input_dir)
        row_in.addWidget(btn_browse_in)

        btn_add_in = QPushButton("➕ Add Folder...", self)
        btn_add_in.setObjectName("secondaryButton")
        btn_add_in.setToolTip("Queue another camera directory (Drone, ActionCam, Cam B, B-Roll)")
        btn_add_in.clicked.connect(self._add_folder_dialog)
        row_in.addWidget(btn_add_in)
        paths_layout.addLayout(row_in)

        # Soundtrack File Row
        row_audio = QHBoxLayout()
        row_audio.addWidget(QLabel("Soundtrack Audio:"))
        self.edit_soundtrack = QLineEdit(self)
        self.edit_soundtrack.setPlaceholderText("Select background music file (.wav, .mp3, .flac)...")
        self.edit_soundtrack.textChanged.connect(self._on_soundtrack_text_changed)
        row_audio.addWidget(self.edit_soundtrack, stretch=1)
        btn_browse_audio = QPushButton("🎵 Browse...", self)
        btn_browse_audio.setObjectName("secondaryButton")
        btn_browse_audio.setToolTip("Select soundtrack audio file (.wav, .mp3, .flac)")
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
        btn_browse_out = QPushButton("📁 Browse...", self)
        btn_browse_out.setObjectName("secondaryButton")
        btn_browse_out.setToolTip("Select output destination folder")
        btn_browse_out.clicked.connect(self._browse_output_dir)
        row_out.addWidget(btn_browse_out)
        paths_layout.addLayout(row_out)

        # Soundtrack details summary label
        self.lbl_audio_info = QLabel("Soundtrack: None selected", self)
        self.lbl_audio_info.setStyleSheet("color: #a1a1aa; font-style: italic;")
        paths_layout.addWidget(self.lbl_audio_info)

        # AI Genre Recommendation Banner (Phase 3)
        self.recommendation_banner = QWidget(self)
        rec_layout = QHBoxLayout(self.recommendation_banner)
        rec_layout.setContentsMargins(4, 4, 4, 4)
        self.lbl_recommendation = QLabel("💡 AI Recommendation: Waiting for soundtrack...", self)
        self.lbl_recommendation.setStyleSheet("color: #38bdf8; font-weight: bold;")
        self.btn_apply_recommendation = QPushButton("✨ Apply Preset", self)
        self.btn_apply_recommendation.setObjectName("primaryButton")
        self.btn_apply_recommendation.setToolTip("Instantly configure pipeline with recommended genre parameters")
        self.btn_apply_recommendation.setEnabled(False)
        self.btn_apply_recommendation.clicked.connect(self._apply_current_recommendation)
        rec_layout.addWidget(self.lbl_recommendation, stretch=1)
        rec_layout.addWidget(self.btn_apply_recommendation)
        self.recommendation_banner.setVisible(False)
        paths_layout.addWidget(self.recommendation_banner)

        layout.addWidget(paths_group)

        # 2. Source Media Library GroupBox
        media_group = QGroupBox("Source Media Library (Drag & Drop Supported)", self)
        media_layout = QVBoxLayout(media_group)
        media_layout.setSpacing(8)

        # Asset Table
        self.table = QTableWidget(self)
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "#", "Filename", "Role", "Resolution", "FPS", "Duration", "Frames", "Codec", "File Size"
        ])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.table.cellDoubleClicked.connect(self._on_table_cell_double_clicked)
        media_layout.addWidget(self.table)

        # Controls below table
        btn_layout = QHBoxLayout()
        self.lbl_status = QLabel("0 clips loaded (0.0s total footage)", self)
        self.lbl_status.setStyleSheet("color: #a1a1aa; font-weight: bold;")
        btn_layout.addWidget(self.lbl_status)
        btn_layout.addStretch()

        btn_refresh = QPushButton("🔄 Refresh Metadata", self)
        btn_refresh.setObjectName("secondaryButton")
        btn_refresh.setToolTip("Scan folder and probe metadata for all clips")
        btn_refresh.clicked.connect(self._refresh_library)
        btn_layout.addWidget(btn_refresh)

        btn_remove = QPushButton("🗑️ Remove Selected", self)
        btn_remove.setObjectName("secondaryButton")
        btn_remove.setToolTip("Remove selected video clips from list")
        btn_remove.clicked.connect(self._remove_selected)
        btn_layout.addWidget(btn_remove)

        btn_clear = QPushButton("❌ Clear All", self)
        btn_clear.setObjectName("dangerButton")
        btn_clear.setToolTip("Clear all video clips and paths")
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
        dir_path = QFileDialog.getExistingDirectory(self, "Select Primary Source Footage Directory")
        if dir_path:
            self.set_input_dir(Path(dir_path))

    def _add_folder_dialog(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(self, "Select Additional Camera Footage Directory")
        if dir_path:
            self.add_footage_folder(Path(dir_path))

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
            self.recommendation_banner.setVisible(False)
        self.media_changed.emit()

    def set_input_dir(self, path: Path | str) -> None:
        """Set the primary input directory and automatically scan for video files."""
        p = Path(path)
        self.edit_input_dir.setText(str(p.resolve()))
        self._input_dirs = [p]
        self._scan_directory(p)

    def add_footage_folder(self, folder: Path | str, default_role: str | None = None) -> None:
        """Queue an additional camera footage directory and infer its camera role."""
        p = Path(folder)
        if not p.is_dir():
            return
        if p not in self._input_dirs:
            self._input_dirs.append(p)
        if not self.edit_input_dir.text().strip():
            self.edit_input_dir.setText(str(p.resolve()))

        inferred = default_role or infer_camera_role(p).value
        new_files = [
            f for f in p.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_VIDEO_EXTS
        ]
        for f in new_files:
            if f not in self._video_files:
                self._video_files.append(f)
                self._clip_roles[f] = inferred

        self._rebuild_table()
        self._start_probing_queue()
        self._update_status()
        self._update_recommendation_if_ready()
        self.media_changed.emit()

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
        inferred_role = infer_camera_role(directory).value
        self._video_files = sorted(files, key=lambda f: f.name)
        for f in self._video_files:
            if f not in self._clip_roles:
                self._clip_roles[f] = infer_camera_role(f).value or inferred_role

        self._rebuild_table()
        self._start_probing_queue()
        self._update_status()
        self._update_recommendation_if_ready()
        self.media_changed.emit()

    def _add_video_files(self, files: list[Path]) -> None:
        """Add individual video files to the library."""
        for f in files:
            if f not in self._video_files:
                self._video_files.append(f)
                if f not in self._clip_roles:
                    self._clip_roles[f] = infer_camera_role(f).value
        self._rebuild_table()
        self._start_probing_queue()
        self._update_status()
        self._update_recommendation_if_ready()
        self.media_changed.emit()

    def _inspect_audio(self, audio_path: Path) -> None:
        """Probe audio file metadata, update label, and trigger AI genre recommendation."""
        try:
            analysis = self.adapter.quick_analyze_audio(audio_path)
            self.lbl_audio_info.setText(
                f"Soundtrack: {audio_path.name} | "
                f"BPM: {analysis.tempo_bpm:.1f} | "
                f"Duration: {analysis.duration:.1f}s | "
                f"Beats: {len(analysis.beats)} | "
                f"Sections: {len(analysis.sections)}"
            )
            # Run Genre Recommendation
            roles = [self._clip_roles.get(f, "cam_a") for f in self._video_files]
            recommender = GenreRecommender()
            rec = recommender.recommend(
                bpm=analysis.tempo_bpm,
                duration=analysis.duration,
                section_count=len(analysis.sections),
                camera_roles=roles,
            )
            self._current_recommendation = rec
            self.lbl_recommendation.setText(
                f"💡 AI Recommendation: {rec.major_genre} ➔ {rec.display_name} "
                f"({rec.bpm:.0f} BPM, Confidence: {int(rec.confidence * 100)}%)"
            )
            self.btn_apply_recommendation.setEnabled(True)
            self.recommendation_banner.setVisible(True)
        except Exception:
            file_size_mb = audio_path.stat().st_size / (1024 * 1024)
            self.lbl_audio_info.setText(f"Soundtrack: {audio_path.name} ({file_size_mb:.2f} MB)")
            self.recommendation_banner.setVisible(False)

    def _update_recommendation_if_ready(self) -> None:
        """Re-evaluate AI genre recommendation if soundtrack is already selected."""
        snd = self.get_soundtrack_path()
        if snd and snd.is_file():
            self._inspect_audio(snd)

    def _apply_current_recommendation(self) -> None:
        """Apply recommended genre preset to the application."""
        if self._current_recommendation:
            self.preset_recommended.emit(self._current_recommendation.preset)

    # -------------------------------------------------------------
    # Table & Probing Operations
    # -------------------------------------------------------------
    def _rebuild_table(self) -> None:
        """Rebuild the table rows based on _video_files and populate role selector."""
        self.table.setRowCount(len(self._video_files))
        for idx, file_path in enumerate(self._video_files):
            self.table.setItem(idx, 0, QTableWidgetItem(str(idx + 1)))
            self.table.setItem(idx, 1, QTableWidgetItem(file_path.name))

            # Role dropdown
            combo_role = QComboBox()
            role_items = [
                (CameraRole.CAM_A.value, get_role_display_name(CameraRole.CAM_A)),
                (CameraRole.CAM_B.value, get_role_display_name(CameraRole.CAM_B)),
                (CameraRole.DRONE.value, get_role_display_name(CameraRole.DRONE)),
                (CameraRole.ACTION_CAM.value, get_role_display_name(CameraRole.ACTION_CAM)),
                (CameraRole.B_ROLL.value, get_role_display_name(CameraRole.B_ROLL)),
                (CameraRole.EXTERNAL_AUDIO.value, get_role_display_name(CameraRole.EXTERNAL_AUDIO)),
            ]
            current_role = self._clip_roles.get(file_path, infer_camera_role(file_path).value)
            for r_val, r_text in role_items:
                combo_role.addItem(r_text, r_val)
                if r_val == current_role:
                    combo_role.setCurrentIndex(combo_role.count() - 1)

            combo_role.currentIndexChanged.connect(
                lambda _, p=file_path, cb=combo_role: self._on_role_changed(p, cb)
            )
            self.table.setCellWidget(idx, 2, combo_role)

            meta = self._probed_metadata.get(file_path)
            if meta:
                self.table.setItem(idx, 3, QTableWidgetItem(f"{meta.width}x{meta.height}"))
                self.table.setItem(idx, 4, QTableWidgetItem(f"{meta.fps:.1f}"))
                self.table.setItem(idx, 5, QTableWidgetItem(f"{meta.duration:.2f}s"))
                self.table.setItem(idx, 6, QTableWidgetItem(str(meta.total_frames)))
                self.table.setItem(idx, 7, QTableWidgetItem(meta.codec))
            else:
                self.table.setItem(idx, 3, QTableWidgetItem("Probing..."))
                self.table.setItem(idx, 4, QTableWidgetItem("-"))
                self.table.setItem(idx, 5, QTableWidgetItem("-"))
                self.table.setItem(idx, 6, QTableWidgetItem("-"))
                self.table.setItem(idx, 7, QTableWidgetItem("-"))

            size_mb = file_path.stat().st_size / (1024 * 1024) if file_path.is_file() else 0.0
            self.table.setItem(idx, 8, QTableWidgetItem(f"{size_mb:.2f} MB"))

    def _on_role_changed(self, file_path: Path, combo: QComboBox) -> None:
        new_role = combo.currentData()
        if new_role:
            self._clip_roles[file_path] = new_role
            self._update_recommendation_if_ready()
            self.media_changed.emit()

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
            self.table.setItem(idx, 3, QTableWidgetItem(f"{meta.width}x{meta.height}"))
            self.table.setItem(idx, 4, QTableWidgetItem(f"{meta.fps:.1f}"))
            self.table.setItem(idx, 5, QTableWidgetItem(f"{meta.duration:.2f}s"))
            self.table.setItem(idx, 6, QTableWidgetItem(str(meta.total_frames)))
            self.table.setItem(idx, 7, QTableWidgetItem(meta.codec))
        self._update_status()
        self.footage_probed.emit()

    @Slot(str, str)
    def _on_probe_failed(self, path_str: str, err: str) -> None:
        p = Path(path_str)
        if p in self._video_files:
            idx = self._video_files.index(p)
            self.table.setItem(idx, 3, QTableWidgetItem("Error"))

    def _on_table_selection_changed(self) -> None:
        """Emit video_selected when a video clip row is clicked."""
        selected_rows = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if selected_rows:
            row = selected_rows[0]
            if 0 <= row < len(self._video_files):
                self.video_selected.emit(self._video_files[row])

    def _on_table_cell_double_clicked(self, row: int, col: int) -> None:
        """Emit video_selected when a clip is double-clicked."""
        if 0 <= row < len(self._video_files):
            self.video_selected.emit(self._video_files[row])

    def _refresh_library(self) -> None:
        self._probed_metadata.clear()
        self._rebuild_table()
        self._start_probing_queue()

    def _remove_selected(self) -> None:
        selected_rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()), reverse=True)
        for row in selected_rows:
            if 0 <= row < len(self._video_files):
                p = self._video_files[row]
                del self._video_files[row]
                if p in self._clip_roles:
                    del self._clip_roles[p]
        self._rebuild_table()
        self._update_status()
        self._update_recommendation_if_ready()
        self.media_changed.emit()

    def clear_all(self) -> None:
        """Clear all loaded assets and inputs."""
        self._video_files.clear()
        self._input_dirs.clear()
        self._clip_roles.clear()
        self._probed_metadata.clear()
        self.edit_input_dir.clear()
        self.edit_soundtrack.clear()
        self.table.setRowCount(0)
        self.lbl_audio_info.setText("Soundtrack: None selected")
        self.recommendation_banner.setVisible(False)
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
    def get_footage_probe_summary(self) -> tuple[float, int, int]:
        """(total probed seconds, probed clip count, total clip count) - cheap, no new probing."""
        probed = [self._probed_metadata[f].duration for f in self._video_files if f in self._probed_metadata]
        return float(sum(probed)), len(probed), len(self._video_files)

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

    def get_input_dirs(self) -> list[Path]:
        """Return all distinct source footage directories queued."""
        dirs = [d for d in self._input_dirs if d.is_dir()]
        primary = self.get_input_dir()
        if primary and primary not in dirs:
            dirs.insert(0, primary)
        return dirs

    def get_clip_roles(self) -> dict[Path, str]:
        """Return assigned camera roles per video file."""
        return dict(self._clip_roles)

    def set_input_dirs(self, dirs: list[Path | str]) -> None:
        """Restore multiple queued footage folders."""
        valid_dirs = [Path(d) for d in dirs if Path(d).is_dir()]
        if not valid_dirs:
            return
        self.clear_all()
        for idx, d in enumerate(valid_dirs):
            if idx == 0:
                self.set_input_dir(d)
            else:
                self.add_footage_folder(d)

    def set_clip_roles(self, roles: dict[str | Path, str]) -> None:
        """Restore camera roles for clips and update UI table."""
        for p_key, role_val in roles.items():
            p = Path(p_key)
            self._clip_roles[p] = role_val
        self._rebuild_table()

    def is_valid_setup(self, workflow_mode: str = "standard") -> tuple[bool, str]:
        """Validate if current selections are sufficient to run the editing pipeline."""
        if not self.get_input_dir() and not self._video_files:
            return False, "No valid source video directory or clips loaded."
        if not self._video_files and self.get_input_dir():
            files = [
                f
                for f in self.get_input_dir().iterdir()
                if f.is_file() and f.suffix.lower() in SUPPORTED_VIDEO_EXTS
            ]
            if not files:
                return False, f"No supported video clips found in '{self.get_input_dir()}'."
        if not self.get_soundtrack_path() and workflow_mode != "rough_cut":
            return False, "Soundtrack audio file must be selected."
        return True, "Ready"
