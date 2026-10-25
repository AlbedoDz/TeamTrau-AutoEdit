# TeamTrau Studio v3 — Phase 5 (cross-platform release), rolling-wave plan

> **Parent plan:** `2026-10-24-teamtrau-studio-v3.md`, Phase 5.
> **Status before Phase 5:**
> - Phase 3 is done and gate G3 is signed (2026-10-25).
> - The PySide6 GUI is frozen.
> - Windows is the only OS verified so far.
>
> **Rolling wave:** only the next task carries locked tests. Each locked test is validated against a reference implementation before it is sent.

## Facts (two read-only audits, 2026-10-25)

**Engine**
- **Paths depend on the working directory:**
  - `PipelineConfig.cache_dir = ".cache/autoedit"`.
  - `serve` writes the token and `ai_settings.json` there.
  - The token is written without POSIX `0600` permissions.
  - Inside a macOS `.app` the working directory can be read-only.
- **ffmpeg/ffprobe** are found only through `shutil.which` (PATH). There is no override, and the sidecar must ship ffmpeg.
- **Resolve:** the defaults in `config.py:171-200` are Windows-only, and `platform_caps.resolve_paths()` is never used. `fusionscript.dll` is hard-coded. The worker `.py` is launched with `sys.executable`, which breaks when frozen.
- **Renderer:** the master/preview renderer only uses NVENC or libx264/libx265. The `platform_caps` encoder ladder (videotoolbox/qsv/amf/vaapi) is used only by proxies.
- **Qt leak:**
  - Importing `src.ui.controllers.project_manager` (MCP project tools) runs a package `__init__` that imports PySide6.
  - `uvicorn` and `starlette` are not declared in `pyproject` (they only arrive through `mcp`).
  - `pytest` and `ruff` are runtime dependencies.
- **Tests:**
  - 1184 tests; 105 integration tests skip without ffmpeg; no test needs a real GPU.
  - `test_mcp_comprehensive.py:104` asserts `python.exe`.
  - 23 files import `src.ui`, which needs Qt offscreen on Linux.
  - There is no CI and no `.github/`.
- **Size:** torch+CUDA is about 4 GB. A "lite" sidecar without torch/ultralytics/faster-whisper is feasible because those imports are lazy and fail soft.

**Studio**
- **Electron engine launch:** venv only (`.venv/Scripts/python.exe` or `bin/python`). Packaged builds need an `app.isPackaged` branch that runs `resources/engine/teamtrau-engine(.exe)` with a writable working directory.
- **Shutdown:** a hard kill only, with no graceful shutdown.
- **macOS:** there is no `app.on('activate')`.
- **GPU preference:** defaults to high-performance on every OS. It should be `auto` off Windows.
- **Random origin:** the app is served on `127.0.0.1:<random port>`, so the origin changes on every launch. IndexedDB, OPFS and persisted File System Access handles are keyed by origin and may not survive a restart. This needs to be verified, then fixed with a fixed origin (custom protocol or fixed port).
- **Packaging:** there is no electron-builder config. `dist` is 192 MB, including source maps and onnxruntime WASM.
- **CI:** the upstream `ci.yml` runs on Linux only, triggers on `main`, and never runs `test:electron`.
- **Linux:** HEVC export and WebGPU are likely unavailable; fallbacks are needed.
- **Signing (owner credentials):** a Windows Authenticode certificate, an Apple Developer ID plus notarisation, and a publish token. Unsigned builds work in CI.

## Task list

| Task | Repo | What | Detailed |
|---|---|---|---|
| **P5.0** | Engine | Portability core: OS data dir + `serve --data-dir/--tools-dir`, `TEAMTRAU_CACHE_DIR`, `0600` token, Resolve paths via `platform_caps`, fusionscript name, no Qt on import, declared deps, portable test fix | **yes (below)** |
| P5.1 | Engine | CI matrix (GitHub Actions: ubuntu/windows/macos): CPU torch, ffmpeg per OS, `gui` marker deselected, ruff + pytest | next |
| P5.2 | Studio | Shell portability: fixed origin (verify the persistence bug first), packaged-engine launch branch, graceful shutdown, engine-missing UI, macOS activate, GPU auto off Windows; CI: push on `teamtrau/main` plus a `test:electron` OS matrix | later |
| P5.3 | Engine | PyInstaller **onedir** "lite" sidecar per OS + bundled ffmpeg (licence-checked) + smoke test (start binary → `/v1/health` → a proxy job) | later |
| P5.4 | Studio | electron-builder config (NSIS / dmg+zip / AppImage+deb), `extraResources` engine, exclude source maps, tag-triggered packaging workflow, unsigned artifacts | later |
| P5.5 | Engine | Renderer encoder ladder from `platform_caps` (VideoToolbox / QSV / AMF / VAAPI) with tests | later |
| G5 | Owner | Signing credentials (Authenticode, Apple Developer ID + notarisation) and the release channel | — |

