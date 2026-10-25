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
| P5.1 | Engine | CI (`.github/workflows/engine-ci.yml`, contract `tests/test_engine_ci_p51.py`): Linux on every push, Windows + macOS on PRs to main and manual runs (owner choice: Actions minutes); CPU torch at the locked version; ffmpeg per OS; Qt offscreen (GUI tests run, no marker). Written by the architect; failures found by the first runs become P5.1b | done (architect) |
| **P5.2** | Studio | (done, `3231dea7`) Shell portability: fixed `app://teamtrau` origin (persistence bug confirmed), packaged-engine launch branch, engine-missing warning, macOS activate, GPU auto off Windows; CI: push on `teamtrau/main` plus an `electron-shell` OS matrix | **yes (below)** |
| **P5.3** | Engine | (done, `f83cc58`; shared ffmpeg `c4c65fd`) PyInstaller **onedir** "lite" sidecar (spike: 487 MB, healthy in 2 s) + pinned ffmpeg with SHA-256 + numba cache in the data dir + smoke test | **yes (below)** |
| **P5.4** | Studio | (done, `fc8ae30b`) Local Windows NSIS installer (electron-builder, sidecar as `extraResources`, no source maps). Owner: CI packaging (dmg/AppImage, cross-repo token) later | **yes (below)** |
| **P5.5** | Engine | Renderer hardware encoder ladder from `platform_caps` (VideoToolbox / QSV / AMF; VAAPI later), `hw_encoder` setting, CPU fallback for every family | **yes (below)** |
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

---

## P5.2 — Studio shell portability (Studio repo, branch `teamtrau/main`)

**Why first among the Studio tasks:** the random-origin bug is confirmed.
- **Probe 1** (`http://127.0.0.1:<port>`, two ports): localStorage and IndexedDB written at port A are gone at port B. Each launch picks a new port, so projects in IndexedDB/OPFS and persisted folder handles are lost on every restart.
- **Probe 2** (privileged `app://teamtrau`, two separate launches with the same userData): localStorage and IndexedDB survive. `crossOriginIsolated`, `SharedArrayBuffer`, OPFS, `showDirectoryPicker`, WebCodecs and WebGPU are all available.
- **Probe 3** (the reference handler inside Electron 42): a renderer `fetch` POST with a body reaches the engine with the Bearer token; an SSE response streams (first chunk after 7 ms, before the stream ends); Range returns 206.

**API**
```js
// electron/app-protocol.cjs (new; replaces electron/static-server.cjs)
const APP_ORIGIN = 'app://teamtrau'
const APP_SCHEME_PRIVILEGES = { scheme: 'app', privileges: { standard, secure, supportFetchAPI, stream, corsEnabled, codeCache: true } }
function createAppProtocolHandler({ distDir, getEngine, fetchImpl = fetch }) // => async (Request) => Response

// electron/engine-launch.cjs (new)
function resolveEngineLaunch({ argv, env, platform, isPackaged, resourcesPath, userDataDir, devEngineDir, exists })
// => { mode: 'packaged' | 'venv', command, cwd, args(port, tokenFile) } | { mode: 'missing', error }

// electron/gpu-preference.cjs
function resolveGpuPreference(argv = [], env = {}, platform = process.platform)
```

**Rules**
- **`app-protocol.cjs`** keeps every behaviour of `static-server.cjs`:
  - COOP/COEP/CORP headers on every static and error response; SPA fallback to `index.html`; `/sw.js` 404; traversal guard (403) and NUL/bad encoding (400); MIME table; Range (206, 416, `Accept-Ranges`); `HEAD` with headers and no body.
  - Requests for any host other than `teamtrau` return 404.
  - `/engine` and `/engine/*` proxy to `http://127.0.0.1:<port>` with the prefix stripped and the query kept. Any incoming `Authorization` is replaced by `Bearer <token>`. The request body is forwarded (`duplex: 'half'`) and the response body is **streamed**, never buffered. Drop `content-encoding`/`content-length` from the proxied response. No engine: 503 `{"error":"engine not running"}`. Connection failure: 502 `{"error":"engine connection failed"}`.
  - Move the shared helpers (MIME, `parseRange`, path checks) into `app-protocol.cjs`, then **delete** `static-server.cjs` and `__tests__/static-server.test.cjs`; the new locked test covers the same behaviours. Nothing else may still import `static-server.cjs`.
- **`main.cjs`**:
  - Call `protocol.registerSchemesAsPrivileged([APP_SCHEME_PRIVILEGES])` at module top level (before `app.whenReady`). In `whenReady`, `protocol.handle('app', createAppProtocolHandler({ distDir: DIST_DIR, getEngine }))`, then `win.loadURL(\`${APP_ORIGIN}/${query}\`)`.
  - Remove the static HTTP server and the `onHeadersReceived` COOP/COEP hook (the handler sets the headers).
  - Offline filtering: requests to `app://teamtrau` must be allowed (extend `isAllowedRequest`; keep loopback allowed).
  - Engine launch: replace `getEngineDir`/`getEngineExecutables` with `resolveEngineLaunch({ argv: process.argv, env: process.env, platform: process.platform, isPackaged: app.isPackaged, resourcesPath: process.resourcesPath, userDataDir: app.getPath('userData'), devEngineDir: path.resolve(__dirname, '..', '..', 'TeamTrau-AutoEdit'), exists: fs.existsSync })`, then `startEngine({ command, args, cwd, tokenDir })`.
  - When the result is `missing`, or `startEngine` fails, log it and show one non-blocking `dialog.showMessageBox` warning (type `warning`) after the window loads: the Studio still opens (FreeCut editing works), and the TeamTrau tabs already show "engine offline".
  - `resolveGpuPreference(process.argv, process.env, process.platform)`.
  - macOS: `app.on('activate')` recreates the main window when none is open (the server/protocol stays registered).
- **CI** (`.github/workflows/ci.yml`):
  - Add `teamtrau/main` to the `push.branches` list.
  - New job `electron-shell` with `strategy.matrix.os: [ubuntu-latest, windows-latest, macos-latest]`: checkout, Node 22, `node --test "electron/__tests__/*.test.cjs"` (no install needed: these tests use only Node built-ins; check this, and use the same setup as `quality` if they do not).
  - Do not change the other jobs.

**Files**
- `electron/app-protocol.cjs` (new), `electron/engine-launch.cjs` (new)
- `electron/main.cjs`, `electron/gpu-preference.cjs`
- `electron/static-server.cjs` and `electron/__tests__/static-server.test.cjs` (deleted)
- `.github/workflows/ci.yml`
- `electron/__tests__/app-protocol.test.cjs` (LOCKED, new)
- `electron/__tests__/engine-launch.test.cjs` (LOCKED, new)
- `electron/__tests__/gpu-preference.test.cjs` (LOCKED, replaced: every call now passes the platform, so it also passes on Linux/macOS CI)

**Validation:** the three locked files pass (16 tests) against a reference implementation, and the reference handler was exercised inside Electron 42 (probe 3).
**Expected red:** `Cannot find module '../app-protocol.cjs'`, `Cannot find module '../engine-launch.cjs'`, and `resolveGpuPreference([], {}, 'darwin')` returning `'high-performance'`.

**Acceptance (real app, real workspace, evidence in the HANDOFF)**
1. `npm run test:electron` output (all files), `vp check`, `vp test run` for the TeamTrau suites, `vp build`.
2. Launch the Electron app twice (quit fully in between) on the same real workspace folder used for the P3/P4 acceptance (native handle, no OPFS copy). In the first run open or create a project; in the second run the project list still shows it and opens without asking for the folder again. Screenshot both runs and report `location.origin` (must be `app://teamtrau`) and `crossOriginIsolated` (must be `true`) from the DevTools console or `executeJavaScript`.
3. In the second run: run one engine job (Analyze soundtrack or Classify) and the Agent tab with one prompt, to prove the proxy works for JSON and for streaming. Play the timeline for 10 s (video through Range/blob paths still works).
4. Start once with `--offline` and once with `--engine-dir=<nonexistent>`: the first works normally, the second shows the warning dialog and the editor still opens. Screenshot the dialog.

