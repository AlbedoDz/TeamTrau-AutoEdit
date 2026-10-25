"""P5.3: PyInstaller "lite" engine sidecar (lite env, pinned ffmpeg, tool extraction, frozen runtime)."""

import hashlib
import io
import os
import stat
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from release.sidecar import (
    EXCLUDED_MODULES,
    extract_tools,
    ffmpeg_assets,
    lite_excluded_packages,
    sync_command,
    tool_names,
    verify_sha256,
)

ROOT = Path(__file__).resolve().parents[1]
LOCK = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))


def test_lite_env_drops_gpu_ml_and_qt_but_keeps_the_engine_core():
    excluded = lite_excluded_packages(LOCK)
    for name in ("torch", "torchvision", "ultralytics", "faster-whisper", "ctranslate2",
                 "pyside6", "pyside6-essentials", "pyside6-addons", "shiboken6"):
        assert name in excluded, name
    for name in ("librosa", "numba", "opencv-python", "scipy", "uvicorn", "starlette", "mcp",
                 "opentimelineio", "click"):
        assert name not in excluded, name
    lock_names = {p["name"] for p in LOCK["package"]}
    assert set(excluded) <= lock_names
    assert excluded == sorted(excluded)


def test_sync_command_installs_the_locked_lite_env_on_python_311():
    cmd = sync_command(LOCK)
    assert cmd[:8] == ["uv", "sync", "--locked", "--no-dev", "--group", "sidecar", "--python", "3.11"]
    rest = cmd[8:]
    assert rest[0::2] == ["--no-install-package"] * (len(rest) // 2)
    assert rest[1::2] == lite_excluded_packages(LOCK)


def test_pyinstaller_excludes_heavy_modules_only():
    for mod in ("torch", "torchvision", "ultralytics", "faster_whisper", "ctranslate2", "PySide6",
                "polars", "matplotlib", "tkinter", "src.ui"):
        assert mod in EXCLUDED_MODULES, mod
    for mod in ("librosa", "sklearn", "numba", "cv2", "scipy", "src.core", "src.engine_api"):
        assert mod not in EXCLUDED_MODULES, mod


def test_sidecar_group_declares_pyinstaller():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    group = pyproject["dependency-groups"]["sidecar"]
    assert any(req.replace(" ", "").lower().startswith("pyinstaller>=") for req in group)


@pytest.mark.parametrize("os_name", ["windows", "linux", "macos"])
def test_ffmpeg_assets_are_pinned_with_sha256(os_name):
    assets = ffmpeg_assets(os_name)
    assert assets
    dests = set()
    for asset in assets:
        assert asset.url.startswith("https://") and "latest" not in asset.url, asset.url
        assert len(asset.sha256) == 64 and int(asset.sha256, 16) >= 0
        dests |= set(asset.members.values())
    assert set(tool_names(os_name)) <= dests


def test_tool_names_per_os():
    assert tool_names("windows") == ("ffmpeg.exe", "ffprobe.exe")
    assert tool_names("linux") == ("ffmpeg", "ffprobe")
    assert tool_names("macos") == ("ffmpeg", "ffprobe")


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def _tar_xz(path: Path, entries: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:xz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return path


def test_extract_tools_from_zip_and_tar(tmp_path: Path):
    win = ffmpeg_assets("windows")[0]
    archive = _zip(tmp_path / "w.zip", {
        "pkg/bin/ffmpeg.exe": b"MZ-ffmpeg", "pkg/bin/ffprobe.exe": b"MZ-ffprobe",
        "pkg/bin/ffplay.exe": b"MZ-ffplay", "pkg/LICENSE.txt": b"GPL", "pkg/doc/x.html": b"",
    })
    out = extract_tools(archive, win, tmp_path / "tools-win")
    names = sorted(p.name for p in (tmp_path / "tools-win").iterdir())
    assert names == sorted(win.members.values())
    assert sorted(p.name for p in out) == names
    assert (tmp_path / "tools-win" / "ffmpeg.exe").read_bytes() == b"MZ-ffmpeg"

    lin = ffmpeg_assets("linux")[0]
    archive = _tar_xz(tmp_path / "l.tar.xz", {
        "pkg/bin/ffmpeg": b"ELF-ffmpeg", "pkg/bin/ffprobe": b"ELF-ffprobe",
        "pkg/bin/ffplay": b"ELF-ffplay", "pkg/LICENSE.txt": b"GPL",
    })
    extract_tools(archive, lin, tmp_path / "tools-linux")
    ffmpeg = tmp_path / "tools-linux" / "ffmpeg"
    assert ffmpeg.read_bytes() == b"ELF-ffmpeg"
    if os.name != "nt":
        assert ffmpeg.stat().st_mode & stat.S_IXUSR


def test_extract_tools_rejects_an_archive_missing_a_tool(tmp_path: Path):
    win = ffmpeg_assets("windows")[0]
    archive = _zip(tmp_path / "w.zip", {"pkg/bin/ffmpeg.exe": b"x", "pkg/LICENSE.txt": b"GPL"})
    with pytest.raises(ValueError):
        extract_tools(archive, win, tmp_path / "tools")


def test_verify_sha256(tmp_path: Path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    verify_sha256(f, hashlib.sha256(b"abc").hexdigest())
    with pytest.raises(ValueError):
        verify_sha256(f, "0" * 64)


def test_serve_points_numba_cache_into_the_data_dir(tmp_path: Path, monkeypatch):
    import uvicorn

    import main

    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: None)
    monkeypatch.delenv("TEAMTRAU_CACHE_DIR", raising=False)
    monkeypatch.delenv("NUMBA_CACHE_DIR", raising=False)
    data = tmp_path / "data"
    res = CliRunner().invoke(main.cli, ["serve", "--port", "8798", "--data-dir", str(data)])
    assert res.exit_code == 0, res.output
    assert os.environ.get("NUMBA_CACHE_DIR") == str(data / "numba-cache")
    assert (data / "numba-cache").is_dir()

    monkeypatch.setenv("NUMBA_CACHE_DIR", str(tmp_path / "mine"))
    res = CliRunner().invoke(main.cli, ["serve", "--port", "8798", "--data-dir", str(data)])
    assert res.exit_code == 0, res.output
    assert os.environ["NUMBA_CACHE_DIR"] == str(tmp_path / "mine")
    monkeypatch.delenv("TEAMTRAU_CACHE_DIR", raising=False)


def test_frozen_engine_never_runs_the_resolve_worker_with_itself(monkeypatch):
    from src.dispatchers.resolve_dispatcher import ResolveDispatcher

    monkeypatch.delenv("AUTOEDIT_RESOLVE_PYTHON", raising=False)
    assert ResolveDispatcher._worker_interpreters()[-1] == [sys.executable]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert [sys.executable] not in ResolveDispatcher._worker_interpreters()


def test_spec_uses_the_shared_exclusions_and_ships_the_resolve_worker_source():
    spec = (ROOT / "release" / "teamtrau-engine.spec").read_text(encoding="utf-8")
    assert "EXCLUDED_MODULES" in spec
    assert "resolve_worker.py" in spec
    assert "teamtrau_engine.py" in spec
