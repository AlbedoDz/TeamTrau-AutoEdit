# TeamTrau Studio v3 — Phase 2 (Studio shell) and Phase 4 (render-cache playback), detailed

> **Parent plan:** `2026-10-24-teamtrau-studio-v3.md`. **Spec:** `docs/superpowers/specs/2026-10-24-teamtrau-studio-v3-design.md`.
> **Phase 1 status:** done on Engine branch `feature/studio-engine`. Every exit criterion was verified against a live `main.py serve`.
> **Process:** the same as Phase 1 — one task per Antigravity conversation, LOCKED contract tests copied verbatim, a HANDOFF file per task, and architect review before the next task.

## Facts this plan relies on (read from the Studio code on branch `teamtrau/engine`)

**Electron today**
- `electron/main.cjs` (S0.1 spike) is the only Electron file. Electron is pinned at `42.11.15`.
- It serves `dist/` from an embedded `127.0.0.1` HTTP server and sets COOP/COEP.
- It has no preload, no IPC, no engine sidecar, and package.json has no `"main"` field.
- Its path guard `filePath.startsWith(distDir)` is a prefix check. On Windows, `/..%5cdist-evil%5c...` escapes to a sibling folder. The Range handling does not validate ranges.

**Workspace access**
- FreeCut reaches the workspace only through a `FileSystemDirectoryHandle`:
  - `workspace-fs/root.ts` `setWorkspaceRoot`
  - the picker in `features/workspace-gate/workspace-gate.tsx:135`
  - handles persisted in the IndexedDB database `freecut-handles-db`
- The native File System Access API works in Electron, but the renderer never learns the folder's absolute path. The engine needs that path.

**Runtime CDN dependencies**
- Google Fonts (`index.html`).
- onnxruntime and transformers WASM, plus Hugging Face models. These serve FreeCut's own AI features (transcription, TTS, music generation, interpolation).
- The core editor does not need them.

**i18n**
- i18next with `src/i18n/languages.ts`, base `src/i18n/locales/{lang}.json`, and partials `src/i18n/locales/partials/{lang}/*.json`. There is no `vi`.

**Preview**
- `buildPreviewCompositionData` (`src/features/preview/hooks/use-preview-composition-model.ts:386`) is the single place where every timeline item gets its playable `src`.
- Video items there without a `mediaId` pass through unchanged.
- In S0.4, one continuous all-intra file played with 0.67 % dropped frames at 60 fps. With per-clip sources it dropped 24–97 %.

**Upstream reference only (do not merge):** `upstream/sidecar` (Tauri plus a Python native-inference sidecar and a connection monitor), and FreeCut's `headless/` Node server.

## Design decisions (D-P2/P4)

- **D1 — engine traffic goes through the shell.**
  - The Electron static server reverse-proxies `/engine/*` to the engine and adds the bearer token itself.
  - The renderer never sees the token, there is no CORS, and COEP is untouched.
  - In a plain browser (`vp dev`), a Vite dev proxy does the same.
- **D2 — workspace bridge by id.**
  - The renderer ensures `.teamtrau/workspace.json` = `{"version": 1, "id": "<uuid>"}` exists in the workspace (written through the handle).
  - The shell keeps a registry `id → absolute path` in `userData/workspaces.json`.
  - When the shell does not know an id yet, it shows one native folder dialog and checks that the chosen folder carries that id.
  - Engine jobs receive that path.
- **D3 — render cache lives in the workspace.** The path is `{workspace}/.teamtrau/render-cache/{projectId}/` (`render_cache.json` plus `{key}.mp4`), so the renderer reads it through the same directory handle as blob URLs.
- **D4 — playback substitution.**
  - For each ready, still-valid segment, the covered V1 video items are replaced by **one** synthetic video item whose `src` is the segment.
  - Segments are **video-only**, so the linked A1 audio items keep playing the original sound.
  - Fast scrub keeps the original items. A segment is used only when every covered item is plain: no effects, transform, crop, fades, keyframes, FreeCut transitions, reverse, blend, corner pin or motion, and it has a linked audio companion.
- **D5 — the engine stays where it is during development.**
  - The shell starts `{engineDir}/.venv/Scripts/python.exe` (`.venv/bin/python` on macOS/Linux) running `main.py serve --port P --token-file F`.
  - `engineDir` comes from `--engine-dir` or the `TEAMTRAU_ENGINE_DIR` environment variable, and defaults to the sibling folder `../TeamTrau-AutoEdit`.
  - Packaging the engine is Phase 5.
- **D6 — Electron unit tests use `node:test`.** There is no new test dependency. The script is `"test:electron": "node --test \"electron/__tests__/*.test.cjs\""`.

## Global rules for Phase 2/4 (on top of R1–R15)

- **Repositories and branches**
  - Studio repo `E:\antigravity\teamtrau-studio`, branch **`teamtrau/main`**, created in P2.0 from `teamtrau/engine`.
  - Engine repo `E:\antigravity\TeamTrau-AutoEdit`, branch **`feature/studio-engine`**.
  - Each task names its repo.
- **Studio verification ladder**
  - `npx vp test run <task tests>`
  - `npm run test:electron` (Electron tasks)
  - `npx vp test run`: full, record the counts
  - `npx tsc --noEmit -p tsconfig.json`
  - `npx vp lint src`
  - `npm run check:boundaries && npm run check:layer-boundaries`
  - `npx vp build`
- **Studio isolation:** new code lives in `electron/**`, `src/features/teamtrau/**`, `src/i18n/**` (P2.5 only), `public/fonts/**`, and `index.html`. An edit to any other existing FreeCut file must be a hook the task names, and listed in the HANDOFF.
- **No new npm dependencies.**
- **Locked TypeScript tests:**
  - The Studio has `noUncheckedIndexedAccess` on. Locked tests already account for it.
  - Your code must type-check under `tsc --noEmit`.
  - Tests import from `vitest`.
- **Licences:** keep the FreeCut `LICENSE`. List any added third-party file (fonts) in `NOTICE` with its licence.

---

## P2.0 — Studio hygiene (Studio repo)

1. `git switch -c teamtrau/main teamtrau/engine`.
2. Add `NOTICE`: FreeCut MIT attribution (copyright line from `LICENSE`), "TeamTrau Studio is a fork of FreeCut", and an empty "Third-party assets" section.
3. `package.json`:
   - `"main": "electron/main.cjs"`
   - `"electron:dev": "vp build && electron ."`
   - `"test:electron": "node --test \"electron/__tests__/*.test.cjs\""`
   - Change no other field.
4. Record the baselines in `docs/teamtrau/BASELINE.md`:
   - `npx vp test run` (pass/fail/skip counts, duration)
   - `npx tsc --noEmit -p tsconfig.json` error count
   - `npx vp lint src` result
5. Commit `chore(teamtrau): start teamtrau/main (NOTICE, electron scripts, baseline)`.

No locked tests. The HANDOFF must paste the baseline numbers. If the baseline already has failures or type errors, list them: later tasks must not add to them.

## P2.1 — Static server with engine proxy (`electron/static-server.cjs`, Studio repo)