**Commit:** `feat(teamtrau): fixed app:// origin, packaged-engine launch, platform GPU default, shell CI matrix`.

**Contract test — `electron/__tests__/app-protocol.test.cjs`:**
```js
// P5.2 app:// protocol: one fixed origin (app://teamtrau) so IndexedDB, OPFS and localStorage survive restarts.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const http = require('node:http')

const { APP_ORIGIN, APP_SCHEME_PRIVILEGES, createAppProtocolHandler } = require('../app-protocol.cjs')

function makeDist() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tt-app-'))
  const dist = path.join(root, 'dist')
  fs.mkdirSync(dist)
  fs.writeFileSync(path.join(dist, 'index.html'), '<!doctype html><html><body>app</body></html>')
  fs.writeFileSync(path.join(dist, 'clip.bin'), Buffer.alloc(1000, 7))
  fs.writeFileSync(path.join(dist, 'ort.wasm'), Buffer.from([0, 97, 115, 109]))
  fs.mkdirSync(path.join(root, 'dist-evil'))
  fs.writeFileSync(path.join(root, 'dist-evil', 'secret.txt'), 'secret')
  return dist
}

function assertIsolation(res) {
  assert.equal(res.headers.get('cross-origin-opener-policy'), 'same-origin')
  assert.equal(res.headers.get('cross-origin-embedder-policy'), 'require-corp')
  assert.equal(res.headers.get('cross-origin-resource-policy'), 'same-origin')
}

function fakeEngine() {
  const seen = []
  let release
  const released = new Promise((r) => (release = r))
  const server = http.createServer((req, res) => {
    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', async () => {
      seen.push({ method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString() })
      if (req.url === '/v1/stream') {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' })
        res.write('data: first\n\n')
        await released
        res.end('data: second\n\n')
        return
      }
      res.writeHead(req.method === 'POST' ? 202 : 200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: true, url: req.url }))
    })
  })
  return new Promise((resolve) =>
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port, seen, release })),
  )
}

test('scheme constants give a standard, secure, streaming, fetch-capable origin', () => {
  assert.equal(APP_ORIGIN, 'app://teamtrau')
  assert.equal(APP_SCHEME_PRIVILEGES.scheme, 'app')
  for (const key of ['standard', 'secure', 'supportFetchAPI', 'stream', 'corsEnabled']) {
    assert.equal(APP_SCHEME_PRIVILEGES.privileges[key], true, key)
  }
})

test('serves index with isolation headers, SPA fallback, and blocks sw.js', async () => {
  const handle = createAppProtocolHandler({ distDir: makeDist(), getEngine: () => null })
  const index = await handle(new Request(`${APP_ORIGIN}/`))
  assert.equal(index.status, 200)
  assert.match(index.headers.get('content-type'), /text\/html/)
  assertIsolation(index)
  assert.match(await index.text(), /app/)

  const deep = await handle(new Request(`${APP_ORIGIN}/projects/abc?x=1`))
  assert.equal(deep.status, 200)
  assertIsolation(deep)
  assert.match(await deep.text(), /app/)

  const sw = await handle(new Request(`${APP_ORIGIN}/sw.js`))
  assert.equal(sw.status, 404)

  const wasm = await handle(new Request(`${APP_ORIGIN}/ort.wasm`))
  assert.equal(wasm.headers.get('content-type'), 'application/wasm')
})

test('rejects traversal, NUL bytes and foreign hosts', async () => {
  const handle = createAppProtocolHandler({ distDir: makeDist(), getEngine: () => null })
  const evil = await handle(new Request(`${APP_ORIGIN}/..%2fdist-evil%2fsecret.txt`))
  assert.equal(evil.status, 403)
  assert.doesNotMatch(await evil.text(), /secret/)
  const nul = await handle(new Request(`${APP_ORIGIN}/a%00.html`))
  assert.equal(nul.status, 400)
  const foreign = await handle(new Request('app://other/'))
  assert.equal(foreign.status, 404)
})

test('supports Range, HEAD and unsatisfiable ranges', async () => {
  const handle = createAppProtocolHandler({ distDir: makeDist(), getEngine: () => null })
  const full = await handle(new Request(`${APP_ORIGIN}/clip.bin`))
  assert.equal(full.status, 200)
  assert.equal(full.headers.get('content-length'), '1000')
  assert.equal(full.headers.get('accept-ranges'), 'bytes')
  assert.equal((await full.arrayBuffer()).byteLength, 1000)

  const part = await handle(new Request(`${APP_ORIGIN}/clip.bin`, { headers: { Range: 'bytes=10-19' } }))
  assert.equal(part.status, 206)
  assert.equal(part.headers.get('content-range'), 'bytes 10-19/1000')
  assert.equal(part.headers.get('content-length'), '10')
  assertIsolation(part)
  assert.equal((await part.arrayBuffer()).byteLength, 10)

  const bad = await handle(new Request(`${APP_ORIGIN}/clip.bin`, { headers: { Range: 'bytes=2000-' } }))
  assert.equal(bad.status, 416)
  assert.equal(bad.headers.get('content-range'), 'bytes */1000')

  const head = await handle(new Request(`${APP_ORIGIN}/clip.bin`, { method: 'HEAD' }))
  assert.equal(head.status, 200)
  assert.equal(head.headers.get('content-length'), '1000')
  assert.equal((await head.arrayBuffer()).byteLength, 0)
})

test('engine proxy: 503 without an engine, 502 when unreachable', async () => {
  const none = createAppProtocolHandler({ distDir: makeDist(), getEngine: () => null })
  const r1 = await none(new Request(`${APP_ORIGIN}/engine/v1/health`))
  assert.equal(r1.status, 503)
  assertIsolation(r1)
  assert.deepEqual(await r1.json(), { error: 'engine not running' })

  const dead = await fakeEngine()
  const port = dead.port
  await new Promise((r) => dead.server.close(r))
  const gone = createAppProtocolHandler({ distDir: makeDist(), getEngine: () => ({ port, token: 't' }) })
  const r2 = await gone(new Request(`${APP_ORIGIN}/engine/v1/health`))
  assert.equal(r2.status, 502)
  assert.deepEqual(await r2.json(), { error: 'engine connection failed' })
})

test('engine proxy rewrites the path, replaces Authorization and forwards the body', async () => {
  const engine = await fakeEngine()
  try {
    const handle = createAppProtocolHandler({
      distDir: makeDist(),
      getEngine: () => ({ port: engine.port, token: 'tok' }),
    })
    const res = await handle(
      new Request(`${APP_ORIGIN}/engine/v1/jobs?kind=curate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer evil' },
        body: '{"a":1}',
      }),
    )
    assert.equal(res.status, 202)
    assert.deepEqual(await res.json(), { ok: true, url: '/v1/jobs?kind=curate' })
    const seen = engine.seen.at(-1)
    assert.equal(seen.method, 'POST')
    assert.equal(seen.url, '/v1/jobs?kind=curate')
    assert.equal(seen.headers.authorization, 'Bearer tok')
    assert.equal(seen.body, '{"a":1}')
  } finally {
    engine.release()
    engine.server.close()
  }
})

