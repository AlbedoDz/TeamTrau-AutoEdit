"""PyInstaller lite sidecar builder, pinned ffmpeg downloader, and verification tool."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from src.core.platform_caps import OsName, detect_os

ROOT = Path(__file__).resolve().parents[1]

HEAVY_PACKAGES: tuple[str, ...] = (
    "torch",
    "torchvision",
    "ultralytics",
    "ultralytics-thop",
    "faster-whisper",
    "ctranslate2",
)

EXCLUDED_MODULES: tuple[str, ...] = (
    "torch",
    "torchvision",
    "ultralytics",
    "faster_whisper",
    "ctranslate2",
    "PySide6",
    "shiboken6",
    "polars",
    "matplotlib",
    "tkinter",
    "src.ui",
)


@dataclass(frozen=True)
class FfmpegAsset:
    """Pinned archive containing ffmpeg/ffprobe binaries and license files."""

    url: str
    sha256: str
    members: dict[str, str]  # archive basename -> file name in tools/


_BTBN_BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-24-13-02"

_FFMPEG_ASSETS: dict[OsName, list[FfmpegAsset]] = {
    "windows": [
        FfmpegAsset(
            url=f"{_BTBN_BASE}/ffmpeg-n8.1.3-24-g92611900bb-win64-gpl-8.1.zip",
            sha256="0b1a903367bfbe906125aad78069e3b9dfecac74ea76db05ada2e667c80ac2c9",
            members={
                "ffmpeg.exe": "ffmpeg.exe",
                "ffprobe.exe": "ffprobe.exe",
                "LICENSE.txt": "FFMPEG-LICENSE.txt",
            },
        ),
    ],
    "linux": [
        FfmpegAsset(
            url=f"{_BTBN_BASE}/ffmpeg-n8.1.3-24-g92611900bb-linux64-gpl-8.1.tar.xz",
            sha256="3c2d43d951c1d7936240ec77dbc274d6214fc64973e9993f63e16f03ac908ff1",
            members={
                "ffmpeg": "ffmpeg",
                "ffprobe": "ffprobe",
                "LICENSE.txt": "FFMPEG-LICENSE.txt",
            },
        ),
    ],
    "macos": [
        FfmpegAsset(
            url="https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffmpeg.zip",
            sha256="c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924",
            members={"ffmpeg": "ffmpeg"},
        ),
        FfmpegAsset(
            url="https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffprobe.zip",
            sha256="fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6",
            members={"ffprobe": "ffprobe"},
        ),
    ],
}


def lite_excluded_packages(lock: dict[str, Any]) -> list[str]:
    """Return sorted list of heavy ML and Qt packages present in uv.lock to exclude."""
    heavy = set(HEAVY_PACKAGES)
    excluded: set[str] = set()
    for pkg in lock.get("package", []):
        name = str(pkg.get("name", ""))
        lower = name.lower()
        if name in heavy or lower.startswith("pyside6") or lower.startswith("shiboken6"):
            excluded.add(name)
    return sorted(excluded)


def sync_command(lock: dict[str, Any]) -> list[str]:
    """Build the uv sync command for the lite sidecar virtual environment."""
    cmd = ["uv", "sync", "--locked", "--no-dev", "--group", "sidecar", "--python", "3.11"]
    for pkg in lite_excluded_packages(lock):
        cmd.extend(["--no-install-package", pkg])
    return cmd


def ffmpeg_assets(os_name: OsName) -> list[FfmpegAsset]:
    """Return pinned ffmpeg assets for the given operating system."""
    if os_name not in _FFMPEG_ASSETS:
        raise ValueError(f"Unsupported OS for ffmpeg assets: {os_name}")
    return list(_FFMPEG_ASSETS[os_name])


def tool_names(os_name: OsName) -> tuple[str, str]:
    """Return (ffmpeg, ffprobe) binary names for the target OS."""
    if os_name == "windows":
        return ("ffmpeg.exe", "ffprobe.exe")
    return ("ffmpeg", "ffprobe")


def verify_sha256(path: Path, expected: str) -> None:
    """Verify SHA-256 digest of a file, raising ValueError on mismatch."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    actual = h.hexdigest().lower()
    if actual != expected.lower():
        raise ValueError(f"SHA-256 mismatch for {path.name}: expected {expected}, got {actual}")


