"""Persistent AI endpoint settings store and configuration builder."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from src.core.config import PipelineConfig

logger = logging.getLogger(__name__)

ALLOWED_PROVIDERS: frozenset[str] = frozenset(
    {"auto", "openrouter", "deepseek", "openai", "ollama", "lm_studio", "custom"}
)
ALLOWED_FIELDS: frozenset[str] = frozenset({"provider", "base_url", "model", "api_key"})

DEFAULT_SETTINGS: dict[str, Any] = {
    "provider": "auto",
    "base_url": None,
    "model": "deepseek/deepseek-chat",
    "api_key": None,
}


class AiSettingsStore:
    """Thread-safe, atomic file-backed store for AI provider settings."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._load()

    def _load(self) -> None:
        """Load settings from JSON file if it exists."""
        if self.path.is_file():
            try:
                content = self.path.read_text(encoding="utf-8")
                data = json.loads(content)
                if isinstance(data, dict):
                    for k in ALLOWED_FIELDS:
                        if k in data:
                            self._settings[k] = data[k]
            except Exception as exc:
                logger.warning("Failed to load AI settings from %s: %s", self.path, exc)

    def _save_atomic(self) -> None:
        """Persist current settings atomically to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2)
            os.replace(tmp_path, self.path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    def get(self) -> dict[str, Any]:
        """Return public dictionary without the secret api_key."""
        with self._lock:
            return {
                "provider": self._settings.get("provider", "auto"),
                "base_url": self._settings.get("base_url"),
                "model": self._settings.get("model", "deepseek/deepseek-chat"),
                "has_api_key": bool(self._settings.get("api_key")),
            }

    def secret(self) -> str | None:
        """Return the unredacted API key, or None if not set."""
        with self._lock:
            val = self._settings.get("api_key")
            return str(val) if val else None

    def set(self, update: dict[str, Any]) -> dict[str, Any]:
        """Update settings, validate fields/provider, persist atomically, and return get().

        Raises:
            ValueError: if update is not a dict, contains disallowed fields, or invalid provider.
        """
        if not isinstance(update, dict):
            raise ValueError(f"update must be a dict, got {type(update).__name__}")

        with self._lock:
            # Phase 1: validate all fields before mutation
            for k, v in update.items():
                if k not in ALLOWED_FIELDS:
                    raise ValueError(f"Unknown AI setting field: {k!r}")
                if k == "provider":
                    if v not in ALLOWED_PROVIDERS:
                        raise ValueError(
                            f"Invalid provider: {v!r}. Allowed: {sorted(ALLOWED_PROVIDERS)}"
                        )

            # Phase 2: apply updates
            for k, v in update.items():
                if k == "api_key":
                    self._settings["api_key"] = str(v) if v else None
                elif k == "base_url":
                    self._settings["base_url"] = str(v) if v else None
                elif k == "model":
                    self._settings["model"] = str(v) if v is not None else "deepseek/deepseek-chat"
                elif k == "provider":
                    self._settings["provider"] = str(v)

            self._save_atomic()
            return {
                "provider": self._settings.get("provider", "auto"),
                "base_url": self._settings.get("base_url"),
                "model": self._settings.get("model", "deepseek/deepseek-chat"),
                "has_api_key": bool(self._settings.get("api_key")),
            }


def agent_config_from_settings(store: AiSettingsStore) -> PipelineConfig:
    """Build a PipelineConfig with agent fields populated from the settings store."""
    info = store.get()
    return PipelineConfig(
        agent_provider=info["provider"],
        agent_base_url=info["base_url"],
        agent_model=info["model"],
        agent_api_key=store.secret(),
    )