Extract the HTTP server from `electron/main.cjs` into a module:

```js
// electron/static-server.cjs
createStaticServer({ distDir, getEngine }) -> Promise<{ server, port, url, close(): Promise<void> }>
// getEngine(): { port: number, token: string } | null   (read on every request)
```

**Rules**
- **Bind and headers:** bind `127.0.0.1` on port 0. Every static response carries COEP `require-corp`, COOP `same-origin` and CORP `same-origin`. Remove the spike's `Access-Control-Allow-Origin: *`.
- **Static files:**
  - `/sw.js` returns 404.
  - `/` maps to `index.html`.
  - A missing file falls back to `index.html` (SPA).
- **Path guard:** resolve with `path.resolve(distDir, '.' + decodedPath)`, then require `resolved === root || resolved.startsWith(root + path.sep)`. Anything else returns **403**. A malformed URI encoding returns 400.
- **Range requests:**
  - Supported forms: `bytes=a-b`, `bytes=a-` and the suffix form `bytes=-n`.
  - The end is clamped to `size - 1`.
  - An unsatisfiable range returns **416** with `Content-Range: bytes */size`.
  - A good range returns 206 with `Content-Range`, `Content-Length` and `Accept-Ranges`.
- **`/engine` and `/engine/*` proxy:**
  - When `getEngine()` is null, return **503** JSON `{"error":"engine not running"}`.
  - Otherwise stream the request (method, headers, body) to `http://127.0.0.1:{port}{path without /engine}{query}`. Replace any incoming `Authorization` with `Bearer {token}`. Stream the response back (status, headers, body), so SSE works. If the upstream connection fails, return 502.
- **`main.cjs`:** uses the module and keeps its behaviour. `getEngine` returns null until P2.3.

**Files:**
- `electron/static-server.cjs` (new)
- `electron/main.cjs`
- `electron/__tests__/static-server.test.cjs` (LOCKED)

**Contract test — `electron/__tests__/static-server.test.cjs`:**
```js
// Static server for the Electron shell: headers, SPA fallback, traversal guard, Range, engine proxy.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const http = require('node:http')

const { createStaticServer } = require('../static-server.cjs')

function request(base, rawPath, { method = 'GET', headers = {}, body } = {}) {
  const u = new URL(base)
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: u.hostname, port: u.port, path: rawPath, method, headers, timeout: 5000 },
      (res) => {
        const chunks = []
        res.on('data', (c) => chunks.push(c))
        res.on('end', () =>
          resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }),
        )
      },
    )
    req.on('timeout', () => req.destroy(new Error('request timeout')))
    req.on('error', reject)
    if (body) req.write(body)
    req.end()
  })
}

function makeDist() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tt-static-'))
  const dist = path.join(root, 'dist')
  fs.mkdirSync(dist)
  fs.writeFileSync(path.join(dist, 'index.html'), '<!doctype html><html><body>app</body></html>')
  fs.writeFileSync(path.join(dist, 'clip.bin'), Buffer.alloc(1000, 7))
  fs.mkdirSync(path.join(root, 'dist-evil'))
  fs.writeFileSync(path.join(root, 'dist-evil', 'secret.txt'), 'secret')
  return dist
}

function fakeEngine() {
  const seen = []
  const server = http.createServer((req, res) => {
    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', () => {
      seen.push({ method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString() })
      res.writeHead(req.method === 'POST' ? 202 : 200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: true, url: req.url }))
    })
  })
  return new Promise((resolve) =>
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port, seen })),
  )
}

test('serves index with isolation headers and SPA fallback; sw.js is blocked', async () => {
  const s = await createStaticServer({ distDir: makeDist(), getEngine: () => null })
  try {
    const r = await request(s.url, '/')
    assert.equal(r.status, 200)
    assert.match(r.body.toString(), /<html/)
    assert.equal(r.headers['cross-origin-opener-policy'], 'same-origin')
    assert.equal(r.headers['cross-origin-embedder-policy'], 'require-corp')
    assert.equal(r.headers['cross-origin-resource-policy'], 'same-origin')
    const spa = await request(s.url, '/projects/abc')
    assert.equal(spa.status, 200)
    assert.match(spa.body.toString(), /<html/)
    assert.equal((await request(s.url, '/sw.js')).status, 404)
  } finally {
    await s.close()
  }
})

test('rejects traversal, including into a sibling directory sharing the dist prefix', async () => {
  const dist = makeDist()
  const s = await createStaticServer({ distDir: dist, getEngine: () => null })
  try {
    const base = path.basename(dist)
    const attempts = [
      `/%2e%2e/${base}-evil/secret.txt`,
      `/..%5c${base}-evil%5csecret.txt`,
      `/%2e%2e%5c${base}-evil%5csecret.txt`,
    ]
    for (const p of attempts) {
      const r = await request(s.url, p)
      assert.notEqual(r.body.toString(), 'secret', `leaked sibling file via ${p}`)
      if (process.platform === 'win32' && p.includes('%5c')) assert.equal(r.status, 403, p)
    }
  } finally {
    await s.close()
  }
})

test('supports byte ranges, suffix ranges and rejects unsatisfiable ranges', async () => {
  const s = await createStaticServer({ distDir: makeDist(), getEngine: () => null })
  try {
    const a = await request(s.url, '/clip.bin', { headers: { Range: 'bytes=0-99' } })
    assert.equal(a.status, 206)
    assert.equal(a.body.length, 100)
    assert.equal(a.headers['content-range'], 'bytes 0-99/1000')
    const b = await request(s.url, '/clip.bin', { headers: { Range: 'bytes=900-' } })
    assert.equal(b.status, 206)
    assert.equal(b.headers['content-range'], 'bytes 900-999/1000')
    const c = await request(s.url, '/clip.bin', { headers: { Range: 'bytes=-100' } })
    assert.equal(c.status, 206)
    assert.equal(c.headers['content-range'], 'bytes 900-999/1000')
    assert.equal(c.body.length, 100)
    const d = await request(s.url, '/clip.bin', { headers: { Range: 'bytes=2000-3000' } })
    assert.equal(d.status, 416)
    assert.equal(d.headers['content-range'], 'bytes */1000')
  } finally {
    await s.close()
  }
})

test('proxies /engine/* to the engine with the server-side token only', async () => {
  const eng = await fakeEngine()
  const s = await createStaticServer({
    distDir: makeDist(),
    getEngine: () => ({ port: eng.port, token: 'tok-123' }),
  })
  try {
    const r = await request(s.url, '/engine/v1/jobs/x/events?format=json', {
      headers: { Authorization: 'Bearer evil' },
    })
    assert.equal(r.status, 200)
    assert.equal(JSON.parse(r.body.toString()).url, '/v1/jobs/x/events?format=json')
    assert.equal(eng.seen[0].headers.authorization, 'Bearer tok-123')
    const p = await request(s.url, '/engine/v1/jobs/proxies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"paths":[]}',
    })
    assert.equal(p.status, 202)
    assert.equal(eng.seen[1].method, 'POST')
    assert.equal(eng.seen[1].body, '{"paths":[]}')
  } finally {
    await s.close()
    eng.server.close()
  }
})

test('returns 503 for /engine/* while no engine is running', async () => {
  const s = await createStaticServer({ distDir: makeDist(), getEngine: () => null })
  try {
    assert.equal((await request(s.url, '/engine/v1/health')).status, 503)
  } finally {
    await s.close()
  }
})
```
**Expected red:** `Cannot find module '../static-server.cjs'`.
**Commit:** `feat(electron): static server module (path guard, ranges, engine proxy)`.

