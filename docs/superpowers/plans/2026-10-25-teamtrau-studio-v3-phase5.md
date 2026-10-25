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
| **P5.2** | Studio | Shell portability: fixed `app://teamtrau` origin (persistence bug confirmed), packaged-engine launch branch, engine-missing warning, macOS activate, GPU auto off Windows; CI: push on `teamtrau/main` plus an `electron-shell` OS matrix | **yes (below)** |
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