Global rules: R1–R15. Commit only your own files. Never push with `--no-verify`. Never print secrets.

---

## P5.0 — Engine portability core (Engine repo, branch `feature/studio-engine`)

**API**
```python
# src/core/platform_paths.py (new, pure)
def default_data_dir(os_name: OsName, env: Mapping[str, str], home: Path) -> Path
def resolve_data_dir(cli_value: str | None, env: Mapping[str, str], os_name: OsName, home: Path) -> Path
def prepend_tool_dir(tool_dir: Path, env: MutableMapping[str, str]) -> None
def fusionscript_name(os_name: OsName) -> str
```

**Rules**
- **`default_data_dir`:** `<base>/TeamTrau/engine`, where `<base>` is:
  - Windows: `%LOCALAPPDATA%`, or `home/AppData/Local`;
  - macOS: `home/Library/Application Support`;
  - Linux: `$XDG_DATA_HOME`, or `home/.local/share`.
- **`resolve_data_dir` precedence:** CLI value, then `TEAMTRAU_DATA_DIR`, then the default.
- **`prepend_tool_dir`:** puts the directory first on `PATH` without duplicates. Every existing `shutil.which("ffmpeg")` then finds the bundled binary.
- **`PipelineConfig.cache_dir`:** the default becomes `Path(os.environ["TEAMTRAU_CACHE_DIR"])` when that variable is set, otherwise `Path(".cache/autoedit")` (a `default_factory`). Every other default is unchanged.
- **`main.py serve`** gains `--data-dir` and `--tools-dir`:
  - It resolves the data dir with `detect_os()` and `Path.home()`, then creates `<data>/cache`.
  - It sets `os.environ["TEAMTRAU_CACHE_DIR"] = <data>/cache` before building the app.
  - It prepends `--tools-dir` to PATH.
- **Token and settings files:**
  - Without `--token-file`, write `<data>/engine_token` with `secrets.token_urlsafe(32)`, created with mode `0o600` (`os.open` with that mode, then `chmod` on POSIX).
  - `ai_settings.json` lives in `<data>`.
- **Resolve defaults:**
  - `find_default_resolve_script_path` and `find_default_resolve_exe` use `platform_caps.resolve_paths(detect_os())`. On Windows the values must be the same as today.
  - `resolve_dispatcher` / `resolve_worker` use `fusionscript_name(detect_os())` instead of the literal `fusionscript.dll`.
- **Qt leak:** `src/ui/controllers/__init__.py` must not import Qt modules at import time. Use PEP 562 lazy re-exports (`__getattr__`) so `from src.ui.controllers import ShortcutManager` keeps working.
- **`pyproject.toml`:**
  - Declare `uvicorn` and `starlette` as runtime dependencies, at the versions already in `uv.lock`; re-lock with `uv lock` and change no other package version.
  - Move `pytest` and `ruff` to a `[dependency-groups] dev` group.
  - `uv sync` must still install them for development.
- **Test fix:** make `tests/test_mcp_comprehensive.py:104` portable: assert `Path(cmd).name` starts with `python`. This edit is allowed.

**Files**
- `src/core/platform_paths.py` (new)
- `src/core/config.py`
- `src/dispatchers/resolve_dispatcher.py`
- `src/dispatchers/resolve_worker.py`
- `src/ui/controllers/__init__.py`
- `main.py`
- `pyproject.toml` + `uv.lock`
- `tests/test_mcp_comprehensive.py` (the one line)
- `tests/test_portability_p50.py` (LOCKED)

**Related suites**
- `tests/test_platform_caps.py`
- `tests/test_resolve_dispatcher.py`
- `tests/test_engine_api.py`
- `tests/test_phase1_polish.py`
- `tests/test_mcp_comprehensive.py`
- the GUI suites that import `src.ui.controllers`
- the full suite

**Contract test — `tests/test_portability_p50.py`:**
```python
"""P5.0: engine portability core (data dir, ffmpeg override, token perms, no Qt leak, Resolve paths)."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
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
```
**Expected red:** `ModuleNotFoundError: No module named 'src.core.platform_paths'`.

**Commit:** `feat(engine): portability core (OS data dir, tools dir, private token, Resolve paths, Qt-free imports)`.