## P2.2 — Engine sidecar lifecycle (`electron/engine-sidecar.cjs`, Studio repo)

```js
// electron/engine-sidecar.cjs
startEngine({ command, args, cwd, tokenDir, healthTimeoutMs = 60000, pollMs = 200 })
  -> Promise<{ port, token, tokenFile, pid, stop(): Promise<void> }>
// args(port, tokenFile) -> string[]
```

**Rules**
- **Port:** pick a free loopback port (listen on 0, read the port, close).
- **Token:** `crypto.randomBytes(32).toString('hex')`, written to `{tokenDir}/engine-token-{port}` with mode `0o600`.
- **Spawn:** `spawn(command, args(port, tokenFile), { cwd, windowsHide: true, detached: process.platform !== 'win32' })`. Pipe stdout/stderr into a rotating log `{tokenDir}/engine.log`, at most 5 MB, keeping one old file.
- **Health wait:** poll `GET http://127.0.0.1:{port}/v1/health` every `pollMs`, with a 1 s timeout per request, until it returns 200.
  - If the child exits first: kill and clean up, then reject `Error("engine exited with code N before becoming healthy")` with `err.pid` set.
  - On timeout: kill the process tree, clean up, then reject `Error("engine did not become healthy within X ms")` with `err.pid` set.
- **`stop()`:**
  - Idempotent. Kills the **process tree**: `taskkill /PID pid /T /F` on Windows (via `spawnSync` with a 10 s timeout), `process.kill(-pid, 'SIGKILL')` elsewhere.
  - Deletes the token file.
  - The engine spawns ffmpeg children, so a plain `child.kill()` is not enough.

**Files:**
- `electron/engine-sidecar.cjs` (new)
- `electron/__tests__/engine-sidecar.test.cjs` (LOCKED)
- `electron/__tests__/fixtures/fake-engine.cjs` (LOCKED)

**Contract test — `electron/__tests__/engine-sidecar.test.cjs`:**
```js
// Engine sidecar lifecycle: free port, private token file, health wait, tree kill, failure paths.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { startEngine } = require('../engine-sidecar.cjs')

const FAKE = path.join(__dirname, 'fixtures', 'fake-engine.cjs')

function tokenDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'tt-engine-'))
}

function alive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

async function waitDead(pid, ms = 5000) {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) {
    if (!alive(pid)) return true
    await new Promise((r) => setTimeout(r, 50))
  }
  return false
}

test('starts the engine, waits for health, and stops it cleanly', async () => {
  const dir = tokenDir()
  const eng = await startEngine({
    command: process.execPath,
    args: (port, tokenFile) => [FAKE, '--port', String(port), '--token-file', tokenFile],
    tokenDir: dir,
    healthTimeoutMs: 15000,
  })
  try {
    assert.ok(eng.port > 0)
    assert.ok(eng.token.length >= 32)
    assert.equal(fs.readFileSync(eng.tokenFile, 'utf8').trim(), eng.token)
    if (process.platform !== 'win32') {
      assert.equal(fs.statSync(eng.tokenFile).mode & 0o077, 0)
    }
    const health = await fetch(`http://127.0.0.1:${eng.port}/v1/health`)
    assert.equal(health.status, 200)
    const authed = await fetch(`http://127.0.0.1:${eng.port}/v1/x`, {
      headers: { Authorization: `Bearer ${eng.token}` },
    })
    assert.equal(authed.status, 200)
  } finally {
    await eng.stop()
  }
  assert.ok(await waitDead(eng.pid), 'engine process still alive after stop()')
  assert.equal(fs.existsSync(eng.tokenFile), false)
  await eng.stop() // idempotent
})

test('rejects and kills the process when health never comes up', async () => {
  const t0 = Date.now()
  let caught = null
  try {
    await startEngine({
      command: process.execPath,
      args: (port, tokenFile) => [FAKE, '--never-ready', '--port', String(port), '--token-file', tokenFile],
      tokenDir: tokenDir(),
      healthTimeoutMs: 1500,
      pollMs: 100,
    })
  } catch (err) {
    caught = err
  }
  assert.ok(caught, 'startEngine resolved for a never-ready engine')
  assert.match(caught.message, /did not become healthy/)
  assert.ok(Number.isInteger(caught.pid))
  assert.ok(await waitDead(caught.pid), 'never-ready engine was not killed')
  assert.ok(Date.now() - t0 < 10000)
})

test('rejects fast when the engine process exits early', async () => {
  const t0 = Date.now()
  await assert.rejects(
    startEngine({
      command: process.execPath,
      args: () => ['-e', 'process.exit(3)'],
      tokenDir: tokenDir(),
      healthTimeoutMs: 20000,
    }),
    /exited/,
  )
  assert.ok(Date.now() - t0 < 5000)
})
```
**Fixture (LOCKED) — `electron/__tests__/fixtures/fake-engine.cjs`:**
```js
// Stand-in for `python main.py serve --port P --token-file F` used by engine-sidecar tests.
const fs = require('node:fs')
const http = require('node:http')

const argv = process.argv.slice(2)
const arg = (name) => argv[argv.indexOf(name) + 1]

