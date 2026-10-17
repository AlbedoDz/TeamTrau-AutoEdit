"""Unit tests for PreferencesDialog and AI Engine configuration."""

import os

import pytest

from src.core.config import PipelineConfig
from src.ui.app import create_app
from src.ui.widgets.settings_dialog import PreferencesDialog

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


@pytest.fixture(autouse=True)
def isolate_config_path(tmp_path, monkeypatch):
    """Ensure tests run against an isolated non-existent config file."""
    test_cfg = tmp_path / "isolated_config.json"
    monkeypatch.setattr("src.ui.widgets.settings_dialog.DEFAULT_CONFIG_PATH", test_cfg)


def test_preferences_dialog_initial_state(qapp):
    """Verify PreferencesDialog initializes with all 5 tabs and expected defaults."""
    dialog = PreferencesDialog()
    assert dialog.tabs.count() == 5
    assert dialog.tabs.tabText(0) == "AI & LLM Engine Hub"
    assert dialog.tabs.tabText(1) == "Ingest & Dead-Air"
    assert dialog.tabs.tabText(2) == "Audio & Normalization"
    assert dialog.tabs.tabText(3) == "Subtitles & Transcription"
    assert dialog.tabs.tabText(4) == "AI Agent & MCP Hub"

    # Default provider should be Ollama or active config
    assert dialog.rb_ollama.isChecked() or dialog.rb_openrouter.isChecked()
    assert dialog.spin_silence.value() == -35
    assert dialog.spin_silence_dur.value() == 3.0
    assert dialog.chk_strip_bwf.isChecked() is True
    assert dialog.chk_export_srt.isChecked() is True


def test_preferences_dialog_provider_switching(qapp):
    """Verify radio button provider selection updates default base URL and model suggestions."""
    dialog = PreferencesDialog()

    # Switch to OpenRouter
    dialog.rb_openrouter.setChecked(True)
    assert "openrouter.ai" in dialog.edit_base_url.text()
    assert "gpt-4o" in dialog.combo_model.currentText()
    assert dialog.get_provider_key() == "openrouter"

    # Switch to LM Studio
    dialog.rb_lmstudio.setChecked(True)
    assert "1234" in dialog.edit_base_url.text()
    assert dialog.get_provider_key() == "lm_studio"

    # Switch to Ollama
    dialog.rb_ollama.setChecked(True)
    assert "11434" in dialog.edit_base_url.text()
    assert dialog.get_provider_key() == "ollama"


def test_preferences_dialog_password_reveal(qapp):
    """Verify eye button toggles API key masking."""
    from PySide6.QtWidgets import QLineEdit

    dialog = PreferencesDialog()
    assert dialog.edit_api_key.echoMode() == QLineEdit.EchoMode.Password

    # Reveal
    dialog.btn_reveal_key.setChecked(True)
    assert dialog.edit_api_key.echoMode() == QLineEdit.EchoMode.Normal

    # Mask again
    dialog.btn_reveal_key.setChecked(False)
    assert dialog.edit_api_key.echoMode() == QLineEdit.EchoMode.Password


def test_preferences_dialog_config_bidirectional_sync(qapp):
    """Verify values transfer between PipelineConfig and PreferencesDialog."""
    dialog = PreferencesDialog()

    cfg = PipelineConfig()
    cfg.vlm_provider = "openrouter"
    cfg.vlm_base_url = "https://custom.endpoint/v1"
    cfg.vlm_api_key = "sk-secret-test-key"
    cfg.vlm_model = "deepseek/deepseek-chat"
    cfg.silence_threshold_db = -42.0
    cfg.dead_air_min_duration = 4.5
    cfg.freeze_threshold = 0.035
    cfg.duplicate_hash_threshold = 8
    cfg.enable_audio_fix = False
    cfg.audio_sample_rate = 44100
    cfg.ducking_attenuation_db = -18.0
    cfg.whisper_model_size = "medium"
    cfg.subtitle_cpl = 22
    cfg.export_subtitles_srt = False
    cfg.burn_hardsubs = True
    cfg.enable_vlm_tagging = True

    # Load from config into dialog
    dialog.load_from_config(cfg)
    assert dialog.chk_enable_vlm.isChecked() is True
    assert dialog.edit_base_url.text() == "https://custom.endpoint/v1"
    assert dialog.edit_api_key.text() == "sk-secret-test-key"
    assert dialog.combo_model.currentText() == "deepseek/deepseek-chat"
    assert dialog.spin_silence.value() == -42
    assert dialog.spin_silence_dur.value() == 4.5
    assert dialog.spin_freeze.value() == 0.035
    assert dialog.spin_dedup_hash.value() == 8
    assert dialog.chk_strip_bwf.isChecked() is False
    assert dialog.combo_sample_rate.currentData() == 44100
    assert dialog.spin_ducking.value() == -18
    assert dialog.combo_whisper_model.currentText() == "medium"
    assert dialog.spin_cpl.value() == 22
    assert dialog.chk_export_srt.isChecked() is False
    assert dialog.chk_burn_hardsubs.isChecked() is True

    # Apply back to a fresh config
    target_cfg = PipelineConfig()
    dialog.apply_to_config(target_cfg)

    assert target_cfg.enable_vlm_tagging is True
    assert target_cfg.vlm_base_url == "https://custom.endpoint/v1"
    assert target_cfg.vlm_api_key == "sk-secret-test-key"
    assert target_cfg.vlm_model == "deepseek/deepseek-chat"
    assert target_cfg.silence_threshold_db == -42.0
    assert target_cfg.dead_air_min_duration == 4.5
    assert target_cfg.duplicate_hash_threshold == 8
    assert target_cfg.enable_audio_fix is False
    assert target_cfg.audio_sample_rate == 44100
    assert target_cfg.ducking_attenuation_db == -18.0
    assert target_cfg.whisper_model_size == "medium"
    assert target_cfg.subtitle_cpl == 22
    assert target_cfg.export_subtitles_srt is False
    assert target_cfg.burn_hardsubs is True


def test_preferences_dialog_disk_persistence(qapp, tmp_path):
    """Verify settings serialize to JSON file and reload accurately."""
    custom_config_path = tmp_path / "teamtrau_test_config.json"
    dialog = PreferencesDialog()

    dialog.edit_base_url.setText("http://192.168.1.200:11434/v1")
    dialog.edit_api_key.setText("test-lan-key")
    dialog.spin_silence.setValue(-38)
    dialog.save_to_disk(custom_config_path)

    assert custom_config_path.exists()

    # Create new dialog instance and load from that file
    new_dialog = PreferencesDialog()
    loaded = new_dialog.load_from_disk(custom_config_path)
    assert loaded is True
    assert new_dialog.edit_base_url.text() == "http://192.168.1.200:11434/v1"
    assert new_dialog.edit_api_key.text() == "test-lan-key"
    assert new_dialog.spin_silence.value() == -38


def test_preferences_dialog_ping_callback(qapp):
    """Verify diagnostic ping result updates status label."""
    dialog = PreferencesDialog()

    # Simulate success
    dialog._on_ping_finished(True, 114.2, "Connected (200)")
    assert "114ms" in dialog.lbl_ping_result.text()
    assert "Connected" in dialog.lbl_ping_result.text()

    # Simulate failure
    dialog._on_ping_finished(False, 50.0, "Connection refused [Errno 111]")
    assert "Failed" in dialog.lbl_ping_result.text()
