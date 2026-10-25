"""P5.0: engine portability core (data dir, ffmpeg override, token perms, no Qt leak, Resolve paths)."""

import os
import stat
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from src.core.config import PipelineConfig
from src.core.platform_paths import (
    default_data_dir,
    fusionscript_name,
    prepend_tool_dir,
    resolve_data_dir,
)

ROOT = Path(__file__).resolve().parents[1]


def test_default_data_dir_per_os(tmp_path: Path):
    home = tmp_path / "home"
    assert default_data_dir("windows", {"LOCALAPPDATA": str(tmp_path / "la")}, home) == tmp_path / "la" / "TeamTrau" / "engine"
    assert default_data_dir("windows", {}, home) == home / "AppData" / "Local" / "TeamTrau" / "engine"
    assert default_data_dir("macos", {}, home) == home / "Library" / "Application Support" / "TeamTrau" / "engine"
    assert default_data_dir("linux", {"XDG_DATA_HOME": str(tmp_path / "xdg")}, home) == tmp_path / "xdg" / "TeamTrau" / "engine"
    assert default_data_dir("linux", {}, home) == home / ".local" / "share" / "TeamTrau" / "engine"


def test_resolve_data_dir_precedence(tmp_path: Path):
    env = {"TEAMTRAU_DATA_DIR": str(tmp_path / "from-env")}
    assert resolve_data_dir(str(tmp_path / "cli"), env, "linux", tmp_path) == tmp_path / "cli"
    assert resolve_data_dir(None, env, "linux", tmp_path) == tmp_path / "from-env"
    assert resolve_data_dir(None, {}, "linux", tmp_path) == tmp_path / ".local" / "share" / "TeamTrau" / "engine"


def test_cache_dir_follows_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TEAMTRAU_CACHE_DIR", str(tmp_path / "c"))
    assert PipelineConfig().cache_dir == tmp_path / "c"
    monkeypatch.delenv("TEAMTRAU_CACHE_DIR")
    assert PipelineConfig().cache_dir == Path(".cache/autoedit")


def test_prepend_tool_dir_makes_bundled_ffmpeg_findable(tmp_path: Path):
    import shutil

    tools = tmp_path / "tools"
    tools.mkdir()
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    exe = tools / name
    exe.write_bytes(b"")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": str(tmp_path / "elsewhere")}
    prepend_tool_dir(tools, env)
    assert env["PATH"].split(os.pathsep)[0] == str(tools)
    found = shutil.which("ffmpeg", path=env["PATH"])
    assert found is not None and Path(found).parent == tools
    prepend_tool_dir(tools, env)
    assert env["PATH"].split(os.pathsep).count(str(tools)) == 1


def test_fusionscript_name_per_os():
    assert fusionscript_name("windows") == "fusionscript.dll"
    assert fusionscript_name("macos") == "fusionscript.so"
    assert fusionscript_name("linux") == "fusionscript.so"


def test_serve_uses_data_dir_and_private_token(tmp_path: Path, monkeypatch):
    import uvicorn

    import main

    captured: dict = {}
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: captured.setdefault("ran", True))
    monkeypatch.delenv("TEAMTRAU_CACHE_DIR", raising=False)
    data = tmp_path / "data"
    res = CliRunner().invoke(main.cli, ["serve", "--port", "8797", "--data-dir", str(data)])
    assert res.exit_code == 0, res.output
    assert captured.get("ran") is True
    token = data / "engine_token"
    assert token.is_file() and len(token.read_text(encoding="utf-8").strip()) >= 32
    if os.name != "nt":
        assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert os.environ.get("TEAMTRAU_CACHE_DIR") == str(data / "cache")
    monkeypatch.delenv("TEAMTRAU_CACHE_DIR", raising=False)


def test_project_manager_import_does_not_pull_in_qt():
    code = (
        "import sys\n"
        "import src.ui.controllers.project_manager\n"
        "import src.modules.mcp_server\n"
        "print('QT' if any(m.startswith('PySide6') for m in sys.modules) else 'NOQT')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().endswith("NOQT")