if (argv.includes('--never-ready')) {
  setInterval(() => {}, 1000)
} else {
  const token = fs.readFileSync(arg('--token-file'), 'utf8').trim()
  http
    .createServer((req, res) => {
      if (req.url === '/v1/health') {
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end('{"status":"ok","version":"1"}')
        return
      }
      const ok = req.headers.authorization === `Bearer ${token}`
      res.writeHead(ok ? 200 : 401, { 'Content-Type': 'application/json' })
      res.end(ok ? '{"ok":true}' : '{"error":"unauthorized"}')
    })
    .listen(Number(arg('--port')), '127.0.0.1')
}
```
**Expected red:** `Cannot find module '../engine-sidecar.cjs'`.
**Commit:** `feat(electron): engine sidecar lifecycle (free port, private token, health wait, tree kill)`.

## P2.3 — Shell integration: workspace bridge, preload, engine client (Studio repo)

**Shell side**
- **`electron/workspace-registry.cjs`:**
  - `readWorkspaceId(dir)` returns the `id` from `{dir}/.teamtrau/workspace.json`. It returns null if the file is absent and throws on corrupt JSON.
  - `createWorkspaceRegistry(file)` returns `{ get(id), set(id, dir) }`, persisted as JSON with an atomic tmp-then-rename write.
  - `set` throws `/does not match/` unless `readWorkspaceId(dir) === id`.
  - `get` returns null when the stored folder no longer carries that id.
- **`electron/preload.cjs`** (contextIsolation) exposes `window.teamtrauHost = { isElectron: true, engineBase: '/engine', resolveWorkspacePath(id): Promise<string | null> }` through `ipcRenderer.invoke('teamtrau:resolve-workspace', id)`.
- **`main.cjs`:**
  - **Startup:**
    - Take `app.requestSingleInstanceLock()`; a second instance focuses the first.
    - Register `preload.cjs`.
    - Resolve `engineDir` (D5).
    - Start the engine with `startEngine` using `tokenDir = app.getPath('userData')`.
    - If the engine fails, log it, keep `getEngine()` null, and **still open the window**.
  - **IPC `teamtrau:resolve-workspace`:** if `registry.get(id)` returns a path, return it. Otherwise show `dialog.showOpenDialog({ properties: ['openDirectory'], title: 'Select the same workspace folder for the TeamTrau engine' })`, then `registry.set(id, chosen)`, then return it. Cancelling, or choosing a folder that does not match, returns null.
  - **`before-quit`:** await `engine.stop()`.
  - **Offline flag:** with `--offline`, cancel every request whose host is not `127.0.0.1` or `localhost`, using `session.webRequest.onBeforeRequest`. This is used for the P2.4 acceptance check.

**Renderer side**
- **`src/features/teamtrau/engine/engine-client.ts`:**
  - `createEngineClient({ baseUrl = '/engine', fetchFn = fetch })` returns `{ health, capabilities, submitJob(kind, params), getJob(id), cancel(id), waitForJob(id, { pollMs = 500, signal }) }`. `EngineError` carries `status`.
  - Use only `ok`, `status` and `json()` of the response.
  - `waitForJob`:
    - polls `GET /v1/jobs/{id}` until the status is not `running`
    - for `failed`, throws `EngineError(job.error)`
    - on abort, posts `/v1/jobs/{id}/cancel` and rejects with a `DOMException('…', 'AbortError')`
- **`src/features/teamtrau/engine/workspace-id.ts`:** `ensureWorkspaceId(root: FileSystemDirectoryHandle): Promise<string>` creates or reads `.teamtrau/workspace.json` through the handle. Write your own tests with an in-memory fake handle.
- **Vite dev proxy:** in `vite.config.ts` (named hook), proxy `/engine` to `http://127.0.0.1:${TEAMTRAU_ENGINE_PORT}`, adding `Authorization: Bearer ${TEAMTRAU_ENGINE_TOKEN}`, only when both env vars are set.

**Files:**
- `electron/workspace-registry.cjs` (new)
- `electron/preload.cjs` (new)
- `electron/main.cjs`
- `src/features/teamtrau/engine/{engine-client.ts,workspace-id.ts,index.ts}` (new)
- `src/features/teamtrau/engine/__tests__/workspace-id.test.ts` (yours)
- `vite.config.ts` (dev proxy only)
- Locked tests:
  - `electron/__tests__/workspace-registry.test.cjs`
  - `src/features/teamtrau/engine/__tests__/engine-client.test.ts`

**Contract test — `electron/__tests__/workspace-registry.test.cjs`:**
```js
// Workspace bridge: maps a workspace id (from .teamtrau/workspace.json) to its absolute path.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { createWorkspaceRegistry, readWorkspaceId } = require('../workspace-registry.cjs')

function workspace(id) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tt-ws-'))
  if (id !== undefined) {
    fs.mkdirSync(path.join(dir, '.teamtrau'))
    fs.writeFileSync(path.join(dir, '.teamtrau', 'workspace.json'), JSON.stringify({ version: 1, id }))
  }
  return dir
}

test('readWorkspaceId returns the id, null when absent, and throws on corrupt JSON', () => {
  assert.equal(readWorkspaceId(workspace('w-1')), 'w-1')
  assert.equal(readWorkspaceId(workspace()), null)
  const bad = workspace('w-2')
  fs.writeFileSync(path.join(bad, '.teamtrau', 'workspace.json'), '{bad')
  assert.throws(() => readWorkspaceId(bad))
})

test('registry persists id -> path atomically and validates the folder', () => {
  const store = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tt-reg-')), 'workspaces.json')
  const reg = createWorkspaceRegistry(store)
  assert.equal(reg.get('w-1'), null)
  const ws = workspace('w-1')
  reg.set('w-1', ws)
  assert.equal(reg.get('w-1'), path.resolve(ws))
  assert.equal(createWorkspaceRegistry(store).get('w-1'), path.resolve(ws))
  assert.deepEqual(
    fs.readdirSync(path.dirname(store)).filter((f) => f.endsWith('.tmp')),
    [],
  )
  assert.throws(() => reg.set('w-9', ws), /does not match/)
  assert.throws(() => reg.set('w-1', workspace()), /does not match/)
})

test('get returns null when the remembered folder no longer has that id', () => {
  const store = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tt-reg-')), 'workspaces.json')
  const ws = workspace('w-1')
  const reg = createWorkspaceRegistry(store)
  reg.set('w-1', ws)
  fs.writeFileSync(path.join(ws, '.teamtrau', 'workspace.json'), JSON.stringify({ version: 1, id: 'other' }))
  assert.equal(reg.get('w-1'), null)
})
```
**Contract test — `src/features/teamtrau/engine/__tests__/engine-client.test.ts`:**
```ts
import { describe, expect, it, vi } from 'vitest'

import { createEngineClient, EngineError } from '../engine-client'

type Reply = { status: number; body: unknown }

function fakeFetch(replies: Reply[]) {
  const calls: { url: string; init?: RequestInit }[] = []
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init })
    const r = replies.shift() ?? { status: 500, body: { error: 'no reply queued' } }
    return { ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body }
  })
  return { fn, calls }
}

describe('engine client', () => {
  it('GETs health and capabilities under the base URL', async () => {
    const f = fakeFetch([
      { status: 200, body: { status: 'ok', version: '1' } },
      { status: 200, body: { os: 'windows', h264_encoders: ['h264_nvenc'] } },
    ])
    const client = createEngineClient({ fetchFn: f.fn })
    expect(await client.health()).toEqual({ status: 'ok', version: '1' })
    expect((await client.capabilities()).h264_encoders).toEqual(['h264_nvenc'])
    expect(f.calls.map((c) => c.url)).toEqual(['/engine/v1/health', '/engine/v1/capabilities'])
  })

  it('POSTs a job as JSON and returns its id', async () => {
    const f = fakeFetch([{ status: 202, body: { job_id: 'abc123' } }])
    const client = createEngineClient({ baseUrl: '/engine', fetchFn: f.fn })
    expect(await client.submitJob('proxies', { paths: ['a.mov'], out_dir: 'p' })).toBe('abc123')
    const { url, init } = f.calls[0]!
    expect(url).toBe('/engine/v1/jobs/proxies')
    expect(init?.method).toBe('POST')
    expect(new Headers(init?.headers).get('content-type')).toBe('application/json')
    expect(JSON.parse(String(init?.body))).toEqual({ paths: ['a.mov'], out_dir: 'p' })
  })

  it('throws EngineError carrying the status and the engine message', async () => {
    const f = fakeFetch([{ status: 404, body: { error: 'unknown job kind: nope' } }])
    const client = createEngineClient({ fetchFn: f.fn })
    const err = await client.submitJob('nope', {}).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(EngineError)
    expect((err as EngineError).status).toBe(404)
    expect((err as EngineError).message).toContain('unknown job kind: nope')
  })

  it('waitForJob polls until the job leaves "running"', async () => {
    const f = fakeFetch([
      { status: 200, body: { job_id: 'j', status: 'running', result: null, error: null } },
      { status: 200, body: { job_id: 'j', status: 'running', result: null, error: null } },
      { status: 200, body: { job_id: 'j', status: 'done', result: { rendered: 3 }, error: null } },
    ])
    const client = createEngineClient({ fetchFn: f.fn })
    const job = await client.waitForJob('j', { pollMs: 1 })
    expect(job.status).toBe('done')
    expect(job.result).toEqual({ rendered: 3 })
    expect(f.calls).toHaveLength(3)
    expect(f.calls.every((c) => c.url === '/engine/v1/jobs/j')).toBe(true)
  })

  it('waitForJob rejects with EngineError when the job fails', async () => {
    const f = fakeFetch([
      { status: 200, body: { job_id: 'j', status: 'failed', result: null, error: 'ffmpeg exploded' } },
    ])
    const client = createEngineClient({ fetchFn: f.fn })
    await expect(client.waitForJob('j', { pollMs: 1 })).rejects.toThrow('ffmpeg exploded')
  })

  it('waitForJob cancels the job and rejects with AbortError when aborted', async () => {
    const running = { status: 200, body: { job_id: 'j', status: 'running', result: null, error: null } }
    const f = fakeFetch([running, running, running, { status: 200, body: { cancelled: true } }])
    const client = createEngineClient({ fetchFn: f.fn })
    const ctrl = new AbortController()
    const p = client.waitForJob('j', { pollMs: 5, signal: ctrl.signal })
    setTimeout(() => ctrl.abort(), 8)
    const err = await p.catch((e: unknown) => e)
    expect((err as Error).name).toBe('AbortError')
    const cancel = f.calls.find((c) => c.url === '/engine/v1/jobs/j/cancel')
    expect(cancel?.init?.method).toBe('POST')
  })
})
```
**Expected red:** module not found for both.