test('engine proxy streams the response (agent/SSE) instead of buffering it', async () => {
  const engine = await fakeEngine()
  try {
    const handle = createAppProtocolHandler({
      distDir: makeDist(),
      getEngine: () => ({ port: engine.port, token: 'tok' }),
    })
    const res = await handle(new Request(`${APP_ORIGIN}/engine/v1/stream`))
    assert.equal(res.status, 200)
    const reader = res.body.getReader()
    const first = await Promise.race([
      reader.read(),
      new Promise((_, rej) => setTimeout(() => rej(new Error('first chunk was buffered')), 3000)),
    ])
    assert.match(Buffer.from(first.value).toString(), /first/)
    engine.release()
    let rest = ''
    for (let r = await reader.read(); !r.done; r = await reader.read()) rest += Buffer.from(r.value).toString()
    assert.match(rest, /second/)
  } finally {
    engine.release()
    engine.server.close()
  }
})
```

**Contract test — `electron/__tests__/engine-launch.test.cjs`:**
```js
// P5.2 engine launch: packaged sidecar binary vs development venv, with explicit overrides.
const test = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path')

const { resolveEngineLaunch } = require('../engine-launch.cjs')

const RES = path.resolve('/opt/TeamTrau/resources')
const USER = path.resolve('/home/u/.config/TeamTrau Studio')
const DEV = path.resolve('/src/TeamTrau-AutoEdit')
const always = () => true

function base(over = {}) {
  return {
    argv: [],
    env: {},
    platform: 'win32',
    isPackaged: false,
    resourcesPath: RES,
    userDataDir: USER,
    devEngineDir: DEV,
    exists: always,
    ...over,
  }
}

test('packaged build runs the bundled sidecar with a writable cwd, data dir and tools dir', () => {
  const win = resolveEngineLaunch(base({ isPackaged: true }))
  assert.equal(win.mode, 'packaged')
  assert.equal(win.command, path.join(RES, 'engine', 'teamtrau-engine.exe'))
  assert.equal(win.cwd, USER)
  assert.deepEqual(win.args(8123, 'T'), [
    'serve', '--port', '8123', '--token-file', 'T',
    '--data-dir', path.join(USER, 'engine'),
    '--tools-dir', path.join(RES, 'engine', 'tools'),
  ])
  const mac = resolveEngineLaunch(base({ isPackaged: true, platform: 'darwin' }))
  assert.equal(mac.command, path.join(RES, 'engine', 'teamtrau-engine'))
  const linux = resolveEngineLaunch(base({ isPackaged: true, platform: 'linux' }))
  assert.equal(linux.command, path.join(RES, 'engine', 'teamtrau-engine'))
})

test('development build runs main.py from the engine venv per platform', () => {
  const win = resolveEngineLaunch(base())
  assert.equal(win.mode, 'venv')
  assert.equal(win.command, path.join(DEV, '.venv', 'Scripts', 'python.exe'))
  assert.equal(win.cwd, DEV)
  assert.deepEqual(win.args(8123, 'T'), [path.join(DEV, 'main.py'), 'serve', '--port', '8123', '--token-file', 'T'])
  for (const platform of ['darwin', 'linux']) {
    assert.equal(resolveEngineLaunch(base({ platform })).command, path.join(DEV, '.venv', 'bin', 'python'))
  }
})

test('--engine-dir beats TEAMTRAU_ENGINE_DIR, and both beat the packaged sidecar', () => {
  const a = path.resolve('/a')
  const b = path.resolve('/b')
  const fromArg = resolveEngineLaunch(base({ isPackaged: true, argv: ['x', `--engine-dir=${a}`], env: { TEAMTRAU_ENGINE_DIR: b } }))
  assert.equal(fromArg.mode, 'venv')
  assert.equal(fromArg.cwd, a)
  const spaced = resolveEngineLaunch(base({ argv: ['x', '--engine-dir', a] }))
  assert.equal(spaced.cwd, a)
  const fromEnv = resolveEngineLaunch(base({ isPackaged: true, env: { TEAMTRAU_ENGINE_DIR: b } }))
  assert.equal(fromEnv.mode, 'venv')
  assert.equal(fromEnv.cwd, b)
})

test('reports a missing engine instead of throwing', () => {
  const packaged = resolveEngineLaunch(base({ isPackaged: true, exists: () => false }))
  assert.equal(packaged.mode, 'missing')
  assert.ok(packaged.error.includes(path.join(RES, 'engine', 'teamtrau-engine.exe')), packaged.error)
  const mainPy = path.join(DEV, 'main.py')
  const dev = resolveEngineLaunch(base({ exists: (p) => p !== mainPy }))
  assert.equal(dev.mode, 'missing')
  assert.ok(dev.error.includes(mainPy), dev.error)
})
```

**Contract test — `electron/__tests__/gpu-preference.test.cjs` (replaces the P4.4 file):**
```js
// GPU preference: prefer the discrete (NVIDIA) GPU on Windows unless the user opts out; leave other OSes to Chromium.
const test = require('node:test')
const assert = require('node:assert/strict')

const { resolveGpuPreference, gpuSwitchFor } = require('../gpu-preference.cjs')

test('defaults to high-performance on Windows and auto elsewhere', () => {
  assert.equal(resolveGpuPreference([], {}, 'win32'), 'high-performance')
  assert.equal(resolveGpuPreference(['electron', '.'], {}, 'win32'), 'high-performance')
  assert.equal(resolveGpuPreference([], {}, 'darwin'), 'auto')
  assert.equal(resolveGpuPreference([], {}, 'linux'), 'auto')
})

test('--gpu flag selects the preference (both spellings)', () => {
  assert.equal(resolveGpuPreference(['--gpu=power-saving'], {}, 'win32'), 'power-saving')
  assert.equal(resolveGpuPreference(['--gpu', 'power-saving'], {}, 'win32'), 'power-saving')
  assert.equal(resolveGpuPreference(['--gpu=high-performance'], {}, 'darwin'), 'high-performance')
  assert.equal(resolveGpuPreference(['--gpu=auto'], {}, 'win32'), 'auto')
})

test('TEAMTRAU_GPU env applies when no flag is given; the flag wins', () => {
  assert.equal(resolveGpuPreference([], { TEAMTRAU_GPU: 'power-saving' }, 'win32'), 'power-saving')
  assert.equal(resolveGpuPreference(['--gpu=auto'], { TEAMTRAU_GPU: 'power-saving' }, 'win32'), 'auto')
  assert.equal(resolveGpuPreference([], { TEAMTRAU_GPU: 'high-performance' }, 'linux'), 'high-performance')
})

test('unknown values fall back to the platform default', () => {
  assert.equal(resolveGpuPreference(['--gpu=turbo'], {}, 'win32'), 'high-performance')
  assert.equal(resolveGpuPreference([], { TEAMTRAU_GPU: 'nonsense' }, 'win32'), 'high-performance')
  assert.equal(resolveGpuPreference(['--gpu=turbo'], {}, 'linux'), 'auto')
})

