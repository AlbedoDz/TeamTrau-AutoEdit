"""Interactive AI Agent Co-Pilot Console Dialog for Desktop Studio.

Allows the user to issue natural language editing commands to autonomous AI agents
(OpenRouter, DeepSeek v4, Claude 3.5 Sonnet, GPT-4o, Local Ollama/LM Studio)
and observes real-time tool calls across all project capabilities.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.config import PipelineConfig
from src.modules.agent_controller import (
    AgentCallbacks,
    AgentConfig,
    AgentResult,
    AutoEditAgentController,
)

logger = logging.getLogger(__name__)


class _AgentThreadSignals(QObject):
    thought = Signal(str)
    tool_call = Signal(str, dict)
    tool_result = Signal(str, object)
    response = Signal(str)
    error = Signal(str)
    finished = Signal(object)  # emits AgentResult


class AgentConsoleDialog(QDialog):
    """Interactive dialog window for pairing with the AI Co-Pilot agent."""

    def __init__(
        self,
        config: PipelineConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config or PipelineConfig()
        prov = self.config.agent_provider if self.config.agent_provider != "auto" else (self.config.vlm_provider or "auto")
        model = self.config.vlm_model or self.config.agent_model or "deepseek/deepseek-chat"
        api_key = self.config.agent_api_key or self.config.vlm_api_key
        base_url = self.config.agent_base_url or self.config.vlm_base_url

        self.agent_config = AgentConfig(
            provider=prov,
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=self.config.agent_temperature,
            max_steps=self.config.agent_max_steps,
        )
        self.agent = AutoEditAgentController(self.agent_config)

        self._signals = _AgentThreadSignals()
        self._signals.thought.connect(self._on_thought)
        self._signals.tool_call.connect(self._on_tool_call)
        self._signals.tool_result.connect(self._on_tool_result)
        self._signals.response.connect(self._on_response)
        self._signals.error.connect(self._on_error)
        self._signals.finished.connect(self._on_finished)

        self._worker_thread: threading.Thread | None = None
        self._cancel_event = threading.Event()

        self._init_ui()
        self._update_header_badge()

    def update_config(self, config: PipelineConfig) -> None:
        """Update active agent configuration and refresh header badges."""
        self.config = config
        prov = config.agent_provider if config.agent_provider != "auto" else (config.vlm_provider or "auto")
        model = config.vlm_model or config.agent_model or "deepseek/deepseek-chat"
        api_key = config.agent_api_key or config.vlm_api_key
        base_url = config.agent_base_url or config.vlm_base_url

        self.agent_config = AgentConfig(
            provider=prov,
            base_url=base_url,
            api_key=api_key,
            model=model,
            temperature=config.agent_temperature,
            max_steps=config.agent_max_steps,
        )
        self.agent = AutoEditAgentController(self.agent_config)
        self._update_header_badge()

    def _update_header_badge(self) -> None:
        prov = self.agent.config.provider.lower()
        is_cloud = prov in ("openrouter", "deepseek", "openai")
        if prov == "custom":
            base = (self.agent.config.base_url or "").lower()
            is_cloud = not any(local_ip in base for local_ip in ("localhost", "127.0.0.1", "192.168.", "10.0.", "172.16."))

        tag = "Cloud" if is_cloud else "Local"
        prov_name = {
            "openrouter": "OpenRouter",
            "deepseek": "DeepSeek",
            "openai": "OpenAI",
            "ollama": "Ollama",
            "lm_studio": "LM Studio",
            "custom": "Custom",
        }.get(prov, prov.title())

        badge_color = "#38bdf8" if is_cloud else "#4ade80"
        self.badge_provider.setText(f"[{tag}] {prov_name} | {self.agent.config.model}")
        self.badge_provider.setStyleSheet(
            f"background-color: #27272a; color: {badge_color}; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: bold;"
        )

    def _init_ui(self) -> None:
        self.setWindowTitle("AI Co-Pilot Console & MCP Orchestrator")
        self.resize(760, 560)
        self.setMinimumSize(600, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # Header Info
        header = QHBoxLayout()
        title = QLabel("🤖 AutoEdit AI Co-Pilot & MCP Agent", self)
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #f4f4f5;")
        header.addWidget(title)
        header.addStretch()

        self.badge_provider = QLabel("", self)
        header.addWidget(self.badge_provider)
        layout.addLayout(header)

        # Log & Transcript View
        self.log_view = QTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet(
            "background-color: #18181b; color: #e4e4e7; font-family: Consolas, monospace; font-size: 12px; border: 1px solid #27272a; border-radius: 6px; padding: 8px;"
        )
        layout.addWidget(self.log_view, stretch=1)

        # Quick Suggestion Chips
        chips_layout = QHBoxLayout()
        chips_layout.setSpacing(6)
        chips_label = QLabel("Quick Actions:", self)
        chips_label.setStyleSheet("color: #a1a1aa; font-size: 11px;")
        chips_layout.addWidget(chips_label)

        suggestions = [
            ("⚡ Check Hardware", "Kiểm tra hệ thống phần cứng, CUDA và GPU NVENC."),
            ("🎬 TikTok 9:16", "Dựng video dọc 9:16 phong cách viral, nhạc nền sôi động và phụ đề kinetic."),
            ("🎵 Analyze Soundtrack", "Phân tích nhịp điệu, BPM và các đoạn DROP của nhạc nền."),
            ("📋 Export MCP Config", "Tạo và hiển thị cấu hình MCP chuẩn cho Antigravity và Claude."),
        ]
        for label, prompt_text in suggestions:
            btn = QPushButton(label, self)
            btn.setStyleSheet(
                "background-color: #27272a; color: #d4d4d8; font-size: 11px; padding: 3px 8px; border-radius: 4px;"
            )
            btn.clicked.connect(lambda _, t=prompt_text: self._set_prompt(t))
            chips_layout.addWidget(btn)

        chips_layout.addStretch()
        layout.addLayout(chips_layout)

        # Input Prompt Bar
        input_layout = QHBoxLayout()
        input_layout.setSpacing(8)

        self.edit_input = QLineEdit(self)
        self.edit_input.setPlaceholderText("Enter natural language editing directive in Vietnamese or English...")
        self.edit_input.returnPressed.connect(self._on_send_clicked)
        input_layout.addWidget(self.edit_input, stretch=1)

        self.btn_send = QPushButton("🚀 Run Agent", self)
        self.btn_send.setObjectName("primaryButton")
        self.btn_send.clicked.connect(self._on_send_clicked)
        input_layout.addWidget(self.btn_send)

        self.btn_cancel = QPushButton("⏹ Cancel", self)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)
        input_layout.addWidget(self.btn_cancel)

        layout.addLayout(input_layout)

        # Initial Welcome Message
        self._append_html(
            "<span style='color: #4ade80; font-weight: bold;'>[System Ready]</span> "
            "AI Agent Co-Pilot is online with full access to 23 MCP video editing tools.<br>"
            "Type a directive or click a quick action above to begin."
        )

    def _set_prompt(self, text: str) -> None:
        self.edit_input.setText(text)
        self.edit_input.setFocus()

    def _append_html(self, html: str) -> None:
        self.log_view.append(html)
        sb = self.log_view.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    @Slot()
    def _on_send_clicked(self) -> None:
        prompt = self.edit_input.text().strip()
        if not prompt:
            return

        self.edit_input.clear()
        self.btn_send.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self._cancel_event.clear()

        self._append_html(f"<br><span style='color: #38bdf8; font-weight: bold;'>👤 User:</span> {prompt}")

        callbacks = AgentCallbacks(
            on_thought=lambda t: self._signals.thought.emit(t),
            on_tool_call=lambda name, args: self._signals.tool_call.emit(name, args),
            on_tool_result=lambda name, res: self._signals.tool_result.emit(name, res),
            on_agent_response=lambda r: self._signals.response.emit(r),
            on_error=lambda err: self._signals.error.emit(err),
        )

        def _worker() -> None:
            res = self.agent.run(
                prompt=prompt,
                callbacks=callbacks,
                cancel_event=self._cancel_event,
            )
            self._signals.finished.emit(res)

        self._worker_thread = threading.Thread(target=_worker, daemon=True)
        self._worker_thread.start()

    @Slot()
    def _on_cancel_clicked(self) -> None:
        self._cancel_event.set()
        self._append_html("<span style='color: #f87171;'>[Action] Sending cancel signal to agent...</span>")

    @Slot(str)
    def _on_thought(self, text: str) -> None:
        self._append_html(f"<span style='color: #fbbf24;'>🧠 [Thought]:</span> {text}")

    @Slot(str, dict)
    def _on_tool_call(self, name: str, args: dict[str, Any]) -> None:
        arg_str = ", ".join(f"{k}='{v}'" for k, v in args.items()) if args else ""
        self._append_html(
            f"<span style='color: #a78bfa; font-weight: 500;'>⚡ [Tool Call]:</span> <code>{name}({arg_str})</code>"
        )

    @Slot(str, object)
    def _on_tool_result(self, name: str, res: Any) -> None:
        preview = str(res)
        if len(preview) > 160:
            preview = preview[:160] + "..."
        self._append_html(f"<span style='color: #34d399;'>✓ [Tool Result]:</span> {preview}")

    @Slot(str)
    def _on_response(self, text: str) -> None:
        formatted = text.replace("\n", "<br>")
        self._append_html(
            f"<br><span style='color: #60a5fa; font-weight: bold;'>🤖 [Agent Response]:</span><br>{formatted}"
        )

    @Slot(str)
    def _on_error(self, err: str) -> None:
        self._append_html(f"<span style='color: #ef4444; font-weight: bold;'>❌ [Error]:</span> {err}")

    @Slot(object)
    def _on_finished(self, res: AgentResult) -> None:
        self.btn_send.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        status_color = "#4ade80" if res.success else "#ef4444"
        self._append_html(
            f"<span style='color: {status_color}; font-size: 11px;'>"
            f"--- Task finished in {res.duration_sec:.2f}s (Success: {res.success}) ---</span><br>"
        )
