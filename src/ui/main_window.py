"""Single-Window Unified NLE Desktop Studio MainWindow for TeamTrau AutoEdit."""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.modules.edit_timeline import apply_scene_curation
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QAction, QCloseEvent, QFont, QUndoStack
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from src.core.config import DEFAULT_PRESET_NAME, PRESETS, PipelineConfig, check_cuda_available
from src.core.genre_presets import GENRE_PRESETS
from src.core.project_io import audio_state_to_dict, deliver_settings_to_dict, edit_timeline_to_dict
from src.core.service import JobHandle, StudioBackendAdapter
from src.core.stage_options import apply_genre_pacing
from src.core.types import (
    DeliverResult,
    DeliverSettings,
    EditTimeline,
    PipelineExecutionResult,
    ScoredScene,
)
from src.modules.edit_timeline import apply_scene_curation
from src.ui.adapter_bridge import BackendSignalBridge
from src.ui.controllers import (
    ClipDeleteCommand,
    ClipPasteCommand,
    ProjectData,
    ProjectManager,
    SceneToggleCommand,
    ShortcutManager,
)
from src.ui.controllers.project_manager import (
    DEFAULT_RECENT_PROJECTS_PATH,
    PIPELINE_STAGES,
    PROJECT_FILE_FILTER,
)
from src.ui.widgets.agent_console_dialog import AgentConsoleDialog
from src.ui.widgets.export_dialog import ExportDialog
from src.ui.widgets.export_panel import ExportPanelWidget, preset_label
from src.ui.widgets.inspector_panel import InspectorPanelWidget
from src.ui.widgets.media_browser import MediaBrowserWidget
from src.ui.widgets.player_widget import PlayerWidget
from src.ui.widgets.scene_grid import SceneGridWidget
from src.ui.widgets.settings_dialog import PreferencesDialog
from src.ui.widgets.timeline_view import TimelineClipItem, TimelineViewWidget, sort_variant_keys

logger = logging.getLogger(__name__)

# Workflow stepper (Phase 23): the four steps and the labels used in the progress banner.
STEP_NAMES: tuple[str, ...] = ("1. Ingest", "2. Rough Cut", "3. Sequence", "4. Deliver")
STAGE_LABELS: dict[str, str] = {"rough_cut": "Rough Cut", "sequence": "Sequence", "all": "Auto Run"}
ROUGH_CUT_READY_MESSAGE = "Rough cut ready — review takes, then press Auto-Edit Sequence"


