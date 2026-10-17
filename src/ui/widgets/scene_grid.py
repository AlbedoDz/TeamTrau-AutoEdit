"""Interactive Scene Grid and Visual Quality Dashboard Widget.

Displays detected and scored scenes with keyframe thumbnails, quality badges,
motion energy, dead-air indicators, and manual scene inclusion/exclusion controls.
"""

import logging
import threading
from pathlib import Path

import cv2
from PySide6.QtCore import QMetaObject, QObject, QRect, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.types import ScoredScene

logger = logging.getLogger(__name__)


class ThumbnailWorkerSignals(QObject):
    """Signals emitted by background thumbnail extraction workers."""

    thumbnail_ready = Signal(int, QPixmap)
    thumbnail_failed = Signal(int)


class ThumbnailRunnable(QRunnable):
    """Asynchronous worker extracting a center keyframe thumbnail from source video."""

    def __init__(
        self,
        scene_idx: int,
        clip_path: Path,
        start_time: float,
        end_time: float,
        signals: ThumbnailWorkerSignals,
        target_size: tuple[int, int] = (160, 90),
    ) -> None:
        super().__init__()
        self.scene_idx = scene_idx
        self.clip_path = clip_path
        self.start_time = start_time
        self.end_time = end_time
        self.signals = signals
        self.target_size = target_size

    def run(self) -> None:
        """Extract center frame from video file and emit QPixmap thumbnail."""
        if not self.clip_path.exists():
            self.signals.thumbnail_failed.emit(self.scene_idx)
            return

        try:
            cap = cv2.VideoCapture(str(self.clip_path))
            try:
                mid_point_ms = ((self.start_time + self.end_time) / 2.0) * 1000.0
                cap.set(cv2.CAP_PROP_POS_MSEC, mid_point_ms)
                ret, frame = cap.read()
                if ret and frame is not None and frame.size > 0:
                    thumb = cv2.resize(frame, self.target_size)
                    rgb = cv2.cvtColor(thumb, cv2.COLOR_BGR2RGB)
                    h, w, ch = rgb.shape
                    qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
                    pixmap = QPixmap.fromImage(qimg)
                    self.signals.thumbnail_ready.emit(self.scene_idx, pixmap)
                    return
            finally:
                cap.release()
        except Exception as exc:
            logger.debug("Failed extracting thumbnail for scene %d: %s", self.scene_idx, exc)

        self.signals.thumbnail_failed.emit(self.scene_idx)


