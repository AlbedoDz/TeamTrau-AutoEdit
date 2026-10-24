# TeamTrau Studio v3 — Phase 1: Engine service (detailed)

> **Gate G0 decision (owner, 2026-10-24):** GO **hybrid** — FreeCut GUI for editing + engine NVENC **render cache** for smooth playback of cut-dense timelines (S0.4: live FreeCut playback through cuts misses the target even with the premount fix; an all-intra render of the same timeline plays with 0.67 % dropped / 0 ms stall at 60 fps).
> **Spec:** `docs/superpowers/specs/2026-10-24-teamtrau-studio-v3-design.md` · **Master plan:** `docs/superpowers/plans/2026-10-24-teamtrau-studio-v3.md`
> **Process:** identical to auto-curation v2 (MASTER PROMPT R1–R12, locked contract tests, one task per Antigravity conversation, HANDOFF file, architect review). Prompts: `.claude/orchestrator/P1.x.prompt.md`.

Branch: **`feature/studio-engine`**, created in P1.0 from `spike/s0` (keeps the spike history; spike modules are promoted to production by the tasks below, never used as-is).

Order: P1.0 → P1.1 → P1.2 → P1.3 → P1.4 → P1.5 → P1.6 (each depends on the previous).

Global rules added for Phase 1 (on top of R1–R12 and plan v3 global rules):
- **Every** `subprocess` call has a timeout; every harness/integration helper has a timeout. Nothing may hang.
- Report honestly: a target is "met" only if the measured number satisfies it (lesson from S0.3).
- No new dependencies (starlette, uvicorn, httpx are already installed).
- Never print or commit secrets (the git remote URL contains a token; never paste `git remote -v` or AG2 metadata output).

---

## P1.0 — Hygiene

1. `git checkout -b feature/studio-engine spike/s0`.
2. Commit the architect documents (they are untracked on disk): `docs/superpowers/specs/2026-10-24-teamtrau-studio-v3-design.md`, `docs/superpowers/plans/2026-10-24-teamtrau-studio-v3.md`, `docs/superpowers/plans/2026-10-24-teamtrau-studio-v3-phase1.md`, `docs/ORCHESTRATION.md`. Message: `docs: TeamTrau Studio v3 spec, plan, phase 1 and orchestration notes`.
3. Full suite baseline (`.venv\Scripts\python.exe -m pytest tests -q`) and record the count in the HANDOFF.
4. task.md: add a "Phase 25: TeamTrau Studio v3 — Phase 1 engine" checklist (P1.0..P1.6), P1.0 checked. Commit `chore: start studio v3 phase 1`.

---

## P1.1 — `src/core/platform_caps.py`

**Files:** create `src/core/platform_caps.py`, test `tests/test_platform_caps.py` (LOCKED). Related suites: `tests/test_config.py tests/test_nvenc_renderer.py`.

**API (exact):**
```python
OsName = Literal["windows", "macos", "linux"]
TorchDevice = Literal["cuda", "mps", "cpu"]
H264_LADDER: tuple[str, ...] = ("h264_nvenc", "h264_videotoolbox", "h264_qsv", "h264_amf", "h264_vaapi", "libx264")
HEVC_LADDER: tuple[str, ...] = ("hevc_nvenc", "hevc_videotoolbox", "hevc_qsv", "hevc_amf", "hevc_vaapi", "libx265")

@dataclass(frozen=True)
class PlatformCaps:
    os: OsName
    ffmpeg: str | None
    h264_encoders: tuple[str, ...]   # usable, in ladder order
    hevc_encoders: tuple[str, ...]
    hwaccels: tuple[str, ...]        # as listed by ffmpeg -hwaccels
    torch_device: TorchDevice
    def best_h264(self) -> str | None: ...
    def best_hevc(self) -> str | None: ...

@dataclass(frozen=True)
class ResolvePaths:
    api_dir: Path
    lib_path: Path
    exe_candidates: tuple[Path, ...]

def detect_os(platform_name: str | None = None) -> OsName: ...          # None -> sys.platform; unknown -> ValueError
def parse_ffmpeg_encoders(text: str) -> set[str]: ...
def parse_ffmpeg_hwaccels(text: str) -> tuple[str, ...]: ...
def order_encoders(available: set[str], ladder: tuple[str, ...], os_name: OsName) -> tuple[str, ...]: ...
def pick_torch_device(cuda: bool, mps: bool) -> TorchDevice: ...
def resolve_paths(os_name: OsName) -> ResolvePaths: ...
def probe(ffmpeg: str | None = ..., os_name: OsName | None = None, run=run_tool,
          smoke_test: bool = True, torch_device: TorchDevice | None = None) -> PlatformCaps: ...
def get_caps() -> PlatformCaps: ...   # functools.cache around probe()
```

**Rules:**
- `detect_os`: `win32`/`cygwin` → windows, `darwin` → macos, anything starting with `linux` → linux, else `ValueError`.
- `parse_ffmpeg_encoders`: names from the lines after the `------` separator (second whitespace token of each line).
- `parse_ffmpeg_hwaccels`: non-empty lines after the `Hardware acceleration methods:` header, order kept.
- `order_encoders`: keep ladder order; drop names not in `available`; drop names not allowed on the OS: suffix `_nvenc` → windows, linux; `_videotoolbox` → macos; `_qsv` → windows, linux; `_amf` → windows; `_vaapi` → linux; software (`libx264`, `libx265`) → all.
- `probe`: `ffmpeg` default `shutil.which("ffmpeg")`. If `ffmpeg is None` → all encoder/hwaccel tuples empty (no `run` calls). Otherwise run `[ffmpeg, "-hide_banner", "-encoders"]` and `[ffmpeg, "-hide_banner", "-hwaccels"]` (timeout 15 s each). With `smoke_test=True`, every **hardware** candidate (not libx264/libx265) is checked with a 1-frame encode `[ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.1", "-frames:v", "1", "-c:v", <encoder>, "-f", "null", "-"]` (timeout 20 s); non-zero return code or exception → dropped. `torch_device` default: lazy `import torch` → `pick_torch_device(torch.cuda.is_available(), torch.backends.mps.is_available())`, any import error → "cpu".
- `resolve_paths`: windows → api `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting`, lib `C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll`, exes `…\DaVinci Resolve\Resolve.exe` and `…\DaVinci Resolve Studio\Resolve.exe`; macos → api `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting`, lib `/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so`, exe `/Applications/DaVinci Resolve/DaVinci Resolve.app`; linux → api `/opt/resolve/Developer/Scripting`, lib `/opt/resolve/libs/Fusion/fusionscript.so`, exe `/opt/resolve/bin/resolve`.
- This task does **not** rewire existing modules (that happens where each consumer is touched).