**Manual acceptance (paste in HANDOFF):**
1. `npm run electron:dev` with the engine checkout present.
2. In DevTools, `await (await fetch('/engine/v1/health')).json()` returns `{status:"ok"}`, and `fetch('/engine/v1/capabilities')` returns 200.
3. `fetch('http://127.0.0.1:<enginePort>/v1/capabilities')` with no token returns 401 or is blocked.
4. Quit the app. Then `tasklist | findstr python` shows **no** leftover `main.py serve` process.
5. Rename the engine folder, start again. The editor still opens and `/engine/v1/health` returns 503.

**Commit:** `feat(electron): workspace bridge, preload, engine client and sidecar wiring`.

## P2.4 — Offline-safe shell assets and product name (Studio repo)

**Rules**
- **Fonts:**
  - Download the IBM Plex woff2 files the UI uses from the official `IBM/plex` GitHub release (OFL-1.1): Sans 300/400/500/600/700 and Mono 400/500/600. Put them in `public/fonts/`, named `IBMPlexSans-<Weight>.woff2` and `IBMPlexMono-<Weight>.woff2`, where Weight is Light, Regular, Medium, SemiBold or Bold.
  - Add `@font-face` rules (in `src/features/teamtrau/fonts.css`, imported once from `src/main.tsx`).
  - Remove the Google Fonts `<link>`s from `index.html`.
  - Record the release URL, licence and SHA-256 of each file in `NOTICE`.
- **Product name:** `index.html` `<title>` becomes `TeamTrau Studio`, and the `application-name` / `apple-mobile-web-app-title` metas become `TeamTrau Studio`. Do not rename internal `freecut` ids.
- **Offline acceptance (manual, paste in HANDOFF):**
  - `electron . --offline` opens the editor with correct fonts, so check a screenshot.
  - Open an engine-written project and play 10 s.
  - List which features report "offline" errors (AI transcription, TTS and so on). They are expected to fail gracefully. Report any that crash the UI.

**Files:**
- `public/fonts/*`
- `src/features/teamtrau/fonts.css`
- `src/main.tsx` (one import line)
- `index.html`
- `NOTICE`
- `src/features/teamtrau/__tests__/offline-assets.test.ts` (LOCKED)

**Contract test — `src/features/teamtrau/__tests__/offline-assets.test.ts`:**
```ts
// @vitest-environment node
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const root = process.cwd()

describe('offline shell assets', () => {
  it('index.html loads no remote fonts or stylesheets', () => {
    const html = readFileSync(resolve(root, 'index.html'), 'utf8')
    expect(html).not.toMatch(/fonts\.googleapis\.com|fonts\.gstatic\.com/)
    expect(html).not.toMatch(/<link[^>]+href="https?:\/\//)
  })

  it('ships the IBM Plex UI fonts locally', () => {
    for (const f of ['IBMPlexSans-Regular.woff2', 'IBMPlexSans-SemiBold.woff2', 'IBMPlexMono-Regular.woff2']) {
      expect(existsSync(resolve(root, 'public', 'fonts', f)), f).toBe(true)
    }
  })

  it('names the product TeamTrau Studio in the document title', () => {
    const html = readFileSync(resolve(root, 'index.html'), 'utf8')
    expect(html).toMatch(/<title>[^<]*TeamTrau Studio[^<]*<\/title>/)
  })
})
```
**Expected red:** remote font links found, font files missing, title is `FreeCut`.
**Commit:** `feat(teamtrau): offline fonts and TeamTrau Studio product name`.

## P2.5 — Vietnamese locale (Studio repo)

**Rules**
- Add `{ code: 'vi', nativeName: 'Tiếng Việt', englishName: 'Vietnamese', dir: 'ltr' }` to `SUPPORTED_LANGUAGES`. Add `'vi'` to the language list in `src/i18n/lazy-locales.test.ts` test 5; this is the only allowed edit to that existing test.
- Create `src/i18n/locales/vi.json` and `src/i18n/locales/partials/vi/*.json` with **exactly** the English key set, translated into natural Vietnamese for video editors.
  - Keep `{{placeholders}}`, HTML tags and keyboard shortcut names unchanged.
  - Product names stay as they are: FreeCut, TeamTrau, WebGPU, NVENC.
  - Common editing terms keep their customary Vietnamese/English usage: timeline, clip, keyframe, render and proxy may stay in English where Vietnamese editors use them so.
  - Write a short glossary at `docs/teamtrau/vi-glossary.md` and follow it consistently.
- Do the work file by file. Do not machine-copy the English.

**Files:**
- `src/i18n/languages.ts`
- `src/i18n/lazy-locales.test.ts` (the one line)
- `src/i18n/locales/vi.json`
- `src/i18n/locales/partials/vi/*.json`
- `docs/teamtrau/vi-glossary.md`
- `src/i18n/vi-locale.test.ts` (LOCKED)