def extract_tools(archive: Path, asset: FfmpegAsset, tools_dir: Path) -> list[Path]:
    """Extract mapped members from a .zip or .tar.xz archive into tools_dir."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    found: set[str] = set()

    if archive.name.lower().endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = PurePosixPath(info.filename.replace("\\", "/")).name
                if base in asset.members:
                    dest = tools_dir / asset.members[base]
                    with zf.open(info, "r") as src, dest.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if os.name != "nt":
                        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    found.add(base)
    else:
        with tarfile.open(archive, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                base = PurePosixPath(member.name.replace("\\", "/")).name
                if base in asset.members:
                    src_f = tf.extractfile(member)
                    if src_f is None:
                        continue
                    dest = tools_dir / asset.members[base]
                    with src_f, dest.open("wb") as dst:
                        shutil.copyfileobj(src_f, dst)
                    if os.name != "nt":
                        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    found.add(base)

    missing = sorted(set(asset.members.keys()) - found)
    if missing:
        raise ValueError(f"Archive {archive.name} is missing required member(s): {', '.join(missing)}")

    return [tools_dir / asset.members[k] for k in asset.members]


def _download_asset(asset: FfmpegAsset, downloads_dir: Path, timeout_sec: float = 300.0) -> Path:
    """Download an asset into downloads_dir or reuse an existing verified copy."""
    downloads_dir.mkdir(parents=True, exist_ok=True)
    url_name = PurePosixPath(urllib.parse.urlparse(asset.url).path).name
    archive_path = downloads_dir / url_name

    if archive_path.is_file():
        try:
            verify_sha256(archive_path, asset.sha256)
            print(f"[tools] reusing verified {archive_path.name}", flush=True)
            return archive_path
        except ValueError:
            archive_path.unlink(missing_ok=True)

    print(f"[tools] downloading {asset.url} ...", flush=True)
    part_path = archive_path.with_suffix(archive_path.suffix + ".part")
    req = urllib.request.Request(asset.url, headers={"User-Agent": "TeamTrau-Sidecar-Builder/1.0"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp, part_path.open("wb") as out_f:
        while True:
            if time.perf_counter() - t0 > timeout_sec:
                raise TimeoutError(f"Download timed out after {timeout_sec:.0f}s: {asset.url}")
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            out_f.write(chunk)
    part_path.replace(archive_path)
    verify_sha256(archive_path, asset.sha256)
    print(f"[tools] verified SHA-256 for {archive_path.name} ({time.perf_counter() - t0:.1f}s)", flush=True)
    return archive_path


def step_sync(root: Path) -> None:
    """Step 1: sync the lite sidecar environment into .venv-sidecar."""
    t0 = time.perf_counter()
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    cmd = sync_command(lock)
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(root / ".venv-sidecar")}
    print(f"[sync] running uv sync into .venv-sidecar ({len(lite_excluded_packages(lock))} excluded packages)...", flush=True)
    res = subprocess.run(cmd, cwd=root, env=env, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(f"uv sync failed with exit code {res.returncode}")
    print(f"[sync] completed in {time.perf_counter() - t0:.1f}s", flush=True)


def step_freeze(root: Path, os_name: OsName) -> None:
    """Step 2: freeze the lite engine with PyInstaller in onedir mode."""
    t0 = time.perf_counter()
    if os_name == "windows":
        pyinstaller_bin = root / ".venv-sidecar" / "Scripts" / "pyinstaller.exe"
    else:
        pyinstaller_bin = root / ".venv-sidecar" / "bin" / "pyinstaller"
    if not pyinstaller_bin.is_file():
        raise RuntimeError(f"PyInstaller binary not found in sidecar venv: {pyinstaller_bin}")

    cmd = [
        str(pyinstaller_bin),
        "--noconfirm",
        "--distpath",
        str(root / "dist"),
        "--workpath",
        str(root / "build" / "pyinstaller"),
        str(root / "release" / "teamtrau-engine.spec"),
    ]
    print("[freeze] running PyInstaller...", flush=True)
    res = subprocess.run(cmd, cwd=root, timeout=900)
    if res.returncode != 0:
        raise RuntimeError(f"PyInstaller failed with exit code {res.returncode}")
    print(f"[freeze] completed in {time.perf_counter() - t0:.1f}s", flush=True)


def step_tools(root: Path, os_name: OsName) -> list[str]:
    """Step 3: download, verify, and extract pinned ffmpeg/ffprobe binaries."""
    t0 = time.perf_counter()
    downloads_dir = root / "build" / "downloads"
    tools_dir = root / "dist" / "teamtrau-engine" / "tools"
    assets = ffmpeg_assets(os_name)
    for asset in assets:
        archive = _download_asset(asset, downloads_dir)
        extracted = extract_tools(archive, asset, tools_dir)
        print(f"[tools] extracted {[p.name for p in extracted]} -> {tools_dir}", flush=True)
    print(f"[tools] completed in {time.perf_counter() - t0:.1f}s", flush=True)
    return [a.url for a in assets]


def step_info(root: Path, os_name: OsName) -> Path:
    """Step 4: write BUILD_INFO.json into dist/teamtrau-engine/."""
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    commit = proc.stdout.strip() if proc.returncode == 0 else "unknown"
    info = {
        "git_commit": commit,
        "build_date": datetime.now(timezone.utc).isoformat(),
        "os": os_name,
        "ffmpeg_assets": [a.url for a in ffmpeg_assets(os_name)],
    }
    info_path = root / "dist" / "teamtrau-engine" / "BUILD_INFO.json"
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(f"[info] wrote {info_path} (commit={commit[:8]})", flush=True)
    return info_path


def step_smoke(root: Path, os_name: OsName) -> None:
    """Step 5: run smoke_sidecar.py against the freshly built executable."""
    t0 = time.perf_counter()
    exe_name = "teamtrau-engine.exe" if os_name == "windows" else "teamtrau-engine"
    exe_path = root / "dist" / "teamtrau-engine" / exe_name
    smoke_data = root / "build" / "smoke-data"
    shutil.rmtree(smoke_data, ignore_errors=True)
    cmd = [
        sys.executable,
        str(root / "release" / "smoke_sidecar.py"),
        str(exe_path),
        "--data-dir",
        str(smoke_data),
    ]
    print(f"[smoke] running smoke_sidecar.py against {exe_path.name}...", flush=True)
    res = subprocess.run(cmd, cwd=root, timeout=300)
    if res.returncode != 0:
        raise RuntimeError(f"Smoke test failed with exit code {res.returncode}")
    print(f"[smoke] passed in {time.perf_counter() - t0:.1f}s", flush=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: python -m release.sidecar build [--skip-sync] [--skip-smoke]."""
    parser = argparse.ArgumentParser(description="Build the TeamTrau lite PyInstaller sidecar.")
    sub = parser.add_subparsers(dest="command", required=True)
    build_p = sub.add_parser("build", help="Sync lite env, freeze, bundle ffmpeg, and smoke test.")
    build_p.add_argument("--skip-sync", action="store_true", help="Skip uv sync step.")
    build_p.add_argument("--skip-smoke", action="store_true", help="Skip post-build smoke test.")
    args = parser.parse_args(argv)

    try:
        os_name = detect_os()
        if args.command == "build":
            if not args.skip_sync:
                step_sync(ROOT)
            step_freeze(ROOT, os_name)
            step_tools(ROOT, os_name)
            step_info(ROOT, os_name)
            if not args.skip_smoke:
                step_smoke(ROOT, os_name)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
