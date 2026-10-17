"""Export Dispatcher and Live NVENC Render Monitor Widget.

Manages NLE export targets (DaVinci Resolve FCPXML, Premiere XML, OTIO, CapCut),
hardware acceleration settings, real-time FFmpeg NVENC render monitoring,
and final deliverable file handoffs.
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.core.config import PipelineConfig, check_cuda_available, check_nvenc_available
from src.core.types import PipelineExecutionResult

logger = logging.getLogger(__name__)


def find_default_capcut_draft_dir() -> Path | None:
    """Auto-discover default CapCut Desktop projects directory on Windows."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate = Path(local_app_data) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
        if candidate.exists():
            return candidate
    return None


def open_path_in_os(path: Path) -> bool:
    """Cross-platform helper to reveal or open directory/file in OS default app."""
    if not path.exists():
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception as exc:
        logger.warning("Failed opening %s: %s", path, exc)
        return False


class ExportPanelWidget(QWidget):
    """Multi-NLE Export Configuration & Real-Time NVENC Render Monitor Widget."""

    export_options_changed = Signal()
    open_directory_requested = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._last_result: PipelineExecutionResult | None = None
        self._output_dir: Path = Path("./output")

        self._init_ui()

    def _init_ui(self) -> None:
        """Construct export options, live render monitor, and deliverable handoffs."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(10)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(12)

        # -------------------------------------------------------------
        # Section 1: Multi-NLE Interchange Formats
        # -------------------------------------------------------------
        group_nle = QGroupBox("NLE Sequence Exporters & Integration", content)
        layout_nle = QVBoxLayout(group_nle)
        layout_nle.setSpacing(8)

        self.chk_fcpxml = QCheckBox("DaVinci Resolve Studio FCPXML (.fcpxml)", group_nle)
        self.chk_fcpxml.setChecked(True)
        self.chk_fcpxml.toggled.connect(lambda: self.export_options_changed.emit())
        layout_nle.addWidget(self.chk_fcpxml)

        self.chk_premiere = QCheckBox("Adobe Premiere Pro Final Cut Pro 7 XML (.xml)", group_nle)
        self.chk_premiere.setChecked(True)
        self.chk_premiere.toggled.connect(lambda: self.export_options_changed.emit())
        layout_nle.addWidget(self.chk_premiere)

        self.chk_otio = QCheckBox("OpenTimelineIO Interchange Sequence (.otio)", group_nle)
        self.chk_otio.setChecked(True)
        self.chk_otio.toggled.connect(lambda: self.export_options_changed.emit())
        layout_nle.addWidget(self.chk_otio)

        self.chk_capcut = QCheckBox("CapCut Desktop Project Draft (draft_content.json)", group_nle)
        self.chk_capcut.setChecked(True)
        self.chk_capcut.toggled.connect(self._on_capcut_toggled)
        layout_nle.addWidget(self.chk_capcut)

        # CapCut Draft Directory Row
        row_capcut_dir = QHBoxLayout()
        row_capcut_dir.setContentsMargins(20, 0, 0, 0)
        row_capcut_dir.addWidget(QLabel("CapCut Projects Directory:"))
        self.edit_capcut_dir = QLineEdit(group_nle)
        default_capcut = find_default_capcut_draft_dir()
        if default_capcut:
            self.edit_capcut_dir.setText(str(default_capcut))
        else:
            self.edit_capcut_dir.setPlaceholderText("Leave empty to export into output folder, or select CapCut draft dir...")
        self.edit_capcut_dir.textChanged.connect(lambda: self.export_options_changed.emit())
        row_capcut_dir.addWidget(self.edit_capcut_dir, stretch=1)

        btn_browse_capcut = QPushButton("📁 Browse...", group_nle)
        btn_browse_capcut.setObjectName("secondaryButton")
        btn_browse_capcut.setToolTip("Select CapCut draft destination folder")
        btn_browse_capcut.clicked.connect(self._browse_capcut_dir)
        row_capcut_dir.addWidget(btn_browse_capcut)
        layout_nle.addLayout(row_capcut_dir)

        # DaVinci Resolve Studio API Automation
        self.chk_resolve_dispatch = QCheckBox("Automate DaVinci Resolve Studio IPC (Import media & configure canvas)", group_nle)
        self.chk_resolve_dispatch.setChecked(False)
        self.chk_resolve_dispatch.toggled.connect(self._on_resolve_toggled)
        layout_nle.addWidget(self.chk_resolve_dispatch)

        self.chk_dry_run_resolve = QCheckBox("Simulate DaVinci Resolve (Dry-Run mode)", group_nle)
        self.chk_dry_run_resolve.setChecked(False)
        self.chk_dry_run_resolve.setEnabled(False)
        layout_nle.addWidget(self.chk_dry_run_resolve)

        # Variant Export Scope
        row_variant = QHBoxLayout()
        row_variant.addWidget(QLabel("Timeline Variant Target:"))
        self.combo_variant_target = QComboBox(group_nle)
        self.combo_variant_target.addItems([
            "Active Workspace Variant (Current Cut)",
            "Batch Export All Generated Variations (1-3 Variations)",
        ])
        self.combo_variant_target.currentIndexChanged.connect(lambda: self.export_options_changed.emit())
        row_variant.addWidget(self.combo_variant_target, stretch=1)
        layout_nle.addLayout(row_variant)

        layout.addWidget(group_nle)

        # -------------------------------------------------------------
        # Section 2: Standalone Video Render Engine & Teaser Settings
        # -------------------------------------------------------------
        group_render = QGroupBox("Master Video Rendering (FFmpeg NVENC / CPU)", content)
        layout_render = QVBoxLayout(group_render)
        layout_render.setSpacing(8)

        self.chk_enable_render = QCheckBox("Render Standalone Master Video (.mp4)", group_render)
        self.chk_enable_render.setChecked(True)
        self.chk_enable_render.toggled.connect(self._on_render_toggle)
        layout_render.addWidget(self.chk_enable_render)

        row_hw = QHBoxLayout()
        cuda_ok = check_cuda_available()
        nvenc_ok = check_nvenc_available()
        hw_desc = "NVIDIA NVENC Hardware Active" if nvenc_ok else ("CUDA Available (CPU Encoder)" if cuda_ok else "CPU Only")
        self.lbl_hw_status = QLabel(f"Hardware Acceleration: <b>{hw_desc}</b>", group_render)
        self.lbl_hw_status.setStyleSheet("color: #4ade80;" if nvenc_ok else "color: #facc15;")
        row_hw.addWidget(self.lbl_hw_status)
        row_hw.addStretch()

        self.chk_force_cpu = QCheckBox("Force CPU Fallback (libx265)", group_render)
        self.chk_force_cpu.setChecked(not nvenc_ok)
        self.chk_force_cpu.toggled.connect(lambda: self.export_options_changed.emit())
        row_hw.addWidget(self.chk_force_cpu)
        layout_render.addLayout(row_hw)

        # Retention Teaser Hook Row
        row_teaser = QHBoxLayout()
        self.chk_enable_teaser = QCheckBox("Include Retention Teaser Hook (3-5 Fast Cuts at Start)", group_render)
        self.chk_enable_teaser.setChecked(True)
        self.chk_enable_teaser.toggled.connect(self._on_teaser_toggled)
        row_teaser.addWidget(self.chk_enable_teaser)

        row_teaser.addSpacing(16)
        row_teaser.addWidget(QLabel("Teaser Duration (s):"))
        self.spin_teaser_dur = QDoubleSpinBox(group_render)
        self.spin_teaser_dur.setRange(2.0, 15.0)
        self.spin_teaser_dur.setSingleStep(0.5)
        self.spin_teaser_dur.setValue(6.0)
        self.spin_teaser_dur.valueChanged.connect(lambda: self.export_options_changed.emit())
        row_teaser.addWidget(self.spin_teaser_dur)
        row_teaser.addStretch()
        layout_render.addLayout(row_teaser)

        # Advanced NVENC Quality Controls
        self.grp_nvenc_adv = QGroupBox("Advanced NVENC Quality Overrides", group_render)
        self.grp_nvenc_adv.setCheckable(True)
        self.grp_nvenc_adv.setChecked(False)
        self.grp_nvenc_adv.toggled.connect(lambda: self.export_options_changed.emit())
        layout_nvenc_adv = QFormLayout(self.grp_nvenc_adv)
        layout_nvenc_adv.setSpacing(6)

        self.spin_nvenc_bitrate = QSpinBox(self.grp_nvenc_adv)
        self.spin_nvenc_bitrate.setRange(10, 200)
        self.spin_nvenc_bitrate.setValue(60)
        self.spin_nvenc_bitrate.setSuffix(" Mbps")
        self.spin_nvenc_bitrate.setToolTip("Target average encoding bitrate for master deliverable")
        self.spin_nvenc_bitrate.valueChanged.connect(lambda: self.export_options_changed.emit())
        layout_nvenc_adv.addRow("Target Bitrate:", self.spin_nvenc_bitrate)

        self.spin_nvenc_cq = QSpinBox(self.grp_nvenc_adv)
        self.spin_nvenc_cq.setRange(1, 35)
        self.spin_nvenc_cq.setValue(18)
        self.spin_nvenc_cq.setToolTip("Constant Quality / CRF factor (Lower = Higher Quality, 18 = Visually Lossless)")
        self.spin_nvenc_cq.valueChanged.connect(lambda: self.export_options_changed.emit())
        layout_nvenc_adv.addRow("Constant Quality (CQ):", self.spin_nvenc_cq)

        layout_render.addWidget(self.grp_nvenc_adv)

        layout.addWidget(group_render)

        # -------------------------------------------------------------
        # Section 2b: Workflow Presets & Branding Engine
        # -------------------------------------------------------------
        group_workflow = QGroupBox("Workflow Presets & Branding Engine", content)
        layout_wf = QVBoxLayout(group_workflow)
        layout_wf.setSpacing(8)

        # Workflow mode selector
        row_wf = QHBoxLayout()
        row_wf.addWidget(QLabel("Workflow Mode:", group_workflow))
        self.combo_workflow = QComboBox(group_workflow)
        self.combo_workflow.addItems([
            "Standard (Beat-Synced Rhythm Cuts)",
            "Build / Timelapse (Speed Ramping 1x-8x)",
            "Unboxing / Narration (1.0x Voice-Over)",
        ])
        self.combo_workflow.currentIndexChanged.connect(lambda: self.export_options_changed.emit())
        row_wf.addWidget(self.combo_workflow, stretch=1)
        layout_wf.addLayout(row_wf)

        # Audio fix toggle
        self.chk_audio_fix = QCheckBox("Auto-Clean BWF Timecode & Normalize 24-bit PCM (Fixes Resolve silence)", group_workflow)
        self.chk_audio_fix.setChecked(True)
        self.chk_audio_fix.toggled.connect(lambda: self.export_options_changed.emit())
        layout_wf.addWidget(self.chk_audio_fix)

        # Watermark Section
        row_wm_header = QHBoxLayout()
        self.chk_watermark = QCheckBox("Overlay Brand Watermark (Logo PNG)", group_workflow)
        self.chk_watermark.setChecked(False)
        self.chk_watermark.toggled.connect(self._on_watermark_toggled)
        row_wm_header.addWidget(self.chk_watermark)
        row_wm_header.addStretch()
        layout_wf.addLayout(row_wm_header)

        # Watermark Path row
        row_wm_path = QHBoxLayout()
        self.edit_watermark_path = QLineEdit(group_workflow)
        self.edit_watermark_path.setPlaceholderText("Select transparent PNG logo file...")
        self.edit_watermark_path.setEnabled(False)
        self.edit_watermark_path.textChanged.connect(lambda: self.export_options_changed.emit())
        row_wm_path.addWidget(self.edit_watermark_path, stretch=1)

        self.btn_browse_wm = QPushButton("📁 Browse...", group_workflow)
        self.btn_browse_wm.setObjectName("secondaryButton")
        self.btn_browse_wm.setToolTip("Select PNG watermark logo file")
        self.btn_browse_wm.setEnabled(False)
        self.btn_browse_wm.clicked.connect(self._browse_watermark)
        row_wm_path.addWidget(self.btn_browse_wm)
        layout_wf.addLayout(row_wm_path)

        # Watermark Options row
        row_wm_opts = QHBoxLayout()
        row_wm_opts.addWidget(QLabel("Position:", group_workflow))
        self.combo_wm_pos = QComboBox(group_workflow)
        self.combo_wm_pos.addItems(["top-right", "top-left", "bottom-right", "bottom-left", "center"])
        self.combo_wm_pos.setEnabled(False)
        self.combo_wm_pos.currentIndexChanged.connect(lambda: self.export_options_changed.emit())
        row_wm_opts.addWidget(self.combo_wm_pos)

        row_wm_opts.addSpacing(16)
        row_wm_opts.addWidget(QLabel("Opacity:", group_workflow))
        self.spin_wm_opacity = QDoubleSpinBox(group_workflow)
        self.spin_wm_opacity.setRange(0.05, 1.0)
        self.spin_wm_opacity.setSingleStep(0.05)
        self.spin_wm_opacity.setValue(0.85)
        self.spin_wm_opacity.setEnabled(False)
        self.spin_wm_opacity.valueChanged.connect(lambda: self.export_options_changed.emit())
        row_wm_opts.addWidget(self.spin_wm_opacity)
        row_wm_opts.addStretch()
        layout_wf.addLayout(row_wm_opts)

        layout.addWidget(group_workflow)

        # -------------------------------------------------------------
        # Section 3: Live NVENC Render Monitor
        # -------------------------------------------------------------
        group_monitor = QGroupBox("Live NVENC Render Monitor", content)
        layout_monitor = QVBoxLayout(group_monitor)
        layout_monitor.setSpacing(6)

        # Stat Gauges Row
        row_gauges = QHBoxLayout()
        self.lbl_gauge_status = QLabel("Status: Idle", group_monitor)
        self.lbl_gauge_status.setStyleSheet("font-weight: bold; color: #a1a1aa;")
        row_gauges.addWidget(self.lbl_gauge_status)

        self.lbl_gauge_frame = QLabel("Frame: -- / --", group_monitor)
        row_gauges.addWidget(self.lbl_gauge_frame)

        self.lbl_gauge_speed = QLabel("Speed: -- fps (--x)", group_monitor)
        row_gauges.addWidget(self.lbl_gauge_speed)

        self.lbl_gauge_encoder = QLabel("Encoder: hevc_nvenc", group_monitor)
        row_gauges.addWidget(self.lbl_gauge_encoder)
        row_gauges.addStretch()
        layout_monitor.addLayout(row_gauges)

        # Progress bar
        self.render_progress = QProgressBar(group_monitor)
        self.render_progress.setRange(0, 100)
        self.render_progress.setValue(0)
        self.render_progress.setTextVisible(True)
        layout_monitor.addWidget(self.render_progress)

        self.lbl_render_message = QLabel("Awaiting pipeline render execution...", group_monitor)
        self.lbl_render_message.setStyleSheet("color: #71717a; font-size: 11px;")
        layout_monitor.addWidget(self.lbl_render_message)

        layout.addWidget(group_monitor)

        # -------------------------------------------------------------
        # Section 4: Deliverables & Handoff Actions
        # -------------------------------------------------------------
        group_deliverables = QGroupBox("Deliverables & Post-Edit Actions", content)
        layout_deliv = QVBoxLayout(group_deliverables)
        layout_deliv.setSpacing(8)

        # Deliverable Paths Box
        self.box_deliverables = QFrame(group_deliverables)
        self.box_deliverables.setFrameShape(QFrame.Shape.StyledPanel)
        self.box_deliverables.setStyleSheet("background-color: #121214; border: 1px solid #27272a; border-radius: 4px; padding: 6px;")
        box_layout = QVBoxLayout(self.box_deliverables)
        box_layout.setSpacing(4)

        self.lbl_deliv_fcpxml = QLabel("DaVinci FCPXML: (not generated yet)", self.box_deliverables)
        box_layout.addWidget(self.lbl_deliv_fcpxml)

        self.lbl_deliv_premiere = QLabel("Premiere XML: (not generated yet)", self.box_deliverables)
        box_layout.addWidget(self.lbl_deliv_premiere)

        self.lbl_deliv_otio = QLabel("OpenTimelineIO: (not generated yet)", self.box_deliverables)
        box_layout.addWidget(self.lbl_deliv_otio)

        self.lbl_deliv_capcut = QLabel("CapCut Draft: (not generated yet)", self.box_deliverables)
        box_layout.addWidget(self.lbl_deliv_capcut)

        self.lbl_deliv_master = QLabel("Master MP4: (not generated yet)", self.box_deliverables)
        font_bold = QFont()
        font_bold.setBold(True)
        self.lbl_deliv_master.setFont(font_bold)
        self.lbl_deliv_master.setStyleSheet("color: #38bdf8;")
        box_layout.addWidget(self.lbl_deliv_master)

        self.lbl_deliv_variants = QLabel("", self.box_deliverables)
        self.lbl_deliv_variants.setStyleSheet("color: #a78bfa; font-weight: 500;")
        self.lbl_deliv_variants.setVisible(False)
        box_layout.addWidget(self.lbl_deliv_variants)

        layout_deliv.addWidget(self.box_deliverables)

        # Action Buttons Row
        row_actions = QHBoxLayout()
        self.btn_open_folder = QPushButton("📂 Open Deliverable Folder", group_deliverables)
        self.btn_open_folder.setObjectName("secondaryButton")
        self.btn_open_folder.setToolTip("Open rendered output folder in File Explorer")
        self.btn_open_folder.clicked.connect(self._on_open_deliverable_folder)
        row_actions.addWidget(self.btn_open_folder)

        self.btn_play_master = QPushButton("▶️ Play Master Video", group_deliverables)
        self.btn_play_master.setObjectName("secondaryButton")
        self.btn_play_master.setToolTip("Play master rendered video with system media player")
        self.btn_play_master.setEnabled(False)
        self.btn_play_master.clicked.connect(self._on_play_master_video)
        row_actions.addWidget(self.btn_play_master)

        self.btn_open_capcut = QPushButton("✂️ Open CapCut Draft", group_deliverables)
        self.btn_open_capcut.setObjectName("secondaryButton")
        self.btn_open_capcut.setToolTip("Open CapCut draft directory in File Explorer")
        self.btn_open_capcut.setEnabled(False)
        self.btn_open_capcut.clicked.connect(self._on_open_capcut_draft)
        row_actions.addWidget(self.btn_open_capcut)

        row_actions.addStretch()
        layout_deliv.addLayout(row_actions)

        layout.addWidget(group_deliverables)

        scroll.setWidget(content)
        main_layout.addWidget(scroll)

    # -------------------------------------------------------------------------
    # Public Configuration API
    # -------------------------------------------------------------------------

    def get_export_options(self) -> dict[str, Any]:
        """Collect current export options into a serializable dictionary."""
        mode_idx = self.combo_workflow.currentIndex()
        mode_str = "standard"
        if mode_idx == 1:
            mode_str = "build_timelapse"
        elif mode_idx == 2:
            mode_str = "unboxing_narration"

        return {
            "enable_fcpxml": self.chk_fcpxml.isChecked(),
            "enable_premiere": self.chk_premiere.isChecked(),
            "enable_otio": self.chk_otio.isChecked(),
            "enable_capcut": self.chk_capcut.isChecked(),
            "capcut_draft_dir": self.get_capcut_draft_dir(),
            "enable_render": self.chk_enable_render.isChecked(),
            "force_cpu": self.chk_force_cpu.isChecked(),
            "enable_teaser": self.chk_enable_teaser.isChecked(),
            "teaser_duration": self.spin_teaser_dur.value(),
            "resolve_dispatch": self.chk_resolve_dispatch.isChecked(),
            "dry_run_resolve": self.chk_dry_run_resolve.isChecked(),
            "workflow_mode": mode_str,
            "enable_audio_fix": self.chk_audio_fix.isChecked(),
            "watermark_enabled": self.chk_watermark.isChecked(),
            "watermark_path": self.edit_watermark_path.text().strip() or None,
            "watermark_position": self.combo_wm_pos.currentText(),
            "watermark_opacity": self.spin_wm_opacity.value(),
            "variant_export_scope": "all" if self.combo_variant_target.currentIndex() == 1 else "active",
            "nvenc_bitrate": f"{self.spin_nvenc_bitrate.value()}M",
            "nvenc_cq": self.spin_nvenc_cq.value(),
            "nvenc_overrides_enabled": self.grp_nvenc_adv.isChecked(),
        }

    def apply_to_config(self, config: PipelineConfig) -> None:
        """Inject user selections into active PipelineConfig instance."""
        mode_idx = self.combo_workflow.currentIndex()
        mode_str = "standard"
        if mode_idx == 1:
            mode_str = "build_timelapse"
        elif mode_idx == 2:
            mode_str = "unboxing_narration"
        config.apply_workflow_mode(mode_str)  # type: ignore

        config.enable_capcut_export = self.chk_capcut.isChecked()
        config.capcut_draft_dir = self.get_capcut_draft_dir()
        config.enable_teaser = self.chk_enable_teaser.isChecked()
        config.teaser_duration = self.spin_teaser_dur.value()
        config.enable_audio_fix = self.chk_audio_fix.isChecked()

        config.watermark_enabled = self.chk_watermark.isChecked()
        wm_txt = self.edit_watermark_path.text().strip()
        if wm_txt and Path(wm_txt).exists():
            config.watermark_path = Path(wm_txt)
        config.watermark_position = self.combo_wm_pos.currentText()  # type: ignore
        config.watermark_opacity = self.spin_wm_opacity.value()

        if self.grp_nvenc_adv.isChecked():
            config.nvenc_bitrate = f"{self.spin_nvenc_bitrate.value()}M"
            config.nvenc_cq = self.spin_nvenc_cq.value()

        # Variant generation count: 3 for all variants, 1 for active only
        config.generate_variants = 3 if self.combo_variant_target.currentIndex() == 1 else 1

    def load_from_config(self, config: PipelineConfig) -> None:
        """Populate Export panel controls from PipelineConfig."""
        if config.workflow_mode == "build_timelapse":
            self.combo_workflow.setCurrentIndex(1)
        elif config.workflow_mode == "unboxing_narration":
            self.combo_workflow.setCurrentIndex(2)
        else:
            self.combo_workflow.setCurrentIndex(0)

        self.chk_capcut.setChecked(config.enable_capcut_export)
        if config.capcut_draft_dir:
            self.edit_capcut_dir.setText(str(config.capcut_draft_dir))

        self.chk_enable_teaser.setChecked(config.enable_teaser)
        self.spin_teaser_dur.setValue(config.teaser_duration)
        self.chk_audio_fix.setChecked(config.enable_audio_fix)

        self.chk_watermark.setChecked(config.watermark_enabled)
        if config.watermark_path:
            self.edit_watermark_path.setText(str(config.watermark_path))
        wm_pos_idx = self.combo_wm_pos.findText(config.watermark_position)
        if wm_pos_idx >= 0:
            self.combo_wm_pos.setCurrentIndex(wm_pos_idx)
        self.spin_wm_opacity.setValue(config.watermark_opacity)

        if hasattr(config, "nvenc_bitrate") and config.nvenc_bitrate:
            try:
                b_num = int(re.sub(r"[^\d]", "", str(config.nvenc_bitrate)))
                self.spin_nvenc_bitrate.setValue(b_num)
            except ValueError:
                pass
        if hasattr(config, "nvenc_cq") and config.nvenc_cq:
            self.spin_nvenc_cq.setValue(int(config.nvenc_cq))

        if getattr(config, "generate_variants", 3) >= 3:
            self.combo_variant_target.setCurrentIndex(1)
        else:
            self.combo_variant_target.setCurrentIndex(0)

    def get_capcut_draft_dir(self) -> Path | None:
        """Return user selected CapCut draft directory if specified and valid."""
        txt = self.edit_capcut_dir.text().strip()
        if txt and Path(txt).is_dir():
            return Path(txt)
        return None

    def set_output_dir(self, output_dir: Path) -> None:
        """Update active output deliverables directory."""
        self._output_dir = output_dir

    def is_render_enabled(self) -> bool:
        """Check whether master video rendering is toggled on."""
        return self.chk_enable_render.isChecked()

    def is_force_cpu(self) -> bool:
        """Check whether CPU fallback is requested."""
        return self.chk_force_cpu.isChecked()

    def is_resolve_dispatch_enabled(self) -> bool:
        """Check whether DaVinci Resolve dispatch is enabled."""
        return self.chk_resolve_dispatch.isChecked()

    def is_dry_run_resolve(self) -> bool:
        """Check whether Resolve dry run is enabled."""
        return self.chk_dry_run_resolve.isChecked()

    # -------------------------------------------------------------------------
    # Live Render Monitor Slots
    # -------------------------------------------------------------------------

    @Slot(float, str)
    def update_render_progress(self, stage_pct: float, message: str) -> None:
        """Parse real-time FFmpeg progress and update monitor gauges."""
        self.render_progress.setValue(int(stage_pct))
        self.lbl_render_message.setText(message)

        if "Rendering frame" in message:
            self.lbl_gauge_status.setText("Status: Rendering")
            self.lbl_gauge_status.setStyleSheet("font-weight: bold; color: #38bdf8;")
            # Parse frame numbers: "Rendering frame 120/450 (26.7%)"
            m = re.search(r"frame\s+(\d+)/(\d+)", message)
            if m:
                self.lbl_gauge_frame.setText(f"Frame: {m.group(1)} / {m.group(2)}")
        elif "Finalizing" in message:
            self.lbl_gauge_status.setText("Status: Finalizing Container")
        elif "complete" in message.lower():
            self.lbl_gauge_status.setText("Status: Complete")
            self.lbl_gauge_status.setStyleSheet("font-weight: bold; color: #4ade80;")

    def reset_render_monitor(self) -> None:
        """Reset monitor metrics prior to new render execution."""
        self.render_progress.setValue(0)
        self.lbl_gauge_status.setText("Status: Starting")
        self.lbl_gauge_status.setStyleSheet("font-weight: bold; color: #facc15;")
        self.lbl_gauge_frame.setText("Frame: -- / --")
        self.lbl_gauge_speed.setText("Speed: -- fps")
        self.lbl_render_message.setText("Initializing FFmpeg encoder process...")

    def set_deliverables(self, result: PipelineExecutionResult) -> None:
        """Update deliverable file paths and enable handoff buttons."""
        self._last_result = result

        if result.success:
            self.render_progress.setValue(100)
            self.lbl_gauge_status.setText("Status: Completed Successfully")
            self.lbl_gauge_status.setStyleSheet("font-weight: bold; color: #4ade80;")
            self.lbl_render_message.setText(f"Export finished in {result.total_duration:.1f}s ({result.clip_count} clips)")

            if result.fcpxml_path:
                self.lbl_deliv_fcpxml.setText(f"DaVinci FCPXML: <b>{result.fcpxml_path.name}</b>")
            if result.premiere_xml_path:
                self.lbl_deliv_premiere.setText(f"Premiere XML: <b>{result.premiere_xml_path.name}</b>")
            if result.otio_path:
                self.lbl_deliv_otio.setText(f"OpenTimelineIO: <b>{result.otio_path.name}</b>")
            if result.capcut_draft_path:
                self.lbl_deliv_capcut.setText(f"CapCut Draft: <b>{result.capcut_draft_path.name}</b>")
                self.btn_open_capcut.setEnabled(True)

            if result.render_path and result.render_path.exists():
                self.lbl_deliv_master.setText(f"Master MP4: <b>{result.render_path.name}</b> ({result.encoder_used or 'NVENC'})")
                self.btn_play_master.setEnabled(True)
            elif not self.is_render_enabled():
                self.lbl_deliv_master.setText("Master MP4: (Render skipped by user)")
            else:
                self.lbl_deliv_master.setText("Master MP4: (Not rendered)")

            if getattr(result, "timeline_variants", None):
                var_names = [v.display_name for v in result.timeline_variants.values()]
                self.lbl_deliv_variants.setText(f"Timeline Variants ({len(var_names)}): <b>{' | '.join(var_names)}</b>")
                self.lbl_deliv_variants.setVisible(True)
            else:
                self.lbl_deliv_variants.setVisible(False)
        else:
            self.lbl_deliv_variants.setVisible(False)
            self.lbl_gauge_status.setText("Status: Failed")
            self.lbl_gauge_status.setStyleSheet("font-weight: bold; color: #f87171;")
            self.lbl_render_message.setText(f"Error: {result.error_message or 'Unknown failure'}")

    # -------------------------------------------------------------------------
    # Internal Slots & Actions
    # -------------------------------------------------------------------------

    @Slot(bool)
    def _on_capcut_toggled(self, checked: bool) -> None:
        self.edit_capcut_dir.setEnabled(checked)
        self.export_options_changed.emit()

    @Slot(bool)
    def _on_resolve_toggled(self, checked: bool) -> None:
        self.chk_dry_run_resolve.setEnabled(checked)
        self.export_options_changed.emit()

    @Slot(bool)
    def _on_render_toggle(self, checked: bool) -> None:
        self.chk_force_cpu.setEnabled(checked)
        self.export_options_changed.emit()

    @Slot(bool)
    def _on_teaser_toggled(self, checked: bool) -> None:
        self.spin_teaser_dur.setEnabled(checked)
        self.export_options_changed.emit()

    @Slot()
    def _browse_capcut_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select CapCut Draft Projects Directory")
        if folder:
            self.edit_capcut_dir.setText(folder)
            self.export_options_changed.emit()

    @Slot()
    def _on_open_deliverable_folder(self) -> None:
        target = self._output_dir
        if self._last_result and self._last_result.render_path:
            target = self._last_result.render_path.parent
        elif self._last_result and self._last_result.fcpxml_path:
            target = self._last_result.fcpxml_path.parent

        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)

        if not open_path_in_os(target):
            QMessageBox.warning(self, "Open Folder Failed", f"Could not open directory: {target}")

    @Slot()
    def _on_play_master_video(self) -> None:
        if self._last_result and self._last_result.render_path and self._last_result.render_path.exists():
            if not open_path_in_os(self._last_result.render_path):
                QMessageBox.warning(self, "Play Video Failed", f"Could not launch media player for: {self._last_result.render_path}")

    @Slot()
    def _on_open_capcut_draft(self) -> None:
        if self._last_result and self._last_result.capcut_draft_path and self._last_result.capcut_draft_path.exists():
            if not open_path_in_os(self._last_result.capcut_draft_path):
                QMessageBox.warning(self, "Open CapCut Failed", f"Could not open draft folder: {self._last_result.capcut_draft_path}")

    @Slot(bool)
    def _on_watermark_toggled(self, checked: bool) -> None:
        """Enable or disable watermark input controls."""
        self.edit_watermark_path.setEnabled(checked)
        self.btn_browse_wm.setEnabled(checked)
        self.combo_wm_pos.setEnabled(checked)
        self.spin_wm_opacity.setEnabled(checked)
        self.export_options_changed.emit()

    @Slot()
    def _browse_watermark(self) -> None:
        """Open file dialog to pick watermark logo PNG."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Watermark Logo Image",
            str(Path.home()),
            "Image Files (*.png *.jpg *.jpeg *.webp);;All Files (*.*)",
        )
        if path:
            self.edit_watermark_path.setText(path)
            self.export_options_changed.emit()