**Contract test — `src/i18n/vi-locale.test.ts`:**
```ts
// @vitest-environment node
import { describe, expect, it } from 'vitest'

import { SUPPORTED_LANGUAGE_CODES } from './languages'

const bases = import.meta.glob('./locales/*.json', { eager: true, import: 'default' }) as Record<
  string,
  unknown
>
const partials = import.meta.glob('./locales/partials/*/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>

function leaves(obj: unknown, prefix = '', out = new Map<string, string>()): Map<string, string> {
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v !== null && typeof v === 'object') leaves(v, key, out)
    else out.set(key, String(v))
  }
  return out
}

function placeholders(s: string): string[] {
  return [...s.matchAll(/\{\{\s*([\w.]+)[^}]*\}\}/g)].map((m) => m[1] ?? '').sort()
}

function pairs(): [string, Map<string, string>, Map<string, string>][] {
  const out: [string, Map<string, string>, Map<string, string>][] = [
    ['en.json', leaves(bases['./locales/en.json']), leaves(bases['./locales/vi.json'] ?? {})],
  ]
  for (const path of Object.keys(partials).filter((p) => p.startsWith('./locales/partials/en/'))) {
    const vi = partials[path.replace('/partials/en/', '/partials/vi/')]
    out.push([path, leaves(partials[path]), leaves(vi ?? {})])
  }
  return out
}

describe('Vietnamese locale', () => {
  it('is a supported language', () => {
    expect(SUPPORTED_LANGUAGE_CODES).toContain('vi')
  })

  it('has exactly the English keys in the base file and every partial', () => {
    for (const [file, en, vi] of pairs()) {
      expect([...vi.keys()].sort(), file).toEqual([...en.keys()].sort())
    }
  })

  it('keeps every {{placeholder}} of the English string', () => {
    for (const [file, en, vi] of pairs()) {
      for (const [key, value] of en) {
        expect(placeholders(vi.get(key) ?? ''), `${file}:${key}`).toEqual(placeholders(value))
      }
    }
  })

  it('is actually translated (at least 85% of longer strings differ from English)', () => {
    let total = 0
    let differ = 0
    for (const [, en, vi] of pairs()) {
      for (const [key, value] of en) {
        if (value.length <= 3) continue
        total += 1
        if (vi.get(key) !== value) differ += 1
      }
    }
    expect(total).toBeGreaterThan(1000)
    expect(differ / total).toBeGreaterThanOrEqual(0.85)
  })
})
```
**Expected red:** `vi` not in `SUPPORTED_LANGUAGE_CODES`; the key sets differ.
**Commit:** `feat(i18n): Vietnamese locale`.

**Phase 2 exit (architect check):**
- Electron opens offline with the engine started.
- `/engine` is proxied with the token hidden.
- Quitting leaves no engine or ffmpeg process.
- The UI is switchable to Vietnamese.
- All Studio gates are green against the P2.0 baseline.

---

## P4.0 — Render cache for Studio (Engine repo, branch `feature/studio-engine`)

**Rules**
- **`FreeCutTimeline` signatures:** gains `items: tuple[Mapping[str, Any], ...] = ()`. These are signatures of the V1 video items in time order. Each one is `{"id", "mediaId", "from", "durationInFrames", "sourceStart", "sourceEnd", "speed"}`, with values copied from `project.json`:
  - `sourceStart` defaults to `0`.
  - `sourceEnd` defaults to the computed value.
  - `speed` defaults to `1.0`.
- **`write_manifest` signatures:** gains `item_sigs: Sequence[Mapping[str, Any]] | None = None`. When given, every segment entry gets `"items"`: the signatures of its `clip_indices`, in order. Manifest `version` stays `1`, since the field is additive.
- **`render_missing` video-only segments:** segment files must contain **one video stream and no audio**.
  - After a segment renders to `{key}.part.mp4`, remux it to video-only with `ffmpeg -i part -map 0:v:0 -c copy -an {key}.v.part.mp4` (timeout 120 s).
  - Then `os.replace` it to `{key}.mp4`, and delete the intermediate files on every path, error paths included.
- **`run_render_cache`:**
  - `cache_dir` becomes optional, defaulting to `{workspace}/.teamtrau/render-cache/{project_id}`.
  - It passes `item_sigs=freecut_tl.items` to `write_manifest`.
  - The result adds `"cache_dir"`.

**Files:**
- `src/integrations/freecut_reader.py`
- `src/modules/render_cache.py`
- `src/engine_api/runners.py`
- `tests/integration/test_render_cache_studio_real.py` (LOCKED)

**Related suites:**
- `tests/test_render_cache.py`
- `tests/integration/test_render_cache_real.py`
- `tests/integration/test_freecut_reader_real.py`
- `tests/test_engine_api.py`
- `tests/test_phase1_polish.py`

**Contract test — `tests/integration/test_render_cache_studio_real.py`:**
```python
"""Render cache for Studio: item signatures in the manifest, default cache dir, video-only segments."""

import json
import subprocess
import threading
from pathlib import Path

from src.core.types import EditClip, EditTimeline
from src.engine_api.runners import run_render_cache
from src.integrations.freecut_reader import read_freecut_project
from src.integrations.freecut_writer import write_freecut_project


def _stream_types(p: Path) -> list[str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return out.split()


def _project(tmp_path: Path, make_clip) -> tuple[Path, str]:
    a = make_clip("a.mp4", duration=2.0)
    b = make_clip("b.mp4", duration=2.0)
    clips = [EditClip(source_path=p, source_in=0.2, frames=20, available=1.0) for p in (a, b, a)]
    tl = EditTimeline(name="t", fps=30.0, canvas_width=640, canvas_height=360, clips=clips)
    ws = tmp_path / "ws"
    return ws, write_freecut_project(tl, ws, "RC")


def test_reader_exposes_item_signatures(tmp_path: Path, make_clip):
    ws, pid = _project(tmp_path, make_clip)
    ft = read_freecut_project(ws, pid)
    proj = json.loads((ws / "projects" / pid / "project.json").read_text(encoding="utf-8"))
    video = sorted((i for i in proj["timeline"]["items"] if i["type"] == "video"),
                   key=lambda i: i["from"])
    assert [s["id"] for s in ft.items] == [v["id"] for v in video]
    for s, v in zip(ft.items, video):
        assert set(s) == {"id", "mediaId", "from", "durationInFrames", "sourceStart", "sourceEnd",
                          "speed"}
        for k in ("mediaId", "from", "durationInFrames", "sourceStart", "sourceEnd"):
            assert s[k] == v[k]
        assert s["speed"] == v.get("speed", 1.0)


def test_render_cache_job_for_studio(tmp_path: Path, make_clip):
    ws, pid = _project(tmp_path, make_clip)
    events: list[dict] = []
    res = run_render_cache({"workspace": str(ws), "project_id": pid, "max_segment_sec": 1.0},
                           events.append, threading.Event())
    cache_dir = ws / ".teamtrau" / "render-cache" / pid
    assert Path(res["manifest"]) == cache_dir / "render_cache.json"
    assert Path(res["cache_dir"]) == cache_dir
    man = json.loads((cache_dir / "render_cache.json").read_text(encoding="utf-8"))
    ft = read_freecut_project(ws, pid)
    sigs = [s for seg in man["segments"] for s in seg["items"]]
    assert sigs == [dict(s) for s in ft.items]
    for seg in man["segments"]:
        assert seg["ready"] is True
        assert [s["from"] for s in seg["items"]][0] == seg["start_frame"]
        assert _stream_types(cache_dir / seg["file"]) == ["video"]
    assert not [p.name for p in cache_dir.iterdir() if ".part" in p.name]
```
**Expected red:** `AttributeError: 'FreeCutTimeline' object has no attribute 'items'` and `KeyError: 'cache_dir'`.
**Commit:** `feat(render-cache): studio manifest item signatures, workspace cache dir, video-only segments`.