**Contract test — `tests/test_platform_caps.py`:**
```python
"""platform_caps: OS / encoders / hwaccels / torch / Resolve paths (no real ffmpeg)."""

import pytest

from src.core import platform_caps as pc

ENCODERS_TXT = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V....D h264_qsv             H.264 (Intel Quick Sync Video acceleration) (codec h264)
 V....D h264_amf             AMD AMF H.264 Encoder (codec h264)
 V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
 V....D libx265              libx265 H.265 / HEVC (codec hevc)
 A....D aac                  AAC (Advanced Audio Coding)
"""
HWACCELS_TXT = "Hardware acceleration methods:\ncuda\ndxva2\nqsv\nd3d11va\n\n"


class _R:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_parse_encoders():
    assert pc.parse_ffmpeg_encoders(ENCODERS_TXT) == {
        "libx264", "h264_nvenc", "h264_qsv", "h264_amf", "hevc_nvenc", "libx265", "aac",
    }


def test_parse_hwaccels_keeps_order():
    assert pc.parse_ffmpeg_hwaccels(HWACCELS_TXT) == ("cuda", "dxva2", "qsv", "d3d11va")


@pytest.mark.parametrize(
    "raw, expected",
    [("win32", "windows"), ("cygwin", "windows"), ("darwin", "macos"), ("linux", "linux")],
)
def test_detect_os(raw, expected):
    assert pc.detect_os(raw) == expected


def test_detect_os_rejects_unknown():
    with pytest.raises(ValueError):
        pc.detect_os("sunos5")


def test_order_encoders_follows_ladder_and_os():
    avail = pc.parse_ffmpeg_encoders(ENCODERS_TXT) | {"h264_videotoolbox", "h264_vaapi"}
    assert pc.order_encoders(avail, pc.H264_LADDER, "windows") == (
        "h264_nvenc", "h264_qsv", "h264_amf", "libx264",
    )
    assert pc.order_encoders(avail, pc.H264_LADDER, "macos") == ("h264_videotoolbox", "libx264")
    assert pc.order_encoders(avail, pc.H264_LADDER, "linux") == (
        "h264_nvenc", "h264_qsv", "h264_vaapi", "libx264",
    )
    assert pc.order_encoders({"aac"}, pc.H264_LADDER, "windows") == ()


def test_pick_torch_device():
    assert pc.pick_torch_device(cuda=True, mps=False) == "cuda"
    assert pc.pick_torch_device(cuda=True, mps=True) == "cuda"
    assert pc.pick_torch_device(cuda=False, mps=True) == "mps"
    assert pc.pick_torch_device(cuda=False, mps=False) == "cpu"


def test_probe_smoke_tests_hardware_encoders_only():
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=None):
        calls.append(list(cmd))
        assert timeout is not None
        if "-encoders" in cmd:
            return _R(0, ENCODERS_TXT)
        if "-hwaccels" in cmd:
            return _R(0, HWACCELS_TXT)
        return _R(1 if ("h264_nvenc" in cmd or "hevc_nvenc" in cmd) else 0)

    caps = pc.probe(ffmpeg="ffmpeg", os_name="windows", run=fake_run, smoke_test=True,
                    torch_device="cpu")
    assert caps.os == "windows"
    assert caps.ffmpeg == "ffmpeg"
    assert caps.h264_encoders == ("h264_qsv", "h264_amf", "libx264")
    assert caps.hevc_encoders == ("libx265",)
    assert caps.hwaccels == ("cuda", "dxva2", "qsv", "d3d11va")
    assert caps.torch_device == "cpu"
    smoked = [c[c.index("-c:v") + 1] for c in calls if "-c:v" in c]
    assert "libx264" not in smoked and "libx265" not in smoked
    assert set(smoked) == {"h264_nvenc", "h264_qsv", "h264_amf", "hevc_nvenc"}


def test_probe_without_smoke_test_trusts_listing():
    def fake_run(cmd, timeout=None):
        if "-encoders" in cmd:
            return _R(0, ENCODERS_TXT)
        return _R(0, HWACCELS_TXT)

    caps = pc.probe(ffmpeg="ffmpeg", os_name="linux", run=fake_run, smoke_test=False,
                    torch_device="cuda")
    assert caps.h264_encoders == ("h264_nvenc", "h264_qsv", "libx264")
    assert caps.hevc_encoders == ("hevc_nvenc", "libx265")


def test_probe_without_ffmpeg():
    def boom(cmd, timeout=None):
        raise AssertionError("must not run anything without ffmpeg")

    caps = pc.probe(ffmpeg=None, os_name="linux", run=boom, smoke_test=True, torch_device="cpu")
    assert caps.ffmpeg is None
    assert (caps.h264_encoders, caps.hevc_encoders, caps.hwaccels) == ((), (), ())


def test_best_encoder_helpers():
    caps = pc.PlatformCaps(os="linux", ffmpeg="ffmpeg", h264_encoders=("h264_vaapi", "libx264"),
                           hevc_encoders=(), hwaccels=(), torch_device="cpu")
    assert caps.best_h264() == "h264_vaapi"
    assert caps.best_hevc() is None


@pytest.mark.parametrize(
    "os_name, lib_name",
    [("windows", "fusionscript.dll"), ("macos", "fusionscript.so"), ("linux", "fusionscript.so")],
)
def test_resolve_paths_per_os(os_name, lib_name):
    paths = pc.resolve_paths(os_name)
    assert paths.lib_path.name == lib_name
    assert paths.api_dir.name == "Scripting"
    assert len(paths.exe_candidates) >= 1
```
**Expected red:** `ImportError: cannot import name 'platform_caps'`. **Commit:** `feat(platform): platform_caps (OS, encoders, hwaccels, torch, Resolve paths)`.

---

## P1.2 — Production proxy generator (`src/modules/proxy_generator.py`)

Rewrite the spike module to this API (spike tests may be replaced by the locked file below; delete the spike-only helpers that the new API supersedes).

**API (exact):**
```python
class ProxyGenerationError(RuntimeError): ...

@dataclass(frozen=True)
class ProxySpec:
    height: int = 1080
    fps: float | None = None   # None = keep the source rate (still CFR)
    gop: int = 1               # 1 = all-intra

def needs_proxy(info: MediaInfo, max_side: int = 1920) -> list[str]: ...   # reasons, [] = none
def compute_proxy_key(src: Path | str, spec: ProxySpec = ProxySpec(), *,
                      size: int | None = None, mtime_ns: int | None = None) -> str: ...
def build_proxy_cmd(src: Path | str, dst_part: Path | str, encoder: str, spec: ProxySpec,
                    ffmpeg: str = "ffmpeg") -> list[str]: ...
def make_proxy(src: Path | str, out_dir: Path | str, spec: ProxySpec = ProxySpec(), *,
               encoders: Sequence[str] | None = None, run=run_tool, ffmpeg: str | None = None,
               timeout: float = 3600.0) -> Path: ...
```
**Rules:**
- `needs_proxy` reasons in this order when they apply: `"resolution"` (max(width, height) > `max_side`), `f"codec:{codec}"` for codec in {hevc, prores, dnxhd, av1, vp9}, `"bit_depth"` (bit_depth > 8), `"chroma"` (pix_fmt contains "422" or "444"), `"high_fps"` (fps > 60.5), `"vfr"`, `"log"`.
- Key: `sha1(f"{resolved path}|{size}|{mtime_ns}|{height}|{fps}|{gop}")[:16]` (size/mtime_ns from `stat()` when not given; missing file → 0).
- Command: `[ffmpeg, "-y", "-hide_banner", "-v", "error", "-i", src, "-vf", f"scale=-2:{height}"]`, then `"-r", f"{fps:g}"` only when `spec.fps` is set, then `"-fps_mode", "cfr", "-pix_fmt", "yuv420p", "-bf", "0"`, then the encoder part, then `"-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", "-f", "mp4", dst_part`.
  - `h264_nvenc`: `"-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-g", "0" if gop == 1 else str(gop)` (NVENC: gop 0 = intra-only; `-g 1 -bf 0` is rejected, finding of S0.3).
  - `libx264`: `"-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", str(gop)`.
  - other hardware encoders (`*_videotoolbox`, `*_qsv`, `*_amf`, `*_vaapi`): `"-c:v", encoder, "-g", str(gop), "-b:v", "20M"`.
- `make_proxy`: output `out_dir / f"{key}.mp4"`; if it exists → return it without encoding. Otherwise try `encoders` (default `get_caps().h264_encoders or ("libx264",)`) in order, each writing `out_dir / f"{key}.mp4.part"` via `run(cmd, timeout=timeout)`; success = return code 0 **and** the part file exists → `os.replace` to the final name and return. On failure delete the part file and try the next encoder. All fail → `ProxyGenerationError` naming each encoder and its last stderr line. `ffmpeg` default `shutil.which("ffmpeg") or "ffmpeg"`.