test('maps preferences to Chromium switches', () => {
  assert.equal(gpuSwitchFor('high-performance'), 'force_high_performance_gpu')
  assert.equal(gpuSwitchFor('power-saving'), 'force_low_power_gpu')
  assert.equal(gpuSwitchFor('auto'), null)
})
```

---

## P5.3 — PyInstaller "lite" engine sidecar (Engine repo, branch `feature/studio-engine`)

**Architect spike (2026-10-25, Windows, scratch copy of HEAD `8844833`)**
- **Lite environment:** `uv sync --locked --no-dev --python 3.11` while skipping torch, torchvision, ultralytics, faster-whisper, ctranslate2 and every PySide6/shiboken6 package.
  - `import main` then loads 935 modules, with no torch, no PySide6 and no polars.
  - `serve` answers `/v1/health`, and `analyze_soundtrack` and `proxies` complete on the demo footage.
  - Without `--python 3.11`, uv picked Python 3.14. The repo has no `.python-version`, and OTIO then failed to import.
- **PyInstaller 6.22.3 onedir:** builds in about 2–3 minutes. The frozen `teamtrau-engine.exe serve` is healthy after 2.0 s, and both jobs complete.
- **Size:** 670 MB at first. Excluding `polars` (pulled in by `narwhals`) and `matplotlib` (pulled in by `librosa.display`) brings it to **487 MB**. Keep `sklearn`, which `librosa.segment` needs. The largest remaining parts are llvmlite 115 MB, cv2 112 MB, av.libs 63 MB and scipy 50 MB.
- **numba:** JIT compiles on the first `analyze_soundtrack` of every launch (41 s frozen), unless the cache persists; with a cache the second launch takes 7 s. In the spike numba wrote its cache inside `_internal/librosa/__pycache__`, which is read-only in an installed app, so the engine must point `NUMBA_CACHE_DIR` at its data dir.
- **Resolve worker:** `resolve_worker.py` runs in a system Python (fusionscript needs a matching version). Its imports are stdlib-only (`src.core.platform_caps`, `platform_paths`, `ffmpeg_utils`), so the frozen app ships those `.py` files as data. When frozen, `sys.executable` is the sidecar itself and must not be tried as the worker interpreter.
- **ffmpeg:** pinned builds with SHA-256:
  - Windows/Linux: BtbN `autobuild-2026-10-24-13-02`, n8.1.3, GPL. This is the same 8.1 line as development and engine CI.
  - macOS arm64: martin-riedl.de release 9.0.2 (static, the only release line it publishes).
  - BtbN removes old autobuilds after a while. When a pinned URL returns 404, update the pin in one place (URL plus SHA-256).

**API (`release/sidecar.py`, new; `release/__init__.py` empty)**
```python
HEAVY_PACKAGES: tuple[str, ...]   # torch, torchvision, ultralytics, ultralytics-thop, faster-whisper, ctranslate2
EXCLUDED_MODULES: tuple[str, ...] # torch, torchvision, ultralytics, faster_whisper, ctranslate2, PySide6, shiboken6,
                                  # polars, matplotlib, tkinter, src.ui
