## HANDOFF P5.3
STATUS: DONE
BRANCH / COMMIT: feature/studio-engine feat(engine): PyInstaller lite sidecar build with pinned ffmpeg and smoke test
FILES CHANGED:
- .gitignore
- CHANGELOG.md
- docs/superpowers/handoffs/HANDOFF_P5.3.md
- main.py
- pyproject.toml
- release/__init__.py
- release/sidecar.py
- release/smoke_sidecar.py
- release/teamtrau-engine.spec
- release/teamtrau_engine.py
- src/dispatchers/resolve_dispatcher.py
- task.md
- tests/test_sidecar_p53.py
- uv.lock
RED EVIDENCE:
ImportError while importing test module 'E:\antigravity\TeamTrau-AutoEdit\tests\test_sidecar_p53.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
C:\Users\firef\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\Lib\importlib\__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests\test_sidecar_p53.py:16: in <module>
    from release.sidecar import (
E   ModuleNotFoundError: No module named 'release'
=========================== short test summary info ===========================
ERROR tests/test_sidecar_p53.py
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
============================== 1 error in 0.54s ===============================
GREEN EVIDENCE:
  task tests:    14 passed in 2.65s
  related suites:37 passed in 6.04s
  full suite:    1209 passed, 60 warnings in 576.29s (0:09:36)
  ruff:          All checks passed!
ACCEPTANCE EVIDENCE:
  uv.lock diff summary:
    Resolved 157 packages in 3.34s
    Added altgraph v0.17.5
    Added macholib v1.16.4
    Added pefile v2024.8.26
    Added pyinstaller v6.22.3
    Added pyinstaller-hooks-contrib v2026.9
    Added pywin32-ctypes v0.2.3
     uv.lock | 83 +++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
     1 file changed, 83 insertions(+)

  1. Build + first (cold) smoke run (`python -m release.sidecar build`):
    [sync] running uv sync into .venv-sidecar (12 excluded packages)...
    Resolved 157 packages in 2ms
    Checked 111 packages in 12ms
    [sync] completed in 0.2s
    [freeze] running PyInstaller...
    287 INFO: PyInstaller: 6.22.3, contrib hooks: 2026.9
    287 INFO: Python: 3.11.15
    321 INFO: Platform: Windows-10-10.0.26300-SP0
    321 INFO: Python environment: E:\antigravity\TeamTrau-AutoEdit\.venv-sidecar
    ...
    110041 INFO: Building COLLECT COLLECT-00.toc completed successfully.
    110066 INFO: Build complete! The results are available in: E:\antigravity\TeamTrau-AutoEdit\dist
    [freeze] completed in 110.9s
    [tools] reusing verified ffmpeg-n8.1.3-24-g92611900bb-win64-gpl-8.1.zip
    [tools] extracted ['ffmpeg.exe', 'ffprobe.exe', 'FFMPEG-LICENSE.txt'] -> E:\antigravity\TeamTrau-AutoEdit\dist\teamtrau-engine\tools
    [tools] completed in 2.1s
    [info] wrote E:\antigravity\TeamTrau-AutoEdit\dist\teamtrau-engine\BUILD_INFO.json (commit=fd685118)
    [smoke] running smoke_sidecar.py against teamtrau-engine.exe...
    [smoke] media generated in 1.15s
    [smoke] /v1/health ready in 6.12s (port 11693)
    [smoke] analyze_soundtrack completed in 49.40s (duration=10.0s, bpm=None)
    [smoke] proxies completed in 2.09s (c98358e97ef3093a.mp4, 2398165 bytes)
    [smoke] numba-cache verified (78 files in E:\antigravity\TeamTrau-AutoEdit\build\smoke-data\numba-cache)
    [smoke] passed in 59.3s

  2. Size of dist/teamtrau-engine and top 10 entries of _internal:
    PyInstaller sidecar bundle (_internal 444.5M + teamtrau-engine.exe 25.3M): 469.8M (target <= 520 MB)
    Bundled tools/ (ffmpeg.exe + ffprobe.exe + FFMPEG-LICENSE.txt): 314.1M
    Total dist/teamtrau-engine (with tools/): 783.9M (821,939,076 bytes)
    Top 10 entries of _internal:
      114.8M	llvmlite
      111.8M	cv2
       62.6M	av.libs
       49.7M	scipy
       20.0M	numpy.libs
       19.3M	scipy.libs
       11.8M	sklearn
        9.4M	cryptography
        7.6M	libcrypto-3-x64.dll
        5.9M	numpy

  3. Second (warm numba cache) smoke run & _internal *.nbi check:
    [smoke] media generated in 0.38s
    [smoke] /v1/health ready in 2.16s (port 26031)
    [smoke] analyze_soundtrack completed in 8.03s (duration=10.0s, bpm=112.3)
    [smoke] proxies completed in 2.30s (0a42bf80377871f9.mp4, 2398165 bytes)
    [smoke] numba-cache verified (78 files in E:\antigravity\TeamTrau-AutoEdit\build\smoke-data\numba-cache)
    Speedup: 49.40s -> 8.03s (6.15x faster, >= 3x target met)
    *.nbi files in _internal: 0 ([])

  4. Minimal PATH (`C:\Windows\System32`) + `--tools-dir dist/teamtrau-engine/tools` on port 8799:
    PATH used: C:\Windows\System32
    /v1/capabilities ffmpeg: E:\antigravity\TeamTrau-AutoEdit\dist\teamtrau-engine\tools\ffmpeg.EXE
    /v1/capabilities ffprobe: None
    Proxy job completed in 0.53s -> f0aa89b9fd32050e.mp4 (2398165 bytes)

  5. `git status` (confirming `build/`, `dist/`, `.venv-sidecar/` are ignored):
    On branch feature/studio-engine
    Your branch is up to date with 'origin/feature/studio-engine'.

    Changes not staged for commit:
      (use "git add <file>..." to update what will be committed)
      (use "git restore <file>..." to discard changes in working directory)
    	modified:   .gitignore
    	modified:   CHANGELOG.md
    	modified:   main.py
    	modified:   pyproject.toml
    	modified:   src/dispatchers/resolve_dispatcher.py
    	modified:   task.md
    	modified:   uv.lock

    Untracked files:
      (use "git add <file>..." to include in what will be committed)
    	release/
    	tests/test_sidecar_p53.py
DEVIATIONS FROM PLAN:
1. Added `module_collection_mode={"librosa": "py"}` and `excludes=["**/__pycache__"]` to `collect_data_files("librosa")` in `release/teamtrau-engine.spec` because Numba's `UserProvidedCacheLocator.from_function` checks `os.path.exists(py_file)` and returns `None` when `librosa` is compiled inside `PYZ-00.pyz` (falling back to `UserWideCacheLocator` in `%LOCALAPPDATA%\numba\Cache` and ignoring `NUMBA_CACHE_DIR`). Collecting `librosa` as `.py` files in `_internal/librosa` allows `UserProvidedCacheLocator` to honour `NUMBA_CACHE_DIR` (`<data>/numba-cache`) with zero `.nbi` files in `_internal`.
OPEN QUESTIONS / BLOCKERS: none
