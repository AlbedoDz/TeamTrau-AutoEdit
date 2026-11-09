# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release.sidecar import EXCLUDED_MODULES  # noqa: E402

worker_sources = [
    (str(ROOT / "src" / "dispatchers" / "resolve_worker.py"), "src/dispatchers"),
    (str(ROOT / "src" / "core" / "platform_caps.py"), "src/core"),
    (str(ROOT / "src" / "core" / "platform_paths.py"), "src/core"),
    (str(ROOT / "src" / "core" / "ffmpeg_utils.py"), "src/core"),
    (str(ROOT / "src" / "core" / "__init__.py"), "src/core"),
]
# YuNet face detector model and its MIT license, read by src.modules.media_classifier.
model_assets = [
    (str(ROOT / "src" / "modules" / "assets" / "face_detection_yunet_2023mar.onnx"), "src/modules/assets"),
    (str(ROOT / "src" / "modules" / "assets" / "face_detection_yunet_LICENSE.txt"), "src/modules/assets"),
]

datas = collect_data_files("librosa", excludes=["**/__pycache__"]) + worker_sources + model_assets
hiddenimports = collect_submodules("src", filter=lambda n: not n.startswith("src.ui"))

a = Analysis(
    [str(ROOT / "release" / "teamtrau_engine.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(EXCLUDED_MODULES),
    noarchive=False,
    optimize=0,
    module_collection_mode={"librosa": "py"},
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="teamtrau-engine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="teamtrau-engine",
)
