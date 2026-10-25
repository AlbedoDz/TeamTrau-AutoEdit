"""UI controllers package with PEP 562 lazy re-exports to avoid eager Qt imports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.ui.controllers.project_manager import ProjectData, ProjectManager
    from src.ui.controllers.shortcut_manager import ShortcutManager
    from src.ui.controllers.undo_commands import (
        ClipDeleteCommand,
        ClipPasteCommand,
        SceneToggleCommand,
    )

__all__ = [
    "ClipDeleteCommand",
    "ClipPasteCommand",
    "ProjectData",
    "ProjectManager",
    "SceneToggleCommand",
    "ShortcutManager",
]

_LAZY_MAP: dict[str, str] = {
    "ProjectData": "src.ui.controllers.project_manager",
    "ProjectManager": "src.ui.controllers.project_manager",
    "ShortcutManager": "src.ui.controllers.shortcut_manager",
    "ClipDeleteCommand": "src.ui.controllers.undo_commands",
    "ClipPasteCommand": "src.ui.controllers.undo_commands",
    "SceneToggleCommand": "src.ui.controllers.undo_commands",
}


def __getattr__(name: str) -> Any:
    """Lazily import controller classes so importing project_manager stays Qt-free."""
    mod_name = _LAZY_MAP.get(name)
    if mod_name is not None:
        import importlib

        mod = importlib.import_module(mod_name)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