**Contract test — `tests/test_proxy_generator.py`** (replaces the spike file):
```python
"""Production proxy generator: heuristics, cache key, commands, encoder fallback."""

from fractions import Fraction
from pathlib import Path

import pytest

from src.core.media_info import MediaInfo
from src.modules import proxy_generator as pg


class _R:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


def info(**kw) -> MediaInfo:
    base = dict(path=Path("x.mp4"), has_video=True, width=1920, height=1080, fps=Fraction(30),
                codec="h264", pix_fmt="yuv420p", bit_depth=8)
    base.update(kw)
    return MediaInfo(**base)


def _has(cmd: list[str], pair: list[str]) -> bool:
    return any(cmd[i:i + len(pair)] == pair for i in range(len(cmd)))


def test_light_1080p_h264_needs_no_proxy():
    assert pg.needs_proxy(info()) == []


@pytest.mark.parametrize(
    "kw, reason",
    [
        (dict(width=3840, height=2160), "resolution"),
        (dict(width=2160, height=3840), "resolution"),
        (dict(codec="hevc"), "codec:hevc"),
        (dict(codec="prores"), "codec:prores"),
        (dict(bit_depth=10, pix_fmt="yuv420p10le"), "bit_depth"),
        (dict(bit_depth=10, pix_fmt="yuv422p10le"), "chroma"),
        (dict(fps=Fraction(120000, 1001)), "high_fps"),
        (dict(is_vfr=True), "vfr"),
        (dict(is_log=True), "log"),
    ],
)
def test_heavy_media_reasons(kw, reason):
    assert reason in pg.needs_proxy(info(**kw))


def test_reason_order():
    heavy = info(width=3840, height=2160, codec="hevc", bit_depth=10, pix_fmt="yuv422p10le")
    assert pg.needs_proxy(heavy) == ["resolution", "codec:hevc", "bit_depth", "chroma"]


def test_key_is_stable_and_parameter_sensitive(tmp_path: Path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    k = pg.compute_proxy_key(f)
    assert len(k) == 16 and int(k, 16) >= 0
    assert pg.compute_proxy_key(f) == k
    assert pg.compute_proxy_key(f, pg.ProxySpec(height=720)) != k
    assert pg.compute_proxy_key(f, pg.ProxySpec(gop=6)) != k
    assert pg.compute_proxy_key(f, pg.ProxySpec(fps=60.0)) != k
    assert pg.compute_proxy_key(f, size=999) != k
    assert pg.compute_proxy_key(f, mtime_ns=1) != k


def test_nvenc_intra_command(tmp_path: Path):
    cmd = pg.build_proxy_cmd(tmp_path / "in.mov", tmp_path / "out.mp4.part", "h264_nvenc",
                             pg.ProxySpec(fps=60.0))
    assert cmd[0] == "ffmpeg"
    assert "scale=-2:1080" in cmd
    for pair in (["-c:v", "h264_nvenc"], ["-g", "0"], ["-bf", "0"], ["-pix_fmt", "yuv420p"],
                 ["-fps_mode", "cfr"], ["-r", "60"], ["-c:a", "aac"], ["-ar", "48000"],
                 ["-ac", "2"], ["-f", "mp4"]):
        assert _has(cmd, pair), pair
    assert cmd[-1].endswith("out.mp4.part")


def test_libx264_and_gop_variants(tmp_path: Path):
    cpu = pg.build_proxy_cmd(tmp_path / "in.mov", tmp_path / "o.part", "libx264", pg.ProxySpec())
    assert _has(cpu, ["-c:v", "libx264"]) and _has(cpu, ["-g", "1"]) and _has(cpu, ["-bf", "0"])
    assert _has(cpu, ["-preset", "veryfast"])
    assert "-r" not in cpu and _has(cpu, ["-fps_mode", "cfr"])
    gop6 = pg.build_proxy_cmd(tmp_path / "in.mov", tmp_path / "o.part", "h264_nvenc",
                              pg.ProxySpec(height=720, gop=6))
    assert _has(gop6, ["-g", "6"]) and "scale=-2:720" in gop6


def test_make_proxy_falls_back_and_renames(tmp_path: Path):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"v")
    tried: list[str] = []

    def fake_run(cmd, timeout=None):
        assert timeout is not None
        tried.append(cmd[cmd.index("-c:v") + 1])
        if "h264_nvenc" in cmd:
            return _R(1, "nvenc not available")
        Path(cmd[-1]).write_bytes(b"proxy")
        return _R(0)

    out = pg.make_proxy(src, tmp_path / "px", encoders=("h264_nvenc", "libx264"), run=fake_run)
    assert tried == ["h264_nvenc", "libx264"]
    assert out == tmp_path / "px" / f"{pg.compute_proxy_key(src)}.mp4"
    assert out.read_bytes() == b"proxy"
    assert not list((tmp_path / "px").glob("*.part"))


def test_make_proxy_reuses_existing(tmp_path: Path):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"v")
    out_dir = tmp_path / "px"
    out_dir.mkdir()
    existing = out_dir / f"{pg.compute_proxy_key(src)}.mp4"
    existing.write_bytes(b"old")

    def boom(cmd, timeout=None):
        raise AssertionError("must not encode again")

    assert pg.make_proxy(src, out_dir, encoders=("libx264",), run=boom) == existing


def test_make_proxy_all_encoders_fail(tmp_path: Path):
    src = tmp_path / "clip.mov"
    src.write_bytes(b"v")

    def failing(cmd, timeout=None):
        Path(cmd[-1]).write_bytes(b"half")
        return _R(1, "Error while encoding")

    with pytest.raises(pg.ProxyGenerationError) as exc:
        pg.make_proxy(src, tmp_path / "px", encoders=("h264_nvenc", "libx264"), run=failing)
    assert "h264_nvenc" in str(exc.value) and "libx264" in str(exc.value)
    assert not list((tmp_path / "px").glob("*.part"))
```

**Contract test — `tests/integration/test_proxy_generator_real.py`:**
```python
"""Real ffmpeg: all-intra CFR proxy with stereo 48 kHz audio."""

import shutil
import subprocess
from pathlib import Path

from src.modules import proxy_generator as pg

FFPROBE = shutil.which("ffprobe")


def _probe(path: Path, *args: str) -> str:
    return subprocess.run([FFPROBE, "-v", "error", *args, str(path)], capture_output=True,
                          text=True, check=True, timeout=60).stdout


def test_libx264_all_intra_proxy(make_clip, tmp_path: Path):
    src = make_clip("Lễ src.mp4", duration=2.0, size="640x360")
    out = pg.make_proxy(src, tmp_path / "proxies", pg.ProxySpec(height=240), encoders=("libx264",))
    assert out.is_file() and out.suffix == ".mp4"
    types = {t.strip(",") for t in _probe(out, "-select_streams", "v:0", "-show_entries",
                                          "frame=pict_type", "-of", "csv=p=0").split()}
    assert types == {"I"}
    v = _probe(out, "-select_streams", "v:0", "-show_entries",
               "stream=height,r_frame_rate,avg_frame_rate", "-of", "csv=p=0").strip().split(",")
    assert v[0] == "240" and v[1] == v[2]
    a = _probe(out, "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels",
               "-of", "csv=p=0").strip().split(",")
    assert a == ["48000", "2"]
```
**Expected red:** `AttributeError`/`ImportError` on `ProxySpec` / `needs_proxy` (spike API differs). **Commit:** `feat(proxy): production proxy generator (heuristics, caps-driven encoder fallback, atomic output)`.

---

