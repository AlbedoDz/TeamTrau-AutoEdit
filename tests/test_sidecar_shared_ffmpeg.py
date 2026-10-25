"""Shared ffmpeg builds: DLLs next to the tools are extracted with their own names."""

import zipfile
from pathlib import Path

from release.sidecar import extract_tools, ffmpeg_assets


def test_windows_asset_is_the_shared_build_and_copies_its_dlls(tmp_path: Path):
    win = ffmpeg_assets("windows")[0]
    assert "-shared-" in win.url and win.libraries == "*.dll"
    archive = tmp_path / "w.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name in ("pkg/bin/ffmpeg.exe", "pkg/bin/ffprobe.exe", "pkg/bin/ffplay.exe",
                     "pkg/bin/avcodec-62.dll", "pkg/bin/avformat-62.dll", "pkg/LICENSE.txt",
                     "pkg/lib/avcodec.lib"):
            zf.writestr(name, name)
    out = extract_tools(archive, win, tmp_path / "tools")
    names = sorted(p.name for p in (tmp_path / "tools").iterdir())
    assert names == sorted(["ffmpeg.exe", "ffprobe.exe", "FFMPEG-LICENSE.txt",
                            "avcodec-62.dll", "avformat-62.dll"])
    assert sorted(p.name for p in out) == names


def test_static_builds_copy_no_libraries():
    assert all(not a.libraries for os_name in ("linux", "macos") for a in ffmpeg_assets(os_name))