@dataclass(frozen=True)
class FfmpegAsset: url: str; sha256: str; members: dict[str, str]   # archive basename -> file name in tools/
def lite_excluded_packages(lock: dict) -> list[str]   # HEAVY_PACKAGES present in the lock + every pyside6*/shiboken6* package, sorted
def sync_command(lock: dict) -> list[str]             # uv sync --locked --no-dev --group sidecar --python 3.11 + --no-install-package <each>
def ffmpeg_assets(os_name: OsName) -> list[FfmpegAsset]
def tool_names(os_name: OsName) -> tuple[str, str]    # ("ffmpeg.exe", "ffprobe.exe") on windows
def verify_sha256(path: Path, expected: str) -> None  # ValueError on mismatch
def extract_tools(archive: Path, asset: FfmpegAsset, tools_dir: Path) -> list[Path]  # zip or tar.xz; ValueError if a member is missing
def main(argv: list[str] | None = None) -> int        # python -m release.sidecar build [--skip-sync] [--skip-smoke]
```
Pinned assets (exact values):
| OS | URL | SHA-256 | members |
|---|---|---|---|
| windows | `https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-24-13-02/ffmpeg-n8.1.3-24-g92611900bb-win64-gpl-8.1.zip` | `0b1a903367bfbe906125aad78069e3b9dfecac74ea76db05ada2e667c80ac2c9` | ffmpeg.exe, ffprobe.exe, LICENSE.txt → FFMPEG-LICENSE.txt |
| linux | `.../autobuild-2026-10-24-13-02/ffmpeg-n8.1.3-24-g92611900bb-linux64-gpl-8.1.tar.xz` | `3c2d43d951c1d7936240ec77dbc274d6214fc64973e9993f63e16f03ac908ff1` | ffmpeg, ffprobe, LICENSE.txt → FFMPEG-LICENSE.txt |
| macos | `https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffmpeg.zip` | `c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924` | ffmpeg |
| macos | `https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffprobe.zip` | `fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6` | ffprobe |

**Rules**
- **`build` steps:** each step is a small function, in this order:
  1. **sync:** run `sync_command(lock)` with `UV_PROJECT_ENVIRONMENT=.venv-sidecar`.
  2. **freeze:** `<.venv-sidecar>/(Scripts|bin)/pyinstaller --noconfirm --distpath dist --workpath build/pyinstaller release/teamtrau-engine.spec`.
  3. **tools:** download each asset into `build/downloads/` (reuse a file whose SHA-256 already matches), `verify_sha256`, then `extract_tools` into `dist/teamtrau-engine/tools/`.
  4. **info:** write `dist/teamtrau-engine/BUILD_INFO.json` with the engine git commit, the build date and the ffmpeg asset URLs.
  5. **smoke:** run `release/smoke_sidecar.py dist/teamtrau-engine/teamtrau-engine(.exe)`.

  Every subprocess and download has a timeout. Failures exit non-zero with a one-line reason.
- **`release/teamtrau_engine.py`:** entry point; it only calls `main.cli()`.
- **`release/teamtrau-engine.spec`:**
  - onedir, console, name `teamtrau-engine`, `pathex` set to the repo root.
  - `excludes=list(EXCLUDED_MODULES)` (import it from `release.sidecar`).
  - `hiddenimports=collect_submodules("src", filter=lambda n: not n.startswith("src.ui"))`.
  - `datas`: `collect_data_files("librosa")`, plus the Resolve worker sources at their package-relative paths: `src/dispatchers/resolve_worker.py`, `src/core/platform_caps.py`, `src/core/platform_paths.py`, `src/core/ffmpeg_utils.py`, `src/core/__init__.py`.
- **`release/smoke_sidecar.py <exe>`:**
  - Generates its own media with the bundled `tools/ffmpeg`: a 10 s sine WAV and a 3 s `testsrc2` MP4.
  - Starts `<exe> serve --port <free> --data-dir <tmp> --tools-dir <exe dir>/tools`, with stdout/stderr going to a log file (never an unread PIPE: a full pipe blocks the engine).
  - Waits for `/v1/health` (60 s max), runs `analyze_soundtrack` and `proxies` through the HTTP API with the token from `<tmp>/engine_token`, and checks that the proxy file exists.
  - Checks that `<tmp>/numba-cache` is not empty, prints each step's time, and terminates the engine in `finally`.
- **`main.py serve`:**
  - After the data dir is resolved: `NUMBA_CACHE_DIR = os.environ.setdefault("NUMBA_CACHE_DIR", str(<data>/"numba-cache"))`, and create that directory.
  - This must happen before anything imports librosa or numba. Check this: `serve` imports must not pull numba in earlier.
- **`ResolveDispatcher._worker_interpreters`:** append `[sys.executable]` only when `not getattr(sys, "frozen", False)`.
- **`pyproject.toml`:** add `[dependency-groups] sidecar = ["pyinstaller>=6.22.3"]`, then run `uv lock`. No other version may change: show the `uv.lock` diff summary.
- **`.gitignore`:** `build/` and `dist/` are already ignored; add `.venv-sidecar/`.
- **Do not** change engine behaviour for the full (non-lite) install. Torch, whisper and YOLO stay optional and keep failing soft.

**Files**
- `release/__init__.py`, `release/sidecar.py`, `release/teamtrau_engine.py`, `release/teamtrau-engine.spec`, `release/smoke_sidecar.py` (all new)
- `main.py`, `src/dispatchers/resolve_dispatcher.py`, `pyproject.toml` + `uv.lock`, `.gitignore`
- `tests/test_sidecar_p53.py` (LOCKED)

**Related suites:** `tests/test_portability_p50.py`, `tests/test_resolve_dispatcher.py`, `tests/test_engine_api.py`, `tests/test_engine_ci_p51.py`, the full suite, and ruff.

**Validation:** the locked test passes (14 tests) against a reference implementation in a scratch copy, and the spike built and ran the frozen sidecar as described above.
**Expected red:** `ModuleNotFoundError: No module named 'release'`.

**Acceptance (paste raw output in the HANDOFF)**
1. Run `python -m release.sidecar build` on this Windows machine and paste its full step log, including the smoke output with timings.
2. Report the `du -sh dist/teamtrau-engine` size (target ≤ 520 MB) and the top 10 entries of `_internal`.
3. Run the smoke a **second** time and paste it. `analyze_soundtrack` must be at least 3× faster than in the first run (numba cache), and `_internal` must contain no `*.nbi` files.
4. Run `dist/teamtrau-engine/teamtrau-engine.exe serve --port 8799 --data-dir <tmp>` with `PATH` reduced to `C:\Windows\System32` (no system ffmpeg, no venv) and `--tools-dir dist/teamtrau-engine/tools`. Show that the `ffmpeg` field of `/v1/capabilities` points into `dist/teamtrau-engine/tools`, and that one proxy job completes.
5. Run `git status` to show that `build/`, `dist/` and `.venv-sidecar/` are ignored.

**Commit:** `feat(engine): PyInstaller lite sidecar build with pinned ffmpeg and smoke test`.

**Contract test — `tests/test_sidecar_p53.py`:**
```python
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
```

---

## P5.4 — Local Windows installer (Studio repo, branch `teamtrau/main`)

**Owner decision (2026-10-25):**
- Build the Windows NSIS installer locally first, with the lite sidecar taken from the engine dist and the shared ffmpeg build.
- Cross-repo CI packaging comes later. It needs a read token for the private engine repo, stored as a Studio repo secret.

**Architect spike (scratch, electron-builder 26.18.0, Electron 42.11.15, engine `c4c65fd` sidecar)**
- **Build:** electron-builder with `electronDist` pointed at the installed `node_modules/electron/dist` (no Electron download) builds `TeamTrau-Studio-Setup-0.1.0.exe` in about 2.5 minutes.
  - The installer is 345 MB and `win-unpacked` is 1.2 GB.
  - `app.asar` contains no `.map` files and no `__tests__`.
- **Packaged shell:** the shell only needs Node built-ins plus its own `electron/*.cjs` modules, so the packaged app needs no `node_modules`.
- **`win-unpacked` launched through CDP:**
  - `location.origin` is `app://teamtrau` and `crossOriginIsolated` is `true`.
  - `/engine/v1/health` returns 200 from the **bundled sidecar** (packaged mode of `engine-launch.cjs`).
  - `/v1/capabilities.ffmpeg` points to `resources\engine\tools\ffmpeg.EXE`.
  - The `[gpu]` log shows the RTX 3070.
  - After the app is killed, no `teamtrau-engine.exe` process is left.
- **Engine side (done by the architect, `c4c65fd`):** the Windows sidecar bundles the shared ffmpeg build, which shrinks `tools/` from 314 MB to 192 MB.

**API (`electron/builder-config.cjs`, new)**
```js
function defaultEngineDist(studioRoot, env) // TEAMTRAU_ENGINE_DIST, else <studioRoot>/../TeamTrau-AutoEdit/dist/teamtrau-engine
function builderConfig({ studioRoot, engineDist, platform = process.platform, exists = fs.existsSync }) // electron-builder config object
```

**Rules**
- **`builderConfig`:**
  - Returns exactly what the locked test expects. Use `win.icon` = `public/icons/icon-512.png`.
  - On `win32` it throws when `<engineDist>/teamtrau-engine.exe` or `<engineDist>/tools/ffmpeg.exe` is missing. The message names the missing path and the command `python -m release.sidecar build`.
  - It never throws for other platforms (CI packaging is later).
- **`electron-builder.config.cjs`** (new, repo root): `module.exports = builderConfig({ studioRoot: __dirname, engineDist: defaultEngineDist(__dirname, process.env) })`.
- **`package.json`:**
  - Add the devDependency `"electron-builder": "26.18.0"` (exact) with `npm install -D electron-builder@26.18.0`, and commit the `package-lock.json` change.
  - Add the script `"dist:win": "vp build && electron-builder --win --config electron-builder.config.cjs"`.
  - Do **not** rename the package: keep `name: "freecut"`, the FreeCut internal ids. The packaged identity comes from `productName`/`extraMetadata`, so the installed app keeps its data in `%APPDATA%\TeamTrau Studio`, separate from the development profile.
- **Output:** `out/installer/`, which is already ignored through `out`; check this.
- **Documentation:** `docs/teamtrau/packaging.md` (new, short), covering:
  - The prerequisites, and how to build the sidecar in the engine repo.
  - `npm run dist:win`.
  - `TEAMTRAU_ENGINE_DIST`.
  - Where the installer lands.
  - That the build is unsigned, so Windows SmartScreen will warn until gate G5 signing.
  - The ffmpeg GPL notice: `tools/FFMPEG-LICENSE.txt` ships with the app.

**Files**
- `electron/builder-config.cjs` (new) and `electron-builder.config.cjs` (new)
- `package.json`, `package-lock.json`
- `docs/teamtrau/packaging.md` (new), `docs/teamtrau/CHANGELOG.md`
- `electron/__tests__/builder-config.test.cjs` (LOCKED, new)

**Validation:** the locked test passes (6 tests) against a reference implementation. The reference config, wrapped exactly as the root config above, built a working installer in the spike.
**Expected red:** `Cannot find module '../builder-config.cjs'`.

**Acceptance (Windows, this machine; paste raw output and screenshots in the HANDOFF)**
1. In the engine repo, `dist/teamtrau-engine` already exists from `c4c65fd`. Rebuild it only if it is missing, with `python -m release.sidecar build`.
2. Run `npm run dist:win` in the Studio repo. Paste the tail of the log, the installer file name and size, the `win-unpacked` size, and the `.map` count inside `app.asar` (must be 0; `npx asar list`).
3. Install silently into a scratch folder: `TeamTrau-Studio-Setup-<v>.exe /S /D=<scratch>\TeamTrauStudio` (the `/D` argument must be last and unquoted).
4. Launch the installed exe with `--remote-debugging-port=9333`. Report `location.origin`, `crossOriginIsolated`, `/engine/v1/health` and the `ffmpeg` field of `/engine/v1/capabilities` (it must be under the install folder).
5. In the installed app, pick the same real workspace folder used in P3/P4/P5.2 (native handle, no OPFS) and open one project.
   - Run Sequence → Analyze soundtrack, and play 5 s of the timeline.
   - Screenshot both, as `p54_installed_editor.png` and `p54_installed_job.png`.
6. Quit the app, then show that no `teamtrau-engine.exe` process remains (`tasklist`).
7. Uninstall silently with the uninstaller in the install folder (`/S`). Show that the install folder is gone and that `%APPDATA%\TeamTrau Studio` is still there.
8. The `npm run test:electron` output, plus the Studio verification ladder: `vp check`, `vp test run` for the TeamTrau suites, and `vp build`.

**Commit:** `feat(teamtrau): local Windows installer with the bundled engine sidecar (electron-builder NSIS)`.

**Contract test — `electron/__tests__/builder-config.test.cjs`:**
```js
// P5.4 electron-builder config: Windows NSIS installer with the PyInstaller engine sidecar as extraResources.
const test = require('node:test')
const assert = require('node:assert/strict')
const path = require('node:path')

const { builderConfig, defaultEngineDist } = require('../builder-config.cjs')

const STUDIO = path.resolve('/src/teamtrau-studio')
const ENGINE = path.resolve('/src/TeamTrau-AutoEdit/dist/teamtrau-engine')

function config(over = {}) {
  return builderConfig({ studioRoot: STUDIO, engineDist: ENGINE, platform: 'win32', exists: () => true, ...over })
}

test('identity: TeamTrau Studio app id, product name and packaged metadata', () => {
  const c = config()
  assert.equal(c.appId, 'com.teamtrau.studio')
  assert.equal(c.productName, 'TeamTrau Studio')
  assert.equal(c.extraMetadata.productName, 'TeamTrau Studio')
  assert.equal(c.extraMetadata.main, 'electron/main.cjs')
})

test('packs the built renderer and the Electron shell only, without source maps or tests', () => {
  const c = config()
  assert.equal(c.asar, true)
  for (const pattern of ['electron/**/*.cjs', '!electron/__tests__/**', 'dist/**/*', '!dist/**/*.map', 'package.json']) {
    assert.ok(c.files.includes(pattern), pattern)
  }
  assert.ok(!c.files.some((f) => f === '**/*' || f.startsWith('src/') || f.startsWith('node_modules')), c.files.join(','))
  assert.deepEqual(c.directories, { output: 'out/installer' })
})