## P1.3 — Production FreeCut writer (`src/integrations/freecut_writer.py`)

Harden the spike writer. Public API:
```python
def write_freecut_project(timeline: EditTimeline, workspace: Path, project_name: str,
                          proxies: Mapping[Path, Path] | None = None) -> str: ...
```
**Rules (in addition to the S0.2 contract doc `docs/spikes/S0.2-freecut-contract.md`):**
- Every JSON file is written atomically (`<name>.tmp` then `os.replace`); no `*.tmp` may remain.
- Each call creates a **new** project id `p-<uuid4>`; existing projects are never modified; `index.json` keeps all ids.
- Every registered media gets `media/{id}/source.link.json` = `{"version": 1, "path": "<absolute original path>"}` (used by the reader in P1.4). Media files themselves: hard link → symlink → copy (S0.3 ladder) into `media/{id}/{sanitized name}`.
- Video items: `type "video"`, track `track-v1`, `from` (cumulative frames), `durationInFrames == clip.frames`, `sourceStart = round(clip.source_in * source_fps)`, `sourceEnd = sourceStart + round(clip.frames * clip.speed * source_fps / timeline.fps)`, `src` absent or `""`, `mediaId`. Clips whose media has audio also get a linked `type "audio"` item on `track-a1` with the same `from`, `durationInFrames` and `linkedGroupId`.
- `proxies[src]` → `content/proxies/{key}/proxy.mp4` (link ladder) + `meta.json` `{"version": 4, "status": "ready", "codec": "avc", width, height, sourceWidth, sourceHeight, createdAt}`, and `contentHash = key` in that media's `metadata.json`; `key = compute_proxy_key(src)`.
- Regenerate the Studio fixture `src/features/teamtrau/__tests__/fixtures/engine-project.json` from the new writer and keep the Studio test green (`npx vp test run src/features/teamtrau` in `E:\antigravity\teamtrau-studio`, branch `teamtrau/engine` created from `spike/s0-cuts`). That Studio commit is the only Studio change allowed in P1.3.

**Contract test — `tests/integration/test_freecut_writer_real.py`:**
```python
"""FreeCut writer on real media: layout, frame math, linked audio, proxies, atomicity."""

import json
import re
from pathlib import Path

from src.core.types import EditClip, EditTimeline
from src.integrations.freecut_writer import write_freecut_project
from src.modules.proxy_generator import compute_proxy_key


def _tl(paths: list[Path], soundtrack: Path | None = None) -> EditTimeline:
    clips = [EditClip(source_path=p, source_in=0.5, frames=45, available=1.5) for p in paths]
    return EditTimeline(name="t", fps=30.0, canvas_width=1920, canvas_height=1080, clips=clips,
                        soundtrack_path=soundtrack)


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def test_project_layout_and_frame_math(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=3.0)
    b = make_clip("b clip Việt.mp4", duration=3.0)
    ws = tmp_path / "ws"
    pid = write_freecut_project(_tl([a, b, a]), ws, "Demo")
    assert re.fullmatch(r"p-[0-9a-f-]{36}", pid)
    proj = _load(ws / "projects" / pid / "project.json")
    assert proj["schemaVersion"] == 15 and proj["id"] == pid and proj["name"] == "Demo"
    items = proj["timeline"]["items"]
    video = sorted((i for i in items if i["type"] == "video"), key=lambda i: i["from"])
    assert [v["from"] for v in video] == [0, 45, 90]
    assert all(v["durationInFrames"] == 45 and v.get("src", "") == "" for v in video)
    assert all(v["sourceStart"] == 15 and v["sourceEnd"] == 60 for v in video)
    media_ids = {v["mediaId"] for v in video}
    assert len(media_ids) == 2
    for mid in media_ids:
        assert (ws / "media" / mid / "metadata.json").is_file()
        link = _load(ws / "media" / mid / "source.link.json")
        assert Path(link["path"]).resolve() in {a.resolve(), b.resolve()}
    audio = [i for i in items if i["type"] == "audio"]
    assert sorted(x["linkedGroupId"] for x in audio) == sorted(v["linkedGroupId"] for v in video)
    index = _load(ws / "index.json")
    assert pid in [p["id"] for p in index["projects"]]
    assert (ws / ".freecut-workspace.json").is_file()
    assert not list(ws.rglob("*.tmp"))


def test_new_project_each_call(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=2.0)
    ws = tmp_path / "ws"
    p1 = write_freecut_project(_tl([a]), ws, "One")
    p2 = write_freecut_project(_tl([a]), ws, "Two")
    assert p1 != p2
    ids = [p["id"] for p in _load(ws / "index.json")["projects"]]
    assert p1 in ids and p2 in ids
    assert _load(ws / "projects" / p1 / "project.json")["name"] == "One"


def test_proxy_is_adoptable(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=2.0)
    px = make_clip("a_proxy.mp4", duration=2.0, size="160x90")
    ws = tmp_path / "ws"
    pid = write_freecut_project(_tl([a]), ws, "Px", proxies={a: px})
    key = compute_proxy_key(a)
    pdir = ws / "content" / "proxies" / key
    assert (pdir / "proxy.mp4").is_file()
    meta = _load(pdir / "meta.json")
    assert (meta["version"], meta["status"], meta["codec"]) == (4, "ready", "avc")
    video = [i for i in _load(ws / "projects" / pid / "project.json")["timeline"]["items"]
             if i["type"] == "video"]
    md = _load(ws / "media" / video[0]["mediaId"] / "metadata.json")
    assert md["contentHash"] == key
```
**Expected red:** `source.link.json` missing / `sourceEnd` mismatch (whatever the spike gets wrong) — report the exact failures. **Commit:** `feat(integrations): production FreeCut writer (atomic, source links, proxy adoption)`.

---

## P1.4 — FreeCut reader (`src/integrations/freecut_reader.py`)

Needed so the engine can render-cache the timeline the user edits in Studio.

**API:**
```python
@dataclass(frozen=True)
class FreeCutTimeline:
    timeline: EditTimeline          # V1 (top-most video track) items in time order
    starts: tuple[int, ...]         # timeline start frame of each clip (gaps allowed)
    project_id: str

def read_freecut_project(workspace: Path, project_id: str) -> FreeCutTimeline: ...
```
**Rules:** read `projects/{id}/project.json`; `schemaVersion` > 15 → `ValueError("unsupported schema ...")`; fps/canvas from `metadata`; take video items of the **first** video track in track `order`, sorted by `from`; clip `source_path` from `media/{mediaId}/source.link.json` `path` when present, else `media/{mediaId}/{fileName}` from `metadata.json`; `source_in = sourceStart / sourceFps`, `frames = durationInFrames`, `speed = item.get("speed", 1.0)`, `available = (sourceEnd - sourceStart) / sourceFps`; items whose media is missing → `ValueError` naming the media id.