## P4.1 — Playback substitution, pure function (Studio repo)

`src/features/teamtrau/render-cache/substitute.ts` exports:
- the types `SegmentItemSig`, `ManifestSegment`, `RenderCacheManifest`, `RcItem` (`{ id; from; durationInFrames; [k: string]: unknown }`) and `RcTrack` (`{ id; kind?; order; items: RcItem[] }`)
- the function:

```ts
applyRenderCache<T extends RcTrack>(args: {
  tracks: readonly T[]
  keyframes: readonly { itemId: string }[]
  transitions: readonly { leftClipId: string; rightClipId: string }[]
  manifest: RenderCacheManifest
  segmentUrls: ReadonlyMap<string, string> // segment key -> playable URL
}): { tracks: T[]; usedSegments: string[] }
```

**Rules**
- **Where it applies:** **V1** is the `kind === 'video'` track with the lowest `order`. Only V1 changes, and the input is never mutated.
- **When a segment is used:** all of the following must hold.
  - It is `ready` and has a URL.
  - The V1 video items overlapping `[start_frame, start_frame + frames)` are exactly the segment's `items` ids.
  - Each of those items matches its signature: `mediaId`, `from`, `durationInFrames`, `sourceStart ?? 0`, `sourceEnd`, `speed ?? 1`.
  - Each item is plain (D4). Neutral values count as plain: `blendMode: 'normal'`, `fadeIn: 0`, empty arrays.
  - No item has keyframes or takes part in a FreeCut transition.
  - Every item's `linkedGroupId` is shared by some `type: 'audio'` item.
- **Synthetic item:**
  - Built as `{...firstCoveredItem}`, overridden with `id: 'rc-' + key`, `label: 'Render cache'`, `from: start_frame`, `durationInFrames: frames`, `src: url`, `sourceStart: 0`, `sourceEnd: frames`, `sourceDuration: frames`, `sourceFps: manifest.fps` and `speed: 1`.
  - The keys `mediaId`, `linkedGroupId` and `audioSrc` are **deleted**, not set to undefined.
- **Result:** V1 items are sorted by `from`. `usedSegments` lists keys in manifest order.

**Files:**
- `src/features/teamtrau/render-cache/substitute.ts` (new)
- `src/features/teamtrau/render-cache/__tests__/substitute.test.ts` (LOCKED)

**Contract test — `src/features/teamtrau/render-cache/__tests__/substitute.test.ts`:**
```ts
import { describe, expect, it } from 'vitest'

import { applyRenderCache, type RenderCacheManifest } from '../substitute'

type AnyItem = Record<string, unknown> & { id: string; from: number; durationInFrames: number }

const V1 = 'track-v1'

function vid(id: string, from: number, dur: number, extra: Record<string, unknown> = {}): AnyItem {
  return {
    id,
    type: 'video',
    trackId: V1,
    from,
    durationInFrames: dur,
    label: id,
    mediaId: `m-${id}`,
    src: '',
    sourceStart: 10,
    sourceEnd: 10 + dur,
    speed: 1,
    linkedGroupId: `g-${id}`,
    ...extra,
  }
}

function aud(v: AnyItem): AnyItem {
  return {
    id: `a-${v.id}`,
    type: 'audio',
    trackId: 'track-a1',
    from: v.from,
    durationInFrames: v.durationInFrames,
    label: v.id,
    mediaId: v.mediaId,
    src: '',
    linkedGroupId: v.linkedGroupId,
  }
}

function tracks(video: AnyItem[], audio: AnyItem[] = video.map(aud), overlay: AnyItem[] = []) {
  return [
    { id: V1, name: 'V1', kind: 'video' as const, order: 0, muted: false, items: video },
    { id: 'track-v2', name: 'V2', kind: 'video' as const, order: 1, muted: false, items: overlay },
    { id: 'track-a1', name: 'A1', kind: 'audio' as const, order: 2, muted: false, items: audio },
  ]
}

function sig(i: AnyItem) {
  return {
    id: i.id,
    mediaId: i.mediaId as string,
    from: i.from,
    durationInFrames: i.durationInFrames,
    sourceStart: i.sourceStart as number,
    sourceEnd: i.sourceEnd as number,
    speed: (i.speed as number | undefined) ?? 1,
  }
}

function seg(key: string, items: AnyItem[], ready = true) {
  return {
    index: 0,
    start_frame: items[0]!.from,
    frames: items.reduce((s, i) => s + i.durationInFrames, 0),
    key,
    file: `${key}.mp4`,
    ready,
    items: items.map(sig),
  }
}

function manifest(segments: ReturnType<typeof seg>[]): RenderCacheManifest {
  return { version: 1, fps: 30, canvas: [1920, 1080], segments }
}

const urls = new Map([
  ['k1', 'blob:k1'],
  ['k2', 'blob:k2'],
])

function run(t: ReturnType<typeof tracks>, m: RenderCacheManifest, extra: Partial<Parameters<typeof applyRenderCache>[0]> = {}) {
  return applyRenderCache({ tracks: t, keyframes: [], transitions: [], manifest: m, segmentUrls: urls, ...extra })
}

describe('applyRenderCache', () => {
  it('replaces the covered V1 items with one silent synthetic item and keeps audio', () => {
    const a = vid('a', 0, 15)
    const b = vid('b', 15, 15)
    const c = vid('c', 30, 15)
    const t = tracks([a, b, c])
    const res = run(t, manifest([seg('k1', [a, b])]))

    expect(res.usedSegments).toEqual(['k1'])
    const v1 = res.tracks.find((tr) => tr.id === V1)!
    expect(v1.items.map((i) => i.id)).toEqual(['rc-k1', 'c'])
    const rc = v1.items[0] as AnyItem
    expect(rc).toMatchObject({
      type: 'video',
      trackId: V1,
      from: 0,
      durationInFrames: 30,
      src: 'blob:k1',
      sourceStart: 0,
      sourceEnd: 30,
      sourceFps: 30,
      speed: 1,
    })
    for (const key of ['mediaId', 'linkedGroupId', 'audioSrc']) expect(key in rc).toBe(false)
    expect(res.tracks.find((tr) => tr.id === 'track-a1')!.items).toEqual(t[2]!.items)
    expect(t[0]!.items).toHaveLength(3) // input not mutated
  })

  it('uses several segments and leaves other video tracks untouched', () => {
    const a = vid('a', 0, 15)
    const b = vid('b', 15, 15)
    const title = vid('title', 0, 30, { trackId: 'track-v2', linkedGroupId: undefined })
    const res = run(tracks([a, b], undefined, [title]), manifest([seg('k1', [a]), seg('k2', [b])]))
    expect(res.usedSegments).toEqual(['k1', 'k2'])
    expect(res.tracks[0]!.items.map((i) => i.id)).toEqual(['rc-k1', 'rc-k2'])
    expect(res.tracks[1]!.items).toEqual([title])
  })

  it('ignores segments that are not ready or have no URL', () => {
    const a = vid('a', 0, 15)
    const t = tracks([a])
    expect(run(t, manifest([seg('k1', [a], false)])).usedSegments).toEqual([])
    const res = run(t, manifest([seg('k9', [a])]))
    expect(res.usedSegments).toEqual([])
    expect(res.tracks).toEqual(t)
  })

  it('ignores stale segments whose items no longer match the timeline', () => {
    const a = vid('a', 0, 15)
    const b = vid('b', 15, 15)
    const m = manifest([seg('k1', [a, b])])
    const trimmed = tracks([a, vid('b', 15, 10)])
    expect(run(trimmed, m).usedSegments).toEqual([])
    const moved = tracks([a, vid('b', 16, 15)])
    expect(run(moved, m).usedSegments).toEqual([])
    const reTimed = tracks([a, vid('b', 15, 15, { sourceStart: 11, sourceEnd: 26 })])
    expect(run(reTimed, m).usedSegments).toEqual([])
    const extra = tracks([a, b, vid('x', 20, 5)])
    expect(run(extra, m).usedSegments).toEqual([])
  })

  it.each([
    ['effects', { effects: [{ id: 'e1' }] }],
    ['transform', { transform: { x: 5 } }],
    ['crop', { crop: { left: 0.1 } }],
    ['fadeIn', { fadeIn: 0.5 }],
    ['fadeOut', { fadeOut: 0.5 }],
    ['reversed', { isReversed: true }],
    ['blend mode', { blendMode: 'screen' }],
    ['corner pin', { cornerPin: { tl: [0, 0] } }],
    ['motion', { motionModifiers: [{ id: 'm' }] }],
  ])('ignores a segment whose item has %s', (_name, extra) => {
    const a = vid('a', 0, 15, extra)
    expect(run(tracks([a]), manifest([seg('k1', [a])])).usedSegments).toEqual([])
  })

  it('accepts neutral values of optional properties', () => {
    const a = vid('a', 0, 15, { blendMode: 'normal', fadeIn: 0, effects: [], motionModifiers: [] })
    expect(run(tracks([a]), manifest([seg('k1', [a])])).usedSegments).toEqual(['k1'])
  })

  it('ignores items with keyframes, FreeCut transitions, or no linked audio companion', () => {
    const a = vid('a', 0, 15)
    const b = vid('b', 15, 15)
    const m = manifest([seg('k1', [a, b])])
    expect(run(tracks([a, b]), m, { keyframes: [{ itemId: 'b' }] }).usedSegments).toEqual([])
    expect(
      run(tracks([a, b]), m, { transitions: [{ leftClipId: 'a', rightClipId: 'b' }] }).usedSegments,
    ).toEqual([])
    expect(run(tracks([a, b], [aud(a)]), m).usedSegments).toEqual([])
  })
})
```
**Expected red:** module not found.
**Commit:** `feat(teamtrau): render-cache playback substitution`.

