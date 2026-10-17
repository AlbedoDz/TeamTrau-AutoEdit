"""Pipeline speed benchmark on synthetic footage.

Generates 6 synthetic 1080p30 clips (30 s, testsrc2 + sine, some with a static
tail) and one 60 s music track, then runs analysis (``submit_job``) and deliver
(``submit_deliver_job`` mp4) repeatedly so the cache effect is visible.

Usage: python scripts/benchmark_pipeline.py [--keep] [--workdir DIR] [--runs N]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import PipelineConfig  # noqa: E402
from src.core.service import JobCallbacks, StudioBackendAdapter  # noqa: E402

CLIP_COUNT = 6
CLIP_SECONDS = 30
MUSIC_SECONDS = 60


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def generate_media(root: Path) -> tuple[Path, Path]:
    """Create synthetic clips and a soundtrack under ``root``."""
    clips_dir = root / "footage"
    clips_dir.mkdir(parents=True, exist_ok=True)
    for i in range(CLIP_COUNT):
        out = clips_dir / f"clip_{i:02d}.mp4"
        if out.exists():
            continue
        src = f"testsrc2=size=1920x1080:rate=30:duration={CLIP_SECONDS}"
        # Even clips go static after 12 s (frozen last frame) to exercise dead-air logic.
        vf = f"hue=h={i * 30}"
        if i % 2 == 0:
            vf += f",trim=0:12,tpad=stop_mode=clone:stop_duration={CLIP_SECONDS - 12}"
        vf += ",format=yuv420p"
        _ffmpeg(
            "-f", "lavfi", "-i", src,
            "-f", "lavfi", "-i", f"sine=frequency={220 + 40 * i}:duration={CLIP_SECONDS}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-shortest", str(out),
        )
    music = root / "music.mp3"
    if not music.exists():
        _ffmpeg(
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={MUSIC_SECONDS}",
            "-f", "lavfi",
            "-i", f"aevalsrc='0.6*sin(2*PI*80*t)*lt(mod(t,0.5),0.08)':d={MUSIC_SECONDS}:s=44100",
            "-filter_complex", "[0:a]volume=0.15[a];[a][1:a]amix=inputs=2:duration=first",
            "-c:a", "libmp3lame", str(music),
        )
    return clips_dir, music


def run_once(clips_dir: Path, music: Path, out_dir: Path, cache_dir: Path) -> dict[str, float]:
    """Run analysis + deliver once and return per-stage wall seconds."""
    cfg = PipelineConfig.from_preset(
        "1080p30",
        cache_dir=cache_dir,
        enable_vlm_tagging=False,
        enable_transcription=False,
        target_duration=45.0,
    )
    timings: dict[str, float] = {}
    state: dict[str, object] = {"name": None, "t0": time.perf_counter()}

    def _close() -> None:
        name = state["name"]
        if name is not None:
            timings[str(name)] = timings.get(str(name), 0.0) + time.perf_counter() - float(state["t0"])  # type: ignore[arg-type]
            state["name"] = None

    def on_stage(name: str, _idx: int, _total: int) -> None:
        _close()
        state["name"], state["t0"] = name, time.perf_counter()

    cb = JobCallbacks(on_stage_changed=on_stage)
    adapter = StudioBackendAdapter()
    t_total = time.perf_counter()
    handle = adapter.submit_job(clips_dir, music, out_dir, cfg, callbacks=cb)
    _close()
    res = handle.result
    if res is None or not res.success:
        raise RuntimeError(f"analysis failed: {getattr(res, 'error_message', res)}")
    timings["ANALYSIS TOTAL"] = time.perf_counter() - t_total

    t0 = time.perf_counter()
    dh = adapter.submit_deliver_job(
        res.edit_timelines["master"], out_dir / "deliver", cfg, formats={"mp4"}, callbacks=cb
    )
    _close()
    if dh.result is None or not dh.result.success:
        raise RuntimeError(f"deliver failed: {getattr(dh.result, 'error_message', dh.result)}")
    timings["DELIVER TOTAL"] = time.perf_counter() - t0
    timings["GRAND TOTAL"] = timings["ANALYSIS TOTAL"] + timings["DELIVER TOTAL"]
    return timings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", type=Path, default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--runs", type=int, default=2)
    args = ap.parse_args()

    root = args.workdir or Path(tempfile.mkdtemp(prefix="autoedit_bench_"))
    root.mkdir(parents=True, exist_ok=True)
    print(f"workdir: {root}")
    clips_dir, music = generate_media(root)
    cache_dir = root / "cache"
    shutil.rmtree(cache_dir, ignore_errors=True)

    runs: list[dict[str, float]] = []
    for n in range(args.runs):
        out_dir = root / f"out_{n}"
        shutil.rmtree(out_dir, ignore_errors=True)
        runs.append(run_once(clips_dir, music, out_dir, cache_dir))

    names = list(dict.fromkeys(k for r in runs for k in r))
    header = f"{'stage':<52}" + "".join(f"{'run ' + str(i + 1):>10}" for i in range(len(runs)))
    print(header)
    print("-" * len(header))
    for name in names:
        print(f"{name:<52}" + "".join(f"{r.get(name, 0.0):>9.2f}s" for r in runs))
    if not args.keep and args.workdir is None:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
