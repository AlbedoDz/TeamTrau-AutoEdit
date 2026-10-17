"""Smart Inspector Sidebar Widget for Professional NLE Pipeline Controls."""

import logging
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.core.config import PipelineConfig
from src.core.types import WatermarkPosition, WorkflowMode

logger = logging.getLogger(__name__)


class InspectorPanelWidget(QWidget):
    """Right dock inspector panel providing interactive controls for pacing, AI tools,

    speed-ramping, watermark branding, and teaser hook assembly.
    """

    analyze_optimize_clicked = Signal()
    open_preferences_clicked = Signal()
    config_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspectorPanelWidget")
        self.setMinimumWidth(280)
        self.setMaximumWidth(420)

        self._init_ui()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(6)

        # Header Title
        lbl_title = QLabel("✨ Smart Inspector")
        lbl_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #38bdf8; padding: 4px;")
        main_layout.addWidget(lbl_title)

        # Scroll Area for dense inspector controls
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        # 1. Workflow & Preset Group
        grp_workflow = QGroupBox("Workflow & Pacing Preset", content)
        form_wf = QFormLayout(grp_workflow)
        form_wf.setSpacing(6)

        self.combo_workflow = QComboBox(grp_workflow)
        self.combo_workflow.addItem("Standard (Music Beat-Sync)", "standard")
        self.combo_workflow.addItem("Cinematic Wedding / Narrative", "standard")
        self.combo_workflow.addItem("Build / Timelapse (Speed-Ramp)", "build_timelapse")
        self.combo_workflow.addItem("Unboxing / Narration (Speech-Focus)", "unboxing_narration")
        self.combo_workflow.addItem("Rough Cut & Defect Pruning", "rough_cut")
        self.combo_workflow.currentIndexChanged.connect(self._on_workflow_preset_changed)
        form_wf.addRow("Preset:", self.combo_workflow)

        layout.addWidget(grp_workflow)

        # Defect Cleaning Controls (Rough Cut)
        self.grp_defect_pruning = QGroupBox("Defect Cleaning Controls", content)
        form_defect = QFormLayout(self.grp_defect_pruning)
        form_defect.setSpacing(6)

        self.chk_prune_wobble = QCheckBox("Auto-Prune Start/Stop Wobble", self.grp_defect_pruning)
        self.chk_prune_wobble.setChecked(True)
        form_defect.addRow(self.chk_prune_wobble)

        self.chk_prune_shake = QCheckBox("Slice Out Camera Shake & Whip-Pan", self.grp_defect_pruning)
        self.chk_prune_shake.setChecked(True)
        form_defect.addRow(self.chk_prune_shake)

        self.chk_prune_focus = QCheckBox("Cut Out Lens Defocus & Occlusions", self.grp_defect_pruning)
        self.chk_prune_focus.setChecked(True)
        form_defect.addRow(self.chk_prune_focus)

        self.chk_audio_conditioning = QCheckBox("80Hz High-Pass Filter for Camera Audio", self.grp_defect_pruning)
        self.chk_audio_conditioning.setChecked(True)
        form_defect.addRow(self.chk_audio_conditioning)

        self.spin_min_take = QDoubleSpinBox(self.grp_defect_pruning)
        self.spin_min_take.setRange(1.0, 5.0)
        self.spin_min_take.setSingleStep(0.1)
        self.spin_min_take.setValue(1.5)
        self.spin_min_take.setSuffix(" s")
        form_defect.addRow("Min Usable Take:", self.spin_min_take)

        self.grp_defect_pruning.setVisible(False)
        layout.addWidget(self.grp_defect_pruning)

        # 2. Duration & Pacing
        grp_duration = QGroupBox("Duration & Pacing", content)
        vbox_dur = QVBoxLayout(grp_duration)
        vbox_dur.setSpacing(6)

        # Duration mode
        self.combo_dur_mode = QComboBox(grp_duration)
        self.combo_dur_mode.addItem("Fixed Target Duration", "fixed")
        self.combo_dur_mode.addItem("Footage Driven (Downbeat Snapped)", "footage_driven")
        self.combo_dur_mode.addItem("Full Track Audio Length", "full_track")
        vbox_dur.addWidget(self.combo_dur_mode)

        # Target duration slider & readout
        dur_readout_layout = QHBoxLayout()
        self.lbl_duration_val = QLabel("Target: 180s (3m 00s)")
        self.lbl_duration_val.setStyleSheet("font-weight: bold; color: #e4e4e7;")
        dur_readout_layout.addWidget(self.lbl_duration_val)
        dur_readout_layout.addStretch()
        vbox_dur.addLayout(dur_readout_layout)

        self.slider_duration = QSlider(Qt.Orientation.Horizontal, grp_duration)
        self.slider_duration.setRange(15, 600)
        self.slider_duration.setValue(180)
        self.slider_duration.valueChanged.connect(self._on_duration_slider_changed)
        vbox_dur.addWidget(self.slider_duration)

        # Quick Preset Buttons
        quick_btn_layout = QHBoxLayout()
        quick_btn_layout.setSpacing(4)
        for sec, text in [(30, "30s"), (60, "60s"), (180, "180s"), (300, "5m")]:
            btn = QPushButton(text, grp_duration)
            btn.setFixedHeight(24)
            btn.setToolTip(f"Set target sequence duration to {text}")
            btn.clicked.connect(lambda _, s=sec: self.slider_duration.setValue(s))
            quick_btn_layout.addWidget(btn)
        btn_full = QPushButton("Full", grp_duration)
        btn_full.setFixedHeight(24)
        btn_full.setToolTip("Set duration mode to full soundtrack length")
        btn_full.clicked.connect(lambda: self.combo_dur_mode.setCurrentIndex(2))
        quick_btn_layout.addWidget(btn_full)
        vbox_dur.addLayout(quick_btn_layout)

        layout.addWidget(grp_duration)

        # 3. AI Tools Switches (Matching Mockup)
        grp_ai = QGroupBox("AI Production Tools", content)
        vbox_ai = QVBoxLayout(grp_ai)
        vbox_ai.setSpacing(4)

        self.chk_ai_scene = QCheckBox("AI Scene Detect (Adaptive Cuts)", grp_ai)
        self.chk_ai_scene.setChecked(True)
        vbox_ai.addWidget(self.chk_ai_scene)

        self.chk_ai_beat = QCheckBox("AI Beat Sync (Downbeat Climax Alignment)", grp_ai)
        self.chk_ai_beat.setChecked(True)
        vbox_ai.addWidget(self.chk_ai_beat)

        self.chk_ai_color = QCheckBox("AI VLM Semantic Scene Tagging & Key Moments", grp_ai)
        self.chk_ai_color.setChecked(True)
        vbox_ai.addWidget(self.chk_ai_color)

        self.chk_ai_stabilize = QCheckBox("AI Stabilization / Motion Energy", grp_ai)
        self.chk_ai_stabilize.setChecked(False)
        vbox_ai.addWidget(self.chk_ai_stabilize)

        self.chk_ai_voice = QCheckBox("AI Voice Enhance (Dialogue Ducking)", grp_ai)
        self.chk_ai_voice.setChecked(True)
        vbox_ai.addWidget(self.chk_ai_voice)

        layout.addWidget(grp_ai)

        # 3b. High-Retention & Visual Continuity
        grp_retention = QGroupBox("High-Retention Visual & Subtitle Controls", content)
        form_retention = QFormLayout(grp_retention)
        form_retention.setSpacing(6)

        self.combo_reframing = QComboBox(grp_retention)
        self.combo_reframing.addItem("Auto-Hybrid (Smart Crop + Blur Backdrop)", "auto_hybrid")
        self.combo_reframing.addItem("YOLO Pan & Scan Only", "smart_crop")
        self.combo_reframing.addItem("Blurred Backdrop 9:16", "blur_backdrop")
        form_retention.addRow("Reframing Layout:", self.combo_reframing)

        self.combo_subtitles = QComboBox(grp_retention)
        self.combo_subtitles.addItem("Kinetic Pop (.ASS)", "kinetic_ass")
        self.combo_subtitles.addItem("Clean Standard (.SRT)", "clean_srt")
        form_retention.addRow("Subtitle Preset:", self.combo_subtitles)

        self.chk_exposure_guard = QCheckBox("Exposure Continuity Guard (10-frame Dissolve)", grp_retention)
        self.chk_exposure_guard.setChecked(True)
        form_retention.addRow(self.chk_exposure_guard)

        self.chk_ken_burns = QCheckBox("Ambient Motion (Ken Burns on Static Shots)", grp_retention)
        self.chk_ken_burns.setChecked(True)
        form_retention.addRow(self.chk_ken_burns)

        self.spin_ken_burns_zoom = QDoubleSpinBox(grp_retention)
        self.spin_ken_burns_zoom.setRange(1.01, 1.30)
        self.spin_ken_burns_zoom.setSingleStep(0.01)
        self.spin_ken_burns_zoom.setValue(1.08)
        self.spin_ken_burns_zoom.setSuffix("x")
        self.spin_ken_burns_zoom.setToolTip("Subtle Ken Burns zoom scale factor on low-motion scenes")
        form_retention.addRow("Ken Burns Zoom:", self.spin_ken_burns_zoom)

        layout.addWidget(grp_retention)

        # 4. Dynamic Speed Ramping (1x - 8x)
        grp_speed = QGroupBox("Dynamic Speed Ramping", content)
        form_speed = QFormLayout(grp_speed)
        form_speed.setSpacing(6)

        self.chk_speed_ramping = QCheckBox("Enable Variable Speed Ramping", grp_speed)
        self.chk_speed_ramping.setChecked(False)
        form_speed.addRow(self.chk_speed_ramping)

        self.combo_max_speed = QComboBox(grp_speed)
        self.combo_max_speed.addItem("2.0x Multiplier", 2.0)
        self.combo_max_speed.addItem("4.0x Multiplier", 4.0)
        self.combo_max_speed.addItem("8.0x Multiplier (Apex)", 8.0)
        self.combo_max_speed.setCurrentIndex(2)
        form_speed.addRow("Max Ramp Multiplier:", self.combo_max_speed)

        layout.addWidget(grp_speed)

        # 5. Watermark & Branding Engine
        grp_wm = QGroupBox("Watermark & Branding", content)
        form_wm = QFormLayout(grp_wm)
        form_wm.setSpacing(6)

        self.chk_watermark = QCheckBox("Enable Logo Watermark Overlay", grp_wm)
        self.chk_watermark.setChecked(False)
        form_wm.addRow(self.chk_watermark)

        wm_path_widget = QWidget(grp_wm)
        wm_path_layout = QHBoxLayout(wm_path_widget)
        wm_path_layout.setContentsMargins(0, 0, 0, 0)
        self.edit_wm_path = QLineEdit(wm_path_widget)
        self.edit_wm_path.setPlaceholderText("Select PNG logo...")
        self.btn_browse_wm = QPushButton("📁 Browse...", wm_path_widget)
        self.btn_browse_wm.setObjectName("secondaryButton")
        self.btn_browse_wm.setToolTip("Select PNG watermark logo file")
        self.btn_browse_wm.clicked.connect(self._on_browse_watermark)
        wm_path_layout.addWidget(self.edit_wm_path)
        wm_path_layout.addWidget(self.btn_browse_wm)
        form_wm.addRow("Logo File:", wm_path_widget)

        self.combo_wm_pos = QComboBox(grp_wm)
        self.combo_wm_pos.addItem("Top-Left", "top-left")
        self.combo_wm_pos.addItem("Top-Right", "top-right")
        self.combo_wm_pos.addItem("Bottom-Left", "bottom-left")
        self.combo_wm_pos.addItem("Bottom-Right", "bottom-right")
        self.combo_wm_pos.addItem("Center", "center")
        self.combo_wm_pos.setCurrentIndex(1)
        form_wm.addRow("Position:", self.combo_wm_pos)

        # Opacity slider
        self.slider_wm_opacity = QSlider(Qt.Orientation.Horizontal, grp_wm)
        self.slider_wm_opacity.setRange(10, 100)
        self.slider_wm_opacity.setValue(85)
        form_wm.addRow("Opacity (85%):", self.slider_wm_opacity)
        self.slider_wm_opacity.valueChanged.connect(
            lambda v: form_wm.labelForField(self.slider_wm_opacity).setText(f"Opacity ({v}%):")
        )

        self.spin_wm_margin = QSpinBox(grp_wm)
        self.spin_wm_margin.setRange(0, 120)
        self.spin_wm_margin.setValue(32)
        self.spin_wm_margin.setSuffix(" px")
        form_wm.addRow("Margin Offset:", self.spin_wm_margin)

        layout.addWidget(grp_wm)

        # 6. Retention Teaser Hook
        grp_teaser = QGroupBox("Retention Teaser Hook", content)
        form_teaser = QFormLayout(grp_teaser)
        form_teaser.setSpacing(6)

        self.chk_teaser = QCheckBox("Generate Rapid Teaser Intro", grp_teaser)
        self.chk_teaser.setChecked(True)
        form_teaser.addRow(self.chk_teaser)

        self.spin_teaser_dur = QSpinBox(grp_teaser)
        self.spin_teaser_dur.setRange(3, 10)
        self.spin_teaser_dur.setValue(6)
        self.spin_teaser_dur.setSuffix(" s")
        form_teaser.addRow("Hook Duration:", self.spin_teaser_dur)

        layout.addWidget(grp_teaser)

        layout.addStretch()
        scroll.setWidget(content)
        main_layout.addWidget(scroll, stretch=1)

        # Bottom Action Buttons
        self.btn_analyze = QPushButton("⚡ Analyze & Optimize", self)
        self.btn_analyze.setObjectName("primaryButton")
        self.btn_analyze.setFixedHeight(34)
        self.btn_analyze.setToolTip("Run automated scene detection and rhythm sync pipeline")
        self.btn_analyze.clicked.connect(self.analyze_optimize_clicked)
        main_layout.addWidget(self.btn_analyze)

        self.btn_prefs = QPushButton("⚙️ Preferences / AI Settings...", self)
        self.btn_prefs.setFixedHeight(28)
        self.btn_prefs.setToolTip("Open system preferences and AI model configuration")
        self.btn_prefs.clicked.connect(self.open_preferences_clicked)
        main_layout.addWidget(self.btn_prefs)

    @Slot(int)
    def _on_duration_slider_changed(self, value: int) -> None:
        mins = value // 60
        secs = value % 60
        self.lbl_duration_val.setText(f"Target: {value}s ({mins}m {secs:02d}s)")

    @Slot(int)
    def _on_workflow_preset_changed(self, index: int) -> None:
        mode = self.combo_workflow.currentData()
        if mode == "build_timelapse":
            self.chk_speed_ramping.setChecked(True)
            self.grp_defect_pruning.setVisible(False)
        elif mode == "unboxing_narration":
            self.chk_speed_ramping.setChecked(False)
            self.chk_ai_voice.setChecked(True)
            self.grp_defect_pruning.setVisible(False)
        elif mode == "rough_cut":
            self.chk_speed_ramping.setChecked(False)
            self.chk_teaser.setChecked(False)
            self.grp_defect_pruning.setVisible(True)
        else:
            self.grp_defect_pruning.setVisible(False)

    @Slot()
    def _on_browse_watermark(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Watermark Image",
            "",
            "Image Files (*.png *.jpg *.jpeg *.webp);;All Files (*)",
        )
        if file_path:
            self.edit_wm_path.setText(file_path)
            self.chk_watermark.setChecked(True)

    def apply_to_config(self, config: PipelineConfig) -> None:
        """Transfer Inspector panel settings into PipelineConfig."""
        # Workflow
        wf_mode = cast(WorkflowMode, self.combo_workflow.currentData() or "standard")
        config.workflow_mode = wf_mode

        # Duration
        dur_mode = self.combo_dur_mode.currentData() or "fixed"
        config.duration_mode = dur_mode
        config.target_duration = float(self.slider_duration.value())

        # Speed ramping
        config.enable_speed_ramping = self.chk_speed_ramping.isChecked()
        max_speed = self.combo_max_speed.currentData()
        if max_speed:
            config.max_speed_multiplier = float(max_speed)

        # Watermark
        config.watermark_enabled = self.chk_watermark.isChecked()
        wm_txt = self.edit_wm_path.text().strip()
        config.watermark_path = Path(wm_txt) if wm_txt else None
        pos = self.combo_wm_pos.currentData()
        if pos:
            config.watermark_position = cast(WatermarkPosition, pos)
        config.watermark_opacity = float(self.slider_wm_opacity.value()) / 100.0
        config.watermark_margin = int(self.spin_wm_margin.value())

        # Teaser
        config.enable_teaser = self.chk_teaser.isChecked()
        config.teaser_duration = float(self.spin_teaser_dur.value())

        # AI Tools
        config.enable_dead_air_detection = self.chk_ai_scene.isChecked()
        config.enable_structural_sync = self.chk_ai_beat.isChecked()
        config.enable_vlm_tagging = self.chk_ai_color.isChecked()

        # Retention & Continuity
        config.reframing_mode = self.combo_reframing.currentData() or "auto_hybrid"
        config.subtitle_style = self.combo_subtitles.currentData() or "kinetic_ass"
        config.enable_exposure_guard = self.chk_exposure_guard.isChecked()
        config.enable_ken_burns = self.chk_ken_burns.isChecked()
        config.ken_burns_scale_factor = float(self.spin_ken_burns_zoom.value())

        # Rough Cut Defect Pruning
        config.enable_head_tail_trim = self.chk_prune_wobble.isChecked()
        config.enable_shake_pruning = self.chk_prune_shake.isChecked()
        config.enable_focus_guard = self.chk_prune_focus.isChecked()
        config.enable_audio_conditioning = self.chk_audio_conditioning.isChecked()
        config.min_take_duration_sec = float(self.spin_min_take.value())

    def load_from_config(self, config: PipelineConfig) -> None:
        """Populate Inspector controls from PipelineConfig."""
        # Workflow
        idx = self.combo_workflow.findData(config.workflow_mode)
        if idx >= 0:
            self.combo_workflow.setCurrentIndex(idx)

        # Duration
        dur_idx = self.combo_dur_mode.findData(config.duration_mode)
        if dur_idx >= 0:
            self.combo_dur_mode.setCurrentIndex(dur_idx)
        if config.target_duration:
            self.slider_duration.setValue(int(config.target_duration))

        # Speed ramping
        self.chk_speed_ramping.setChecked(config.enable_speed_ramping)
        sp_idx = self.combo_max_speed.findData(config.max_speed_multiplier)
        if sp_idx >= 0:
            self.combo_max_speed.setCurrentIndex(sp_idx)

        # Watermark
        self.chk_watermark.setChecked(config.watermark_enabled)
        if config.watermark_path:
            self.edit_wm_path.setText(str(config.watermark_path))
        wm_pos_idx = self.combo_wm_pos.findData(config.watermark_position)
        if wm_pos_idx >= 0:
            self.combo_wm_pos.setCurrentIndex(wm_pos_idx)
        self.slider_wm_opacity.setValue(int(config.watermark_opacity * 100))
        self.spin_wm_margin.setValue(int(config.watermark_margin))

        # Teaser
        self.chk_teaser.setChecked(config.enable_teaser)
        self.spin_teaser_dur.setValue(int(config.teaser_duration))

        # Retention & Continuity
        reframe_idx = self.combo_reframing.findData(getattr(config, "reframing_mode", "auto_hybrid"))
        if reframe_idx >= 0:
            self.combo_reframing.setCurrentIndex(reframe_idx)

        sub_idx = self.combo_subtitles.findData(getattr(config, "subtitle_style", "kinetic_ass"))
        if sub_idx >= 0:
            self.combo_subtitles.setCurrentIndex(sub_idx)

        self.chk_exposure_guard.setChecked(getattr(config, "enable_exposure_guard", True))
        self.chk_ken_burns.setChecked(getattr(config, "enable_ken_burns", True))
        self.spin_ken_burns_zoom.setValue(float(getattr(config, "ken_burns_scale_factor", 1.08)))
        self.chk_ai_color.setChecked(getattr(config, "enable_vlm_tagging", True))

        # Rough Cut Defect Pruning
        self.chk_prune_wobble.setChecked(getattr(config, "enable_head_tail_trim", True))
        self.chk_prune_shake.setChecked(getattr(config, "enable_shake_pruning", True))
        self.chk_prune_focus.setChecked(getattr(config, "enable_focus_guard", True))
        self.chk_audio_conditioning.setChecked(getattr(config, "enable_audio_conditioning", True))
        self.spin_min_take.setValue(float(getattr(config, "min_take_duration_sec", 1.5)))
        self.grp_defect_pruning.setVisible(config.workflow_mode == "rough_cut")
