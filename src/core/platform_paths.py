"""Cross-platform data directory, tool PATH injection, and DaVinci Resolve library naming."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from src.core.platform_caps import OsName


def default_data_dir(os_name: OsName, env: Mapping[str, str], home: Path) -> Path:
    """Return the default TeamTrau engine data directory for the given OS."""
    if os_name == "windows":
        la = env.get("LOCALAPPDATA")
        base = Path(la) if la else home / "AppData" / "Local"
    elif os_name == "macos":
        base = home / "Library" / "Application Support"
    elif os_name == "linux":
        xdg = env.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else home / ".local" / "share"
    else:
        raise ValueError(f"Unsupported OS: {os_name}")
    return base / "TeamTrau" / "engine"


def resolve_data_dir(
    cli_value: str | None,
    env: Mapping[str, str],
    os_name: OsName,
    home: Path,
) -> Path:
    """Resolve the active engine data directory with CLI > env > OS default precedence."""
    if cli_value:
        return Path(cli_value)
    env_val = env.get("TEAMTRAU_DATA_DIR")
    if env_val:
        return Path(env_val)
    return default_data_dir(os_name, env, home)


def prepend_tool_dir(tool_dir: Path, env: MutableMapping[str, str]) -> None:
    """Prepend tool_dir to env['PATH'] without duplicates."""
    target = str(tool_dir)
    current = env.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p and p != target]
    env["PATH"] = os.pathsep.join([target, *parts])


def fusionscript_name(os_name: OsName) -> str:
    """Return the DaVinci Resolve fusionscript shared library filename for the given OS."""
    if os_name == "windows":
        return "fusionscript.dll"
    if os_name in ("macos", "linux"):
        return "fusionscript.so"
    raise ValueError(f"Unsupported OS: {os_name}")