## P4.2 — Wire the render cache into the preview, then measure (Studio repo)

**Rules**
- **Loader:** `src/features/teamtrau/render-cache/loader.ts`.
  - `loadRenderCache(root, projectId)` reads `.teamtrau/render-cache/{projectId}/render_cache.json` through the directory handle.
  - It returns `{ manifest, urls }`, with blob URLs created from `getFile()` for ready segments.
  - A missing manifest returns null. A manifest with `version !== 1` throws.
  - Revoke old blob URLs when the manifest is reloaded.
  - Write your own tests with a fake handle.
- **Store:** a small Zustand store `useRenderCacheStore` (`enabled`, default true; `manifest`; `urls`; `status`; `lastError`), in `features/teamtrau`.
- **Hook (the one named FreeCut edit):**
  - `buildPreviewCompositionData` gets an optional param `renderCacheTransform?: (tracks) => tracks`. It is applied to the **playback** track list only, after `src` resolution.
  - `playbackSpans` for substituted items come from the transformed tracks. `fastScrub*` stays untouched.
  - Thread the transform in from the preview model through `src/features/preview/deps/teamtrau.ts`, so layer boundaries pass.
  - Check `utils/render-pump-preseek.ts:51` and make preseek use the substituted `src` for frames inside a used segment.
- **Command:** a "Render preview (engine)" action in the TeamTrau sidebar or the preview toolbar (smallest UI that fits).
  - It calls `ensureWorkspaceId`, then `teamtrauHost.resolveWorkspacePath`, then `engine.submitJob('render_cache', { workspace, project_id })`, then `waitForJob` with progress.
  - Then it reloads the cache.
  - Outside Electron, the button is disabled with a tooltip.
  - Any project edit leaves stale segments simply unused (P4.1 validation), so no invalidation code is needed.
- **Measurement (paste raw numbers in HANDOFF and in `docs/teamtrau/P4.2-playback.md`):**
  - Use the S0.4 harness (`scripts/spikes/verify_s04_cuts.mjs`), with the timelines T1 (12 cuts at 60 fps), T2 (30 one-second cuts at 60 fps) and T3 (12 cuts at 120 fps).
  - Each timeline is written by the engine, then render-cached through the new command, then played 30 s in Electron with `?previewPerfPanel`.
  - Report dropped %, max stall, and A/V offset by ear (clap / tick sample).
  - Targets (spec §5): 60 fps at **≤ 1 %** dropped and max stall ≤ 50 ms; 120 fps at **≤ 3 %**.
  - R14 applies: report misses as misses.

**Files:**
- `src/features/teamtrau/render-cache/{loader.ts,store.ts,index.ts}` + tests
- `src/features/teamtrau/components/render-preview-button.tsx`
- `src/features/preview/hooks/use-preview-composition-model.ts` (param only)
- `src/features/preview/deps/teamtrau.ts`
- `src/features/preview/utils/render-pump-preseek.ts` (if needed)
- the one mount point for the button
- `docs/teamtrau/P4.2-playback.md`

**Commit:** `feat(teamtrau): render-cache playback in the preview + measurements`.

**Phase 4 gate (G4, owner):**
- Numbers meet or miss the targets.
- Audio stays in sync.
- Editing a clip falls back to the live preview instantly.

**Later in Phase 4 (outline, detailed after G4):**
- 4.3 Auto-proxy on import through the engine (`webUtils.getPathForFile` in the preload).
- 4.4 Perf meter in the UI.
- 4.5 Background re-render of stale segments.