test('reuses the installed Electron and ships the engine sidecar outside the asar', () => {
  const c = config()
  assert.equal(c.electronDist, path.join(STUDIO, 'node_modules', 'electron', 'dist'))
  assert.deepEqual(c.extraResources, [{ from: ENGINE, to: 'engine', filter: ['**/*'] }])
})

test('Windows target is a per-user NSIS x64 installer with a chosen directory', () => {
  const c = config()
  assert.deepEqual(c.win.target, [{ target: 'nsis', arch: ['x64'] }])
  assert.equal(c.nsis.oneClick, false)
  assert.equal(c.nsis.perMachine, false)
  assert.equal(c.nsis.allowToChangeInstallationDirectory, true)
  assert.match(c.nsis.artifactName, /\$\{version\}/)
  assert.equal(c.nsis.deleteAppDataOnUninstall, false)
})

test('refuses to build without the engine sidecar and says how to make it', () => {
  const exe = path.join(ENGINE, 'teamtrau-engine.exe')
  assert.throws(() => config({ exists: (p) => p !== exe }), (err) => {
    assert.ok(err.message.includes(exe), err.message)
    assert.ok(err.message.includes('python -m release.sidecar build'), err.message)
    return true
  })
  const tools = path.join(ENGINE, 'tools', 'ffmpeg.exe')
  assert.throws(() => config({ exists: (p) => p !== tools }), /ffmpeg/)
  const linux = config({ platform: 'linux', exists: (p) => p !== exe })
  assert.equal(linux.extraResources[0].from, ENGINE)
})

test('engine dist defaults to the sibling engine checkout and honours TEAMTRAU_ENGINE_DIST', () => {
  assert.equal(defaultEngineDist(STUDIO, {}), path.resolve(STUDIO, '..', 'TeamTrau-AutoEdit', 'dist', 'teamtrau-engine'))
  const custom = path.resolve('/x/engine')
  assert.equal(defaultEngineDist(STUDIO, { TEAMTRAU_ENGINE_DIST: custom }), custom)
})
```

---

## P5.5 — Renderer hardware encoder ladder (Engine repo, branch `feature/studio-engine`)

**Why:** masters and previews only use NVENC or libx264/libx265. Machines without NVIDIA hardware encode on the CPU, even though `platform_caps.probe()` already finds smoke-tested VideoToolbox/QSV/AMF encoders.

**Architect findings (2026-10-25, this laptop: RTX 3070 + AMD iGPU, ffmpeg 8.1)**
- `get_caps()` reports `('h264_nvenc', 'h264_amf', 'libx264')` and `('hevc_nvenc', 'hevc_amf', 'libx265')`, so AMF can be tested for real on this machine.
- Real encodes with the planned argument sets:
  - **Pass:** `hevc_amf` 8-bit (`-quality quality -rc cqp -qp_i/-qp_p -profile:v main -pix_fmt nv12`); `h264_amf` High, including intra (`-g 1 -bf 0`).
  - **Fail:** `hevc_amf` 10-bit, with "10-bit encoder is not supported by AMD GPU drivers versions lower than 23.30". This is a real case that must fall back to the CPU.
- `ffmpeg -h encoder=…` confirms that the QSV/AMF options exist. `h264_qsv` accepts only `nv12`. VideoToolbox is not in the Windows build, so its arguments follow the ffmpeg documentation and are checked only by unit tests.
- **VAAPI is out of scope:** it needs `-vaapi_device` plus `format=nv12,hwupload` inside the filter graph. It remains a later task.
- **Validation:** a reference implementation in a scratch copy passes the locked unit test (29 tests) and the real-ffmpeg locked test. On this machine that is 5 cases: h264/hevc NVENC, h264/hevc AMF, and the HDR 10-bit AMF fallback. `test_nvenc_renderer`, `test_render_cache`, `test_master_codecs` and `test_render_real` also pass (107 tests).

**API**
```python
# src/dispatchers/encoder_ladder.py (new, pure)
HwFamily = Literal["nvenc", "videotoolbox", "qsv", "amf"]
HW_FAMILIES: tuple[HwFamily, ...] = ("nvenc", "videotoolbox", "qsv", "amf")
def pick_hw_encoder(codec: str, available: tuple[str, ...], preference: str) -> str | None
def hw_video_args(encoder: str, *, cq: int, bitrate: str | None, maxrate: str | None,
                  bufsize: str | None, ten_bit: bool, intra_only: bool) -> list[str]

# src/core/config.py
PipelineConfig.hw_encoder: Literal["auto", "nvenc", "videotoolbox", "qsv", "amf", "none"] = "auto"