class SceneGridWidget(QWidget):
    """Interactive Scene Grid & Visual Quality Dashboard."""

    scene_toggled = Signal(int, bool)
    scene_selected = Signal(object)
    scenes_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scenes: list[ScoredScene] = []
        self._excluded_indices: set[int] = set()
        self._thumbnails: dict[int, QPixmap] = {}
        self._selected_index: int | None = None

        self.worker_signals = ThumbnailWorkerSignals()
        self.worker_signals.thumbnail_ready.connect(self._on_thumbnail_ready)
        self.worker_signals.thumbnail_failed.connect(self._on_thumbnail_failed)

        self._init_ui()

    @staticmethod
    def create_fallback_pixmap(width: int = 160, height: int = 90, text: str = "No Preview") -> QPixmap:
        """Generate a neutral placeholder pixmap when video thumbnail is unavailable."""
        pixmap = QPixmap(width, height)
        pixmap.fill(QColor("#18181b"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#71717a"))
        font = QFont("Segoe UI", 9)
        painter.setFont(font)
        painter.drawText(QRect(0, 0, width, height), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        return pixmap

    def _init_ui(self) -> None:
        """Construct filter bar, master table, and detail inspector layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 1. Filter and Control Bar
        filter_group = QGroupBox("Scene Filtering & Quality Controls", self)
        filter_layout = QVBoxLayout(filter_group)
        filter_layout.setSpacing(6)

        row_top = QHBoxLayout()
        self.lbl_stats = QLabel("Scenes: 0 total | 0 included | 0 excluded", self)
        font_stats = QFont()
        font_stats.setBold(True)
        self.lbl_stats.setFont(font_stats)
        row_top.addWidget(self.lbl_stats)
        row_top.addStretch()

        self.btn_include_all = QPushButton("✅ Include All", self)
        self.btn_include_all.setObjectName("secondaryButton")
        self.btn_include_all.setToolTip("Include all detected scenes in sequence")
        self.btn_include_all.clicked.connect(self.include_all)
        row_top.addWidget(self.btn_include_all)

        self.btn_exclude_all = QPushButton("🚫 Exclude All", self)
        self.btn_exclude_all.setObjectName("secondaryButton")
        self.btn_exclude_all.setToolTip("Exclude all scenes from sequence")
        self.btn_exclude_all.clicked.connect(self.exclude_all)
        row_top.addWidget(self.btn_exclude_all)

        self.btn_exclude_dead_air = QPushButton("⚡ Exclude Dead-Air", self)
        self.btn_exclude_dead_air.setObjectName("secondaryButton")
        self.btn_exclude_dead_air.setToolTip("Automatically exclude silent or frozen footage")
        self.btn_exclude_dead_air.clicked.connect(self.exclude_dead_air_scenes)
        row_top.addWidget(self.btn_exclude_dead_air)

        self.btn_vlm_tag = QPushButton("🤖 VLM Tag All", self)
        self.btn_vlm_tag.setObjectName("secondaryButton")
        self.btn_vlm_tag.setToolTip("Run AI VLM semantic analysis & key moment tagging on all scenes")
        self.btn_vlm_tag.clicked.connect(self._on_run_vlm_tagging)
        row_top.addWidget(self.btn_vlm_tag)

        filter_layout.addLayout(row_top)

        row_filters = QHBoxLayout()
        row_filters.addWidget(QLabel("Search / Tag:"))
        self.edit_search = QLineEdit(self)
        self.edit_search.setPlaceholderText("Filter by clip name, mood, or tag...")
        self.edit_search.textChanged.connect(self.filter_scenes)
        row_filters.addWidget(self.edit_search, stretch=1)

        row_filters.addWidget(QLabel("Min Composite Score:"))
        self.spin_min_score = QDoubleSpinBox(self)
        self.spin_min_score.setRange(0.0, 1.0)
        self.spin_min_score.setSingleStep(0.05)
        self.spin_min_score.setValue(0.0)
        self.spin_min_score.valueChanged.connect(self.filter_scenes)
        row_filters.addWidget(self.spin_min_score)

        self.chk_hide_dead_air = QCheckBox("Hide Dead-Air", self)
        self.chk_hide_dead_air.toggled.connect(self.filter_scenes)
        row_filters.addWidget(self.chk_hide_dead_air)

        self.chk_only_included = QCheckBox("Show Included Only", self)
        self.chk_only_included.toggled.connect(self.filter_scenes)
        row_filters.addWidget(self.chk_only_included)

        self.btn_reset_filter = QPushButton("🔄 Reset Filters", self)
        self.btn_reset_filter.setObjectName("secondaryButton")
        self.btn_reset_filter.setToolTip("Reset search query and score filters")
        self.btn_reset_filter.clicked.connect(self.reset_filters)
        row_filters.addWidget(self.btn_reset_filter)

        filter_layout.addLayout(row_filters)
        layout.addWidget(filter_group)

        # 2. Main Content Splitter (Table on left, Inspector on right)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Left: Table View
        self.table = QTableWidget(self)
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "Inc",
            "Thumbnail",
            "Clip & Interval",
            "Composite",
            "Sharpness",
            "Aesthetic",
            "Motion",
            "Status",
            "VLM Semantic / Tags",
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 42)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(3, 85)
        self.table.setColumnWidth(4, 75)
        self.table.setColumnWidth(5, 75)
        self.table.setColumnWidth(6, 75)
        self.table.setColumnWidth(7, 85)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().setDefaultSectionSize(60)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.table.itemChanged.connect(self._on_item_changed)
        splitter.addWidget(self.table)

        # Right: Detail Inspector Card
        self.inspector_panel = self._create_inspector_panel()
        splitter.addWidget(self.inspector_panel)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)

        layout.addWidget(splitter, stretch=1)

    def _create_inspector_panel(self) -> QWidget:
        """Create right-side detail inspection pane for selected scene."""
        card = QFrame(self)
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet("background-color: #16161a; border: 1px solid #27272a; border-radius: 6px;")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 12, 12, 12)
        card_layout.setSpacing(10)

        title = QLabel("Scene Inspector", card)
        font_title = QFont()
        font_title.setPointSize(12)
        font_title.setBold(True)
        title.setFont(font_title)
        card_layout.addWidget(title)

        # Large Thumbnail Preview
        self.inspector_thumb = QLabel(card)
        self.inspector_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.inspector_thumb.setFixedHeight(140)
        self.inspector_thumb.setStyleSheet("background-color: #121214; border: 1px solid #27272a; border-radius: 4px;")
        self.inspector_thumb.setPixmap(self.create_fallback_pixmap(240, 135, "Select a Scene"))
        card_layout.addWidget(self.inspector_thumb)

        # Metadata fields scroll area
        scroll = QScrollArea(card)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: none; background: transparent;")
        info_widget = QWidget()
        info_layout = QVBoxLayout(info_widget)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(6)

        self.lbl_insp_clip = QLabel("Clip: -", info_widget)
        self.lbl_insp_clip.setWordWrap(True)
        info_layout.addWidget(self.lbl_insp_clip)

        self.lbl_insp_time = QLabel("Time: -", info_widget)
        info_layout.addWidget(self.lbl_insp_time)

        self.lbl_insp_composite = QLabel("Composite Score: -", info_widget)
        info_layout.addWidget(self.lbl_insp_composite)

        self.lbl_insp_metrics = QLabel("Sharpness: - | Aesthetic: -", info_widget)
        info_layout.addWidget(self.lbl_insp_metrics)

        self.lbl_insp_motion = QLabel("Motion Energy: -", info_widget)
        info_layout.addWidget(self.lbl_insp_motion)

        self.lbl_insp_status = QLabel("Dead-Air Status: -", info_widget)
        info_layout.addWidget(self.lbl_insp_status)

        self.lbl_insp_crop = QLabel("9:16 Crop Window: -", info_widget)
        self.lbl_insp_crop.setWordWrap(True)
        info_layout.addWidget(self.lbl_insp_crop)

        self.lbl_insp_vlm = QLabel("VLM Analysis: -", info_widget)
        self.lbl_insp_vlm.setWordWrap(True)
        info_layout.addWidget(self.lbl_insp_vlm)

        info_layout.addStretch()
        scroll.setWidget(info_widget)
        card_layout.addWidget(scroll, stretch=1)

        # Inclusion toggle button
        self.btn_toggle_include = QPushButton("Exclude Scene", card)
        self.btn_toggle_include.setObjectName("secondaryButton")
        self.btn_toggle_include.setEnabled(False)
        self.btn_toggle_include.clicked.connect(self._on_toggle_current_scene)
        card_layout.addWidget(self.btn_toggle_include)

        return card

    # -------------------------------------------------------------------------
    # Public Data Population & Retrieval API
    # -------------------------------------------------------------------------

    def set_scenes(self, scenes: list[ScoredScene]) -> None:
        """Replace all scenes and asynchronously extract preview thumbnails."""
        self._scenes = list(scenes)
        self._excluded_indices.clear()
        self._thumbnails.clear()
        self._selected_index = None

        self._populate_table()
        self._update_stats_label()
        self._update_inspector(None)
        self._request_thumbnails()
        self.scenes_changed.emit()

    def add_scene(self, scene: ScoredScene) -> None:
        """Append a newly discovered scored scene to the active grid."""
        idx = len(self._scenes)
        self._scenes.append(scene)
        self._append_row(idx, scene)
        self._update_stats_label()

        # Request thumbnail in thread pool
        pool = QThreadPool.globalInstance()
        worker = ThumbnailRunnable(
            scene_idx=idx,
            clip_path=scene.scene.clip_path,
            start_time=scene.scene.start_time,
            end_time=scene.scene.end_time,
            signals=self.worker_signals,
            target_size=(96, 54),
        )
        pool.start(worker)
        self.scenes_changed.emit()

    def update_or_add_scene(self, scene: ScoredScene) -> None:
        """Update existing scene if match found by clip path and time interval, else append."""
        for idx, existing in enumerate(self._scenes):
            if (
                existing.scene.clip_path == scene.scene.clip_path
                and abs(existing.scene.start_time - scene.scene.start_time) < 0.05
                and abs(existing.scene.end_time - scene.scene.end_time) < 0.05
            ):
                self._scenes[idx] = scene
                self._render_row_items(idx, scene)
                if self._selected_index == idx:
                    self._update_inspector(scene)
                self.scenes_changed.emit()
                return
        self.add_scene(scene)

    @Slot()
    def _on_run_vlm_tagging(self) -> None:
        """Trigger asynchronous VLM semantic tagging on all loaded scenes."""
        if not self._scenes:
            return
        self.btn_vlm_tag.setEnabled(False)
        self.btn_vlm_tag.setText("🤖 Tagging...")

        main_win = self.window()
        cfg = getattr(main_win, "config", None)
        if cfg is None:
            from src.core.config import PipelineConfig

            cfg = PipelineConfig(enable_vlm_tagging=True)
        else:
            cfg.enable_vlm_tagging = True

        def _worker() -> None:
            try:
                from src.modules.vlm_assistant import VLMAssistant

                assistant = VLMAssistant(cfg)
                assistant.tag_scored_scenes(self._scenes)
            except Exception as exc:
                logger.warning("VLM tagging failed: %s", exc)
            finally:
                QMetaObject.invokeMethod(self, "_on_vlm_tagging_completed", Qt.ConnectionType.QueuedConnection)

        threading.Thread(target=_worker, daemon=True).start()

    @Slot()
    def _on_vlm_tagging_completed(self) -> None:
        """Handle completion of background VLM tagging."""
        self.btn_vlm_tag.setEnabled(True)
        self.btn_vlm_tag.setText("🤖 VLM Tag All")
        self._populate_table()
        if self._selected_index is not None and self._selected_index < len(self._scenes):
            self._update_inspector(self._scenes[self._selected_index])
        self.scenes_changed.emit()

    def clear_scenes(self) -> None:
        """Clear all active scenes and reset states."""
        self._scenes.clear()
        self._excluded_indices.clear()
        self._thumbnails.clear()
        self._selected_index = None
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        self._update_stats_label()
        self._update_inspector(None)
        self.scenes_changed.emit()

    def get_all_scenes(self) -> list[ScoredScene]:
        """Return all managed scenes regardless of inclusion status."""
        return list(self._scenes)

    def get_included_scenes(self) -> list[ScoredScene]:
        """Return only active scenes marked as included."""
        return [
            scene
            for idx, scene in enumerate(self._scenes)
            if idx not in self._excluded_indices
        ]

    def get_excluded_scenes(self) -> list[ScoredScene]:
        """Return scenes marked as excluded."""
        return [
            scene
            for idx, scene in enumerate(self._scenes)
            if idx in self._excluded_indices
        ]

    def is_scene_included(self, scene_idx: int) -> bool:
        """Check if scene at index is included."""
        return 0 <= scene_idx < len(self._scenes) and scene_idx not in self._excluded_indices

    def set_scene_included(self, scene_idx: int, included: bool) -> None:
        """Explicitly set inclusion status for a scene index."""
        if not (0 <= scene_idx < len(self._scenes)):
            return

        if included:
            self._excluded_indices.discard(scene_idx)
        else:
            self._excluded_indices.add(scene_idx)

        # Update table checkbox without trigger recursion
        self.table.blockSignals(True)
        item = self.table.item(scene_idx, 0)
        if item:
            item.setCheckState(Qt.CheckState.Checked if included else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)

        self._update_stats_label()
        if self._selected_index == scene_idx:
            self._update_inspector(self._scenes[scene_idx])

        self.scene_toggled.emit(scene_idx, included)
        self.scenes_changed.emit()

    def include_all(self) -> None:
        """Include all currently managed scenes."""
        self._excluded_indices.clear()
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(Qt.CheckState.Checked)
        self.table.blockSignals(False)
        self._update_stats_label()
        if self._selected_index is not None and self._selected_index < len(self._scenes):
            self._update_inspector(self._scenes[self._selected_index])
        self.scenes_changed.emit()

    def exclude_all(self) -> None:
        """Exclude all currently managed scenes."""
        self._excluded_indices = set(range(len(self._scenes)))
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_stats_label()
        if self._selected_index is not None and self._selected_index < len(self._scenes):
            self._update_inspector(self._scenes[self._selected_index])
        self.scenes_changed.emit()

    def exclude_dead_air_scenes(self) -> None:
        """Exclude all scenes tagged with dead-air."""
        for idx, scene in enumerate(self._scenes):
            if self._is_dead_air(scene):
                self._excluded_indices.add(idx)

        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                is_inc = row not in self._excluded_indices
                item.setCheckState(Qt.CheckState.Checked if is_inc else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)

        self._update_stats_label()
        if self._selected_index is not None and self._selected_index < len(self._scenes):
            self._update_inspector(self._scenes[self._selected_index])
        self.scenes_changed.emit()

    def reset_filters(self) -> None:
        """Reset search, score threshold, and visibility checkboxes."""
        self.edit_search.clear()
        self.spin_min_score.setValue(0.0)
        self.chk_hide_dead_air.setChecked(False)
        self.chk_only_included.setChecked(False)
        self.filter_scenes()

    @Slot()
    def filter_scenes(self) -> None:
        """Show/hide table rows based on active filter criteria."""
        search_query = self.edit_search.text().strip().lower()
        min_score = self.spin_min_score.value()
        hide_dead_air = self.chk_hide_dead_air.isChecked()
        only_included = self.chk_only_included.isChecked()

        for row, scene in enumerate(self._scenes):
            # 1. Score threshold filter
            if scene.composite_score < min_score:
                self.table.setRowHidden(row, True)
                continue

            # 2. Dead-air filter
            is_dead_air = self._is_dead_air(scene)
            if hide_dead_air and is_dead_air:
                self.table.setRowHidden(row, True)
                continue

            # 3. Inclusion status filter
            is_included = row not in self._excluded_indices
            if only_included and not is_included:
                self.table.setRowHidden(row, True)
                continue

            # 4. Search text filter
            if search_query:
                clip_text = scene.scene.clip_path.name.lower()
                vlm_text = ""
                if scene.vlm_analysis:
                    vlm_text = f"{scene.vlm_analysis.description} {' '.join(scene.vlm_analysis.tags)} {scene.vlm_analysis.mood}".lower()
                combined_text = f"{clip_text} {vlm_text}"
                if search_query not in combined_text:
                    self.table.setRowHidden(row, True)
                    continue

            self.table.setRowHidden(row, False)

    # -------------------------------------------------------------------------
    # Internal Rendering and Table Helpers
    # -------------------------------------------------------------------------

    def _populate_table(self) -> None:
        """Rebuild entire table widget rows for current scene set."""
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._scenes))
        for row, scene in enumerate(self._scenes):
            self._render_row_items(row, scene)
        self.table.blockSignals(False)

    def _append_row(self, row: int, scene: ScoredScene) -> None:
        """Add a single row at bottom of table."""
        self.table.blockSignals(True)
        self.table.insertRow(row)
        self._render_row_items(row, scene)
        self.table.blockSignals(False)

    def _render_row_items(self, row: int, scene: ScoredScene) -> None:
        """Create and bind table widget items for a single scene."""
        # Col 0: Include checkbox
        item_inc = QTableWidgetItem()
        item_inc.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        is_included = row not in self._excluded_indices
        item_inc.setCheckState(Qt.CheckState.Checked if is_included else Qt.CheckState.Unchecked)
        self.table.setItem(row, 0, item_inc)

        # Col 1: Thumbnail placeholder or cached pixmap
        item_thumb = QTableWidgetItem()
        pix = self._thumbnails.get(row) or self.create_fallback_pixmap(96, 54, "...")
        item_thumb.setData(Qt.ItemDataRole.DecorationRole, pix)
        item_thumb.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 1, item_thumb)

        # Col 2: Clip & Interval
        cut = scene.scene
        clip_name = cut.clip_path.name
        interval_text = f"{clip_name}\n[{cut.start_time:05.1f}s - {cut.end_time:05.1f}s] ({cut.duration:.1f}s)"
        item_clip = QTableWidgetItem(interval_text)
        item_clip.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 2, item_clip)

        # Col 3: Composite score badge
        comp_str = f"{scene.composite_score:.2f}"
        item_comp = QTableWidgetItem(comp_str)
        item_comp.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item_comp.setForeground(self._get_score_color(scene.composite_score))
        font_comp = QFont()
        font_comp.setBold(True)
        item_comp.setFont(font_comp)
        item_comp.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 3, item_comp)

        # Col 4: Sharpness
        item_sharp = QTableWidgetItem(f"{scene.sharpness_score:.1f}")
        item_sharp.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item_sharp.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 4, item_sharp)

        # Col 5: Aesthetic
        item_aest = QTableWidgetItem(f"{scene.aesthetic_score:.2f}")
        item_aest.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item_aest.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 5, item_aest)

        # Col 6: Motion Energy
        motion_label = f"{scene.motion_energy:.2f}"
        item_motion = QTableWidgetItem(motion_label)
        item_motion.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if scene.motion_energy >= 0.70:
            item_motion.setForeground(QColor("#38bdf8"))  # Light cyan/blue
        item_motion.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 6, item_motion)

        # Col 7: Status / Dead-Air
        is_dead = self._is_dead_air(scene)
        status_text = "Dead-Air" if is_dead else "Active"
        item_status = QTableWidgetItem(status_text)
        item_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item_status.setForeground(QColor("#f87171") if is_dead else QColor("#4ade80"))
        item_status.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 7, item_status)

        # Col 8: VLM Semantic Tags
        vlm_summary = ""
        if scene.vlm_analysis:
            vlm = scene.vlm_analysis
            tags_str = ", ".join(vlm.tags[:3]) if vlm.tags else "untagged"
            star = "[KEY] " if vlm.is_key_moment else ""
            vlm_summary = f"{star}{vlm.mood} | {tags_str}"
        elif scene.crop_window:
            vlm_summary = f"Crop 9:16 ({scene.crop_window.tracking_subject})"
        else:
            vlm_summary = "Unannotated"
        item_vlm = QTableWidgetItem(vlm_summary)
        item_vlm.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 8, item_vlm)

    def _request_thumbnails(self) -> None:
        """Dispatch thumbnail generation tasks to global threadpool."""
        pool = QThreadPool.globalInstance()
        for idx, scene in enumerate(self._scenes):
            worker = ThumbnailRunnable(
                scene_idx=idx,
                clip_path=scene.scene.clip_path,
                start_time=scene.scene.start_time,
                end_time=scene.scene.end_time,
                signals=self.worker_signals,
                target_size=(96, 54),
            )
            pool.start(worker)

    @Slot(int, QPixmap)
    def _on_thumbnail_ready(self, scene_idx: int, pixmap: QPixmap) -> None:
        """Update cached thumbnail and table decoration."""
        self._thumbnails[scene_idx] = pixmap
        if scene_idx < self.table.rowCount():
            item = self.table.item(scene_idx, 1)
            if item:
                item.setData(Qt.ItemDataRole.DecorationRole, pixmap)

        if self._selected_index == scene_idx:
            # Scale up for inspector pane
            scaled = pixmap.scaled(240, 135, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.inspector_thumb.setPixmap(scaled)

    @Slot(int)
    def _on_thumbnail_failed(self, scene_idx: int) -> None:
        """Handle fallback if thumbnail could not be extracted."""
        fallback = self.create_fallback_pixmap(96, 54, "No Frame")
        self._thumbnails[scene_idx] = fallback
        if scene_idx < self.table.rowCount():
            item = self.table.item(scene_idx, 1)
            if item:
                item.setData(Qt.ItemDataRole.DecorationRole, fallback)

    @Slot(QTableWidgetItem)
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Handle user checking/unchecking inclusion in column 0."""
        if item.column() != 0:
            return

        row = item.row()
        if not (0 <= row < len(self._scenes)):
            return

        is_checked = item.checkState() == Qt.CheckState.Checked
        was_included = row not in self._excluded_indices

        if is_checked != was_included:
            if is_checked:
                self._excluded_indices.discard(row)
            else:
                self._excluded_indices.add(row)

            self._update_stats_label()
            if self._selected_index == row:
                self._update_inspector(self._scenes[row])

            self.scene_toggled.emit(row, is_checked)
            self.scenes_changed.emit()

    @Slot()
    def _on_table_selection_changed(self) -> None:
        """Update scene inspector when a row is clicked/selected."""
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            self._selected_index = None
            self._update_inspector(None)
            self.scene_selected.emit(None)
            return

        row = selected_rows[0].row()
        if 0 <= row < len(self._scenes):
            self._selected_index = row
            scene = self._scenes[row]
            self._update_inspector(scene)
            self.scene_selected.emit(scene)

    def _update_inspector(self, scene: ScoredScene | None) -> None:
        """Refresh right inspector view details."""
        if scene is None:
            self.inspector_thumb.setPixmap(self.create_fallback_pixmap(240, 135, "Select a Scene"))
            self.lbl_insp_clip.setText("Clip: -")
            self.lbl_insp_time.setText("Time: -")
            self.lbl_insp_composite.setText("Composite Score: -")
            self.lbl_insp_metrics.setText("Sharpness: - | Aesthetic: -")
            self.lbl_insp_motion.setText("Motion Energy: -")
            self.lbl_insp_status.setText("Dead-Air Status: -")
            self.lbl_insp_crop.setText("9:16 Crop Window: -")
            self.lbl_insp_vlm.setText("VLM Analysis: -")
            self.btn_toggle_include.setEnabled(False)
            self.btn_toggle_include.setText("Exclude Scene")
            return

        # Large thumbnail
        idx = self._selected_index if self._selected_index is not None else 0
        if idx in self._thumbnails:
            pix = self._thumbnails[idx]
            scaled = pix.scaled(240, 135, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.inspector_thumb.setPixmap(scaled)
        else:
            self.inspector_thumb.setPixmap(self.create_fallback_pixmap(240, 135, "Loading..."))

        cut = scene.scene
        self.lbl_insp_clip.setText(f"<b>Clip:</b> {cut.clip_path.name}")
        self.lbl_insp_time.setText(
            f"<b>Time:</b> {cut.start_time:.2f}s - {cut.end_time:.2f}s "
            f"({cut.duration:.2f}s, frames {cut.start_frame}-{cut.end_frame})"
        )
        self.lbl_insp_composite.setText(
            f"<b>Composite Score:</b> {scene.composite_score:.3f}"
        )
        self.lbl_insp_metrics.setText(
            f"<b>Sharpness:</b> {scene.sharpness_score:.1f} | <b>Aesthetic:</b> {scene.aesthetic_score:.2f}"
        )

        motion_str = f"{scene.motion_energy:.2f}"
        if scene.motion_energy >= 0.70:
            motion_str += " (Action-Drop Candidate)"
        elif scene.motion_energy <= 0.25:
            motion_str += " (Scenic / Low-Motion)"
        self.lbl_insp_motion.setText(f"<b>Motion Energy:</b> {motion_str}")

        is_dead = self._is_dead_air(scene)
        status_str = "<font color='#f87171'>Dead-Air Detected</font>" if is_dead else "<font color='#4ade80'>Clean / Active</font>"
        self.lbl_insp_status.setText(f"<b>Status:</b> {status_str}")

        if scene.crop_window:
            cw = scene.crop_window
            self.lbl_insp_crop.setText(
                f"<b>9:16 Crop Window:</b> [{cw.x}, {cw.y}, {cw.width}x{cw.height}] "
                f"({cw.tracking_subject}, conf: {cw.confidence:.2f})"
            )
        else:
            self.lbl_insp_crop.setText("<b>9:16 Crop Window:</b> None (Landscape or Default)")

        if scene.vlm_analysis:
            vlm = scene.vlm_analysis
            tags_txt = ", ".join(f"#{t}" for t in vlm.tags)
            key_txt = "<b>[KEY MOMENT]</b> " if vlm.is_key_moment else ""
            self.lbl_insp_vlm.setText(
                f"<b>VLM Analysis:</b> {key_txt}<i>{vlm.description}</i><br>"
                f"<b>Mood:</b> {vlm.mood} | <b>Pacing:</b> {vlm.suggested_pacing}<br>"
                f"<b>Tags:</b> {tags_txt}"
            )
        else:
            self.lbl_insp_vlm.setText("<b>VLM Analysis:</b> Not tagged by VLM")

        # Toggle button
        is_inc = self.is_scene_included(idx)
        self.btn_toggle_include.setEnabled(True)
        self.btn_toggle_include.setText("❌ Exclude Scene" if is_inc else "✅ Include Scene")
        self.btn_toggle_include.setToolTip(
            "Exclude this scene cut from timeline assembly" if is_inc else "Include this scene cut in timeline assembly"
        )
        if is_inc:
            self.btn_toggle_include.setObjectName("dangerButton")
        else:
            self.btn_toggle_include.setObjectName("primaryButton")
        # Force restyle
        self.btn_toggle_include.style().unpolish(self.btn_toggle_include)
        self.btn_toggle_include.style().polish(self.btn_toggle_include)

    @Slot()
    def _on_toggle_current_scene(self) -> None:
        """Toggle inclusion of currently selected scene."""
        if self._selected_index is None:
            return
        current_state = self.is_scene_included(self._selected_index)
        self.set_scene_included(self._selected_index, not current_state)

    def _update_stats_label(self) -> None:
        """Update top status pill with counts and durations."""
        total = len(self._scenes)
        included = total - len(self._excluded_indices)
        excluded = len(self._excluded_indices)

        inc_duration = sum(
            scene.scene.duration
            for idx, scene in enumerate(self._scenes)
            if idx not in self._excluded_indices
        )
        self.lbl_stats.setText(
            f"Scenes: {total} total | {included} included ({inc_duration:.1f}s) | {excluded} excluded"
        )

    @staticmethod
    def _is_dead_air(scene: ScoredScene) -> bool:
        """Determine if a scene was flagged as dead-air."""
        meta = scene.metadata or {}
        return bool(meta.get("has_dead_air") or meta.get("is_dead_air") or False)

    @staticmethod
    def _get_score_color(score: float) -> QColor:
        """Get standard traffic-light color for quality score."""
        if score >= 0.70:
            return QColor("#4ade80")  # Green
        if score >= 0.40:
            return QColor("#facc15")  # Yellow
        return QColor("#f87171")  # Red