class StudioMainWindow(QMainWindow):
    """Single-Window Unified NLE Desktop Studio window (CapCut / DaVinci Cut Page architecture)."""

    def __init__(self, backend_adapter: StudioBackendAdapter | None = None) -> None:
        super().__init__()
        self.adapter = backend_adapter or StudioBackendAdapter()
        self.bridge = BackendSignalBridge()
        self.project_manager = ProjectManager()
        self.undo_stack = QUndoStack(self)
        self._clipboard_clip: TimelineClipItem | None = None
        self._current_job: JobHandle | None = None
        self._agent_console_dialog: AgentConsoleDialog | None = None
        self._in_point: float | None = None
        self._out_point: float | None = None
        # Edit state: the EditTimelines produced by analysis (or restored from a project).
        # The timeline view holds the user's live edits; Export reads them from there.
        self._edit_timelines: dict[str, EditTimeline] = {}
        self._active_timeline_key: str = "master"
        # Staged workflow (Phase 23): where the project stands, the real soundtrack analysis and the
        # stage of the job that is running.
        self._pipeline_stage: str = "ingest"
        self._audio_state: Any = None
        self._running_stage: str | None = None
        self._pending_existing: dict[str, EditTimeline] = {}
        self._persist_ui_state: bool = False

        self._init_window()
        self._init_components()
        self._init_menus()
        self._init_topbar()
        self._init_ui()
        self._init_statusbar()
        self._init_shortcuts()
        self._connect_signals()
        self._update_export_enabled()
        self._refresh_stage_controls()

    def _init_window(self) -> None:
        """Configure window geometry, title, and initial attributes."""
        self.setWindowTitle("TeamTrau AutoEdit Desktop Studio - Untitled Project")
        self.resize(1440, 900)
        self.setMinimumSize(1024, 640)

    @property
    def config(self) -> PipelineConfig:
        """Assemble and return active PipelineConfig from UI components and preferences.

        NOTE: this builds a fresh object on every access - mutate a local ``_build_config()``
        result (never ``self.config``) when the change must persist.
        """
        return self._build_config()

    def _build_config(self) -> PipelineConfig:
        """Single source of truth for turning UI state into a PipelineConfig.

        Precedence (later wins): canvas preset -> Preferences (global defaults) ->
        Export panel -> workflow mode side effects -> Inspector (per-project edit controls).
        Settings that exist in two panels are reconciled explicitly:
        - workflow mode: the Inspector's combo is authoritative (it offers Rough Cut);
        - cloud/AI features (VLM tagging) and audio fix need BOTH panels enabled.
        """
        preset_name = (
            self.preset_combo.currentData()
            if hasattr(self, "preset_combo") and self.preset_combo.currentData()
            else DEFAULT_PRESET_NAME
        )
        cfg = PipelineConfig.from_preset(preset_name)
        prefs = getattr(self, "preferences_dialog", None)
        export = getattr(self, "export_panel", None)
        insp = getattr(self, "inspector_panel", None)

        if prefs is not None:
            prefs.apply_to_config(cfg)
        prefs_vlm, prefs_audio_fix = cfg.enable_vlm_tagging, cfg.enable_audio_fix
        if export is not None:
            export.apply_to_config(cfg)
        export_audio_fix = cfg.enable_audio_fix
        if insp is not None:
            mode = insp.combo_workflow.currentData() or "standard"
            cfg.apply_workflow_mode(mode)  # restores the mode's own side effects
            insp.apply_to_config(cfg)
            genre = insp.selected_genre_key()
            if genre:  # pacing knobs no panel shows come from the chosen genre
                apply_genre_pacing(cfg, genre)
            if prefs is not None:
                cfg.enable_vlm_tagging = cfg.enable_vlm_tagging and prefs_vlm
        cfg.enable_audio_fix = prefs_audio_fix and export_audio_fix
        return cfg

    def _init_components(self) -> None:
        """Instantiate core sub-widgets and dialogs."""
        # 1. Media Browser
        self.media_browser = MediaBrowserWidget(backend_adapter=self.adapter, parent=self)
        self.media_browser.media_changed.connect(self._on_media_changed)
        self.media_browser.footage_probed.connect(self._refresh_stage_controls)

        # 2. Scene Quality & Review
        self.scene_grid = SceneGridWidget(self)
        self.scene_grid.scene_toggled.connect(self._on_scene_toggled_by_user)
        self.scene_grid.scenes_changed.connect(self._refresh_stage_controls)

        # 3. Multi-Track Timeline
        self.timeline_view = TimelineViewWidget(self)

        # 4. Program Monitor Viewport
        self.player_widget = PlayerWidget(self)

        # 5. Smart Inspector
        self.inspector_panel = InspectorPanelWidget(self)
        self.inspector_panel.config_changed.connect(lambda: self._set_dirty(True))

        # 6. Export Dispatcher & Modal Dialog
        self.export_panel = ExportPanelWidget(self)
        self.export_dialog = ExportDialog(export_panel=self.export_panel, parent=self)
        self.export_dialog.start_export_requested.connect(self._on_start_deliver)
        self.export_dialog.render_preview_requested.connect(self._on_render_preview)
        self.export_panel.deliver_settings_changed.connect(lambda: self._set_dirty(True))

        # 7. Preferences Modal Dialog
        self.preferences_dialog = PreferencesDialog(backend_adapter=self.adapter, parent=self)
        self.preferences_dialog.settings_applied.connect(self._on_settings_applied)

    def _init_menus(self) -> None:
        """Initialize professional NLE menu bar items (Premiere Pro / DaVinci Resolve standards)."""
        menubar = self.menuBar()

        # -------------------------------------------------------------
        # 1. File Menu
        # -------------------------------------------------------------
        file_menu = menubar.addMenu("&File")

        self.action_new = QAction("&New Project", self)
        self.action_new.setShortcut("Ctrl+N")
        self.action_new.triggered.connect(self._on_new_project)
        file_menu.addAction(self.action_new)

        self.action_open = QAction("&Open Project...", self)
        self.action_open.setShortcut("Ctrl+O")
        self.action_open.triggered.connect(self._on_open_project)
        file_menu.addAction(self.action_open)

        # Open Recent Submenu
        self.recent_menu = file_menu.addMenu("Open &Recent")
        self._update_recent_menu()

        file_menu.addSeparator()

        self.action_save = QAction("&Save Project", self)
        self.action_save.setShortcut("Ctrl+S")
        self.action_save.triggered.connect(self._on_save_project)
        file_menu.addAction(self.action_save)

        self.action_save_as = QAction("Save Project &As...", self)
        self.action_save_as.setShortcut("Ctrl+Shift+S")
        self.action_save_as.triggered.connect(self._on_save_project_as)
        file_menu.addAction(self.action_save_as)

        file_menu.addSeparator()

        self.action_import_folder = QAction("&Import Media Folder...", self)
        self.action_import_folder.setShortcut("Ctrl+I")
        self.action_import_folder.triggered.connect(self._on_import_media_folder)
        file_menu.addAction(self.action_import_folder)

        self.action_import_audio = QAction("Import Soundtrack &Audio...", self)
        self.action_import_audio.setShortcut("Ctrl+Shift+I")
        self.action_import_audio.triggered.connect(self._on_import_soundtrack)
        file_menu.addAction(self.action_import_audio)

        file_menu.addSeparator()

        self.action_export = QAction("&Export...", self)
        self.action_export.setShortcut("Ctrl+M")
        self.action_export.setEnabled(False)  # Enabled once an edit timeline exists
        self.action_export.triggered.connect(self._on_open_export_dialog)
        file_menu.addAction(self.action_export)

        self.action_render_preview = QAction("Render &Preview (540p)", self)
        self.action_render_preview.setShortcut("Ctrl+Shift+M")
        self.action_render_preview.setEnabled(False)
        self.action_render_preview.triggered.connect(self._on_render_preview)
        file_menu.addAction(self.action_render_preview)

        file_menu.addSeparator()

        self.action_exit = QAction("&Exit", self)
        self.action_exit.setShortcut("Ctrl+Q")
        self.action_exit.triggered.connect(self.close)
        file_menu.addAction(self.action_exit)

        # -------------------------------------------------------------
        # 2. Edit Menu (Standard NLE Clipboard, Undo/Redo, Selection)
        # -------------------------------------------------------------
        edit_menu = menubar.addMenu("&Edit")

        self.action_undo = self.undo_stack.createUndoAction(self, "&Undo")
        self.action_undo.setShortcut("Ctrl+Z")
        edit_menu.addAction(self.action_undo)

        self.action_redo = self.undo_stack.createRedoAction(self, "&Redo")
        self.action_redo.setShortcut("Ctrl+Y")
        edit_menu.addAction(self.action_redo)

        edit_menu.addSeparator()

        self.action_cut = QAction("Cu&t Clip", self)
        self.action_cut.setShortcut("Ctrl+X")
        self.action_cut.triggered.connect(self._on_cut_clip)
        edit_menu.addAction(self.action_cut)

        self.action_copy = QAction("&Copy Clip", self)
        self.action_copy.setShortcut("Ctrl+C")
        self.action_copy.triggered.connect(self._on_copy_clip)
        edit_menu.addAction(self.action_copy)

        self.action_paste = QAction("&Paste Clip", self)
        self.action_paste.setShortcut("Ctrl+V")
        self.action_paste.triggered.connect(self._on_paste_clip)
        edit_menu.addAction(self.action_paste)

        self.action_delete = QAction("&Delete Clip", self)
        self.action_delete.setShortcut("Delete")
        self.action_delete.triggered.connect(self._on_delete_clip)
        edit_menu.addAction(self.action_delete)

        edit_menu.addSeparator()

        self.action_select_all = QAction("Select &All Scenes", self)
        self.action_select_all.setShortcut("Ctrl+A")
        self.action_select_all.triggered.connect(self.scene_grid.include_all)
        edit_menu.addAction(self.action_select_all)

        self.action_deselect_all = QAction("&Deselect All", self)
        self.action_deselect_all.setShortcut("Ctrl+D")
        self.action_deselect_all.triggered.connect(self._on_deselect_all)
        edit_menu.addAction(self.action_deselect_all)

        edit_menu.addSeparator()

        self.action_exclude_deadair = QAction("&Exclude Dead-Air & Freeze Scenes", self)
        self.action_exclude_deadair.triggered.connect(self._on_quick_exclude_dead_air)
        edit_menu.addAction(self.action_exclude_deadair)

        self.action_apply_curation = QAction("Apply Scene &Curation to Timeline", self)
        self.action_apply_curation.setToolTip(
            "Replace timeline clips that come from excluded scenes with included ones (keeps the beat grid)"
        )
        self.action_apply_curation.triggered.connect(self._on_apply_scene_curation)
        edit_menu.addAction(self.action_apply_curation)

        edit_menu.addSeparator()

        self.action_prefs = QAction("&Preferences & AI Settings...", self)
        self.action_prefs.setShortcut("Ctrl+,")
        self.action_prefs.triggered.connect(self._on_open_preferences_dialog)
        edit_menu.addAction(self.action_prefs)

        # -------------------------------------------------------------
        # 3. View Menu
        # -------------------------------------------------------------
        view_menu = menubar.addMenu("&View")
        toggle_hud_action = QAction("Toggle 9:16 Crop Guide HUD", self)
        toggle_hud_action.setCheckable(True)
        toggle_hud_action.setChecked(True)
        toggle_hud_action.triggered.connect(self.player_widget.set_vertical_mode)
        view_menu.addAction(toggle_hud_action)

        fit_zoom_action = QAction("Zoom Timeline to Fit", self)
        fit_zoom_action.setShortcut("Shift+Z")
        fit_zoom_action.triggered.connect(self.timeline_view.zoom_fit)
        view_menu.addAction(fit_zoom_action)

        # -------------------------------------------------------------
        # 4. Tools Menu
        # -------------------------------------------------------------
        tools_menu = menubar.addMenu("&Tools")
        clear_cache_action = QAction("&Clear Scene Cache", self)
        clear_cache_action.triggered.connect(self._on_clear_cache)
        tools_menu.addAction(clear_cache_action)

        ping_action = QAction("AI Diagnostic Test Ping", self)
        ping_action.triggered.connect(self._on_open_preferences_dialog)
        tools_menu.addAction(ping_action)

        # -------------------------------------------------------------
        # 5. AI Agent & MCP Hub Menu
        # -------------------------------------------------------------
        agent_menu = menubar.addMenu("&AI Agent")

        action_open_console = QAction("🤖 &Open AI Co-Pilot Console...", self)
        action_open_console.setShortcut("Ctrl+Shift+A")
        action_open_console.triggered.connect(self._on_open_agent_console)
        agent_menu.addAction(action_open_console)

        agent_menu.addSeparator()

        action_copy_antigravity = QAction("📋 Copy MCP Config for &Antigravity", self)
        action_copy_antigravity.triggered.connect(self._on_copy_antigravity_mcp_config)
        agent_menu.addAction(action_copy_antigravity)

        action_copy_claude = QAction("📋 Copy MCP Config for &Claude Desktop", self)
        action_copy_claude.triggered.connect(self._on_copy_claude_mcp_config)
        agent_menu.addAction(action_copy_claude)

        agent_menu.addSeparator()

        action_agent_settings = QAction("⚙️ AI Agent && MCP &Settings...", self)
        action_agent_settings.triggered.connect(self._on_open_preferences_dialog)
        agent_menu.addAction(action_agent_settings)

        # -------------------------------------------------------------
        # 6. Help Menu
        # -------------------------------------------------------------
        help_menu = menubar.addMenu("&Help")
        about_action = QAction("&About TeamTrau AutoEdit", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _init_topbar(self) -> None:
        """Initialize primary header bar matching NLE blueprint."""
        toolbar = QToolBar("Top Controls", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        # Project Branding & Name
        lbl_brand = QLabel(" 🎬 TeamTrau Studio  |  Project: ")
        lbl_brand.setStyleSheet("font-weight: bold; color: #f4f4f5;")
        toolbar.addWidget(lbl_brand)

        self.edit_project_name = QLineEdit("Untitled Project", self)
        self.edit_project_name.setFixedWidth(190)
        self.edit_project_name.setStyleSheet(
            "background: #18181b; border: 1px solid #27272a; border-radius: 4px; padding: 3px 8px; color: #38bdf8;"
        )
        self.edit_project_name.textChanged.connect(lambda: self._set_dirty(True))
        toolbar.addWidget(self.edit_project_name)

        self.lbl_saved = QLabel(" (Saved)  ")
        self.lbl_saved.setStyleSheet("color: #71717a; font-size: 11px;")
        toolbar.addWidget(self.lbl_saved)

        # Quick Project Buttons (Open, Save)
        self.btn_quick_open = QPushButton("📁 Open", self)
        self.btn_quick_open.setToolTip("Open Project (Ctrl+O)")
        self.btn_quick_open.setStyleSheet(
            "background: #18181b; border: 1px solid #27272a; color: #e4e4e7; border-radius: 4px; padding: 3px 8px; font-size: 11px;"
        )
        self.btn_quick_open.clicked.connect(self._on_open_project)
        toolbar.addWidget(self.btn_quick_open)

        self.btn_quick_save = QPushButton("💾 Save", self)
        self.btn_quick_save.setToolTip("Save Project (Ctrl+S)")
        self.btn_quick_save.setStyleSheet(
            "background: #18181b; border: 1px solid #27272a; color: #e4e4e7; border-radius: 4px; padding: 3px 8px; font-size: 11px;"
        )
        self.btn_quick_save.clicked.connect(self._on_save_project)
        toolbar.addWidget(self.btn_quick_save)

        toolbar.addSeparator()

        # Workflow Stepper Pill (Media -> Edit -> Deliver)
        self.lbl_workflow_steps = QLabel(self)
        self.lbl_workflow_steps.setStyleSheet(
            "background: #18181b; border: 1px solid #27272a; border-radius: 10px; padding: 2px 8px; font-size: 11px;"
        )
        self._update_workflow_stepper(1)
        toolbar.addWidget(self.lbl_workflow_steps)

        toolbar.addSeparator()

        # Canvas Preset / Aspect Ratio Selector
        toolbar.addWidget(QLabel(" Ratio: "))
        self.preset_combo = QComboBox(self)
        for name, preset in PRESETS.items():
            self.preset_combo.addItem(preset_label(preset), name)

        default_idx = self.preset_combo.findData(DEFAULT_PRESET_NAME)
        if default_idx >= 0:
            self.preset_combo.setCurrentIndex(default_idx)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        toolbar.addWidget(self.preset_combo)

        toolbar.addSeparator()

        # AI Engine status badge
        self.btn_ai_badge = QPushButton("🟢 AI Engine: Ready", self)
        self.btn_ai_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ai_badge.setToolTip("Click to configure AI Provider & Model")
        self.btn_ai_badge.clicked.connect(self._on_open_preferences_dialog)
        toolbar.addWidget(self.btn_ai_badge)

        # AI Co-Pilot Console Button
        self.btn_agent_console = QPushButton("🤖 AI Co-Pilot", self)
        self.btn_agent_console.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_agent_console.setStyleSheet(
            "QPushButton { background: #18181b; border: 1px solid #38bdf8; color: #38bdf8; "
            "border-radius: 4px; padding: 4px 10px; font-weight: 600; font-size: 11px; }"
            "QPushButton:hover { background: #0284c7; color: #ffffff; }"
            "QPushButton:pressed { background: #0369a1; color: #e0f2fe; }"
        )
        self.btn_agent_console.setToolTip("Open Autonomous AI Agent Co-Pilot (Ctrl+Shift+A)")
        self.btn_agent_console.clicked.connect(self._on_open_agent_console)
        toolbar.addWidget(self.btn_agent_console)

        # Sync topbar badge with active configuration
        self._update_ai_badge()

        # GPU Hardware badge
        gpu_active = check_cuda_available()
        gpu_text = "⚡ RTX 3070 CUDA" if gpu_active else "💻 CPU Mode"
        self.lbl_gpu_badge = QLabel(f" [{gpu_text}] ")
        self.lbl_gpu_badge.setStyleSheet("color: #38bdf8; font-weight: bold;")
        toolbar.addWidget(self.lbl_gpu_badge)

        # Spacer to push action buttons to right
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        # Stop Button
        self.btn_stop = QPushButton("⏹ Stop", self)
        self.btn_stop.setObjectName("dangerButton")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setToolTip("Cancel active pipeline execution")
        self.btn_stop.clicked.connect(self._on_stop_pipeline)
        toolbar.addWidget(self.btn_stop)

        # Primary Run / Export Button
        self.btn_preview = QPushButton("👁 Preview", self)
        self.btn_preview.setEnabled(False)
        self.btn_preview.setToolTip("Render a fast 540p preview of the current edit (Ctrl+Shift+M)")
        self.btn_preview.clicked.connect(self._on_render_preview)
        toolbar.addWidget(self.btn_preview)

        self.btn_run = QPushButton("🚀 Export", self)
        self.btn_run.setObjectName("primaryButton")
        self.btn_run.setEnabled(False)  # Enabled once an edit timeline exists
        self.btn_run.setToolTip("Export the finished edit: video and/or NLE files (Ctrl+M)")
        self.btn_run.clicked.connect(self._on_open_export_dialog)
        toolbar.addWidget(self.btn_run)

    def _init_ui(self) -> None:
        """Initialize central view using Single-Window Unified Splitters."""
        central = QWidget(self)
        central.setObjectName("centralWidget")
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(6, 6, 6, 6)
        central_layout.setSpacing(6)

        # Main Vertical Splitter: Upper (Left/Center/Right) and Lower (Timeline)
        self.main_splitter = QSplitter(Qt.Orientation.Vertical, central)
        self.main_splitter.setChildrenCollapsible(False)

        # --- UPPER SECTION: Horizontal Splitter ---
        self.upper_splitter = QSplitter(Qt.Orientation.Horizontal, self.main_splitter)
        self.upper_splitter.setChildrenCollapsible(False)

        # 1. Left Dock: Asset Library & Curation Tabbed Views
        left_dock = QWidget(self.upper_splitter)
        left_layout = QVBoxLayout(left_dock)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self.tab_widget = QTabWidget(left_dock)
        self.tab_widget.addTab(self.media_browser, "Media Assets")
        self.tab_widget.addTab(self.scene_grid, "Scene Quality & Review")

        # Audio placeholder tab
        tab_audio = QWidget()
        tab_audio_layout = QVBoxLayout(tab_audio)
        lbl_audio = QLabel("Audio Library & Stems", tab_audio)
        lbl_audio.setStyleSheet("color: #a1a1aa; font-weight: bold;")
        tab_audio_layout.addWidget(lbl_audio)
        tab_audio_layout.addWidget(QLabel("Load soundtracks in Media Assets to inspect waveforms."))
        tab_audio_layout.addStretch()
        self.tab_widget.addTab(tab_audio, "Audio & Music")

        # Effects & Text placeholder tab
        tab_fx = QWidget()
        tab_fx_layout = QVBoxLayout(tab_fx)
        lbl_fx = QLabel("Transitions & Motion Graphics", tab_fx)
        lbl_fx.setStyleSheet("color: #a1a1aa; font-weight: bold;")
        tab_fx_layout.addWidget(lbl_fx)
        tab_fx_layout.addWidget(QLabel("Configured via Smart Inspector & Pipeline Presets."))
        tab_fx_layout.addStretch()
        self.tab_widget.addTab(tab_fx, "Transitions & Text")

        # Execution Console & Logs Tab
        console_widget = QWidget(left_dock)
        console_layout = QVBoxLayout(console_widget)
        console_layout.setContentsMargins(6, 6, 6, 6)
        lbl_console = QLabel("Execution Console & Metrics", console_widget)
        lbl_console.setStyleSheet("font-weight: bold; color: #e4e4e7;")
        console_layout.addWidget(lbl_console)

        self.console = QPlainTextEdit(console_widget)
        self.console.setReadOnly(True)
        font_mono = QFont("Consolas", 10)
        self.console.setFont(font_mono)
        self.console.setPlaceholderText(
            "Pipeline execution output, scene discoveries, and render metrics will appear here..."
        )
        console_layout.addWidget(self.console)
        self.tab_widget.addTab(console_widget, "Execution Console & Logs")

        left_layout.addWidget(self.tab_widget)
        self.upper_splitter.addWidget(left_dock)

        # 2. Center Dock: Program Monitor Viewport
        self.upper_splitter.addWidget(self.player_widget)

        # 3. Right Dock: Smart Inspector
        self.upper_splitter.addWidget(self.inspector_panel)

        # Set upper splitter balance: Left ~360px, Center flex, Right ~320px
        self.upper_splitter.setStretchFactor(0, 3)
        self.upper_splitter.setStretchFactor(1, 5)
        self.upper_splitter.setStretchFactor(2, 3)

        self.main_splitter.addWidget(self.upper_splitter)

        # --- LOWER SECTION: Docked Full-Width Multi-Track Timeline ---
        timeline_container = QWidget(self.main_splitter)
        timeline_layout = QVBoxLayout(timeline_container)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(0)
        timeline_layout.addWidget(self.timeline_view)
        self.main_splitter.addWidget(timeline_container)

        # Set vertical balance: Upper 60%, Lower Timeline 40%
        self.main_splitter.setStretchFactor(0, 6)
        self.main_splitter.setStretchFactor(1, 4)

        central_layout.addWidget(self.main_splitter, stretch=1)

        # Bottom Progress Bar & Stage Banner
        banner_box = QHBoxLayout()
        banner_box.setContentsMargins(4, 2, 4, 2)
        self.lbl_stage = QLabel("Status: Idle")
        font_stage = QFont()
        font_stage.setPointSize(11)
        font_stage.setBold(True)
        self.lbl_stage.setFont(font_stage)
        banner_box.addWidget(self.lbl_stage)

        self.lbl_message = QLabel("Awaiting media selection...")
        self.lbl_message.setStyleSheet("color: #a1a1aa;")
        banner_box.addWidget(self.lbl_message)
        banner_box.addStretch()

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedWidth(260)
        banner_box.addWidget(self.progress_bar)

        central_layout.addLayout(banner_box)
        self.setCentralWidget(central)

    def _init_statusbar(self) -> None:
        """Initialize status bar at the bottom of the window."""
        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready | Unified NLE Workspace Active | J-K-L Shuttle Enabled")

    def _init_shortcuts(self) -> None:
        """Install centralized ShortcutManager with Focus Guard."""
        self.shortcut_manager = ShortcutManager(self)
        self.shortcut_manager.install()

        # Connect Shortcut Signals
        self.shortcut_manager.play_pause_requested.connect(self.player_widget.toggle_play_pause)
        self.shortcut_manager.shuttle_changed.connect(self.player_widget.set_shuttle_rate)
        self.shortcut_manager.step_frame_requested.connect(self.player_widget.step_frame)
        self.shortcut_manager.step_seconds_requested.connect(self.player_widget.step_seconds)
        self.shortcut_manager.jump_cut_requested.connect(self._on_shortcut_jump_cut)
        self.shortcut_manager.seek_extreme_requested.connect(self._on_shortcut_seek_extreme)
        self.shortcut_manager.mark_in_requested.connect(self._on_shortcut_mark_in)
        self.shortcut_manager.mark_out_requested.connect(self._on_shortcut_mark_out)
        self.shortcut_manager.clear_in_out_requested.connect(self._on_shortcut_clear_in_out)
        self.shortcut_manager.scene_action_requested.connect(self._on_shortcut_scene_action)
        self.shortcut_manager.zoom_requested.connect(self._on_shortcut_zoom)
        self.shortcut_manager.zoom_fit_requested.connect(self.timeline_view.zoom_fit)
        self.shortcut_manager.open_export_requested.connect(self._on_open_export_dialog)
        self.shortcut_manager.open_preferences_requested.connect(self._on_open_preferences_dialog)

        # Connect Project & Edit Shortcuts
        self.shortcut_manager.new_project_requested.connect(self._on_new_project)
        self.shortcut_manager.open_project_requested.connect(self._on_open_project)
        self.shortcut_manager.save_project_requested.connect(self._on_save_project)
        self.shortcut_manager.save_project_as_requested.connect(self._on_save_project_as)
        self.shortcut_manager.import_media_requested.connect(self._on_import_media_folder)
        self.shortcut_manager.undo_requested.connect(self.undo_stack.undo)
        self.shortcut_manager.redo_requested.connect(self.undo_stack.redo)
        self.shortcut_manager.cut_requested.connect(self._on_cut_clip)
        self.shortcut_manager.copy_requested.connect(self._on_copy_clip)
        self.shortcut_manager.paste_requested.connect(self._on_paste_clip)
        self.shortcut_manager.delete_requested.connect(self._on_delete_clip)
        self.shortcut_manager.select_all_requested.connect(self.scene_grid.include_all)
        self.shortcut_manager.deselect_all_requested.connect(self._on_deselect_all)

    def _connect_signals(self) -> None:
        """Connect bridge and inter-component signals."""
        self.bridge.stage_changed.connect(self._on_stage_changed)
        self.bridge.progress_updated.connect(self._on_progress_updated)
        self.bridge.scene_discovered.connect(self._on_scene_discovered)
        self.bridge.scenes_tagged.connect(self._on_scenes_tagged)
        self.bridge.log_received.connect(self._on_log_received)
        self.bridge.job_finished.connect(self._on_job_finished)
        self.bridge.deliver_finished.connect(self._on_deliver_finished)

        # Bidirectional Player <-> Timeline playhead sync
        self.player_widget.time_changed.connect(self._on_player_time_changed)
        self.player_widget.jump_cut_requested.connect(self._on_shortcut_jump_cut)
        self.player_widget.theater_mode_toggled.connect(self._on_toggle_theater_mode)
        self.player_widget.snapshot_saved.connect(self._on_snapshot_saved)
        self.timeline_view.canvas.playhead_moved.connect(self._on_timeline_playhead_changed)

        # Media Library & Scene Review -> Player Viewport preview sync
        self.media_browser.video_selected.connect(self._on_source_video_selected)
        self.media_browser.preset_recommended.connect(self._on_preset_recommended)
        self.scene_grid.scene_selected.connect(self._on_scene_selected_for_preview)

        # Timeline variant switcher -> Monitor live sequence playback
        self.timeline_view.variant_switched.connect(self._on_timeline_variant_switched)
        self.timeline_view.timeline_edited.connect(self._on_timeline_edited)

        # Inspector actions
        self.inspector_panel.run_rough_cut_clicked.connect(self._on_run_rough_cut)
        self.inspector_panel.run_sequence_clicked.connect(self._on_run_sequence)
        self.inspector_panel.analyze_optimize_clicked.connect(self._on_run_full_auto)
        self.inspector_panel.genre_changed.connect(self._on_genre_selected)
        self.inspector_panel.combo_workflow.currentIndexChanged.connect(lambda _i: self._refresh_stage_controls())
        self.inspector_panel.open_preferences_clicked.connect(self._on_open_preferences_dialog)

    # --- Inter-Component Synchronization Slots ---

    @Slot(float)
    def _on_player_time_changed(self, time_sec: float) -> None:
        """Update timeline playhead when monitor playback ticks or seeks."""
        self.timeline_view.set_playhead_time(time_sec)

    @Slot(float)
    def _on_timeline_playhead_changed(self, time_sec: float) -> None:
        """Update monitor viewport timecode when timeline is scrubbed."""
        self.player_widget.switch_to_timeline_mode()
        self.player_widget.set_time(time_sec)

    @Slot(object)
    def _on_preset_recommended(self, preset: Any) -> None:
        """Apply recommended genre preset to the UI (and thus to every later job config)."""
        if not hasattr(preset, "apply"):
            return
        # Presets mutate a config; keep that ONE object and push it back into the panels.
        cfg = self._build_config()
        preset.apply(cfg)
        self.inspector_panel.set_genre(str(getattr(preset, "key", "")), emit=False)
        if hasattr(preset, "preferred_render_preset"):
            idx = self.preset_combo.findData(preset.preferred_render_preset)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
        self.inspector_panel.load_from_config(cfg)
        self.preferences_dialog.load_from_config(cfg)
        self.export_panel.load_from_config(cfg)
        self.status_bar.showMessage(f"AI Preset Applied: {getattr(preset, 'display_name', '')}", 4000)
        self._append_console(f"[AI PRESET] Applied: {getattr(preset, 'major_genre', '')} ➔ {getattr(preset, 'display_name', '')}")

    @Slot(str)
    def _on_genre_selected(self, key: str) -> None:
        """Genre combo changed: ONE config is built, the genre's preset applied, the panels reloaded.

        The canvas (ratio / resolution / fps) is kept; only pacing, cadence and look follow the genre.
        Press "2. Auto-Edit Sequence" afterwards for a quick re-edit (no re-ingest).
        """
        self._set_dirty(True)
        preset = GENRE_PRESETS.get(key)
        if preset is not None:
            cfg = self._build_config()
            canvas = (cfg.preset_name, cfg.canvas_width, cfg.canvas_height, cfg.fps)
            preset.apply(cfg)
            cfg.preset_name, cfg.canvas_width, cfg.canvas_height, cfg.fps = canvas
            self.inspector_panel.load_from_config(cfg)
            self.preferences_dialog.load_from_config(cfg)
            self.export_panel.load_from_config(cfg)
            self.status_bar.showMessage(f"Genre: {preset.display_name} ({preset.cadence_profile})", 4000)
            self._append_console(f"[GENRE] {preset.major_genre.value} ➔ {preset.display_name} ({preset.cadence_profile})")
        else:
            self.status_bar.showMessage("Genre: Auto (no preset)", 3000)
        if self._edit_timelines:
            self.lbl_message.setText("Genre changed - press 2. Auto-Edit Sequence to re-edit with the new rhythm")

    @Slot(str, object)
    def _on_timeline_variant_switched(self, variant_name: str, variant: Any) -> None:
        """Update Program Monitor sequence when a different timeline / variant is clicked."""
        if isinstance(variant, EditTimeline):
            self._active_timeline_key = variant_name
            self.undo_stack.clear()
            self.player_widget.load_edit_timeline(variant)
            self.status_bar.showMessage(f"Active Sequence: {variant.name}", 3000)
            self._append_console(f"[VARIANT] Switched to: {variant.name}")
            self._set_dirty(True)
            return
        scenes = getattr(variant, "sequence_scenes", [])
        dur = getattr(variant, "total_duration", 0.0)
        if scenes:
            self.player_widget.load_timeline_sequence(scenes, total_duration=dur)
            disp_name = getattr(variant, "display_name", variant_name)
            self.status_bar.showMessage(f"Active Sequence: {disp_name}", 3000)
            self._append_console(f"[VARIANT] Switched to: {disp_name}")

    @Slot()
    def _on_timeline_edited(self) -> None:
        """Mark project dirty and refresh the monitor sequence after a timeline edit."""
        self._set_dirty(True)
        if self._edit_timelines:
            self.player_widget.load_edit_timeline(self.timeline_view.get_edit_timeline())

    @Slot(int)
    def _on_preset_changed(self, index: int) -> None:
        """Update HUD 9:16 guide when canvas preset aspect ratio changes."""
        preset_name = self.preset_combo.currentData()
        if preset_name in PRESETS:
            preset = PRESETS[preset_name]
            is_vert = preset.width < preset.height
            self.player_widget.set_vertical_mode(is_vert)

    @Slot()
    def _on_open_export_dialog(self) -> None:
        """Display Export Modal Dialog (only once an edit timeline exists)."""
        if not self._has_edit_timeline():
            self.status_bar.showMessage("Nothing to export yet - run Rough Cut or Auto-Edit Sequence first", 4000)
            return
        self.export_panel.set_output_dir(self.media_browser.get_output_dir())
        self._refresh_deliver_panel()
        self._update_export_enabled()
        self.export_dialog.exec()

    @Slot()
    def _on_open_preferences_dialog(self) -> None:
        """Display Preferences and AI Configuration Modal Dialog."""
        self.preferences_dialog.exec()

    def _update_ai_badge(self, config: PipelineConfig | None = None) -> None:
        """Synchronize topbar AI badge with active PipelineConfig settings and Cloud/Local status."""
        cfg = config or self.config
        prov = (cfg.vlm_provider or "ollama").lower()
        model = cfg.vlm_model or "qwen2.5:7b"
        short_model = model.split("/")[-1]

        base_url = (cfg.vlm_base_url or "").lower()
        is_cloud = (
            prov in ("openrouter", "openai", "deepseek")
            or "openrouter.ai" in base_url
            or "api.openai.com" in base_url
            or "api.deepseek.com" in base_url
        )
        if not is_cloud and prov == "custom":
            if base_url and not any(loc in base_url for loc in ("localhost", "127.0.0.1", "192.168.", "10.", "172.")):
                is_cloud = True

        if is_cloud:
            tier_label = "Cloud"
            prov_name = {
                "openrouter": "OpenRouter",
                "deepseek": "DeepSeek",
                "openai": "OpenAI",
            }.get(prov, prov.title())
            style = (
                "QPushButton { background: #082f49; border: 1px solid #0284c7; color: #38bdf8; "
                "border-radius: 4px; padding: 4px 10px; font-weight: 600; font-size: 11px; }"
                "QPushButton:hover { background: #0369a1; color: #ffffff; }"
                "QPushButton:pressed { background: #075985; color: #e0f2fe; }"
            )
            tooltip = f"AI Engine: {prov_name} [Cloud API]\nTarget Model: {model}\nClick to configure"
        else:
            tier_label = "Local"
            prov_name = {
                "ollama": "Ollama",
                "lm_studio": "LM Studio",
            }.get(prov, prov.title())
            style = (
                "QPushButton { background: #052e16; border: 1px solid #16a34a; color: #4ade80; "
                "border-radius: 4px; padding: 4px 10px; font-weight: 600; font-size: 11px; }"
                "QPushButton:hover { background: #15803d; color: #ffffff; }"
                "QPushButton:pressed { background: #166534; color: #dcfce7; }"
            )
            tooltip = f"AI Engine: {prov_name} [Local/LAN]\nTarget Model: {model}\nClick to configure"

        self.btn_ai_badge.setText(f"🟢 {tier_label}: {prov_name} ({short_model})")
        self.btn_ai_badge.setStyleSheet(style)
        self.btn_ai_badge.setToolTip(tooltip)

    @Slot(object)
    def _on_settings_applied(self, config: PipelineConfig) -> None:
        """Update UI elements reflecting new preferences configuration."""
        self.preferences_dialog.load_from_config(config)
        self.preferences_dialog.apply_to_config(self.preferences_dialog.config)
        self._update_ai_badge(config)
        if self._agent_console_dialog is not None:
            self._agent_console_dialog.update_config(config)
        prov = (config.vlm_provider or "ollama").title()
        model = config.vlm_model or "qwen2.5:7b"
        self._append_console(f"[SETTINGS] Applied AI provider: {prov}, model: {model}")

    @Slot(object)
    def _on_source_video_selected(self, video_path: Path) -> None:
        """Load selected source footage into Program Monitor for instant live preview."""
        if video_path and Path(video_path).is_file():
            ok = self.player_widget.load_source_video(video_path)
            if ok:
                self.status_bar.showMessage(f"Previewing source: {video_path.name}", 3000)

    @Slot(object)
    def _on_scene_selected_for_preview(self, scene: ScoredScene | None) -> None:
        """Load selected scene cut into Program Monitor for inspection."""
        if scene is not None and getattr(scene, "scene", None) and scene.scene.clip_path.is_file():
            ok = self.player_widget.load_source_video(
                scene.scene.clip_path,
                start_time=scene.scene.start_time,
                end_time=scene.scene.end_time,
            )
            if ok:
                self.status_bar.showMessage(
                    f"Previewing scene: {scene.scene.clip_path.name} ({scene.scene.start_time:.1f}s - {scene.scene.end_time:.1f}s)",
                    3000,
                )

    @Slot(bool)
    def _on_toggle_theater_mode(self, enabled: bool) -> None:
        """Toggle full viewport cinema presentation mode."""
        self.tab_widget.setVisible(not enabled)
        self.inspector_panel.setVisible(not enabled)
        self.status_bar.showMessage(
            "Theater Cinema Viewport Active (F11 to toggle)" if enabled else "Standard Studio Layout Restored",
            3000,
        )

    @Slot(str)
    def _on_snapshot_saved(self, path: str) -> None:
        """Log keyframe snapshot export confirmation."""
        self.status_bar.showMessage(f"Snapshot exported to: {path}", 4000)
        self._append_console(f"[SNAPSHOT] Keyframe exported: {path}")

    # --- Shortcut Handlers ---

    @Slot(int)
    def _on_shortcut_jump_cut(self, direction: int) -> None:
        """Jump playhead to previous (-1) or next (+1) cut boundary."""
        clips = self.timeline_view.canvas.clips
        if not clips:
            return
        curr = self.player_widget.current_time
        boundaries: list[float] = [0.0]
        for c in clips:
            boundaries.append(c.start_time)
            boundaries.append(c.end_time)
        boundaries = sorted(list(set(boundaries)))

        if direction > 0:
            for b in boundaries:
                if b > curr + 0.05:
                    self.player_widget.seek_time(b)
                    return
            self.player_widget.seek_to_end()
        else:
            for b in reversed(boundaries):
                if b < curr - 0.05:
                    self.player_widget.seek_time(b)
                    return
            self.player_widget.seek_to_start()

    @Slot(str)
    def _on_shortcut_seek_extreme(self, target: str) -> None:
        if target == "start":
            self.player_widget.seek_to_start()
        elif target == "end":
            self.player_widget.seek_to_end()

    @Slot()
    def _on_shortcut_mark_in(self) -> None:
        tc = self.player_widget.current_time
        self._in_point = tc
        self.status_bar.showMessage(f"Marked IN point at {tc:.2f}s", 3000)
        self._append_console(f"[IN POINT] Set at {tc:.2f}s")

    @Slot()
    def _on_shortcut_mark_out(self) -> None:
        tc = self.player_widget.current_time
        self._out_point = tc
        self.status_bar.showMessage(f"Marked OUT point at {tc:.2f}s", 3000)
        self._append_console(f"[OUT POINT] Set at {tc:.2f}s")

    @Slot()
    def _on_shortcut_clear_in_out(self) -> None:
        self._in_point = None
        self._out_point = None
        self.status_bar.showMessage("Cleared IN / OUT range", 3000)
        self._append_console("[IN/OUT] Range cleared")

    @Slot(str)
    def _on_shortcut_scene_action(self, action: str) -> None:
        """Apply curation hotkey (pin, reject, favorite) to currently selected scene row."""
        row = self.scene_grid.table.currentRow()
        if row < 0:
            # Never act on an arbitrary scene when nothing is selected.
            self.status_bar.showMessage("Select a scene first (P = keep, X = reject, F = favorite)", 2500)
            return
        if row >= 0:
            target_inc = action in ("pin", "favorite")
            cmd = SceneToggleCommand(self.scene_grid, row, target_inc)
            self.undo_stack.push(cmd)
            self._set_dirty(True)
            status_txt = "PINNED / FAVORITED" if target_inc else "REJECTED"
            self.status_bar.showMessage(f"Scene #{row + 1} {status_txt}", 2000)

    @Slot(float)
    def _on_shortcut_zoom(self, factor: float) -> None:
        curr_zoom = self.timeline_view.canvas.px_per_sec
        new_zoom = max(5.0, min(120.0, curr_zoom * factor))
        self.timeline_view.slider_zoom.setValue(int(new_zoom))

    # --- Standard Studio Pipeline Handlers ---

    @Slot(str, int, int)
    def _on_stage_changed(self, name: str, idx: int, total: int) -> None:
        tag = f"[{STAGE_LABELS[self._running_stage]}] " if self._running_stage in STAGE_LABELS else ""
        self.lbl_stage.setText(f"{tag}Stage {idx}/{total}: {name}")
        self._append_console(f"\n{tag}[STAGE {idx}/{total}] {name}")

    @Slot(float, float, str)
    def _on_progress_updated(self, stage_pct: float, total_pct: float, message: str) -> None:
        self.progress_bar.setValue(int(total_pct))
        self.lbl_message.setText(message)
        self.export_panel.update_render_progress(stage_pct, message)
        if message:
            self._append_console(f"  -> {message}")

    @Slot(object)
    def _on_scene_discovered(self, scene: ScoredScene) -> None:
        self.scene_grid.update_or_add_scene(scene)
        vlm_str = f" [VLM: {scene.vlm_analysis.mood}]" if scene.vlm_analysis else ""
        self._append_console(
            f"  [SCENE] {scene.scene.clip_path.name} "
            f"({scene.scene.start_time:.1f}s - {scene.scene.end_time:.1f}s) "
            f"Score: {scene.composite_score:.2f}{vlm_str}"
        )

    @Slot(list)
    def _on_scenes_tagged(self, scenes: list[ScoredScene]) -> None:
        """Update scene grid with VLM tagged annotations received from Stage 5."""
        self.scene_grid.set_scenes(scenes)
        self._append_console(f"  [VLM] Updated {len(scenes)} scenes with semantic AI analysis.")

    @Slot(str, str)
    def _on_log_received(self, level: str, message: str) -> None:
        self._append_console(f"[{level.upper()}] {message}")

    @Slot(object)
    def _on_job_finished(self, result: PipelineExecutionResult) -> None:
        """A stage finished: load the edit timelines. Nothing is exported until the user asks.

        Rough cut -> golden takes into the review grid, Scene Review tab, ``rough_cut`` timeline active.
        Sequence / all -> master (+ variants) active, the rough cut is kept when there is one.
        """
        self.btn_stop.setEnabled(False)
        self._current_job = None
        stage = getattr(result, "stage", None) or self._running_stage or "all"
        label = STAGE_LABELS.get(stage, "Pipeline")
        pending, self._pending_existing = self._pending_existing, {}
        self._running_stage = None
        self._refresh_stage_controls()

        if result.success:
            if result.scored_scenes:
                self.scene_grid.set_scenes(result.scored_scenes)
            for warning in getattr(result, "warnings", None) or []:
                self._append_console(f"[WARN] {warning}")

            new_timelines = dict(result.edit_timelines)
            if stage == "sequence" and "rough_cut" not in new_timelines and "rough_cut" in pending:
                new_timelines = {"rough_cut": pending["rough_cut"], **new_timelines}
            self._edit_timelines = {k: new_timelines[k] for k in sort_variant_keys(new_timelines)}  # rough cut first

            only_rough = bool(self._edit_timelines) and all(t.kind == "rough_cut" for t in self._edit_timelines.values())
            rough_done = stage == "rough_cut" or only_rough
            self._pipeline_stage = "rough_cut" if rough_done else "sequence"

            audio = getattr(result, "audio_analysis", None)
            if audio is not None:
                self._audio_state = audio
                self.timeline_view.set_audio_state(audio)

            if rough_done:
                self.lbl_stage.setText("Status: Rough cut ready")
                self.lbl_message.setText(ROUGH_CUT_READY_MESSAGE)
                self._append_console("\n=== Rough cut complete ===")
                self._append_console(ROUGH_CUT_READY_MESSAGE)
                self.status_bar.showMessage(ROUGH_CUT_READY_MESSAGE, 8000)
            else:
                self.lbl_stage.setText("Status: Timeline ready")
                self.lbl_message.setText(
                    f"Timeline ready - press Export when you're happy ({result.total_duration:.1f}s, {result.clip_count} clips)"
                )
                self._append_console("\n=== Analysis complete: Timeline ready ===")
                self._append_console("Timeline ready - press Export when you're happy.")
                self.status_bar.showMessage("Timeline ready - press Export when you're happy", 8000)
            self.progress_bar.setValue(100)

            if self._edit_timelines:
                if rough_done and "rough_cut" in self._edit_timelines:
                    key = "rough_cut"
                elif "master" in self._edit_timelines:
                    key = "master"
                else:
                    key = next(iter(self._edit_timelines))
                self._active_timeline_key = key
                self.timeline_view.set_edit_timelines(
                    self._edit_timelines, active_key=key, tempo_bpm=result.audio_bpm
                )
                self.player_widget.load_edit_timeline(self._edit_timelines[key])
                self.player_widget.set_duration(self._edit_timelines[key].duration or result.total_duration)
                self._adopt_timeline_canvas(self._edit_timelines[key])
                self._refresh_deliver_panel()
                self._update_workflow_stepper(PIPELINE_STAGES.index(self._pipeline_stage) + 1)
                self._set_dirty(True)
                if rough_done:
                    self.tab_widget.setCurrentWidget(self.scene_grid)
            else:
                self._append_console("[WARN] Analysis produced no edit timeline - nothing to export.")
        elif getattr(result, "cancelled", False):
            self.lbl_stage.setText(f"Status: {label} cancelled")
            self.lbl_message.setText(f"{label} cancelled.")
            self.progress_bar.setValue(0)
            self._append_console(f"\n[USER] {label} cancelled.")
        else:
            self.lbl_stage.setText("Status: Failed")
            self.lbl_message.setText("Pipeline encountered an error.")
            self._append_console(f"\n[ERROR] Execution failed: {result.error_message}")
        self._update_export_enabled()
        self._refresh_stage_controls()

    @Slot(object)
    def _on_deliver_finished(self, result: DeliverResult) -> None:
        """Export / preview finished: surface deliverables or load the preview proxy."""
        self.btn_stop.setEnabled(False)
        self._current_job = None
        self._update_export_enabled()
        self._refresh_stage_controls()

        if getattr(result, "cancelled", False):
            self.lbl_stage.setText("Status: Export cancelled")
            self.lbl_message.setText("Export cancelled.")
            self.progress_bar.setValue(0)
            self._append_console("\n[USER] Export cancelled - no partial files were kept.")
            return

        if not result.success:
            self.lbl_stage.setText("Status: Export failed")
            self.lbl_message.setText("Export encountered an error.")
            self._append_console(f"\n[ERROR] Export failed: {result.error_message}")
            self.export_panel.set_deliverables(result)
            return

        if result.is_preview:
            self.lbl_stage.setText("Status: Preview ready")
            self.lbl_message.setText("540p preview rendered")
            self.progress_bar.setValue(100)
            if result.render_path and Path(result.render_path).is_file():
                ok = self.player_widget.load_source_video(result.render_path)
                self._append_console(f"[PREVIEW] {result.render_path}" + ("" if ok else " (could not load in monitor)"))
            return

        self.export_panel.set_deliverables(result)
        self._set_dirty(True)
        self._pipeline_stage = "deliver"
        self._update_workflow_stepper(4)
        self.lbl_stage.setText("Status: Export complete")
        self.lbl_message.setText("Export finished")
        self.progress_bar.setValue(100)
        self._append_console("\n=== Export Completed Successfully ===")
        if result.render_path:
            self._append_console(f"Master Render: {result.render_path} ({result.encoder_used or 'n/a'})")
        for label, path in (
            ("DaVinci FCPXML", result.fcpxml_path),
            ("Premiere XML", result.premiere_xml_path),
            ("OpenTimelineIO", result.otio_path),
            ("CapCut Draft", result.capcut_draft_path),
        ):
            if path:
                self._append_console(f"{label}: {path}")

    def _refresh_deliver_panel(self) -> None:
        """Offer the current timelines as variants and re-check sources against the Deliver canvas."""
        if not self._has_edit_timeline():
            return
        live = self.timeline_view.get_edit_timelines() or self._edit_timelines
        active = self.timeline_view.get_active_timeline_key() or self._active_timeline_key
        keys = list(live) or [active]
        self.export_panel.set_available_variants(keys, active)
        self.export_panel.set_source_timeline(self.timeline_view.get_edit_timeline())

    def _adopt_timeline_canvas(self, timeline: EditTimeline) -> None:
        """Analysis defaults: the Deliver canvas / fps follow the timeline, other choices are kept."""
        panel = self.export_panel
        base = DeliverSettings.from_timeline(timeline, self._build_config())
        current = dataclasses.replace(
            panel.get_deliver_settings(base),
            canvas_width=timeline.canvas_width,
            canvas_height=timeline.canvas_height,
            fps=timeline.fps,
        )
        panel.load_deliver_settings(current)

    def _has_edit_timeline(self) -> bool:
        """True once analysis (or a project) has provided at least one edit timeline."""
        return bool(self._edit_timelines)

    def _update_export_enabled(self) -> None:
        """Enable Export / Preview controls only with an edit timeline and no running job."""
        running = bool(self._current_job and self._current_job.is_running())
        enabled = self._has_edit_timeline() and not running
        for widget in (self.action_export, self.action_render_preview, self.btn_run, self.btn_preview):
            widget.setEnabled(enabled)
        self.export_dialog.set_export_enabled(enabled)

    def _append_console(self, text: str) -> None:
        self.console.appendPlainText(text)

    # --- Staged workflow: availability, buttons, jobs ---

    def _is_job_running(self) -> bool:
        return bool(self._current_job and self._current_job.is_running())

    def _stage_availability(self) -> dict[str, tuple[bool, str]]:
        """(runnable, reason when not) for the three stage buttons.

        * Rough Cut: footage only.
        * Sequence: a soundtrack AND golden / curated scenes in the review grid or footage to ingest.
        * Auto Run All: footage + soundtrack (the soundtrack is optional in the standalone rough-cut workflow).
        """
        mb = self.media_browser
        footage_ok, footage_reason = mb.is_valid_setup(workflow_mode="rough_cut")
        has_music = mb.get_soundtrack_path() is not None
        music_reason = "Select a soundtrack in Media Assets."
        wf = self.inspector_panel.combo_workflow.currentData() or "standard"

        sequence: tuple[bool, str]
        if not has_music:
            sequence = (False, music_reason)
        elif self.scene_grid.get_all_scenes():
            if self.scene_grid.get_included_scenes():
                sequence = (True, "")
            else:
                sequence = (False, "Every scene is excluded - include at least one take.")
        elif footage_ok:
            sequence = (True, "")
        else:
            sequence = (False, "Run Rough Cut first, or load footage.")

        full: tuple[bool, str]
        if not footage_ok:
            full = (False, footage_reason)
        elif not has_music and wf != "rough_cut":
            full = (False, music_reason)
        else:
            full = (True, "")
        return {"rough_cut": (footage_ok, footage_reason), "sequence": sequence, "all": full}

    @Slot()
    def _refresh_stage_controls(self) -> None:
        """Re-evaluate the stage buttons (media, soundtrack, scenes, running job) and the time estimate."""
        insp = getattr(self, "inspector_panel", None)
        if insp is None or not hasattr(self, "scene_grid") or not hasattr(self, "media_browser"):
            return
        insp.set_stage_states(self._stage_availability(), busy=self._is_job_running())
        total_sec, probed, total = self.media_browser.get_footage_probe_summary()
        insp.set_rough_cut_estimate(total_sec, probed, total)

    @Slot()
    def _on_media_changed(self) -> None:
        self.export_panel.set_output_dir(self.media_browser.get_output_dir())
        self._refresh_stage_controls()
        if self._is_job_running():
            return
        avail = self._stage_availability()
        names = (("rough_cut", "Rough Cut"), ("sequence", "Auto-Edit Sequence"), ("all", "Auto Run All"))
        runnable = [label for key, label in names if avail[key][0]]
        if runnable:
            self.lbl_message.setText("Ready to run: " + ", ".join(runnable))
        else:
            self.lbl_message.setText(avail["all"][1] or avail["rough_cut"][1])

    @Slot()
    def _on_run_rough_cut(self) -> None:
        """1. Rough Cut: footage only, golden takes + a ``rough_cut`` timeline."""
        self._start_pipeline_job("rough_cut")

    @Slot()
    def _on_run_sequence(self) -> None:
        """2. Auto-Edit Sequence: reuses the reviewed scenes in the grid (no re-ingest) when there are any."""
        self._start_pipeline_job("sequence")

    @Slot()
    def _on_run_full_auto(self) -> None:
        """Auto Run All: rough cut (when ticked) then sequence in one go."""
        self._start_pipeline_job("all")

    def _on_start_pipeline(self) -> None:
        """Trigger the full automatic pipeline (kept entry point: delegates to Auto Run All)."""
        self._start_pipeline_job("all")

    def _start_pipeline_job(self, stage: str) -> None:
        """Submit an analysis job for ``stage`` (rough_cut | sequence | all). Analysis never exports."""
        if self._is_job_running():
            QMessageBox.information(
                self,
                "Pipeline In Progress",
                "An editing or rendering job is already actively running. Please wait for completion or click Stop.",
            )
            return
        if stage not in STAGE_LABELS:
            raise ValueError(f"Unknown stage '{stage}'")

        runnable, reason = self._stage_availability()[stage]
        if not runnable:
            QMessageBox.warning(self, "Incomplete Project Setup", reason)
            return

        label = STAGE_LABELS[stage]
        cfg = self._build_config()

        # Sequence re-uses the reviewed scenes (inclusion flags travel in metadata): no re-ingest.
        scenes: list[ScoredScene] | None = None
        existing: dict[str, EditTimeline] | None = None
        if stage == "sequence" and self.scene_grid.get_all_scenes():
            scenes = [
                dataclasses.replace(sc, metadata={**(sc.metadata or {}), "included": self.scene_grid.is_scene_included(i)})
                for i, sc in enumerate(self.scene_grid.get_all_scenes())
            ]
            existing = dict(self.timeline_view.get_edit_timelines() or self._edit_timelines) or None

        preset_name = self.preset_combo.currentData()
        if scenes is None:
            self.console.clear()
            self.scene_grid.clear_scenes()
            self.timeline_view.clear_timeline()
            self._edit_timelines = {}
        self._append_console(f"[{label}] Initiating pipeline with preset: {preset_name}")
        if scenes is not None:
            self._append_console(
                f"[{label}] Re-using {len(scenes)} reviewed scene(s) "
                f"({sum(1 for sc in scenes if sc.metadata.get('included') is not False)} included) - no re-ingest"
            )

        self._running_stage = stage
        self._pending_existing = dict(existing or {})
        self.btn_stop.setEnabled(True)
        self._update_export_enabled()
        self.progress_bar.setValue(0)
        self.lbl_stage.setText(f"Status: [{label}] Starting...")

        callbacks = self.bridge.to_job_callbacks()

        input_dirs = self.media_browser.get_input_dirs()
        input_target: Path | list[Path]
        if input_dirs and len(input_dirs) > 1:
            input_target = input_dirs
            self._append_console(f"Source Directories ({len(input_dirs)} folders): {', '.join(d.name for d in input_dirs)}")
        elif input_dirs and len(input_dirs) == 1:
            input_target = input_dirs[0]
            self._append_console(f"Source Directory: {input_target}")
        else:
            videos = self.media_browser.get_video_files()
            input_target = self.media_browser.get_input_dir() or (
                videos[0].parent if videos else self.media_browser.get_output_dir()
            )
            self._append_console(f"Source Directory: {input_target}")

        soundtrack = self.media_browser.get_soundtrack_path()
        output_dir = self.media_browser.get_output_dir()

        self._append_console(f"Soundtrack Audio: {soundtrack}")
        self._append_console(f"Deliverable Path: {output_dir}")

        self.export_panel.set_output_dir(output_dir)

        # Analysis NEVER renders or exports (DaVinci / CapCut / Premiere workflow):
        # the user presses Export once the edit is finished.
        job = self.adapter.submit_job(
            input_dir=input_target,
            soundtrack=soundtrack,
            output_dir=output_dir,
            config=cfg,
            callbacks=callbacks,
            run_async=True,
            no_render=True,
            export_nle=False,
            stage=stage,
            scenes=scenes,
            existing_timelines=existing,
        )
        self._current_job = job
        self._refresh_stage_controls()

        def _watch_job() -> None:
            job.wait()
            if job.result:
                self.bridge.notify_finished(job.result)

        threading.Thread(target=_watch_job, daemon=True).start()

    @Slot()
    def _on_start_deliver(self) -> None:
        """Export the current edit (video and/or NLE files). Never re-runs analysis."""
        self._start_deliver(preview=False)

    @Slot()
    def _on_render_preview(self) -> None:
        """Render a fast 540p proxy of the current edit and load it into the monitor."""
        self._start_deliver(preview=True)

    def _start_deliver(self, preview: bool) -> None:
        """Submit a deliver job built from the timeline view's CURRENT edit state."""
        if self._current_job and self._current_job.is_running():
            QMessageBox.information(
                self,
                "Job In Progress",
                "An editing or rendering job is already actively running. Please wait for completion or click Stop.",
            )
            return
        if not self._has_edit_timeline():
            self.status_bar.showMessage("Nothing to export yet - run Analyze & Optimize first", 4000)
            self._update_export_enabled()
            return

        timeline = self.timeline_view.get_edit_timeline()
        if not timeline.clips:
            QMessageBox.warning(self, "Empty Timeline", "The timeline has no clips to export.")
            self._update_export_enabled()
            return

        formats = {"mp4"} if preview else self.export_panel.get_selected_formats()
        if not formats:
            QMessageBox.information(self, "No Format Selected", "Tick at least one export format.")
            self._update_export_enabled()
            return

        output_dir = self.media_browser.get_output_dir()
        cfg = self._build_config()
        try:
            settings = self.export_panel.get_deliver_settings(DeliverSettings.from_timeline(timeline, cfg))
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Deliver Settings", str(exc))
            self._update_export_enabled()
            return
        self.export_panel.set_output_dir(output_dir)
        self.export_panel.reset_render_monitor()
        self.progress_bar.setValue(0)
        self.btn_stop.setEnabled(True)
        kind = "Preview (540p)" if preview else "Export"
        self.lbl_stage.setText(f"Status: {kind}...")
        self._append_console(f"\n[{kind.upper()}] {timeline.name}: {len(timeline.clips)} clips, {timeline.duration:.1f}s")
        if not preview:
            self._append_console(f"Formats: {', '.join(sorted(formats))} -> {output_dir}")
            self._append_console(
                f"Deliver: {settings.canvas_width}x{settings.canvas_height} @ {settings.fps:g}fps, "
                f"master {settings.master}, colour {settings.color}, variants {', '.join(settings.variants)}"
            )
            if self.export_panel.source_mismatch.affected:
                self._append_console(f"[NOTE] {self.export_panel.source_mismatch.message}")

        job = self.adapter.submit_deliver_job(
            timeline,
            output_dir,
            cfg,
            formats=formats,
            callbacks=self.bridge.to_job_callbacks(),
            run_async=True,
            force_cpu=self.export_panel.is_force_cpu(),
            preview=preview,
            resolve=self.export_panel.is_resolve_dispatch_enabled() and not preview,
            dry_run_resolve=self.export_panel.is_dry_run_resolve(),
            settings=settings,
            variant_timelines=dict(self.timeline_view.get_edit_timelines()),
        )
        self._current_job = job
        self._update_export_enabled()

        def _watch_deliver() -> None:
            job.wait()
            if job.result:
                self.bridge.notify_deliver_finished(job.result)

        threading.Thread(target=_watch_deliver, daemon=True).start()

    def _on_stop_pipeline(self) -> None:
        """Cancel ongoing pipeline job."""
        if self._current_job and self._current_job.is_running():
            label = STAGE_LABELS.get(self._running_stage or "", "pipeline")
            self._append_console(f"\n[USER] Cancellation requested ({label}). Halting worker...")
            self.lbl_message.setText(f"Cancelling {label}...")
            self.btn_stop.setEnabled(False)
            self.adapter.cancel_job(self._current_job.job_id)

    # --- Project Management, Persistence & Dirty State ---

    def _set_dirty(self, dirty: bool = True) -> None:
        """Update dirty state and reflect in title bar and save badge."""
        self.project_manager.set_dirty(dirty)
        name = self.edit_project_name.text().strip() or "Untitled Project"
        star = " *" if dirty else ""
        self.setWindowTitle(f"TeamTrau AutoEdit Desktop Studio - {name}{star}")
        if dirty:
            self.lbl_saved.setText(" (Unsaved*)  ")
            self.lbl_saved.setStyleSheet("color: #f87171; font-size: 11px; font-weight: bold;")
        else:
            self.lbl_saved.setText(" (Saved)  ")
            self.lbl_saved.setStyleSheet("color: #71717a; font-size: 11px;")

    def _check_unsaved_changes(self) -> bool:
        """Check for unsaved project modifications and prompt user if dirty."""
        if not self.project_manager.is_dirty:
            return True

        reply = QMessageBox.question(
            self,
            "Unsaved Project Changes",
            f"Project '{self.edit_project_name.text()}' has unsaved modifications.\n"
            "Do you want to save your changes before proceeding?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )

        if reply == QMessageBox.StandardButton.Save:
            return self._on_save_project()
        elif reply == QMessageBox.StandardButton.Discard:
            return True
        return False

    def _update_recent_menu(self) -> None:
        """Populate the Open Recent submenu dynamically from persistent history."""
        self.recent_menu.clear()
        recent_paths = self.project_manager.get_recent_projects()
        if not recent_paths:
            no_rec_action = QAction("No Recent Projects", self)
            no_rec_action.setEnabled(False)
            self.recent_menu.addAction(no_rec_action)
            return

        for p in recent_paths:
            action = QAction(f"{p.name}  ({p.parent})", self)
            action.setData(str(p))
            action.triggered.connect(lambda _, path=p: self._on_open_recent_item(path))
            self.recent_menu.addAction(action)

        self.recent_menu.addSeparator()
        clear_action = QAction("Clear Recent Projects", self)
        clear_action.triggered.connect(self._on_clear_recent_projects)
        self.recent_menu.addAction(clear_action)

    def _update_workflow_stepper(self, step: int) -> None:
        """Update the workflow indicator (1: Ingest, 2: Rough Cut, 3: Sequence, 4: Deliver)."""
        step = max(1, min(len(STEP_NAMES), int(step)))
        self._workflow_step = step
        styled_parts = []
        for i, name in enumerate(STEP_NAMES, start=1):
            if i < step:
                styled_parts.append(f"<font color='#4ade80'>✔ {name}</font>")
            elif i == step:
                styled_parts.append(f"<font color='#38bdf8'><b>● {name}</b></font>")
            else:
                styled_parts.append(f"<font color='#71717a'>{name}</font>")
        pill_text = "  ➔  ".join(styled_parts)
        self.lbl_workflow_steps.setText(f" {pill_text} ")

    @Slot()
    def _on_new_project(self) -> None:
        """Reset workspace and start a fresh project."""
        if not self._check_unsaved_changes():
            return
        self.edit_project_name.setText("Untitled Project")
        self.project_manager.current_project_path = None
        self.media_browser.edit_input_dir.clear()
        self.media_browser.edit_soundtrack.clear()
        self.media_browser.clear_all()
        self.scene_grid.clear_scenes()
        self.timeline_view.clear_timeline()
        self._edit_timelines = {}
        self._active_timeline_key = "master"
        self._pipeline_stage = "ingest"
        self._audio_state = None
        self.timeline_view.set_audio_state(None)
        self.player_widget.canvas.set_frame_pixmap(None)
        self.player_widget._mode = "idle"
        self.player_widget.lbl_mode.setText("🎬 Idle (No Media)")
        self.export_panel.reset_render_monitor()
        self.export_panel.load_deliver_settings(DeliverSettings(canvas_width=1920, canvas_height=1080, fps=30.0))
        self.export_panel.set_source_timeline(None)
        self.console.clear()
        self.undo_stack.clear()
        self._set_dirty(False)
        self._update_workflow_stepper(1)
        self._update_export_enabled()
        self._refresh_stage_controls()
        self._update_ai_badge()
        self._append_console("Created new blank project.")

    @Slot()
    def _on_save_project(self) -> bool:
        """Save active project to disk, or prompt Save As if untitled."""
        if self.project_manager.current_project_path:
            return self._do_save(self.project_manager.current_project_path)
        return self._on_save_project_as()

    @Slot()
    def _on_save_project_as(self) -> bool:
        """Prompt file dialog and save project under a new path."""
        initial_name = f"{self.edit_project_name.text().strip() or 'Project'}.tteproj"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Project As",
            str(Path.cwd() / initial_name),
            PROJECT_FILE_FILTER,
        )
        if not file_path:
            return False
        return self._do_save(Path(file_path))

    def _do_save(self, path: Path) -> bool:
        """Serialize current workspace state into .tteproj JSON file."""
        cfg = self._build_config()

        # 1. Timeline clips on canvas
        timeline_clips_data = []
        for c in self.timeline_view.canvas.clips:
            timeline_clips_data.append({
                "clip_id": c.clip_id,
                "name": c.name,
                "track": c.track,
                "start_time": c.start_time,
                "end_time": c.end_time,
                "duration": c.duration,
                "source_path": str(c.source_path),
                "source_start": c.source_start,
                "source_end": c.source_end,
                "composite_score": c.composite_score,
                "motion_energy": c.motion_energy,
                "is_teaser": c.is_teaser,
            })

        # 2. Scored scenes & curation inclusion state
        scored_scenes_data = []
        included_indices = []
        for idx, s in enumerate(self.scene_grid.get_all_scenes()):
            is_inc = self.scene_grid.is_scene_included(idx)
            if is_inc:
                included_indices.append(idx)
            scored_scenes_data.append(self.project_manager.serialize_scored_scene(s, is_included=is_inc))

        # 3. Audio analysis & song structure
        audio_analysis_data = {}
        if getattr(self.timeline_view.canvas, "audio_analysis", None):
            audio_analysis_data = self.project_manager.serialize_audio_analysis(
                self.timeline_view.canvas.audio_analysis
            )

        # 4. Edit timelines (single source of truth, incl. the user's live edits)
        edit_timelines = self.timeline_view.get_edit_timelines() or dict(self._edit_timelines)
        edit_timelines_data = {k: edit_timeline_to_dict(tl) for k, tl in edit_timelines.items()}
        active_tl_key = self.timeline_view.get_active_timeline_key() or self._active_timeline_key

        # 4b. Legacy variant switcher state (projects loaded without edit timelines)
        variants_data = {}
        loaded_variants = self.timeline_view.get_variants()
        for v_key, var in loaded_variants.items():
            variants_data[v_key] = self.project_manager.serialize_timeline_variant(var)
        active_var = self.timeline_view.get_active_variant_key() if loaded_variants else None

        # 5. Media ingestion & camera roles
        input_dirs = [str(d) for d in self.media_browser.get_input_dirs()]
        clip_roles = {str(p): role for p, role in self.media_browser.get_clip_roles().items()}

        # 6. Deliverables
        deliverables_data = {}
        last_res = getattr(self.export_panel, "_last_result", None)
        if last_res and last_res.success:
            deliverables_data = {
                "fcpxml_path": str(last_res.fcpxml_path) if last_res.fcpxml_path else None,
                "premiere_xml_path": str(last_res.premiere_xml_path) if last_res.premiere_xml_path else None,
                "otio_path": str(last_res.otio_path) if last_res.otio_path else None,
                "capcut_draft_path": str(last_res.capcut_draft_path) if last_res.capcut_draft_path else None,
                "render_path": str(last_res.render_path) if last_res.render_path else None,
                "encoder_used": last_res.encoder_used,
                "total_duration": getattr(last_res, "total_duration", getattr(last_res, "duration", 0.0)),
                "clip_count": getattr(last_res, "clip_count", 0),
                "scenes_detected": getattr(last_res, "scenes_detected", 0),
            }

        # 7. Deliver settings (only meaningful once a timeline exists; otherwise derived on load)
        deliver_settings_data: dict[str, Any] = {}
        if edit_timelines:
            active_tl = edit_timelines.get(active_tl_key) or next(iter(edit_timelines.values()))
            try:
                deliver_settings_data = deliver_settings_to_dict(
                    self.export_panel.get_deliver_settings(DeliverSettings.from_timeline(active_tl, cfg))
                )
            except ValueError as exc:
                logger.warning("Deliver settings not saved: %s", exc)

        project_data = ProjectData(
            project_name=self.edit_project_name.text().strip(),
            input_dir=str(self.media_browser.get_input_dir()) if self.media_browser.get_input_dir() else None,
            input_dirs=input_dirs,
            clip_roles=clip_roles,
            soundtrack_path=str(self.media_browser.get_soundtrack_path()) if self.media_browser.get_soundtrack_path() else None,
            output_dir=str(self.media_browser.get_output_dir()) if self.media_browser.get_output_dir() else None,
            preset_name=self.preset_combo.currentData(),
            config=self.project_manager.serialize_pipeline_config(cfg),
            in_point=getattr(self, "_in_point", None),
            out_point=getattr(self, "_out_point", None),
            playhead_time=getattr(self.player_widget, "current_time", 0.0),
            timeline_clips=timeline_clips_data,
            scored_scenes=scored_scenes_data,
            included_scene_indices=included_indices,
            audio_analysis=audio_analysis_data,
            timeline_variants=variants_data,
            active_variant_name=active_var,
            deliverables=deliverables_data,
            edit_timelines=edit_timelines_data,
            active_timeline_key=active_tl_key,
            deliver_settings=deliver_settings_data,
            pipeline_stage=self._pipeline_stage if self._pipeline_stage in PIPELINE_STAGES else "ingest",
            audio_state=audio_state_to_dict(self._audio_state),
        )

        ok = self.project_manager.save_project(path, project_data)
        if ok:
            self._set_dirty(False)
            self._update_recent_menu()
            self._append_console(f"Project saved: {path} (Scenes: {len(scored_scenes_data)}, Variants: {len(variants_data)})")
            self.status_bar.showMessage(f"Project saved to {path.name}", 4000)
        return ok

    @Slot()
    def _on_open_project(self) -> bool:
        """Prompt file picker to open an existing .tteproj project."""
        if not self._check_unsaved_changes():
            return False
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open TeamTrau Project",
            str(Path.cwd()),
            PROJECT_FILE_FILTER,
        )
        if not file_path:
            return False
        return self._do_open(Path(file_path))

    def _on_open_recent_item(self, path: Path) -> None:
        """Open a project selected from the Open Recent submenu."""
        if not self._check_unsaved_changes():
            return
        self._do_open(path)

    @Slot()
    def _on_clear_recent_projects(self) -> None:
        """Clear the recent projects list."""
        self.project_manager.clear_recent_projects()
        self._update_recent_menu()
        self.status_bar.showMessage("Recent projects cleared", 2000)

    def _do_open(self, path: Path) -> bool:
        """Load and restore workspace state from .tteproj file."""
        data = self.project_manager.load_project(path)
        if not data:
            QMessageBox.critical(self, "Load Error", f"Unable to read or parse project file:\n{path}")
            return False

        self.edit_project_name.setText(data.project_name)

        if data.preset_name:
            idx = self.preset_combo.findData(data.preset_name)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)

        # 1. Restore Media Ingestion & Camera Roles
        if data.input_dirs:
            valid_dirs = [Path(d) for d in data.input_dirs if Path(d).is_dir()]
            if valid_dirs:
                self.media_browser.set_input_dirs(valid_dirs)
        elif data.input_dir and Path(data.input_dir).exists():
            self.media_browser.set_input_dir(data.input_dir)

        if data.clip_roles:
            self.media_browser.set_clip_roles(data.clip_roles)

        if data.soundtrack_path and Path(data.soundtrack_path).exists():
            self.media_browser.set_soundtrack_path(data.soundtrack_path)

        if data.output_dir:
            self.media_browser.set_output_dir(data.output_dir)
            self.export_panel.set_output_dir(Path(data.output_dir))

        # 2. Restore Pipeline & UI Configuration
        if data.config:
            cfg = self.project_manager.deserialize_pipeline_config(data.config)
            self.inspector_panel.load_from_config(cfg)
            self.export_panel.load_from_config(cfg)
            self.preferences_dialog.load_from_config(cfg)
            self.preferences_dialog.apply_to_config(self.preferences_dialog.config)
            self._update_ai_badge()

        # 3. Restore Scored Scenes & Review Grid
        restored_scenes = []
        if data.scored_scenes:
            for sd in data.scored_scenes:
                scene, _ = self.project_manager.deserialize_scored_scene(sd)
                restored_scenes.append(scene)
            self.scene_grid.set_scenes(restored_scenes)
            for idx, sd in enumerate(data.scored_scenes):
                if not sd.get("included", True):
                    self.scene_grid.set_scene_included(idx, False)

        # 5. Restore Edit Timelines (no re-analysis; v2.0 projects are upgraded from variants)
        restored_edit = data.get_edit_timelines()
        self._audio_state = data.get_audio_state()  # REAL beats / bar grid for the ruler (None for old projects)
        self.timeline_view.set_audio_state(self._audio_state)
        self._pipeline_stage = self._derive_pipeline_stage(data, restored_edit)
        restored_variants: dict[str, Any] = {}
        self._edit_timelines = dict(restored_edit)
        if restored_edit:
            active_key = data.active_timeline_key if data.active_timeline_key in restored_edit else next(iter(restored_edit))
            self._active_timeline_key = active_key
            bpm = float(data.audio_analysis.get("tempo_bpm", 0.0)) if data.audio_analysis else 0.0
            self.timeline_view.set_edit_timelines(restored_edit, active_key=active_key, tempo_bpm=bpm)
            self.player_widget.load_edit_timeline(restored_edit[active_key])
            self.player_widget.set_duration(restored_edit[active_key].duration)
            stored_settings = data.get_deliver_settings()
            if stored_settings is not None:
                self.export_panel.set_available_variants(list(restored_edit), active_key)
                self.export_panel.load_deliver_settings(stored_settings)
            else:
                self._adopt_timeline_canvas(restored_edit[active_key])
            self._refresh_deliver_panel()
        else:
            self._active_timeline_key = "master"
            self.timeline_view.clear_timeline()
            if data.timeline_variants:
                for vk, vd in data.timeline_variants.items():
                    restored_variants[vk] = self.project_manager.deserialize_timeline_variant(vd)
                self.timeline_view.set_variants(
                    restored_variants, active_key=data.active_variant_name or "beat_driven"
                )
            # 6. Legacy raw clips (very old projects)
            if data.timeline_clips:
                restored_clips = []
                for cd in data.timeline_clips:
                    restored_clips.append(
                        TimelineClipItem(
                            clip_id=cd["clip_id"],
                            name=cd["name"],
                            track=cd["track"],
                            start_time=cd["start_time"],
                            end_time=cd["end_time"],
                            duration=cd["duration"],
                            source_path=Path(cd["source_path"]),
                            source_start=cd["source_start"],
                            source_end=cd["source_end"],
                            composite_score=cd.get("composite_score", 0.5),
                            motion_energy=cd.get("motion_energy", 0.5),
                            is_teaser=cd.get("is_teaser", False),
                        )
                    )
                self.timeline_view.canvas.clips = restored_clips
                max_end = max((c.end_time for c in restored_clips), default=0.0)
                self.timeline_view.canvas.total_duration = max_end
                self.player_widget.set_duration(max_end)
                self.timeline_view.canvas.update()
                self.timeline_view._update_timecode_label()
            active_var_obj = restored_variants.get(data.active_variant_name or "beat_driven")
            if active_var_obj and active_var_obj.sequence_scenes:
                self.player_widget.load_timeline_sequence(
                    active_var_obj.sequence_scenes, total_duration=active_var_obj.total_duration
                )

        # 8. Restore Playhead and In/Out Points
        if data.playhead_time > 0.0:
            self.player_widget.seek_time(data.playhead_time)
            self.timeline_view.set_playhead_time(data.playhead_time)

        self._in_point = data.in_point
        self._out_point = data.out_point

        # 9. Restore Deliverables
        if data.deliverables:
            bpm_val = float(data.audio_analysis.get("tempo_bpm", 120.0)) if data.audio_analysis else 120.0
            sec_cnt = len(data.audio_analysis.get("sections", [])) if data.audio_analysis else 0
            deliv_res = PipelineExecutionResult(
                success=True,
                total_duration=float(data.deliverables.get("total_duration", 0.0)),
                clip_count=int(data.deliverables.get("clip_count", 0)),
                scenes_detected=int(data.deliverables.get("scenes_detected", len(restored_scenes))),
                audio_bpm=bpm_val,
                sections_detected=sec_cnt,
                fcpxml_path=Path(data.deliverables["fcpxml_path"]) if data.deliverables.get("fcpxml_path") else None,
                premiere_xml_path=Path(data.deliverables["premiere_xml_path"]) if data.deliverables.get("premiere_xml_path") else None,
                otio_path=Path(data.deliverables["otio_path"]) if data.deliverables.get("otio_path") else None,
                capcut_draft_path=Path(data.deliverables["capcut_draft_path"]) if data.deliverables.get("capcut_draft_path") else None,
                render_path=Path(data.deliverables["render_path"]) if data.deliverables.get("render_path") else None,
                encoder_used=data.deliverables.get("encoder_used"),
                resolve_status=None,
                scored_scenes=restored_scenes,
                timeline_variants=restored_variants,
                active_variant_name=data.active_variant_name,
                edit_timelines=dict(restored_edit),
            )
            self.export_panel.set_deliverables(deliv_res)

        self.undo_stack.clear()
        self._set_dirty(False)
        self._update_recent_menu()
        self._update_workflow_stepper(PIPELINE_STAGES.index(self._pipeline_stage) + 1)
        self._update_export_enabled()
        self._refresh_stage_controls()
        self._append_console(f"Opened project: {path} (Scenes: {len(restored_scenes)}, Variants: {len(restored_variants)})")
        self.status_bar.showMessage(f"Loaded project {data.project_name}", 4000)
        return True

    @staticmethod
    def _derive_pipeline_stage(data: ProjectData, edit_timelines: dict[str, EditTimeline]) -> str:
        """Stage of an opened project: the stored one, else derived from its content (old projects)."""
        if data.deliverables:
            return "deliver"
        if data.pipeline_stage in PIPELINE_STAGES and data.pipeline_stage != "ingest":
            return data.pipeline_stage
        if edit_timelines:
            return "rough_cut" if all(t.kind == "rough_cut" for t in edit_timelines.values()) else "sequence"
        if data.timeline_clips or data.timeline_variants:
            return "sequence"
        return "ingest"

    # --- UI state between sessions (opt-in: only the real app launcher enables it) ---

    @staticmethod
    def _ui_state_path() -> Path:
        return DEFAULT_RECENT_PROJECTS_PATH.parent / "ui_state.json"

    def enable_ui_state_persistence(self) -> None:
        """Restore (and from now on save on close) the last stage settings: pruning, cadence, genre,
        long-shot limits and which stage tab was last in use. Tests never call this."""
        self._persist_ui_state = True
        try:
            state = json.loads(self._ui_state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(state, dict):
            return
        insp = self.inspector_panel
        insp.set_genre(str(state.get("genre", "")), emit=False)
        idx = insp.combo_cadence.findData(state.get("cadence", "auto"))
        insp.combo_cadence.setCurrentIndex(max(idx, 0))
        insp.chk_rough_cut_pruning.setChecked(bool(state.get("rough_cut_pruning", True)))
        insp.spin_long_shot.setValue(float(state.get("long_shot_sec", insp.spin_long_shot.value())))
        insp.spin_max_scene.setValue(float(state.get("max_scene_sec", insp.spin_max_scene.value())))
        if state.get("last_stage") == "rough_cut":
            self.tab_widget.setCurrentWidget(self.scene_grid)

    def _save_ui_state(self) -> None:
        insp = self.inspector_panel
        state = {
            "genre": insp.selected_genre_key(),
            "cadence": insp.combo_cadence.currentData() or "auto",
            "rough_cut_pruning": insp.chk_rough_cut_pruning.isChecked(),
            "long_shot_sec": insp.spin_long_shot.value(),
            "max_scene_sec": insp.spin_max_scene.value(),
            "last_stage": self._pipeline_stage,
        }
        try:
            path = self._ui_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save UI state: %s", exc)

    @Slot()
    def _on_import_media_folder(self) -> None:
        """Open directory picker to ingest raw video clips."""
        folder = QFileDialog.getExistingDirectory(self, "Select Video Footage Directory")
        if folder:
            self.media_browser.set_input_dir(folder)
            self._set_dirty(True)
            if not self._has_edit_timeline():
                self._update_workflow_stepper(1)

    @Slot()
    def _on_import_soundtrack(self) -> None:
        """Open file picker to ingest background soundtrack."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Soundtrack Audio",
            "",
            "Audio Files (*.mp3 *.wav *.aac *.flac *.m4a);;All Files (*.*)",
        )
        if file_path:
            self.media_browser.set_soundtrack_path(file_path)
            self._set_dirty(True)

    # --- NLE Edit & Clipboard Slots ---

    @Slot()
    def _on_delete_clip(self) -> None:
        """Delete currently selected timeline clip with undo support."""
        clip = self.timeline_view.canvas.selected_clip
        if clip:
            cmd = ClipDeleteCommand(self.timeline_view, clip)
            self.undo_stack.push(cmd)
            self._set_dirty(True)
            self._append_console(f"[EDIT] Deleted clip: {clip.name}")

    @Slot()
    def _on_copy_clip(self) -> None:
        """Copy selected timeline clip to clipboard."""
        clip = self.timeline_view.canvas.selected_clip
        if clip:
            self._clipboard_clip = copy.deepcopy(clip)
            self.status_bar.showMessage(f"Copied clip: {clip.name}", 3000)
            self._append_console(f"[CLIPBOARD] Copied clip: {clip.name}")

    @Slot()
    def _on_cut_clip(self) -> None:
        """Cut selected timeline clip with undo support."""
        clip = self.timeline_view.canvas.selected_clip
        if clip:
            self._on_copy_clip()
            self._on_delete_clip()

    @Slot()
    def _on_paste_clip(self) -> None:
        """Paste clipboard clip at playhead time with undo support."""
        if not self._clipboard_clip:
            return
        new_clip = copy.deepcopy(self._clipboard_clip)
        new_clip.clip_id = f"CLIP_{uuid.uuid4().hex[:6]}"
        new_clip.start_time = self.player_widget.current_time
        new_clip.end_time = new_clip.start_time + new_clip.duration
        cmd = ClipPasteCommand(self.timeline_view, new_clip)
        self.undo_stack.push(cmd)
        self._set_dirty(True)
        self._append_console(f"[EDIT] Pasted clip: {new_clip.name} at {new_clip.start_time:.2f}s")

    @Slot()
    def _on_deselect_all(self) -> None:
        """Clear active selections across Timeline and Scene Review."""
        self.timeline_view.deselect_all()
        self.scene_grid.table.clearSelection()
        self.status_bar.showMessage("Deselected all items", 2000)

    @Slot()
    def _on_quick_exclude_dead_air(self) -> None:
        """Kaizen 1-click optimization: exclude all detected dead-air & duplicate scenes."""
        self.scene_grid.exclude_dead_air_scenes()
        self._set_dirty(True)
        self.status_bar.showMessage("Excluded dead-air and frozen scenes", 3000)
        self._append_console("[KAIZEN] Excluded dead-air and freeze scenes from master sequence.")

    @Slot(int, bool)
    def _on_scene_toggled_by_user(self, scene_idx: int, included: bool) -> None:
        """Slot invoked when user toggles scene inclusion checkbox in SceneGrid."""
        self._set_dirty(True)
        if self._edit_timelines:
            self.status_bar.showMessage(
                "Scene curation changed - use Edit > Apply Scene Curation to Timeline to update the edit", 6000
            )

    @Slot()
    def _on_apply_scene_curation(self) -> None:
        """Re-fill timeline clips that use excluded scenes (all timelines, beat grid preserved)."""
        if not self._edit_timelines:
            self.status_bar.showMessage("No timeline yet - run Rough Cut or Auto-Edit Sequence first", 3000)
            return
        excluded = self.scene_grid.get_excluded_scenes()
        if not excluded:
            self.status_bar.showMessage("No scenes are excluded - nothing to apply", 3000)
            return
        included = self.scene_grid.get_included_scenes()
        active = self.timeline_view.get_active_timeline_key() or self._active_timeline_key
        try:
            updated: dict[str, EditTimeline] = {}
            total = 0
            for key, tl in self.timeline_view.get_edit_timelines().items():
                new_tl, n = apply_scene_curation(tl, included, excluded)
                updated[key] = new_tl
                total += n
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot Apply Curation", str(exc))
            return
        if total == 0:
            self.status_bar.showMessage("Excluded scenes are not used by the timelines", 3000)
            return
        self._edit_timelines = updated
        self.timeline_view.set_edit_timelines(
            updated, active_key=active, tempo_bpm=self.timeline_view._tempo_bpm
        )
        self.player_widget.load_edit_timeline(updated[active])
        self.undo_stack.clear()
        self._set_dirty(True)
        self._append_console(f"[CURATION] Replaced {total} clip(s) from excluded scenes across {len(updated)} timeline(s).")
        self.status_bar.showMessage(f"Replaced {total} clip(s) from excluded scenes", 4000)

    def _on_clear_cache(self) -> None:
        """Clear scene detection cache on disk and active scene grid."""
        cfg = PipelineConfig()
        from src.modules.ingest_detector import IngestDetector

        cleared = IngestDetector(cfg).clear_cache()
        self.scene_grid.clear_scenes()
        self.timeline_view.clear_timeline()
        self._edit_timelines = {}
        self._update_export_enabled()
        self.player_widget.canvas.set_frame_pixmap(None)
        self.player_widget._mode = "idle"
        self.player_widget.lbl_mode.setText("🎬 Idle (No Media)")
        self.export_panel.reset_render_monitor()
        self._set_dirty(True)
        self._append_console(f"Cleared {cleared} cache files.")

    @Slot()
    def _on_open_agent_console(self) -> None:
        """Open the interactive AI Agent Co-Pilot non-blocking floating window."""
        from src.ui.widgets.agent_console_dialog import AgentConsoleDialog

        if self._agent_console_dialog is None:
            self._agent_console_dialog = AgentConsoleDialog(config=self.config, parent=self)
        else:
            self._agent_console_dialog.update_config(self.config)
        self._agent_console_dialog.show()
        self._agent_console_dialog.raise_()
        self._agent_console_dialog.activateWindow()

    @Slot()
    def _on_copy_antigravity_mcp_config(self) -> None:
        """Copy Antigravity JSON MCP configuration to clipboard."""
        from PySide6.QtWidgets import QApplication

        from src.modules.mcp_server import generate_mcp_config

        cfg = generate_mcp_config(client_name="antigravity", transport="stdio")
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(json.dumps(cfg["config"], indent=2))
        self.status_bar.showMessage("Antigravity MCP configuration copied to clipboard!", 4000)

    @Slot()
    def _on_copy_claude_mcp_config(self) -> None:
        """Copy Claude Desktop JSON MCP configuration to clipboard."""
        from PySide6.QtWidgets import QApplication

        from src.modules.mcp_server import generate_mcp_config

        cfg = generate_mcp_config(client_name="claude_desktop", transport="stdio")
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(json.dumps(cfg["config"], indent=2))
        self.status_bar.showMessage("Claude Desktop MCP configuration copied to clipboard!", 4000)

    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "About TeamTrau AutoEdit",
            "<b>TeamTrau AutoEdit Desktop Studio</b><br>"
            "Professional Unified NLE Workspace Architecture.<br>"
            "Single-window editing inspired by CapCut & DaVinci Resolve.<br>"
            "Cross-platform PySide6 UI with CUDA & NVENC acceleration.",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        """Ensure active jobs are terminated cleanly and prompt for unsaved changes."""
        if not self._check_unsaved_changes():
            event.ignore()
            return

        if self._current_job and self._current_job.is_running():
            logger.info("Terminating active pipeline job before closing...")
            self.adapter.cancel_job(self._current_job.job_id)
            self._current_job.wait(timeout=2.0)
        if self._persist_ui_state:
            self._save_ui_state()
        event.accept()
