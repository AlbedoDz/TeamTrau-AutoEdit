"""Unit and integration tests for AI Agent CLI commands and Desktop Studio GUI."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from PySide6.QtWidgets import QApplication

from main import cli
from src.core.config import PipelineConfig
from src.ui.widgets.agent_console_dialog import AgentConsoleDialog
from src.ui.widgets.settings_dialog import PreferencesDialog

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    """Ensure a singleton QApplication instance exists for GUI tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_cli_mcp_export_config():
    """Verify click CLI mcp --export-config outputs valid JSON."""
    runner = CliRunner()
    result = runner.invoke(cli, ["mcp", "--export-config", "antigravity"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["client"] == "antigravity"
    assert "teamtrau-autoedit" in data["config"]["mcpServers"]


def test_cli_agent_heuristic_execution():
    """Verify click CLI agent command executes prompt in heuristic mode."""
    runner = CliRunner()
    result = runner.invoke(cli, ["agent", "Kiểm tra hệ thống phần cứng", "--provider", "auto"])

    assert result.exit_code == 0
    assert "[AI Agent Active]" in result.output
    assert "check_system_environment" in result.output
    assert "Completed in" in result.output


def test_preferences_dialog_agent_and_mcp_hub(qapp, tmp_path: Path):
    """Verify PreferencesDialog Tab 5 AI Agent & MCP Hub configuration."""
    cfg = PipelineConfig(
        agent_provider="openrouter",
        agent_model="anthropic/claude-3.5-sonnet",
        agent_api_key="sk-test-key",
        mcp_transport="sse",
        mcp_port=8888,
    )
    dialog = PreferencesDialog(config=cfg)

    # Verify Tab 5 exists
    tab_titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert "AI Agent & MCP Hub" in tab_titles

    # Verify UI state populated from config
    assert dialog.combo_agent_model.currentText() == "anthropic/claude-3.5-sonnet"
    assert dialog.edit_agent_api_key.text() == "sk-test-key"
    assert dialog.combo_mcp_transport.currentIndex() == 1  # SSE
    assert dialog.spin_mcp_port.value() == 8888

    # Test copy slots
    dialog._on_copy_antigravity_config()
    assert "✓" in dialog.lbl_mcp_status.text()

    dialog._on_copy_claude_config()
    assert "✓" in dialog.lbl_mcp_status.text()

    dialog._on_copy_cursor_config()
    assert "✓" in dialog.lbl_mcp_status.text()

    # Modify and apply to config
    dialog.combo_agent_provider.setCurrentIndex(0)  # deepseek
    dialog.combo_agent_model.setCurrentText("deepseek/deepseek-chat")
    target_cfg = PipelineConfig()
    dialog.apply_to_config(target_cfg)
    assert target_cfg.agent_provider == "deepseek"
    assert target_cfg.agent_model == "deepseek/deepseek-chat"

    # Persist to disk and reload
    cfg_file = tmp_path / "custom_config.json"
    dialog.save_to_disk(cfg_file)
    assert cfg_file.is_file()

    new_dialog = PreferencesDialog()
    loaded = new_dialog.load_from_disk(cfg_file)
    assert loaded is True
    assert new_dialog.combo_agent_model.currentText() == "deepseek/deepseek-chat"


def test_agent_console_dialog_gui(qapp):
    """Verify AgentConsoleDialog widget initialization, chips, and logging."""
    dialog = AgentConsoleDialog()

    assert dialog.windowTitle() == "AI Co-Pilot Console & MCP Orchestrator"
    assert dialog.edit_input.placeholderText() != ""

    # Test setting prompt via chip
    dialog._set_prompt("Dựng video 9:16 viral TikTok")
    assert dialog.edit_input.text() == "Dựng video 9:16 viral TikTok"

    # Test log streaming slots
    dialog._on_thought("Analyzing rhythm...")
    dialog._on_tool_call("analyze_soundtrack", {"audio_path": "test.mp3"})
    dialog._on_tool_result("analyze_soundtrack", {"bpm": 128})
    dialog._on_response("Finished assembling sequence.")

    log_html = dialog.log_view.toHtml()
    assert "Analyzing rhythm" in log_html
    assert "analyze_soundtrack" in log_html
    assert "Finished assembling sequence" in log_html

    dialog.close()


def test_main_window_agent_console_integration(qapp, monkeypatch):
    """Verify StudioMainWindow has .config property, topbar AI badge syncs, and opens AgentConsoleDialog without error."""
    from src.ui.main_window import StudioMainWindow
    from src.ui.widgets.agent_console_dialog import AgentConsoleDialog

    window = StudioMainWindow()

    # Verify .config property returns valid PipelineConfig
    assert hasattr(window, "config")
    cfg = window.config
    assert isinstance(cfg, PipelineConfig)

    # Verify initial topbar AI badge contains Local or Cloud indicator
    badge_text = window.btn_ai_badge.text()
    assert "🟢" in badge_text
    assert ("Local" in badge_text) or ("Cloud" in badge_text)

    # Verify settings applied dynamically updates AI badge
    cloud_cfg = PipelineConfig(
        vlm_provider="openrouter",
        vlm_model="deepseek/deepseek-chat",
        vlm_base_url="https://openrouter.ai/api/v1",
    )
    window._on_settings_applied(cloud_cfg)
    assert "Cloud: OpenRouter (deepseek-chat)" in window.btn_ai_badge.text()

    # Monkeypatch show on AgentConsoleDialog to verify modeless instantiation
    show_called = []
    monkeypatch.setattr(AgentConsoleDialog, "show", lambda self: show_called.append(True))
    monkeypatch.setattr(AgentConsoleDialog, "raise_", lambda self: None)
    monkeypatch.setattr(AgentConsoleDialog, "activateWindow", lambda self: None)

    window._on_open_agent_console()
    assert len(show_called) == 1
    assert window._agent_console_dialog is not None

    # Cleanup cleanly
    window._set_dirty(False)
    window.close()