# src/dispatchers/nvenc_renderer.py
NvencRenderer.hw_encoders: dict[str, str | None]   # {"h264": ..., "hevc": ...}, set in __init__
NvencRenderer.hardware_available -> bool           # property
encoder_args(use_hw, preview=False); master_encoder_args(settings, use_hw, preview=False, ten_bit=None)   # renamed from use_nvenc
```

**Rules**
- **`pick_hw_encoder`:**
  - `"none"` → `None`.
  - `"nvenc"` → `f"{codec}_nvenc"`, without looking at `available` (keeps today's behaviour when the probe is skipped).
  - Otherwise, the first `available` encoder (it is already ladder-ordered and smoke-tested by `platform_caps.probe`) with prefix `codec_`, whose family is in `HW_FAMILIES`, and that matches the preference (`"auto"` accepts any family). Never VAAPI or libx.
- **`hw_video_args`** handles only VideoToolbox/QSV/AMF and raises `ValueError` for anything else. It does not add colour tags; the renderer appends them.
  - **Profile:** HEVC uses `main10`/`main`, H.264 uses `high`.
  - **Pixel format:** `p010le` for 10-bit HEVC, otherwise `nv12`.
  - **AMF:** `-quality quality -rc cqp -qp_i Q -qp_p Q`, plus `-qp_b Q` for H.264.
  - **QSV:** `-preset medium -global_quality Q`.
  - **VideoToolbox:** `-b:v <bitrate or 8M>`, plus `-maxrate`/`-bufsize` only when given, `-tag:v hvc1` for HEVC, and `-allow_sw 0`.
  - **Intra:** `-g 1 -bf 0`.
  - Argument order is free; the test reads values by flag.
- **`NvencRenderer.__init__`:** keep `self.nvenc_available = check_nvenc_available()`. When `hw_encoder` is not `"nvenc"`/`"none"`, read `get_caps()` (import it into the module namespace: `from src.core.platform_caps import get_caps`, which the test patches). Then set `hw_encoders[c] = pick_hw_encoder(c, caps.<c>_encoders, pref)`.
- **NVENC paths are unchanged:** existing `-preset/-cq/-b:v…` arguments, NVDEC `-hwaccel cuda`, and the CPU fallback.
- **Non-NVENC hardware encoder:** when `use_hw` is set and the codec's selected encoder is VideoToolbox/QSV/AMF:
  - masters use `hw_video_args(enc, cq=nvenc_cq, bitrate=nvenc_bitrate, maxrate=nvenc_maxrate, bufsize=nvenc_bufsize, ten_bit=…, intra_only=self.intra_only)` plus the colour tags;
  - previews use `cq=30` with no bitrate, maxrate or bufsize.
- **Codec with no selected encoder:** NVENC serves it only when `hw_encoder == "nvenc"`, or when it is `"auto"` and `nvenc_available`. Otherwise it goes to the CPU. This is the `_nvenc_ok()` rule, and it fixes a reference-spike bug where `amf` without `hevc_amf` would have used NVENC.
- **`hardware_available`:** `hw_encoder == "nvenc"` or any non-`None` entry in `hw_encoders`.
- **`render_edit_timeline`:** uses hardware when `(hardware_available or _nvenc_ok()) and not force_cpu`.
  - NVDEC (`-hwaccel cuda`) is used only when the chosen encoder ends with `_nvenc`.
  - The existing "hardware failed → re-render on CPU" loop now covers every family.
- **The `__new__`-based clone** (around line 931) must copy `hw_encoders`.
- **`ffmpeg_utils.NVENC_ERROR_MARKERS`:** add `"amf"`, `"amd gpu"`, `"mfx"`, `"qsv"`, `"videotoolbox"` and `"compression session"`.
- **Existing tests (setup-only edits allowed):**
  - Rename the `use_nvenc=` keyword to `use_hw=` in `tests/test_nvenc_renderer.py`.
  - In `tests/test_nvenc_renderer.py` and `tests/test_render_cache.py`, every renderer whose assertions expect NVENC arguments gets `PipelineConfig(hw_encoder="nvenc", …)`. Otherwise `"auto"` would pick VideoToolbox on macOS CI.
  - Do not change any assertion.
- **Out of scope:** VAAPI, and exposing `hw_encoder` in the Studio Deliver UI or the engine API (later).

**Files**
- `src/dispatchers/encoder_ladder.py` (new)
- `src/core/config.py`, `src/core/ffmpeg_utils.py`, `src/dispatchers/nvenc_renderer.py`
- `tests/test_nvenc_renderer.py`, `tests/test_render_cache.py` (setup-only)
- `tests/test_encoder_ladder_p55.py` (LOCKED)
- `tests/integration/test_encoder_ladder_real_p55.py` (LOCKED)

**Related suites:** `tests/test_nvenc_renderer.py`, `tests/test_render_cache.py`, `tests/test_platform_caps.py`, `tests/integration/test_master_codecs.py`, `tests/integration/test_render_real.py`, `tests/integration/test_render_cache_real.py`, the full suite, and ruff.

**Expected red:** `ModuleNotFoundError: No module named 'src.dispatchers.encoder_ladder'`, for both locked files.

**Acceptance (this machine; paste raw output)**
1. `pytest tests/integration/test_encoder_ladder_real_p55.py -v`: all 5 cases pass (NVENC and AMF for h264/hevc, plus the 10-bit fallback). Say which encoder the 10-bit case used.
2. Render the real `Assembled Sequence` timeline (`E:\antigravity\demo-footage\.workspace`, the same project as P5.2/P5.4) as a 1080p30 HEVC Rec.709 master three times, with `hw_encoder` set to `nvenc`, `amf` and `none`. Use a short Python snippet through the same service path the Deliver job uses, or `NvencRenderer.render_edit_timeline`.
   - Paste `encoder_used`, the wall time and the `ffprobe` codec/profile/pix_fmt line of each output.
3. Show that the CPU fallback is visible to the user: render HDR (`rec2020_hlg`) HEVC with `hw_encoder="amf"`, and paste the warning log line and `encoder_used`.

**Commit:** `feat(engine): renderer hardware encoder ladder (VideoToolbox, QSV, AMF) from platform_caps`.

**Contract test — `tests/test_encoder_ladder_p55.py`:**
```python
"""P5.5: renderer hardware encoder ladder (NVENC > VideoToolbox > QSV > AMF > CPU) from platform_caps."""

import pytest

from src.core.config import PipelineConfig
from src.core.ffmpeg_utils import is_hw_encoder_error
from src.core.platform_caps import PlatformCaps
from src.core.types import DeliverSettings
from src.dispatchers import nvenc_renderer
from src.dispatchers.encoder_ladder import HW_FAMILIES, hw_video_args, pick_hw_encoder
from src.dispatchers.nvenc_renderer import NvencRenderer


def test_families_in_ladder_order_without_vaapi():
    assert HW_FAMILIES == ("nvenc", "videotoolbox", "qsv", "amf")


@pytest.mark.parametrize(
    "codec, available, preference, expected",
    [
        ("hevc", ("hevc_nvenc", "hevc_amf", "libx265"), "auto", "hevc_nvenc"),
        ("hevc", ("hevc_amf", "libx265"), "auto", "hevc_amf"),
        ("h264", ("h264_videotoolbox", "libx264"), "auto", "h264_videotoolbox"),
        ("h264", ("h264_qsv", "h264_amf", "libx264"), "auto", "h264_qsv"),
        ("hevc", ("hevc_vaapi", "libx265"), "auto", None),
        ("hevc", ("libx265",), "auto", None),
        ("hevc", (), "auto", None),
        ("hevc", ("hevc_nvenc", "hevc_amf", "libx265"), "amf", "hevc_amf"),
        ("hevc", ("hevc_nvenc", "libx265"), "amf", None),
        ("hevc", ("hevc_nvenc", "hevc_amf", "libx265"), "none", None),
        ("h264", ("h264_amf", "libx264"), "nvenc", "h264_nvenc"),
        ("h264", (), "nvenc", "h264_nvenc"),
    ],
)
def test_pick_hw_encoder(codec, available, preference, expected):
    assert pick_hw_encoder(codec, available, preference) == expected


def _args(encoder, **kw):
    base = {"cq": 18, "bitrate": "60M", "maxrate": "80M", "bufsize": "120M", "ten_bit": False,
            "intra_only": False}
    base.update(kw)
    return hw_video_args(encoder, **base)


def _opt(args, flag):
    return args[args.index(flag) + 1]


def test_amf_constant_qp_quality_preset():
    a = _args("hevc_amf")
    assert a[:2] == ["-c:v", "hevc_amf"]
    assert (_opt(a, "-quality"), _opt(a, "-rc"), _opt(a, "-qp_i"), _opt(a, "-qp_p")) == ("quality", "cqp", "18", "18")
    assert (_opt(a, "-profile:v"), _opt(a, "-pix_fmt")) == ("main", "nv12")
    a10 = _args("hevc_amf", ten_bit=True)
    assert (_opt(a10, "-profile:v"), _opt(a10, "-pix_fmt")) == ("main10", "p010le")
    h = _args("h264_amf", intra_only=True)
    assert (_opt(h, "-qp_b"), _opt(h, "-profile:v"), _opt(h, "-g"), _opt(h, "-bf")) == ("18", "high", "1", "0")


def test_qsv_icq_quality():
    a = _args("hevc_qsv", ten_bit=True)
    assert a[:2] == ["-c:v", "hevc_qsv"]
    assert (_opt(a, "-global_quality"), _opt(a, "-profile:v"), _opt(a, "-pix_fmt")) == ("18", "main10", "p010le")
    h = _args("h264_qsv")
    assert (_opt(h, "-profile:v"), _opt(h, "-pix_fmt")) == ("high", "nv12")