**Contract test — `tests/integration/test_freecut_reader_real.py`:**
```python
"""Writer -> reader round trip on real media."""

import json
from pathlib import Path

import pytest

from src.core.types import EditClip, EditTimeline
from src.integrations.freecut_reader import read_freecut_project
from src.integrations.freecut_writer import write_freecut_project


def _tl(clips: list[EditClip]) -> EditTimeline:
    return EditTimeline(name="t", fps=30.0, canvas_width=1920, canvas_height=1080, clips=clips)


def test_round_trip(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=3.0)
    b = make_clip("b.mp4", duration=3.0)
    clips = [EditClip(a, 0.5, 45, 1.5), EditClip(b, 1.0, 30, 1.0), EditClip(a, 2.0, 15, 0.5)]
    ws = tmp_path / "ws"
    pid = write_freecut_project(_tl(clips), ws, "RT")
    got = read_freecut_project(ws, pid)
    assert got.project_id == pid
    assert got.timeline.fps == 30.0
    assert (got.timeline.canvas_width, got.timeline.canvas_height) == (1920, 1080)
    assert got.starts == (0, 45, 75)
    assert [(Path(c.source_path).resolve(), round(c.source_in, 3), c.frames)
            for c in got.timeline.clips] == [(a.resolve(), 0.5, 45), (b.resolve(), 1.0, 30),
                                              (a.resolve(), 2.0, 15)]


def test_reader_sorts_by_from_and_keeps_gaps(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=3.0)
    ws = tmp_path / "ws"
    two = [EditClip(a, 0.0, 30, 1.0), EditClip(a, 1.0, 30, 1.0)]
    pid = write_freecut_project(_tl(two), ws, "G")
    pfile = ws / "projects" / pid / "project.json"
    proj = json.loads(pfile.read_text(encoding="utf-8"))
    video = [i for i in proj["timeline"]["items"] if i["type"] == "video"]
    video[1]["from"] = 60
    proj["timeline"]["items"].reverse()
    pfile.write_text(json.dumps(proj), encoding="utf-8")
    assert read_freecut_project(ws, pid).starts == (0, 60)


def test_reader_rejects_newer_schema(tmp_path: Path, make_clip):
    a = make_clip("a.mp4", duration=2.0)
    ws = tmp_path / "ws"
    pid = write_freecut_project(_tl([EditClip(a, 0.0, 30, 1.0)]), ws, "S")
    pfile = ws / "projects" / pid / "project.json"
    proj = json.loads(pfile.read_text(encoding="utf-8"))
    proj["schemaVersion"] = 99
    pfile.write_text(json.dumps(proj), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        read_freecut_project(ws, pid)
```
**Expected red:** `ModuleNotFoundError: src.integrations.freecut_reader`. **Commit:** `feat(integrations): FreeCut project reader (V1 timeline, gaps, schema guard)`.

---

## P1.5 — Render cache (`src/modules/render_cache.py`) + intra-only renderer option

**Renderer change (`src/dispatchers/nvenc_renderer.py`, minimal):** `NvencRenderer.__init__(self, config, settings=None, intra_only: bool = False)`. `intra_only=True` requires `settings is not None and settings.master == "h264"` else `ValueError`. In `master_encoder_args` for master `h264`: when `self.intra_only`, append `"-g", "0", "-bf", "0"` to the `h264_nvenc` args and `"-g", "1", "-bf", "0"` to the `libx264` args. Nothing else changes.

