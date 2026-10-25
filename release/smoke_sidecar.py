"""Smoke test runner for the frozen TeamTrau lite engine sidecar."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _find_free_port() -> int:
    """Bind to port 0 on loopback to discover an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _generate_media(ffmpeg_bin: Path, media_dir: Path) -> tuple[Path, Path]:
    """Generate a 10 s sine WAV and a 3 s testsrc2 MP4 using the bundled ffmpeg."""
    media_dir.mkdir(parents=True, exist_ok=True)
    wav_path = media_dir / "smoke_tone.wav"
    mp4_path = media_dir / "smoke_clip.mp4"

    subprocess.run(
        [
            str(ffmpeg_bin),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=10",
            str(wav_path),
        ],
        check=True,
        timeout=60,
    )
    subprocess.run(
        [
            str(ffmpeg_bin),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=48000:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(mp4_path),
        ],
        check=True,
        timeout=60,
    )
    return wav_path, mp4_path


def _http_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    """Send an HTTP JSON request and return (status_code, parsed_json)."""
    headers: dict[str, str] = {}
    data: bytes | None = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body)


def _wait_for_health(base_url: str, proc: subprocess.Popen[Any], timeout_sec: float = 60.0) -> float:
    """Poll /v1/health until 200 OK or timeout, returning elapsed seconds."""
    t0 = time.perf_counter()
    deadline = t0 + timeout_sec
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Engine exited prematurely with code {proc.returncode}")
        try:
            status, body = _http_json(f"{base_url}/v1/health", timeout=2.0)
            if status == 200 and body.get("status") == "ok":
                return time.perf_counter() - t0
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.1)
    raise TimeoutError(f"Engine did not become healthy within {timeout_sec:.0f}s")


def _run_job(
    base_url: str,
    token: str,
    kind: str,
    params: dict[str, Any],
    timeout_sec: float = 180.0,
) -> tuple[dict[str, Any], float]:
    """Submit a job via POST /v1/jobs/{kind} and poll GET /v1/jobs/{id} until done."""
    t0 = time.perf_counter()
    status, sub = _http_json(
        f"{base_url}/v1/jobs/{kind}",
        method="POST",
        token=token,
        payload=params,
        timeout=10.0,
    )
    if status != 202 or "job_id" not in sub:
        raise RuntimeError(f"Failed to submit {kind} job: status={status}, body={sub}")
    job_id = str(sub["job_id"])

    deadline = t0 + timeout_sec
    while time.perf_counter() < deadline:
        _, info = _http_json(f"{base_url}/v1/jobs/{job_id}", token=token, timeout=10.0)
        job_status = info.get("status")
        if job_status == "done":
            return info.get("result") or {}, time.perf_counter() - t0
        if job_status in ("failed", "cancelled"):
            raise RuntimeError(f"Job {kind} ({job_id}) {job_status}: {info.get('error')}")
        time.sleep(0.1)
    raise TimeoutError(f"Job {kind} ({job_id}) timed out after {timeout_sec:.0f}s")


def run_smoke(exe_path: Path, data_dir: Path | None = None) -> int:
    """Execute end-to-end smoke test against the frozen sidecar executable."""
    exe = exe_path.resolve()
    if not exe.is_file():
        raise FileNotFoundError(f"Sidecar executable not found: {exe}")

    tools_dir = exe.parent / "tools"
    ffmpeg_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    ffmpeg_bin = tools_dir / ffmpeg_name
    if not ffmpeg_bin.is_file():
        raise FileNotFoundError(f"Bundled ffmpeg not found: {ffmpeg_bin}")

    resolved_data = (data_dir or (ROOT / "build" / "smoke-data")).resolve()
    resolved_data.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="teamtrau-smoke-media-"))

    t_media0 = time.perf_counter()
    wav_path, mp4_path = _generate_media(ffmpeg_bin, work_dir / "media")
    t_media = time.perf_counter() - t_media0
    print(f"[smoke] media generated in {t_media:.2f}s", flush=True)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = resolved_data / "smoke_engine.log"

    proc: subprocess.Popen[Any] | None = None
    log_file = log_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [
                str(exe),
                "serve",
                "--port",
                str(port),
                "--data-dir",
                str(resolved_data),
                "--tools-dir",
                str(tools_dir),
            ],
            cwd=str(work_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

        t_health = _wait_for_health(base_url, proc, timeout_sec=60.0)
        print(f"[smoke] /v1/health ready in {t_health:.2f}s (port {port})", flush=True)

        token_path = resolved_data / "engine_token"
        if not token_path.is_file():
            raise RuntimeError(f"Engine token file missing: {token_path}")
        token = token_path.read_text(encoding="utf-8").strip()

        audio_res, t_audio = _run_job(
            base_url,
            token,
            "analyze_soundtrack",
            {"path": str(wav_path)},
            timeout_sec=180.0,
        )
        print(
            f"[smoke] analyze_soundtrack completed in {t_audio:.2f}s "
            f"(duration={audio_res.get('duration')}s, bpm={audio_res.get('tempo_bpm'):.1f})",
            flush=True,
        )

        proxy_out_dir = work_dir / "proxies"
        proxy_res, t_proxy = _run_job(
            base_url,
            token,
            "proxies",
            {"paths": [str(mp4_path)], "out_dir": str(proxy_out_dir), "height": 720, "gop": 1},
            timeout_sec=120.0,
        )
        proxies_map = proxy_res.get("proxies") or {}
        proxy_file = Path(proxies_map.get(str(mp4_path), ""))
        if not proxy_file.is_file() or proxy_file.stat().st_size == 0:
            raise RuntimeError(f"Expected proxy file not created: {proxy_file}")
        print(
            f"[smoke] proxies completed in {t_proxy:.2f}s "
            f"({proxy_file.name}, {proxy_file.stat().st_size} bytes)",
            flush=True,
        )

        numba_cache = resolved_data / "numba-cache"
        cache_files = list(numba_cache.rglob("*")) if numba_cache.is_dir() else []
        nbi_files = [p for p in cache_files if p.is_file()]
        if not nbi_files:
            raise RuntimeError(f"Expected non-empty numba-cache in {numba_cache}")
        print(f"[smoke] numba-cache verified ({len(nbi_files)} files in {numba_cache})", flush=True)
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_file.close()
        shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for release/smoke_sidecar.py."""
    parser = argparse.ArgumentParser(description="Smoke test a frozen TeamTrau sidecar binary.")
    parser.add_argument("exe", type=Path, help="Path to teamtrau-engine(.exe)")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Optional engine data directory (defaults to build/smoke-data).",
    )
    args = parser.parse_args(argv)
    try:
        return run_smoke(args.exe, data_dir=args.data_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