def test_videotoolbox_bitrate_hvc1_tag_and_no_software_fallback():
    a = _args("hevc_videotoolbox", ten_bit=True)
    assert a[:2] == ["-c:v", "hevc_videotoolbox"]
    assert (_opt(a, "-b:v"), _opt(a, "-maxrate"), _opt(a, "-bufsize")) == ("60M", "80M", "120M")
    assert (_opt(a, "-profile:v"), _opt(a, "-tag:v"), _opt(a, "-allow_sw"), _opt(a, "-pix_fmt")) == (
        "main10", "hvc1", "0", "p010le")
    preview = _args("h264_videotoolbox", bitrate=None, maxrate=None, bufsize=None)
    assert _opt(preview, "-b:v") == "8M" and "-maxrate" not in preview and "-tag:v" not in preview


@pytest.mark.parametrize("encoder", ["hevc_nvenc", "libx265", "hevc_vaapi", "prores_ks"])
def test_hw_video_args_rejects_encoders_it_does_not_own(encoder):
    with pytest.raises(ValueError):
        _args(encoder)


@pytest.mark.parametrize(
    "stderr",
    [
        "[hevc_amf @ 0x1] 10-bit encoder is not supported by AMD GPU drivers versions lower than 23.30.",
        "[hevc_qsv @ 0x1] Error initializing an internal MFX session: unsupported (-3)",
        "[h264_videotoolbox @ 0x1] Error: cannot create compression session: -12908",
        "[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: unsupported device (2)",
    ],
)
def test_hardware_failures_trigger_the_cpu_fallback(stderr):
    assert is_hw_encoder_error(stderr)


def test_plain_ffmpeg_errors_do_not_trigger_the_cpu_fallback():
    assert not is_hw_encoder_error("No such file or directory")


def _caps(h264, hevc):
    return PlatformCaps(os="windows", ffmpeg="ffmpeg", h264_encoders=h264, hevc_encoders=hevc,
                        hwaccels=(), torch_device="cpu")


def test_renderer_uses_the_caps_ladder_for_masters(monkeypatch):
    monkeypatch.setattr(nvenc_renderer, "get_caps", lambda: _caps(("h264_amf", "libx264"), ("hevc_amf", "libx265")))
    monkeypatch.setattr(nvenc_renderer, "check_nvenc_available", lambda: False)
    s = DeliverSettings(canvas_width=640, canvas_height=360, fps=30.0, master="hevc", color="rec709")
    r = NvencRenderer(PipelineConfig(), settings=s)
    assert r.hw_encoders == {"h264": "h264_amf", "hevc": "hevc_amf"}
    assert r.hardware_available is True
    enc, args = r.master_encoder_args(s, True)
    assert enc == "hevc_amf" and args[:2] == ["-c:v", "hevc_amf"]
    assert "-color_primaries" in args
    cpu, _ = r.master_encoder_args(s, False)
    assert cpu == "libx265"
    assert r.master_encoder_args(s, True, preview=True)[0] == "h264_amf"


def test_codec_without_a_selected_encoder_goes_to_cpu_not_nvenc(monkeypatch):
    monkeypatch.setattr(nvenc_renderer, "get_caps", lambda: _caps(("h264_amf", "libx264"), ("libx265",)))
    monkeypatch.setattr(nvenc_renderer, "check_nvenc_available", lambda: False)
    s = DeliverSettings(canvas_width=640, canvas_height=360, fps=30.0, master="hevc", color="rec709")
    assert NvencRenderer(PipelineConfig(), settings=s).master_encoder_args(s, True)[0] == "libx265"
    monkeypatch.setattr(nvenc_renderer, "check_nvenc_available", lambda: True)
    amf = NvencRenderer(PipelineConfig(hw_encoder="amf"), settings=s)
    assert amf.hw_encoders == {"h264": "h264_amf", "hevc": None}
    assert amf.master_encoder_args(s, True)[0] == "libx265"
    vt = NvencRenderer(PipelineConfig(hw_encoder="videotoolbox"), settings=s)
    assert vt.hardware_available is False
    assert vt.master_encoder_args(s, True)[0] == "libx265"


def test_preference_none_disables_hardware_and_nvenc_skips_the_probe(monkeypatch):
    def boom():
        raise AssertionError("caps must not be probed")

    monkeypatch.setattr(nvenc_renderer, "get_caps", boom)
    monkeypatch.setattr(nvenc_renderer, "check_nvenc_available", lambda: True)
    off = NvencRenderer(PipelineConfig(hw_encoder="none"))
    assert off.hw_encoders == {"h264": None, "hevc": None}
    assert off.hardware_available is False
    nv = NvencRenderer(PipelineConfig(hw_encoder="nvenc"))
    assert nv.hw_encoders == {"h264": "h264_nvenc", "hevc": "hevc_nvenc"}
    assert nv.hardware_available is True
    assert nv.encoder_args(True)[0] == "hevc_nvenc"


def test_hw_encoder_setting_is_a_closed_set():
    with pytest.raises(ValueError):
        PipelineConfig(hw_encoder="vaapi")
```

**Contract test — `tests/integration/test_encoder_ladder_real_p55.py`:**
```python
"""P5.5 (real ffmpeg): every smoke-tested hardware encoder renders a master; failures fall back to CPU."""

import pytest

from src.core.config import PipelineConfig
from src.core.platform_caps import get_caps
from src.core.types import DeliverSettings, EditClip, EditTimeline
from src.dispatchers.encoder_ladder import HW_FAMILIES
from src.dispatchers.nvenc_renderer import NvencRenderer
from tests.integration.conftest import stream_info

_CAPS = get_caps()
_HW = [
    (codec, enc)
    for codec, encs in (("h264", _CAPS.h264_encoders), ("hevc", _CAPS.hevc_encoders))
    for enc in encs
    if enc.rsplit("_", 1)[-1] in HW_FAMILIES
]


def _render(tmp_path, make_clip, family: str, master: str, color: str = "rec709"):
    clip = make_clip("a.mp4", duration=3.0)
    s = DeliverSettings(canvas_width=320, canvas_height=180, fps=30.0, master=master, color=color)
    tl = EditTimeline("t", s.fps, s.canvas_width, s.canvas_height, clips=[EditClip(clip, 0.0, 60, 2.0)])
    cfg = PipelineConfig(cache_dir=tmp_path / "cache", enable_teaser=False, hw_encoder=family)
    return NvencRenderer(cfg, settings=s).render_edit_timeline(tl, tmp_path / "out" / "m.mp4", settings=s)


@pytest.mark.skipif(not _HW, reason="no smoke-tested hardware encoder on this machine")
@pytest.mark.parametrize("codec, encoder", _HW)
def test_each_hardware_encoder_renders_a_master(tmp_path, make_clip, codec, encoder):
    family = encoder.rsplit("_", 1)[-1]
    res = _render(tmp_path, make_clip, family, codec)
    assert res.encoder_used == encoder
    v = stream_info(res.output_path, "video")
    assert v["codec_name"] == codec
    assert (v["width"], v["height"]) == (320, 180)


@pytest.mark.skipif(not _HW, reason="no smoke-tested hardware encoder on this machine")
def test_ten_bit_hdr_master_renders_on_hardware_or_falls_back_to_cpu(tmp_path, make_clip):
    hevc = [enc for codec, enc in _HW if codec == "hevc"]
    if not hevc:
        pytest.skip("no hardware HEVC encoder")
    family = hevc[-1].rsplit("_", 1)[-1]  # the lowest-priority one, e.g. AMF on an NVIDIA + AMD laptop
    res = _render(tmp_path, make_clip, family, "hevc", color="rec2020_hlg")
    assert res.encoder_used in (hevc[-1], "libx265")
    v = stream_info(res.output_path, "video")
    assert v["codec_name"] == "hevc" and v["pix_fmt"] == "yuv420p10le"
```