**Render cache API:**
```python
RENDER_CACHE_VERSION = 1
MANIFEST_NAME = "render_cache.json"

@dataclass(frozen=True)
class Segment:
    index: int
    start_frame: int
    frames: int
    clip_indices: tuple[int, ...]
    key: str

@dataclass(frozen=True)
class CacheStatus:
    segment: Segment
    path: Path        # cache_dir / f"{key}.mp4"
    ready: bool

def segment_key(timeline: EditTimeline, clips: Sequence[EditClip]) -> str: ...
def plan_segments(timeline: EditTimeline, max_segment_sec: float = 10.0,
                  starts: Sequence[int] | None = None) -> list[Segment]: ...
def cache_status(timeline: EditTimeline, cache_dir: Path, max_segment_sec: float = 10.0,
                 starts: Sequence[int] | None = None) -> list[CacheStatus]: ...
def render_missing(timeline: EditTimeline, cache_dir: Path, *, renderer=None,
                   config: PipelineConfig | None = None, max_segment_sec: float = 10.0,
                   starts: Sequence[int] | None = None, force_cpu: bool = False,
                   progress: Callable[[float, str], None] | None = None,
                   cancel_event: threading.Event | None = None) -> list[Path]: ...
def write_manifest(timeline: EditTimeline, cache_dir: Path, max_segment_sec: float = 10.0,
                   starts: Sequence[int] | None = None) -> Path: ...
```
**Rules:**
- `segment_key`: sha1 of canonical JSON (sorted keys) of `{"v": RENDER_CACHE_VERSION, "fps": timeline.fps, "canvas": [w, h], "clips": [...]}`, each clip = `{"path": str(resolved source_path), "size": st_size, "mtime_ns": st_mtime_ns, "in": round(source_in, 6), "frames", "speed", "crop": asdict(crop_window) or None, "kb": ken_burns, "tr": transition_in_frames}`. Labels, markers, scene_start/end, is_teaser and timeline position are **excluded** (ripple edits keep keys). Full 40-hex digest.
- `plan_segments`: start positions = `starts` if given else cumulative frames. Greedy: add clips to the current segment while total ≤ `max_segment_sec * fps`; a gap (start ≠ previous start + previous frames) always starts a new segment; a clip with `transition_in_frames > 0` must stay in the same segment as the previous clip — if a split is needed right before such a clip, move the whole dissolve chain (the clip plus the run of preceding clips linked by dissolves) to the new segment; if that chain is the entire current segment, exceed the limit instead. A single clip longer than the limit is its own segment.
- `render_missing`: for each not-ready segment build a sub-timeline (same fps/canvas, `soundtrack_path=None`, only that segment's clips) and call `renderer.render_edit_timeline(sub, cache_dir / f"{key}.part.mp4", force_cpu=force_cpu, cancel_event=cancel_event)`, then `os.replace` to `{key}.mp4`; return the rendered final paths in segment order. Default renderer: `NvencRenderer(config or PipelineConfig(), settings=DeliverSettings(canvas_width=w, canvas_height=h, fps=fps, master="h264"), intra_only=True)`. Remove a leftover `.part.mp4` on failure and re-raise.
- `write_manifest`: `cache_dir / MANIFEST_NAME` = `{"version": 1, "fps", "canvas": [w, h], "segments": [{"index", "start_frame", "frames", "key", "file": "<key>.mp4", "ready": bool}]}`, written atomically.

**Contract test — `tests/test_render_cache.py`:**
```python
"""Render cache planning, keys, status, rendering orchestration (fake renderer)."""

import json
import os
from pathlib import Path

import pytest

from src.core.config import PipelineConfig
from src.core.types import CropWindow, DeliverSettings, EditClip, EditTimeline, RenderJobResult
from src.dispatchers.nvenc_renderer import NvencRenderer
from src.modules import render_cache as rc


def _touch(p: Path) -> Path:
    p.write_bytes(b"media")
    return p


def clip(p: Path, frames: int = 30, source_in: float = 0.0, **kw) -> EditClip:
    return EditClip(source_path=p, source_in=source_in, frames=frames, available=frames / 30, **kw)


def tl(clips: list[EditClip], fps: float = 30.0, w: int = 1920, h: int = 1080) -> EditTimeline:
    return EditTimeline(name="t", fps=fps, canvas_width=w, canvas_height=h, clips=clips)


def _has(cmd: list[str], pair: list[str]) -> bool:
    return any(cmd[i:i + len(pair)] == pair for i in range(len(cmd)))


def test_intra_only_h264_args():
    s = DeliverSettings(canvas_width=1920, canvas_height=1080, fps=60.0, master="h264")
    r = NvencRenderer(PipelineConfig(), settings=s, intra_only=True)
    _, nv = r.master_encoder_args(s, True)
    assert _has(nv, ["-g", "0"]) and _has(nv, ["-bf", "0"])
    _, cpu = r.master_encoder_args(s, False)
    assert _has(cpu, ["-g", "1"]) and _has(cpu, ["-bf", "0"])
    _, plain = NvencRenderer(PipelineConfig(), settings=s).master_encoder_args(s, True)
    assert "-g" not in plain


def test_intra_only_requires_h264_settings():
    hevc = DeliverSettings(canvas_width=1920, canvas_height=1080, fps=30.0, master="hevc")
    with pytest.raises(ValueError):
        NvencRenderer(PipelineConfig(), settings=hevc, intra_only=True)
    with pytest.raises(ValueError):
        NvencRenderer(PipelineConfig(), settings=None, intra_only=True)


def test_segments_group_cuts_up_to_limit(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    segs = rc.plan_segments(tl([clip(f, 90) for _ in range(5)]), max_segment_sec=10.0)
    assert [s.clip_indices for s in segs] == [(0, 1, 2), (3, 4)]
    assert [(s.start_frame, s.frames) for s in segs] == [(0, 270), (270, 180)]
    assert [s.index for s in segs] == [0, 1]


def test_long_clip_is_its_own_segment(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    segs = rc.plan_segments(tl([clip(f, 600), clip(f, 30), clip(f, 30)]), max_segment_sec=10.0)
    assert [s.clip_indices for s in segs] == [(0,), (1, 2)]


def test_dissolve_is_never_split(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    clips = [clip(f, 120), clip(f, 120), clip(f, 120, transition_in_frames=10)]
    assert [s.clip_indices for s in rc.plan_segments(tl(clips), 10.0)] == [(0,), (1, 2)]


def test_gaps_force_a_boundary(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    segs = rc.plan_segments(tl([clip(f, 30), clip(f, 30)]), 10.0, starts=[0, 60])
    assert [(s.clip_indices, s.start_frame) for s in segs] == [((0,), 0), ((1,), 60)]


def test_key_ignores_position_and_labels(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    g = _touch(tmp_path / "b.mp4")
    seg = [clip(f, 30, 1.0)]
    k = rc.segment_key(tl(seg), seg)
    assert len(k) == 40
    assert rc.segment_key(tl([clip(g, 99), *seg]), seg) == k
    labelled = [clip(f, 30, 1.0, label="x", markers=[{"t": 1}], scene_start=3.0)]
    assert rc.segment_key(tl(labelled), labelled) == k


def test_key_changes_with_pixels(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    base = [clip(f, 30, 1.0)]
    k = rc.segment_key(tl(base), base)
    variants = [
        [clip(f, 31, 1.0)],
        [clip(f, 30, 1.5)],
        [clip(f, 30, 1.0, speed=2.0)],
        [clip(f, 30, 1.0, crop_window=CropWindow(x=0, y=0, width=608, height=1080))],
        [clip(f, 30, 1.0, transition_in_frames=5)],
    ]
    for v in variants:
        assert rc.segment_key(tl(v), v) != k
    assert rc.segment_key(tl(base, fps=60.0), base) != k
    assert rc.segment_key(tl(base, w=1080, h=1920), base) != k
    os.utime(f, ns=(1_000_000_000, 1_000_000_000))
    assert rc.segment_key(tl(base), base) != k


class FakeRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object, Path]] = []

    def render_edit_timeline(self, edit_tl, output_file, force_cpu=False, **kw):
        out = Path(output_file)
        self.calls.append((len(edit_tl.clips), edit_tl.soundtrack_path, out))
        out.write_bytes(b"seg")
        return RenderJobResult(output_path=out, encoder_used="fake", duration=1.0,
                               file_size_bytes=3)


def test_status_and_render_missing(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    t = tl([clip(f, 90) for _ in range(5)])
    t.soundtrack_path = tmp_path / "song.mp3"
    cache = tmp_path / "cache"
    st = rc.cache_status(t, cache, max_segment_sec=10.0)
    assert [s.ready for s in st] == [False, False]
    assert all(s.path == cache / f"{s.segment.key}.mp4" for s in st)
    cache.mkdir()
    st[0].path.write_bytes(b"old")
    fake = FakeRenderer()
    out = rc.render_missing(t, cache, renderer=fake, max_segment_sec=10.0)
    assert out == [st[1].path]
    assert fake.calls[0][0] == 2 and fake.calls[0][1] is None
    assert fake.calls[0][2].name.endswith(".part.mp4")
    assert st[1].path.read_bytes() == b"seg"
    assert not list(cache.glob("*.part*"))
    assert [s.ready for s in rc.cache_status(t, cache, max_segment_sec=10.0)] == [True, True]


def test_render_failure_cleans_part_file(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    t = tl([clip(f, 30)])
    cache = tmp_path / "cache"

    class Broken:
        def render_edit_timeline(self, edit_tl, output_file, force_cpu=False, **kw):
            Path(output_file).write_bytes(b"half")
            raise RuntimeError("encoder died")

    with pytest.raises(RuntimeError, match="encoder died"):
        rc.render_missing(t, cache, renderer=Broken())
    assert not list(cache.glob("*.part*"))


def test_manifest(tmp_path: Path):
    f = _touch(tmp_path / "a.mp4")
    t = tl([clip(f, 90) for _ in range(5)])
    cache = tmp_path / "cache"
    path = rc.write_manifest(t, cache, max_segment_sec=10.0)
    assert path == cache / rc.MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["fps"] == 30.0 and data["canvas"] == [1920, 1080]
    assert [(s["index"], s["start_frame"], s["frames"], s["ready"]) for s in data["segments"]] == [
        (0, 0, 270, False), (1, 270, 180, False)]
    assert all(s["file"] == f"{s['key']}.mp4" for s in data["segments"])
    assert not list(cache.glob("*.tmp"))
```

**Contract test — `tests/integration/test_render_cache_real.py`:**
```python
"""Real ffmpeg render of cache segments: every frame is an I-frame, frame count exact."""

import shutil
import subprocess
from pathlib import Path

from src.core.types import EditClip, EditTimeline
from src.modules import render_cache as rc

FFPROBE = shutil.which("ffprobe")


def test_render_two_segments_all_intra(make_clip, tmp_path: Path):
    a = make_clip("a.mp4", duration=3.0)
    b = make_clip("b.mp4", duration=3.0)
    t = EditTimeline(name="rc", fps=30.0, canvas_width=320, canvas_height=180,
                     clips=[EditClip(a, 0.0, 30, 3.0), EditClip(b, 0.5, 30, 2.5),
                            EditClip(a, 1.0, 45, 2.0)])
    cache = tmp_path / "cache"
    out = rc.render_missing(t, cache, max_segment_sec=2.0, force_cpu=True)
    status = rc.cache_status(t, cache, max_segment_sec=2.0)
    assert out == [s.path for s in status] and all(s.ready for s in status)
    for s in status:
        lines = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pict_type",
             "-of", "csv=p=0", str(s.path)], capture_output=True, text=True, check=True,
            timeout=60).stdout.split()
        assert {x.strip(",") for x in lines} == {"I"}
        assert len(lines) == s.segment.frames
```
**Expected red:** `ModuleNotFoundError: src.modules.render_cache` and `TypeError: unexpected keyword 'intra_only'`. **Commit:** `feat(render-cache): content-hashed all-intra segment cache + intra_only renderer option`.

---

## P1.6 — Engine API service (`src/engine_api/`) + `main.py serve`

**Files:** create `src/engine_api/__init__.py`, `src/engine_api/jobs.py`, `src/engine_api/app.py`, `src/engine_api/runners.py`; modify `main.py` (new `serve` command only); tests `tests/test_engine_api.py` (LOCKED). Related suites: `tests/test_mcp_server.py tests/test_deliver_cli.py`.

**API:**
```python
# jobs.py
JobRunner = Callable[[dict[str, Any], Callable[[dict[str, Any]], None], threading.Event], dict[str, Any]]
JobStatus = Literal["running", "done", "failed", "cancelled"]
class JobRegistry:
    def submit(self, kind: str, runner: JobRunner, params: dict[str, Any]) -> str: ...   # runs in a daemon thread
    def get(self, job_id: str) -> dict[str, Any] | None: ...   # {"job_id","kind","status","result","error","events"}
    def cancel(self, job_id: str) -> bool: ...
# app.py
def create_app(token: str, *, runners: Mapping[str, JobRunner] | None = None,
               caps_fn: Callable[[], PlatformCaps] = get_caps, registry: JobRegistry | None = None,
               max_body_bytes: int = 1_000_000) -> Starlette: ...
def build_server_config(app, port: int) -> uvicorn.Config: ...   # host fixed to "127.0.0.1"
# runners.py
def default_runners() -> dict[str, JobRunner]: ...   # "proxies", "render_cache", "curate"
```
**Rules:**
- Routes: `GET /v1/health` (no auth) → `{"status": "ok", "version": "1"}`; everything else requires `Authorization: Bearer <token>` (else 401 `{"error": "unauthorized"}`); `GET /v1/capabilities` → `dataclasses.asdict(caps_fn())` with tuples as lists; `POST /v1/jobs/{kind}` → 202 `{"job_id"}` (unknown kind 404; body must be a JSON **object** else 400; body larger than `max_body_bytes` → 413 before parsing); `GET /v1/jobs/{id}` → job dict without `events` (404 if unknown); `GET /v1/jobs/{id}/events` → SSE (`event: <type>\ndata: <json>\n\n` for each stored event, keeps streaming until the job ends, then closes); with `?format=json` → `{"events": [...]}`; `POST /v1/jobs/{id}/cancel` → `{"cancelled": bool}`.
- Events: runners call `emit({...})` with a `"type"`; the registry appends a final event: `{"type": "done", "result": ...}`, `{"type": "error", "error": str(exc)}` (status failed) or `{"type": "cancelled"}` (status cancelled when the cancel event is set and the runner returns or raises `InterruptedError`).
- `default_runners`: `proxies` (params `paths`, `out_dir`, optional `height`, `gop` → `make_proxy` per path, emits progress, result `{"proxies": {src: proxy}}`), `render_cache` (params `workspace`, `project_id`, `cache_dir` → `read_freecut_project` + `render_missing` + `write_manifest`, result `{"manifest": path, "rendered": n}`), `curate` (params `input_dir`, `genre`, `workspace` → `curation.run_curation` + `write_freecut_project` of the rough-cut timeline, result `{"project_id", "summary"}`). These runners are covered by integration tests only where cheap; unit tests use fake runners.
- `main.py serve --port 8765 [--token-file PATH]`: token = file content or a random `secrets.token_urlsafe(24)` written to `<cache_dir>/engine_token` (printed path, never the token itself), serve with `build_server_config` (loopback only).

**Contract test — `tests/test_engine_api.py`:**
```python
"""Engine API: auth, capabilities, jobs, events (SSE + JSON), cancel, limits."""

import threading
import time

from starlette.testclient import TestClient

from src.core.platform_caps import PlatformCaps
from src.engine_api.app import build_server_config, create_app

TOKEN = "t0k3n-for-tests"
H = {"Authorization": f"Bearer {TOKEN}"}


def fake_caps() -> PlatformCaps:
    return PlatformCaps(os="windows", ffmpeg="ffmpeg", h264_encoders=("libx264",),
                        hevc_encoders=(), hwaccels=("d3d11va",), torch_device="cpu")


def make_client(runners=None, **kw) -> TestClient:
    return TestClient(create_app(token=TOKEN, runners=runners or {}, caps_fn=fake_caps, **kw))


def wait_done(c: TestClient, job_id: str) -> dict:
    for _ in range(500):
        st = c.get(f"/v1/jobs/{job_id}", headers=H).json()
        if st["status"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_health_needs_no_token():
    r = make_client().get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_other_routes_need_token():
    c = make_client()
    assert c.get("/v1/capabilities").status_code == 401
    assert c.get("/v1/capabilities", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.get("/v1/capabilities", headers=H)
    assert r.status_code == 200
    assert r.json()["h264_encoders"] == ["libx264"] and r.json()["os"] == "windows"


def test_job_lifecycle_and_events():
    def runner(params, emit, cancel):
        emit({"type": "progress", "pct": 50.0, "msg": "half"})
        return {"echo": params["x"]}

    c = make_client({"echo": runner})
    r = c.post("/v1/jobs/echo", json={"x": 7}, headers=H)
    assert r.status_code == 202
    jid = r.json()["job_id"]
    st = wait_done(c, jid)
    assert st["status"] == "done" and st["result"] == {"echo": 7} and "events" not in st
    events = c.get(f"/v1/jobs/{jid}/events?format=json", headers=H).json()["events"]
    assert [e["type"] for e in events] == ["progress", "done"]
    assert events[-1]["result"] == {"echo": 7}


def test_sse_stream_ends_after_done():
    def runner(params, emit, cancel):
        emit({"type": "progress", "pct": 100.0, "msg": "all"})
        return {}

    c = make_client({"quick": runner})
    jid = c.post("/v1/jobs/quick", json={}, headers=H).json()["job_id"]
    wait_done(c, jid)
    with c.stream("GET", f"/v1/jobs/{jid}/events", headers=H) as s:
        body = "".join(s.iter_text())
    assert "event: progress" in body and "event: done" in body


def test_failed_job_reports_error():
    def runner(params, emit, cancel):
        raise ValueError("bad input")

    c = make_client({"boom": runner})
    jid = c.post("/v1/jobs/boom", json={}, headers=H).json()["job_id"]
    st = wait_done(c, jid)
    assert st["status"] == "failed" and st["error"] == "bad input"
    events = c.get(f"/v1/jobs/{jid}/events?format=json", headers=H).json()["events"]
    assert events[-1]["type"] == "error"


def test_cancel_running_job():
    started = threading.Event()

    def runner(params, emit, cancel):
        started.set()
        while not cancel.is_set():
            time.sleep(0.01)
        raise InterruptedError("stopped")

    c = make_client({"slow": runner})
    jid = c.post("/v1/jobs/slow", json={}, headers=H).json()["job_id"]
    assert started.wait(5)
    assert c.post(f"/v1/jobs/{jid}/cancel", headers=H).json() == {"cancelled": True}
    st = wait_done(c, jid)
    assert st["status"] == "cancelled"
    events = c.get(f"/v1/jobs/{jid}/events?format=json", headers=H).json()["events"]
    assert events[-1]["type"] == "cancelled"


def test_unknown_kind_and_job():
    c = make_client()
    assert c.post("/v1/jobs/nope", json={}, headers=H).status_code == 404
    assert c.get("/v1/jobs/does-not-exist", headers=H).status_code == 404


def test_body_must_be_a_small_json_object():
    def runner(params, emit, cancel):
        return {}

    c = make_client({"echo": runner}, max_body_bytes=100)
    json_headers = {**H, "Content-Type": "application/json"}
    assert c.post("/v1/jobs/echo", content=b"[1, 2]", headers=json_headers).status_code == 400
    big = {"x": "a" * 1000}
    assert c.post("/v1/jobs/echo", json=big, headers=H).status_code == 413


def test_server_binds_loopback_only():
    cfg = build_server_config(create_app(token=TOKEN, caps_fn=fake_caps), port=8765)
    assert cfg.host == "127.0.0.1" and cfg.port == 8765
```
**Expected red:** `ModuleNotFoundError: src.engine_api`. **Commit:** `feat(engine-api): loopback job service (auth, SSE events, cancel) + main.py serve`.

---

## Phase 1 exit criteria (architect checks)
- All locked tests green; full suite ≥ baseline + new tests, 0 failures; ruff clean.
- `main.py serve` starts, `GET /v1/health` answers, `POST /v1/jobs/proxies` on `E:\antigravity\perf-samples\4k60_hevc_422_10bit.mov` produces an all-intra proxy that the Studio adopts (manual check by architect with the S0.3 harness).
- `render_cache` job on a FreeCut project written by the engine produces a manifest whose segments are all `ready` and all-intra.
- Phase 2 (Studio shell) and Phase 4 (Studio plays the render cache) are planned in detail only after these pass.

---

## P1.7 — Phase 1 review polish (added after the Phase 1 review)

Fix exactly these review findings, nothing else:
1. `src/core/platform_caps.py` `probe`: the `-encoders` and `-hwaccels` calls must not crash `probe`/`get_caps`. On `subprocess.TimeoutExpired` or `OSError` from `run`, treat that list as empty (same style as the smoke test handling).
2. `src/integrations/freecut_writer.py`:
   - Read and parse `index.json` **before** creating `projects/{id}/`, so a corrupt index leaves no orphan directory.
   - Remove the cv2 fallback in `probe_file_stats` together with its bare `except Exception: pass`. `probe_media` is the only probe; when it finds no video stream, the existing defaults apply.
3. `src/integrations/freecut_reader.py`:
   - A corrupt `source.link.json` or `metadata.json` raises `ValueError` naming the media id. Do not silently fall through, and use no bare `except`.
   - Select video tracks by `kind == "video"` only, and remove the `startswith("track-v")` and `type` heuristics. FreeCut `TimelineTrack.kind` is `'video' | 'audio'`.
4. `src/dispatchers/nvenc_renderer.py`:
   - Document in the `_derive` docstring that `settings=None` keeps the receiver's settings.
   - Wrap the `__init__` signature to at most 100 characters.
5. `src/engine_api/`:
   - `create_app` raises `ValueError` for an empty or whitespace token.
   - `_AuthMiddleware` compares with `hmac.compare_digest`.
   - `main.py serve` rejects an empty token file with `click.BadParameter` before starting the server.
   - `JobRegistry.__init__(self, max_finished: int = 200)`: on every `submit`, evict the oldest *finished* jobs (status not `running`) so at most `max_finished` remain. Running jobs are never evicted.
6. `src/modules/visual_scorer.py` `_init_aesthetic_model`: when `torch.cuda.is_available()` is false, log at INFO ("PyTorch build has no CUDA; using OpenCV aesthetic engine") and do **not** load the model.
   - Today a CPU-only torch on a GPU machine loads MobileNet, then every batch fails on `.to("cuda")` and silently falls back.

**Files:**
- `src/core/platform_caps.py`
- `src/integrations/freecut_writer.py`
- `src/integrations/freecut_reader.py`
- `src/dispatchers/nvenc_renderer.py`
- `src/engine_api/app.py`
- `src/engine_api/jobs.py`
- `main.py`
- `src/modules/visual_scorer.py`
- tests: `tests/test_phase1_polish.py` (LOCKED)

**Related suites:**
- `tests/test_platform_caps.py`
- `tests/test_freecut_writer.py`
- `tests/test_freecut_writer_index.py`
- `tests/integration/test_freecut_writer_real.py`
- `tests/integration/test_freecut_reader_real.py`
- `tests/test_engine_api.py`
- `tests/test_render_cache.py`
- `tests/test_visual_scorer*.py`

All existing tests stay green without editing them.

**Contract test — `tests/test_phase1_polish.py`:**
```python
"""Phase 1 review polish: failure paths of probe, writer, reader, engine API, visual scorer."""

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.core.config import PipelineConfig
from src.core.platform_caps import probe
from src.core.types import EditClip, EditTimeline
from src.engine_api.app import create_app
from src.engine_api.jobs import JobRegistry
from src.integrations.freecut_reader import read_freecut_project
from src.integrations.freecut_writer import write_freecut_project


def _tl(path: Path) -> EditTimeline:
    clip = EditClip(source_path=path, source_in=0.0, frames=30, available=1.0)
    return EditTimeline(name="t", fps=30.0, canvas_width=1920, canvas_height=1080, clips=[clip])


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired(cmd="ffmpeg", timeout=15),
                                 OSError("ffmpeg vanished")])
def test_probe_survives_ffmpeg_list_failure(exc):
    def boom(*_a, **_k):
        raise exc

    caps = probe(ffmpeg="ffmpeg", os_name="linux", run=boom, smoke_test=False,
                 torch_device="cpu")
    assert caps.h264_encoders == () and caps.hevc_encoders == () and caps.hwaccels == ()
    assert caps.best_h264() is None


def test_corrupt_index_leaves_no_orphan_project_dir(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "index.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        write_freecut_project(_tl(tmp_path / "a.mp4"), ws, "x")
    projects = ws / "projects"
    assert not projects.exists() or not any(projects.iterdir())


@pytest.mark.parametrize("name", ["source.link.json", "metadata.json"])
def test_reader_rejects_corrupt_media_json(tmp_path: Path, name: str):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"not really video")
    ws = tmp_path / "ws"
    pid = write_freecut_project(_tl(src), ws, "x")
    proj = json.loads((ws / "projects" / pid / "project.json").read_text(encoding="utf-8"))
    mid = next(i["mediaId"] for i in proj["timeline"]["items"] if i["type"] == "video")
    if name == "metadata.json":
        (ws / "media" / mid / "source.link.json").unlink()
    (ws / "media" / mid / name).write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError, match=mid):
        read_freecut_project(ws, pid)


def test_create_app_rejects_empty_token():
    with pytest.raises(ValueError):
        create_app("", runners={})
    with pytest.raises(ValueError):
        create_app("   ", runners={})


def test_serve_rejects_empty_token_file(tmp_path: Path, monkeypatch):
    import uvicorn

    from main import cli

    started: list[bool] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: started.append(True))
    tok = tmp_path / "token"
    tok.write_text("  \n", encoding="utf-8")
    res = CliRunner().invoke(cli, ["serve", "--port", "8798", "--token-file", str(tok)])
    assert res.exit_code != 0
    assert started == []


def _wait_done(reg: JobRegistry, job_id: str, timeout: float = 10.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        job = reg.get(job_id)
        if job is not None and job["status"] != "running":
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def test_registry_evicts_oldest_finished_jobs():
    reg = JobRegistry(max_finished=2)
    ok = lambda params, emit, cancel: {"ok": True}  # noqa: E731
    ids = []
    for _ in range(3):
        jid = reg.submit("k", ok, {})
        _wait_done(reg, jid)
        ids.append(jid)
    gate = threading.Event()
    running = reg.submit("k", lambda p, e, c: gate.wait(10) and {}, {})
    assert reg.get(ids[0]) is None
    assert reg.get(ids[1]) is not None and reg.get(ids[2]) is not None
    assert reg.get(running)["status"] == "running"
    gate.set()
    _wait_done(reg, running)


def test_visual_scorer_skips_torch_model_without_torch_cuda(monkeypatch):
    import torch

    import src.modules.visual_scorer as vs

    monkeypatch.setattr(vs, "check_cuda_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    scorer = vs.VisualScorer(PipelineConfig())
    assert scorer._torch_model is None
```
**Expected red:** 8 failed, 1 passed.
- The `metadata.json` case of `test_reader_rejects_corrupt_media_json` already passes, because the current code raises "source path could not be resolved". It is a guard.
- The other 8 fail as follows:
  - `TimeoutExpired`/`OSError` from `probe`
  - the orphan-directory assertion
  - DID NOT RAISE in the reader (`source.link.json` case)
  - DID NOT RAISE in `create_app`
  - `assert 0 != 0` in `serve`
  - `TypeError` from `JobRegistry(max_finished=...)`
  - a MobileNet model is loaded.

**Commit:** `refactor(engine): phase 1 review polish (probe guards, reader errors, token checks, job eviction, torch device)`.
