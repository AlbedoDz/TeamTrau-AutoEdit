"""Preferences and AI Engine Settings Dialog for TeamTrau AutoEdit."""

import json
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.config import PipelineConfig
from src.core.service import StudioBackendAdapter

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".teamtrau" / "config.json"


class _PingWorkerSignals(QObject):
    ping_finished = Signal(bool, float, str)


class PreferencesDialog(QDialog):
    """Preferences and AI Configuration Modal Dialog."""

    settings_applied = Signal(object)  # emits PipelineConfig

    def __init__(
        self,
        config: PipelineConfig | None = None,
        backend_adapter: StudioBackendAdapter | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config or PipelineConfig()
        self.adapter = backend_adapter or StudioBackendAdapter()
        self._ping_signals = _PingWorkerSignals()
        self._ping_signals.ping_finished.connect(self._on_ping_finished)

        self._init_window()
        self._init_ui()
        self.load_from_config(self.config)
        self.load_from_disk()
        self.apply_to_config(self.config)

    def _init_window(self) -> None:
        self.setWindowTitle("Preferences & AI Engine Settings")
        self.resize(680, 520)
        self.setMinimumSize(580, 440)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Header Title
        title_label = QLabel("⚙️ Preferences & AI Engine Settings")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #f4f4f5;")
        layout.addWidget(title_label)

        # Tabs container
        self.tabs = QTabWidget(self)

        self._init_tab_ai()
        self._init_tab_ingest()
        self._init_tab_audio()
        self._init_tab_subtitles()
        self._init_tab_agent_mcp()

        layout.addWidget(self.tabs, stretch=1)

        # Bottom Action Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Cancel", self)
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_save = QPushButton("Save & Apply", self)
        self.btn_save.setObjectName("primaryButton")
        self.btn_save.clicked.connect(self._on_save_and_apply)
        btn_layout.addWidget(self.btn_save)

        layout.addLayout(btn_layout)

    def _init_tab_ai(self) -> None:
        """Tab 1: AI & LLM Engine Hub."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        # Provider Selection Group
        grp_provider = QGroupBox("AI Provider & Architecture", tab)
        vbox_prov = QVBoxLayout(grp_provider)

        self.btn_grp_provider = QButtonGroup(self)
        self.rb_openrouter = QRadioButton("OpenRouter (Cloud VLM / LLM)", grp_provider)
        self.rb_ollama = QRadioButton("Ollama LAN / Local (qwen2.5, llama3.2-vision)", grp_provider)
        self.rb_lmstudio = QRadioButton("LM Studio (Local / Network)", grp_provider)
        self.rb_custom = QRadioButton("Custom OpenAI-Compatible Endpoint", grp_provider)

        self.btn_grp_provider.addButton(self.rb_openrouter, 1)
        self.btn_grp_provider.addButton(self.rb_ollama, 2)
        self.btn_grp_provider.addButton(self.rb_lmstudio, 3)
        self.btn_grp_provider.addButton(self.rb_custom, 4)

        self.rb_ollama.setChecked(True)
        self.btn_grp_provider.idToggled.connect(self._on_provider_toggled)

        vbox_prov.addWidget(self.rb_openrouter)
        vbox_prov.addWidget(self.rb_ollama)
        vbox_prov.addWidget(self.rb_lmstudio)
        vbox_prov.addWidget(self.rb_custom)
        layout.addWidget(grp_provider)

        # Form Layout for Endpoints & Credentials
        form = QFormLayout()
        form.setSpacing(8)

        self.edit_base_url = QLineEdit(tab)
        self.edit_base_url.setPlaceholderText("http://localhost:11434/v1")
        form.addRow("Endpoint Base URL:", self.edit_base_url)

        # API Key with Reveal Toggle
        key_widget = QWidget(tab)
        key_layout = QHBoxLayout(key_widget)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)

        self.edit_api_key = QLineEdit(key_widget)
        self.edit_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_api_key.setPlaceholderText("Optional for local Ollama/LM Studio")
        key_layout.addWidget(self.edit_api_key)

        self.btn_reveal_key = QPushButton("👁", key_widget)
        self.btn_reveal_key.setFixedWidth(36)
        self.btn_reveal_key.setCheckable(True)
        self.btn_reveal_key.toggled.connect(self._on_toggle_reveal_key)
        key_layout.addWidget(self.btn_reveal_key)

        form.addRow("API Secret Key:", key_widget)

        # Model Name Combo (Editable)
        self.combo_model = QComboBox(tab)
        self.combo_model.setEditable(True)
        self.combo_model.addItems([
            "qwen2.5:7b",
            "qwen2.5-coder:7b",
            "llama3.2-vision",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-sonnet",
            "deepseek/deepseek-chat",
        ])
        form.addRow("Target Model Name:", self.combo_model)

        layout.addLayout(form)

        # Diagnostic Ping Section
        ping_widget = QWidget(tab)
        ping_layout = QHBoxLayout(ping_widget)
        ping_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_test_ping = QPushButton("⚡ Test Connection", ping_widget)
        self.btn_test_ping.clicked.connect(self._on_test_ping_clicked)
        ping_layout.addWidget(self.btn_test_ping)

        self.lbl_ping_result = QLabel("⚪ Status: Ready to test", ping_widget)
        self.lbl_ping_result.setStyleSheet("color: #a1a1aa; font-weight: 500;")
        ping_layout.addWidget(self.lbl_ping_result)
        ping_layout.addStretch()

        layout.addWidget(ping_widget)
        layout.addStretch()

        self.tabs.addTab(tab, "AI & LLM Engine Hub")

    def _init_tab_ingest(self) -> None:
        """Tab 2: Ingest & Dead-Air Thresholds."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        form.setSpacing(12)

        # Silence threshold: slider + spinbox
        silence_widget = QWidget(tab)
        silence_layout = QHBoxLayout(silence_widget)
        silence_layout.setContentsMargins(0, 0, 0, 0)

        self.slider_silence = QSlider(Qt.Orientation.Horizontal, silence_widget)
        self.slider_silence.setRange(-50, -20)
        self.slider_silence.setValue(-35)

        self.spin_silence = QSpinBox(silence_widget)
        self.spin_silence.setRange(-50, -20)
        self.spin_silence.setValue(-35)
        self.spin_silence.setSuffix(" dB")

        self.slider_silence.valueChanged.connect(self.spin_silence.setValue)
        self.spin_silence.valueChanged.connect(self.slider_silence.setValue)

        silence_layout.addWidget(self.slider_silence)
        silence_layout.addWidget(self.spin_silence)
        form.addRow("Silence Detection Threshold:", silence_widget)

        # Min silence duration
        self.spin_silence_dur = QDoubleSpinBox(tab)
        self.spin_silence_dur.setRange(1.0, 10.0)
        self.spin_silence_dur.setValue(3.0)
        self.spin_silence_dur.setSingleStep(0.5)
        self.spin_silence_dur.setSuffix(" s")
        form.addRow("Min Silence Duration:", self.spin_silence_dur)

        # Freeze frame threshold
        self.spin_freeze = QDoubleSpinBox(tab)
        self.spin_freeze.setRange(0.001, 0.050)
        self.spin_freeze.setValue(0.020)
        self.spin_freeze.setSingleStep(0.005)
        self.spin_freeze.setDecimals(3)
        form.addRow("Freeze Frame Sensitivity:", self.spin_freeze)

        # 64-bit dHash Hamming distance
        self.spin_dedup_hash = QSpinBox(tab)
        self.spin_dedup_hash.setRange(1, 15)
        self.spin_dedup_hash.setValue(6)
        self.spin_dedup_hash.setSuffix(" bits")
        form.addRow("Duplicate dHash Threshold:", self.spin_dedup_hash)

        layout.addLayout(form)
        layout.addStretch()
        self.tabs.addTab(tab, "Ingest & Dead-Air")

    def _init_tab_audio(self) -> None:
        """Tab 3: Audio & Normalization."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        form.setSpacing(12)

        # BWF Timecode stripping
        self.chk_strip_bwf = QCheckBox("Strip BWF Timecode Metadata (Eliminates DaVinci drift)", tab)
        self.chk_strip_bwf.setChecked(True)
        form.addRow("BWF Normalization:", self.chk_strip_bwf)

        # Target sample rate
        self.combo_sample_rate = QComboBox(tab)
        self.combo_sample_rate.addItem("48000 Hz (Cinema / Video Standard)", 48000)
        self.combo_sample_rate.addItem("44100 Hz (Audio CD Standard)", 44100)
        self.combo_sample_rate.addItem("22050 Hz (Low Latency Analysis)", 22050)
        form.addRow("Target Audio Sample Rate:", self.combo_sample_rate)

        # Audio ducking depth
        ducking_widget = QWidget(tab)
        ducking_layout = QHBoxLayout(ducking_widget)
        ducking_layout.setContentsMargins(0, 0, 0, 0)

        self.slider_ducking = QSlider(Qt.Orientation.Horizontal, ducking_widget)
        self.slider_ducking.setRange(-24, -6)
        self.slider_ducking.setValue(-14)

        self.spin_ducking = QSpinBox(ducking_widget)
        self.spin_ducking.setRange(-24, -6)
        self.spin_ducking.setValue(-14)
        self.spin_ducking.setSuffix(" dB")

        self.slider_ducking.valueChanged.connect(self.spin_ducking.setValue)
        self.spin_ducking.valueChanged.connect(self.slider_ducking.setValue)

        ducking_layout.addWidget(self.slider_ducking)
        ducking_layout.addWidget(self.spin_ducking)
        form.addRow("Speech Ducking Attenuation:", ducking_widget)

        layout.addLayout(form)
        layout.addStretch()
        self.tabs.addTab(tab, "Audio & Normalization")

    def _init_tab_subtitles(self) -> None:
        """Tab 4: Subtitles & Transcription."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        form.setSpacing(12)

        # Whisper Model Size
        self.combo_whisper_model = QComboBox(tab)
        self.combo_whisper_model.addItems(["tiny", "base", "small", "medium", "large-v3-turbo"])
        self.combo_whisper_model.setCurrentText("base")
        form.addRow("Whisper Speech Model:", self.combo_whisper_model)

        # CPL (Characters Per Line)
        self.spin_cpl = QSpinBox(tab)
        self.spin_cpl.setRange(18, 45)
        self.spin_cpl.setValue(37)
        self.spin_cpl.setSuffix(" chars")
        form.addRow("Max Characters Per Line (CPL):", self.spin_cpl)

        # Subtitle export checkboxes
        self.chk_export_srt = QCheckBox("Export .srt subtitle sidecar file", tab)
        self.chk_export_srt.setChecked(True)
        form.addRow("Sidecar Subtitles:", self.chk_export_srt)

        self.chk_burn_hardsubs = QCheckBox("Burn hardcoded subtitles into Master MP4", tab)
        self.chk_burn_hardsubs.setChecked(False)
        form.addRow("Master Hardsubs:", self.chk_burn_hardsubs)

        layout.addLayout(form)
        layout.addStretch()
        self.tabs.addTab(tab, "Subtitles & Transcription")

    def _init_tab_agent_mcp(self) -> None:
        """Tab 5: Universal AI Agent & MCP Hub."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        # Agent Engine Settings Group
        grp_agent = QGroupBox("Autonomous AI Agent Settings", tab)
        form_agent = QFormLayout(grp_agent)
        form_agent.setSpacing(8)

        self.combo_agent_provider = QComboBox(grp_agent)
        self.combo_agent_provider.addItem("DeepSeek Cloud (api.deepseek.com)", "deepseek")
        self.combo_agent_provider.addItem("OpenRouter (openrouter.ai)", "openrouter")
        self.combo_agent_provider.addItem("OpenAI Cloud (api.openai.com)", "openai")
        self.combo_agent_provider.addItem("Ollama LAN / Local (localhost:11434)", "ollama")
        self.combo_agent_provider.addItem("LM Studio Local (localhost:1234)", "lm_studio")
        self.combo_agent_provider.addItem("Custom Endpoint", "custom")
        form_agent.addRow("Agent Provider:", self.combo_agent_provider)

        self.combo_agent_model = QComboBox(grp_agent)
        self.combo_agent_model.setEditable(True)
        self.combo_agent_model.addItems([
            "deepseek/deepseek-chat",
            "deepseek/deepseek-r1",
            "anthropic/claude-3.5-sonnet",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "qwen2.5:7b",
            "qwen2.5-coder:7b",
        ])
        form_agent.addRow("Target Model:", self.combo_agent_model)

        self.edit_agent_api_key = QLineEdit(grp_agent)
        self.edit_agent_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_agent_api_key.setPlaceholderText("API key for DeepSeek, OpenRouter, or OpenAI")
        form_agent.addRow("Agent API Key:", self.edit_agent_api_key)

        layout.addWidget(grp_agent)

        # MCP Server Settings Group
        grp_mcp = QGroupBox("Model Context Protocol (MCP) Server", tab)
        form_mcp = QFormLayout(grp_mcp)
        form_mcp.setSpacing(8)

        self.combo_mcp_transport = QComboBox(grp_mcp)
        self.combo_mcp_transport.addItems(["stdio (Standard I/O CLI)", "sse (Server-Sent Events)"])
        form_mcp.addRow("MCP Transport:", self.combo_mcp_transport)

        self.spin_mcp_port = QSpinBox(grp_mcp)
        self.spin_mcp_port.setRange(1024, 65535)
        self.spin_mcp_port.setValue(8000)
        form_mcp.addRow("SSE Port:", self.spin_mcp_port)

        # Action Buttons
        btn_box = QHBoxLayout()
        self.btn_copy_antigravity = QPushButton("📋 Copy Antigravity Config", grp_mcp)
        self.btn_copy_antigravity.clicked.connect(self._on_copy_antigravity_config)
        btn_box.addWidget(self.btn_copy_antigravity)

        self.btn_copy_claude = QPushButton("📋 Copy Claude Config", grp_mcp)
        self.btn_copy_claude.clicked.connect(self._on_copy_claude_config)
        btn_box.addWidget(self.btn_copy_claude)

        self.btn_copy_cursor = QPushButton("📋 Copy Cursor Config", grp_mcp)
        self.btn_copy_cursor.clicked.connect(self._on_copy_cursor_config)
        btn_box.addWidget(self.btn_copy_cursor)

        form_mcp.addRow("Agent Integration:", btn_box)

        self.lbl_mcp_status = QLabel("⚪ MCP Ready: 23 Tools, 3 Resources, 2 Prompts registered", grp_mcp)
        self.lbl_mcp_status.setStyleSheet("color: #a1a1aa; font-size: 11px;")
        form_mcp.addRow("MCP Status:", self.lbl_mcp_status)

        layout.addWidget(grp_mcp)
        layout.addStretch()

        self.tabs.addTab(tab, "AI Agent & MCP Hub")

    @Slot()
    def _on_copy_antigravity_config(self) -> None:
        from PySide6.QtWidgets import QApplication

        from src.modules.mcp_server import generate_mcp_config

        transport = "sse" if self.combo_mcp_transport.currentIndex() == 1 else "stdio"
        port = self.spin_mcp_port.value()
        cfg = generate_mcp_config(client_name="antigravity", transport=transport, port=port)
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(json.dumps(cfg["config"], indent=2))
        self.lbl_mcp_status.setText("✓ Antigravity MCP config copied to clipboard!")
        self.lbl_mcp_status.setStyleSheet("color: #4ade80; font-size: 11px;")

    @Slot()
    def _on_copy_claude_config(self) -> None:
        from PySide6.QtWidgets import QApplication

        from src.modules.mcp_server import generate_mcp_config

        transport = "sse" if self.combo_mcp_transport.currentIndex() == 1 else "stdio"
        port = self.spin_mcp_port.value()
        cfg = generate_mcp_config(client_name="claude_desktop", transport=transport, port=port)
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(json.dumps(cfg["config"], indent=2))
        self.lbl_mcp_status.setText("✓ Claude Desktop MCP config copied to clipboard!")
        self.lbl_mcp_status.setStyleSheet("color: #4ade80; font-size: 11px;")

    @Slot()
    def _on_copy_cursor_config(self) -> None:
        from PySide6.QtWidgets import QApplication

        from src.modules.mcp_server import generate_mcp_config

        transport = "sse" if self.combo_mcp_transport.currentIndex() == 1 else "stdio"
        port = self.spin_mcp_port.value()
        cfg = generate_mcp_config(client_name="cursor", transport=transport, port=port)
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(json.dumps(cfg["config"], indent=2))
        self.lbl_mcp_status.setText("✓ Cursor MCP config copied to clipboard!")
        self.lbl_mcp_status.setStyleSheet("color: #4ade80; font-size: 11px;")

    # --- Slots & Actions ---

    @Slot(int, bool)
    def _on_provider_toggled(self, btn_id: int, checked: bool) -> None:
        if not checked:
            return
        if btn_id == 1:  # OpenRouter
            self.edit_base_url.setText("https://openrouter.ai/api/v1")
            self.combo_model.setCurrentText("openai/gpt-4o-mini")
        elif btn_id == 2:  # Ollama LAN
            self.edit_base_url.setText("http://localhost:11434/v1")
            self.combo_model.setCurrentText("qwen2.5:7b")
        elif btn_id == 3:  # LM Studio
            self.edit_base_url.setText("http://localhost:1234/v1")
            self.combo_model.setCurrentText("qwen2.5:7b")
        elif btn_id == 4:  # Custom
            if not self.edit_base_url.text():
                self.edit_base_url.setText("http://192.168.1.100:11434/v1")

    @Slot(bool)
    def _on_toggle_reveal_key(self, checked: bool) -> None:
        if checked:
            self.edit_api_key.setEchoMode(QLineEdit.EchoMode.Normal)
            self.btn_reveal_key.setText("🔒")
        else:
            self.edit_api_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.btn_reveal_key.setText("👁")

    @Slot()
    def _on_test_ping_clicked(self) -> None:
        """Run diagnostic latency test to the configured AI endpoint asynchronously."""
        base_url = self.edit_base_url.text().strip() or None
        api_key = self.edit_api_key.text().strip() or None
        model = self.combo_model.currentText().strip() or None

        self.lbl_ping_result.setText("🟡 Testing connection...")
        self.btn_test_ping.setEnabled(False)

        def _worker() -> None:
            success, latency_ms, msg = self.adapter.ping_ai_endpoint(
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_sec=3.0,
            )
            self._ping_signals.ping_finished.emit(success, latency_ms, msg)

        threading.Thread(target=_worker, daemon=True).start()

    @Slot(bool, float, str)
    def _on_ping_finished(self, success: bool, latency_ms: float, msg: str) -> None:
        self.btn_test_ping.setEnabled(True)
        if success:
            self.lbl_ping_result.setText(f"🟢 Connected: {latency_ms:.0f}ms ({msg})")
            self.lbl_ping_result.setStyleSheet("color: #4ade80; font-weight: bold;")
        else:
            self.lbl_ping_result.setText(f"🔴 Connection Failed: {msg[:40]}")
            self.lbl_ping_result.setStyleSheet("color: #f87171; font-weight: bold;")

    @Slot()
    def _on_save_and_apply(self) -> None:
        """Save settings to disk and apply to active pipeline configuration."""
        self.apply_to_config(self.config)
        self.save_to_disk()
        self.settings_applied.emit(self.config)
        self.accept()

    def get_provider_key(self) -> str:
        """Return the active provider string identifier."""
        btn_id = self.btn_grp_provider.checkedId()
        mapping = {1: "openrouter", 2: "ollama", 3: "lm_studio", 4: "custom"}
        return mapping.get(btn_id, "ollama")

    def apply_to_config(self, config: PipelineConfig) -> None:
        """Transfer preferences dialog values into target PipelineConfig."""
        # AI Tab
        config.vlm_provider = self.get_provider_key()
        config.vlm_base_url = self.edit_base_url.text().strip() or None
        config.vlm_api_key = self.edit_api_key.text().strip() or None
        config.vlm_model = self.combo_model.currentText().strip() or "qwen2.5:7b"

        # Ingest Tab
        config.silence_threshold_db = float(self.spin_silence.value())
        config.dead_air_min_duration = float(self.spin_silence_dur.value())
        config.freeze_threshold = float(self.spin_freeze.value())
        config.duplicate_hash_threshold = int(self.spin_dedup_hash.value())

        # Audio Tab
        config.enable_audio_fix = self.chk_strip_bwf.isChecked()
        sr_val = self.combo_sample_rate.currentData()
        if sr_val:
            config.audio_sample_rate = int(sr_val)
        config.ducking_attenuation_db = float(self.spin_ducking.value())

        # Subtitles Tab
        config.whisper_model_size = self.combo_whisper_model.currentText().strip()
        config.subtitle_cpl = int(self.spin_cpl.value())
        config.export_subtitles_srt = self.chk_export_srt.isChecked()
        config.burn_hardsubs = self.chk_burn_hardsubs.isChecked()

        # Agent & MCP Tab
        config.agent_provider = self.combo_agent_provider.currentData() or "auto"
        agent_model = self.combo_agent_model.currentText().strip()
        config.agent_model = agent_model or config.vlm_model
        agent_key = self.edit_agent_api_key.text().strip() or None
        config.agent_api_key = agent_key or config.vlm_api_key
        config.mcp_transport = "sse" if self.combo_mcp_transport.currentIndex() == 1 else "stdio"
        config.mcp_port = self.spin_mcp_port.value()

    def load_from_config(self, config: PipelineConfig) -> None:
        """Populate preferences dialog controls from PipelineConfig."""
        # AI Tab
        prov = (config.vlm_provider or "ollama").lower()
        if prov == "openrouter":
            self.rb_openrouter.setChecked(True)
        elif prov == "lm_studio":
            self.rb_lmstudio.setChecked(True)
        elif prov == "custom":
            self.rb_custom.setChecked(True)
        else:
            self.rb_ollama.setChecked(True)

        if config.vlm_base_url:
            self.edit_base_url.setText(config.vlm_base_url)
        elif prov == "openrouter":
            self.edit_base_url.setText("https://openrouter.ai/api/v1")
        elif prov in ("ollama", "lm_studio"):
            self.edit_base_url.setText("http://localhost:11434/v1")

        if config.vlm_api_key:
            self.edit_api_key.setText(config.vlm_api_key)

        if config.vlm_model:
            self.combo_model.setCurrentText(config.vlm_model)

        # Ingest Tab
        self.spin_silence.setValue(int(config.silence_threshold_db))
        self.spin_silence_dur.setValue(float(config.dead_air_min_duration))
        self.spin_freeze.setValue(float(config.freeze_threshold))
        self.spin_dedup_hash.setValue(int(config.duplicate_hash_threshold))

        # Audio Tab
        self.chk_strip_bwf.setChecked(bool(config.enable_audio_fix))
        idx = self.combo_sample_rate.findData(config.audio_sample_rate)
        if idx >= 0:
            self.combo_sample_rate.setCurrentIndex(idx)
        self.spin_ducking.setValue(int(config.ducking_attenuation_db))

        # Subtitles Tab
        if config.whisper_model_size:
            self.combo_whisper_model.setCurrentText(config.whisper_model_size)
        self.spin_cpl.setValue(int(config.subtitle_cpl))
        self.chk_export_srt.setChecked(bool(config.export_subtitles_srt))
        self.chk_burn_hardsubs.setChecked(bool(config.burn_hardsubs))

        # Agent & MCP Tab
        agent_idx = self.combo_agent_provider.findData(config.agent_provider)
        if agent_idx >= 0:
            self.combo_agent_provider.setCurrentIndex(agent_idx)
        if config.agent_model:
            self.combo_agent_model.setCurrentText(config.agent_model)
        if config.agent_api_key:
            self.edit_agent_api_key.setText(config.agent_api_key)
        self.combo_mcp_transport.setCurrentIndex(1 if config.mcp_transport == "sse" else 0)
        self.spin_mcp_port.setValue(config.mcp_port or 8000)

    def save_to_disk(self, config_path: Path | None = None) -> None:
        """Serialize configuration parameters into JSON file."""
        target_path = config_path or DEFAULT_CONFIG_PATH
        target_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "vlm_provider": self.get_provider_key(),
            "vlm_base_url": self.edit_base_url.text().strip(),
            "vlm_api_key": self.edit_api_key.text().strip(),
            "vlm_model": self.combo_model.currentText().strip(),
            "silence_threshold_db": self.spin_silence.value(),
            "dead_air_min_duration": self.spin_silence_dur.value(),
            "freeze_threshold": self.spin_freeze.value(),
            "duplicate_hash_threshold": self.spin_dedup_hash.value(),
            "enable_audio_fix": self.chk_strip_bwf.isChecked(),
            "audio_sample_rate": self.combo_sample_rate.currentData(),
            "ducking_attenuation_db": self.spin_ducking.value(),
            "whisper_model_size": self.combo_whisper_model.currentText().strip(),
            "subtitle_cpl": self.spin_cpl.value(),
            "export_subtitles_srt": self.chk_export_srt.isChecked(),
            "burn_hardsubs": self.chk_burn_hardsubs.isChecked(),
            "agent_provider": self.combo_agent_provider.currentData(),
            "agent_model": self.combo_agent_model.currentText().strip(),
            "agent_api_key": self.edit_agent_api_key.text().strip(),
            "mcp_transport": "sse" if self.combo_mcp_transport.currentIndex() == 1 else "stdio",
            "mcp_port": self.spin_mcp_port.value(),
        }
        try:
            target_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("Preferences successfully saved to %s", target_path)
        except Exception as exc:
            logger.warning("Could not persist preferences to %s: %s", target_path, exc)

    def load_from_disk(self, config_path: Path | None = None) -> bool:
        """Load configuration parameters from disk if file exists."""
        target_path = config_path or DEFAULT_CONFIG_PATH
        if not target_path.exists():
            return False
        try:
            raw = json.loads(target_path.read_text(encoding="utf-8"))
            if "vlm_provider" in raw:
                prov = raw["vlm_provider"]
                if prov == "openrouter":
                    self.rb_openrouter.setChecked(True)
                elif prov == "lm_studio":
                    self.rb_lmstudio.setChecked(True)
                elif prov == "custom":
                    self.rb_custom.setChecked(True)
                else:
                    self.rb_ollama.setChecked(True)
            if raw.get("vlm_base_url"):
                self.edit_base_url.setText(raw["vlm_base_url"])
            if raw.get("vlm_api_key"):
                self.edit_api_key.setText(raw["vlm_api_key"])
            if raw.get("vlm_model"):
                self.combo_model.setCurrentText(raw["vlm_model"])
            if "silence_threshold_db" in raw:
                self.spin_silence.setValue(int(raw["silence_threshold_db"]))
            if "dead_air_min_duration" in raw:
                self.spin_silence_dur.setValue(float(raw["dead_air_min_duration"]))
            if "freeze_threshold" in raw:
                self.spin_freeze.setValue(float(raw["freeze_threshold"]))
            if "duplicate_hash_threshold" in raw:
                self.spin_dedup_hash.setValue(int(raw["duplicate_hash_threshold"]))
            if "enable_audio_fix" in raw:
                self.chk_strip_bwf.setChecked(bool(raw["enable_audio_fix"]))
            if "audio_sample_rate" in raw:
                idx = self.combo_sample_rate.findData(raw["audio_sample_rate"])
                if idx >= 0:
                    self.combo_sample_rate.setCurrentIndex(idx)
            if "ducking_attenuation_db" in raw:
                self.spin_ducking.setValue(int(raw["ducking_attenuation_db"]))
            if "whisper_model_size" in raw:
                self.combo_whisper_model.setCurrentText(raw["whisper_model_size"])
            if "subtitle_cpl" in raw:
                self.spin_cpl.setValue(int(raw["subtitle_cpl"]))
            if "export_subtitles_srt" in raw:
                self.chk_export_srt.setChecked(bool(raw["export_subtitles_srt"]))
            if "burn_hardsubs" in raw:
                self.chk_burn_hardsubs.setChecked(bool(raw["burn_hardsubs"]))
            if "agent_provider" in raw:
                idx = self.combo_agent_provider.findData(raw["agent_provider"])
                if idx >= 0:
                    self.combo_agent_provider.setCurrentIndex(idx)
            if raw.get("agent_model"):
                self.combo_agent_model.setCurrentText(raw["agent_model"])
            if raw.get("agent_api_key"):
                self.edit_agent_api_key.setText(raw["agent_api_key"])
            if "mcp_transport" in raw:
                self.combo_mcp_transport.setCurrentIndex(1 if raw["mcp_transport"] == "sse" else 0)
            if "mcp_port" in raw:
                self.spin_mcp_port.setValue(int(raw["mcp_port"]))
            self.apply_to_config(self.config)
            return True
        except Exception as exc:
            logger.warning("Could not read preferences from %s: %s", target_path, exc)
            return False
