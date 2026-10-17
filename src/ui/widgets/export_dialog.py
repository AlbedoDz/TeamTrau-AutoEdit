"""Export Modal Dialog for Unified Desktop NLE Workspace."""

import logging

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.widgets.export_panel import ExportPanelWidget

logger = logging.getLogger(__name__)


class ExportDialog(QDialog):
    """Modal dialog wrapping ExportPanelWidget for streamlined multi-NLE exports

    and real-time NVENC render monitoring.
    """

    start_export_requested = Signal()
    render_preview_requested = Signal()

    def __init__(
        self,
        export_panel: ExportPanelWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("⚡ Deliverable Export & Multi-NLE Handoff")
        self.resize(820, 640)
        self.setMinimumSize(680, 500)

        self.export_panel = export_panel or ExportPanelWidget(self)

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # Header Title
        title_box = QHBoxLayout()
        lbl_title = QLabel("⚡ Deliverable Export & Multi-NLE Handoff")
        lbl_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #38bdf8;")
        title_box.addWidget(lbl_title)
        title_box.addStretch()
        layout.addLayout(title_box)

        # Embedded ExportPanelWidget
        layout.addWidget(self.export_panel, stretch=1)

        # Bottom Button Bar
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()

        self.btn_close = QPushButton("❌ Close", self)
        self.btn_close.setToolTip("Close Export Dialog")
        self.btn_close.clicked.connect(self.accept)
        btn_bar.addWidget(self.btn_close)

        self.btn_preview = QPushButton("👁 Render Preview (540p)", self)
        self.btn_preview.setObjectName("secondaryButton")
        self.btn_preview.setFixedHeight(32)
        self.btn_preview.setToolTip("Render a fast 540p proxy of the current edit and load it in the monitor")
        self.btn_preview.clicked.connect(self._on_preview_clicked)
        btn_bar.addWidget(self.btn_preview)

        self.btn_run_export = QPushButton("🚀 Export", self)
        self.btn_run_export.setObjectName("primaryButton")
        self.btn_run_export.setFixedHeight(32)
        self.btn_run_export.setToolTip("Export the current edit: master video and/or NLE handoff files")
        self.btn_run_export.clicked.connect(self._on_start_clicked)
        btn_bar.addWidget(self.btn_run_export)

        layout.addLayout(btn_bar)

    @Slot()
    def _on_start_clicked(self) -> None:
        self.set_export_enabled(False)
        self.start_export_requested.emit()

    @Slot()
    def _on_preview_clicked(self) -> None:
        self.set_export_enabled(False)
        self.render_preview_requested.emit()

    def set_export_enabled(self, enabled: bool) -> None:
        """Enable or disable the Export and Preview buttons together."""
        self.btn_run_export.setEnabled(enabled)
        self.btn_preview.setEnabled(enabled)
