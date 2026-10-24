"""Spike S0.3 Benchmarks: Proxy Encode Speed & Project Assembly Harness.

Measures proxy encoding speed across all 6 perf-samples using:
- h264_nvenc (GOP 1 and GOP 6)
- libx264 -preset veryfast (GOP 1 and GOP 6)

Builds test workspaces for editor playback measurements (Task 4):
a) 4k60_hevc_422_10bit original vs proxy GOP 1 vs proxy GOP 6
b) 4k120_h264 original vs 1080p@120 proxy
c) Multi-clip timeline (>= 10 cuts mixing 4k60 H.264 and HEVC 10-bit) original vs proxies
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.core.types import EditClip, EditTimeline  # noqa: E402
from src.integrations.freecut_writer import probe_file_stats, write_freecut_project  # noqa: E402
from src.modules.proxy_generator import build_proxy_ffmpeg_cmd, compute_proxy_key  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SAMPLES_DIR = Path(r"E:\antigravity\perf-samples")
PROXIES_DIR = Path(r"E:\antigravity\perf-proxies")
BENCHMARK_RESULTS_FILE = Path(r"E:\antigravity\perf-proxies\encode_benchmark_results.json")

SAMPLE_FILES = [
    "1080p60_h264_allintra.mp4",
    "4k120_h264.mp4",
    "4k60_h264_8bit.mp4",
    "4k60_hevc_10bit.mp4",
    "4k60_hevc_422_10bit.mov",
    "phone_vfr.mp4",
]


def run_single_encode(
    src_path: Path,
    dst_path: Path,
    encoder: str,
    gop: int,
    height: int = 1080,
    fps: float | None = None,
    force_reencode: bool = False,
) -> dict[str, Any]:
    """Execute a single FFmpeg proxy encode run and return performance metrics."""
    if not force_reencode and dst_path.exists() and dst_path.stat().st_size > 0:
        meta = probe_file_stats(src_path)
        source_duration = meta.get("duration", 30.0)
        return {
            "success": True,
            "sample": src_path.name,
            "encoder": encoder,
            "gop": gop,
            "source_duration_s": round(source_duration, 2),
            "encode_time_s": 0.0,
            "speed_factor_x": 0.0,
            "proxy_size_mb": round(dst_path.stat().st_size / (1024 * 1024), 2),
            "proxy_path": str(dst_path),
        }

    dst_path.unlink(missing_ok=True)
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_proxy_ffmpeg_cmd(
        src=src_path,
        dst=dst_path,
        encoder=encoder,
        height=height,
        fps=fps,
        gop=gop,
    )

    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    t1 = time.perf_counter()

    encode_duration = t1 - t0
    if proc.returncode != 0 or not dst_path.exists():
        logger.error("Encode failed for %s with %s (gop=%d):\n%s", src_path.name, encoder, gop, proc.stderr[-500:])
        return {
            "success": False,
            "error": proc.stderr[-500:],
            "encode_time_s": encode_duration,
        }

    dst_size_bytes = dst_path.stat().st_size
    meta = probe_file_stats(src_path)
    source_duration = meta.get("duration", 30.0)
    speed_factor = source_duration / encode_duration if encode_duration > 0 else 0.0

    return {
        "success": True,
        "sample": src_path.name,
        "encoder": encoder,
        "gop": gop,
        "source_duration_s": round(source_duration, 2),
        "encode_time_s": round(encode_duration, 3),
        "speed_factor_x": round(speed_factor, 2),
        "proxy_size_mb": round(dst_size_bytes / (1024 * 1024), 2),
        "proxy_path": str(dst_path),
    }


def run_all_encode_benchmarks() -> list[dict[str, Any]]:
    """Run encode benchmark across all 6 samples x 4 encoder configurations."""
    PROXIES_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    configs = [
        ("h264_nvenc", 1),
        ("h264_nvenc", 6),
        ("libx264", 1),
        ("libx264", 6),
    ]

    print("\n==========================================================================================")
    print(" STARTING ENCODE SPEED BENCHMARK (Task 2: NVENC vs libx264, GOP 1 vs GOP 6)")
    print("==========================================================================================")

    for sample_name in SAMPLE_FILES:
        src_path = SAMPLES_DIR / sample_name
        if not src_path.exists():
            print(f"[ERROR] Sample file missing: {src_path}")
            continue

        meta = probe_file_stats(src_path)
        fps_param: float | None = None
        # Keep 120 fps if 4k120
        if "120" in sample_name:
            fps_param = 120.0
        elif meta.get("fps", 0) > 0:
            fps_param = float(meta["fps"])

        for encoder, gop in configs:
            tag = f"{encoder}_gop{gop}"
            key = compute_proxy_key(src_path, height=1080, fps=fps_param, gop=gop)
            out_file = PROXIES_DIR / f"{src_path.stem}_{tag}_{key[:8]}.mp4"

            print(f"\n[RUNNING] {sample_name} | {encoder} | GOP {gop} -> {out_file.name} ...")
            run_data = run_single_encode(
                src_path=src_path,
                dst_path=out_file,
                encoder=encoder,
                gop=gop,
                height=1080,
                fps=fps_param,
            )
            results.append(run_data)

            if run_data.get("success"):
                print(
                    f"  -> Done in {run_data['encode_time_s']}s "
                    f"({run_data['speed_factor_x']}x real-time) | Size: {run_data['proxy_size_mb']} MB"
                )
            else:
                print(f"  -> FAILED: {run_data.get('error')}")

    BENCHMARK_RESULTS_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nBenchmark results saved to: {BENCHMARK_RESULTS_FILE}")
    print_markdown_table(results)
    return results


def print_markdown_table(results: list[dict[str, Any]]) -> None:
    """Print markdown formatted table of encode speeds."""
    print("\n### Proxy Encode Speed Benchmark Table (× Real-Time)\n")
    print("| Sample | Duration (s) | NVENC GOP 1 | NVENC GOP 6 | libx264 GOP 1 | libx264 GOP 6 |")
    print("|---|---|---|---|---|---|")

    # Group by sample
    grouped: dict[str, dict[str, Any]] = {}
    for r in results:
        sample = r["sample"]
        if sample not in grouped:
            grouped[sample] = {"duration": r.get("source_duration_s", 30.0)}
        cfg_key = f"{r['encoder']}_gop{r['gop']}"
        grouped[sample][cfg_key] = f"{r.get('speed_factor_x', 0.0)}x ({r.get('encode_time_s')}s)"

    for sample, data in grouped.items():
        dur = data["duration"]
        nv1 = data.get("h264_nvenc_gop1", "N/A")
        nv6 = data.get("h264_nvenc_gop6", "N/A")
        x1 = data.get("libx264_gop1", "N/A")
        x6 = data.get("libx264_gop6", "N/A")
        print(f"| `{sample}` | {dur}s | {nv1} | {nv6} | {x1} | {x6} |")
    print()


def build_playback_workspaces() -> dict[str, Path]:
    """Create test workspaces for editor playback measurements (Task 4)."""
    ws_base = Path(r"E:\antigravity\studio-workspaces-s03")
    shutil.rmtree(ws_base, ignore_errors=True)
    ws_base.mkdir(parents=True, exist_ok=True)
    workspaces: dict[str, Path] = {}

    # a) 4k60_hevc_422_10bit original vs proxy GOP 1 vs proxy GOP 6
    hevc_422_src = SAMPLES_DIR / "4k60_hevc_422_10bit.mov"
    if hevc_422_src.exists():
        # Make proxies
        proxy_g1 = PROXIES_DIR / "proxy_hevc_422_gop1.mp4"
        run_single_encode(hevc_422_src, proxy_g1, "h264_nvenc", gop=1, height=1080, fps=60.0)

        proxy_g6 = PROXIES_DIR / "proxy_hevc_422_gop6.mp4"
        run_single_encode(hevc_422_src, proxy_g6, "h264_nvenc", gop=6, height=1080, fps=60.0)

        # 1. Original (expected: 0 frames picture)
        ws_orig = ws_base / "ws_hevc422_orig"
        tl_orig = EditTimeline(
            name="HEVC 422 Original",
            fps=60.0,
            canvas_width=3840,
            canvas_height=2160,
            clips=[EditClip(source_path=hevc_422_src, source_in=0.0, frames=1800, available=30.0, label="HEVC 422")],
        )
        write_freecut_project(tl_orig, ws_orig, "HEVC 422 Original")
        workspaces["hevc422_orig"] = ws_orig

        # 2. Proxy GOP 1
        ws_g1 = ws_base / "ws_hevc422_proxy_g1"
        tl_g1 = EditTimeline(
            name="HEVC 422 Proxy GOP1",
            fps=60.0,
            canvas_width=1920,
            canvas_height=1080,
            clips=[EditClip(source_path=hevc_422_src, source_in=0.0, frames=1800, available=30.0, label="HEVC 422")],
        )
        write_freecut_project(tl_g1, ws_g1, "HEVC 422 Proxy GOP1", proxies={hevc_422_src: proxy_g1})
        workspaces["hevc422_proxy_g1"] = ws_g1

        # 3. Proxy GOP 6
        ws_g6 = ws_base / "ws_hevc422_proxy_g6"
        tl_g6 = EditTimeline(
            name="HEVC 422 Proxy GOP6",
            fps=60.0,
            canvas_width=1920,
            canvas_height=1080,
            clips=[EditClip(source_path=hevc_422_src, source_in=0.0, frames=1800, available=30.0, label="HEVC 422")],
        )
        write_freecut_project(tl_g6, ws_g6, "HEVC 422 Proxy GOP6", proxies={hevc_422_src: proxy_g6})
        workspaces["hevc422_proxy_g6"] = ws_g6

    # b) 4k120_h264 original vs 1080p@120 proxy
    src_4k120 = SAMPLES_DIR / "4k120_h264.mp4"
    if src_4k120.exists():
        proxy_120 = PROXIES_DIR / "proxy_4k120_1080p.mp4"
        run_single_encode(src_4k120, proxy_120, "h264_nvenc", gop=1, height=1080, fps=120.0)

        # Original
        ws_120_orig = ws_base / "ws_4k120_orig"
        tl_120_orig = EditTimeline(
            name="4K120 Original",
            fps=120.0,
            canvas_width=3840,
            canvas_height=2160,
            clips=[EditClip(source_path=src_4k120, source_in=0.0, frames=3600, available=30.0, label="4K120 Orig")],
        )
        write_freecut_project(tl_120_orig, ws_120_orig, "4K120 Original")
        workspaces["4k120_orig"] = ws_120_orig

        # Proxy
        ws_120_proxy = ws_base / "ws_4k120_proxy"
        tl_120_proxy = EditTimeline(
            name="4K120 Proxy",
            fps=120.0,
            canvas_width=1920,
            canvas_height=1080,
            clips=[EditClip(source_path=src_4k120, source_in=0.0, frames=3600, available=30.0, label="4K120 Proxy")],
        )
        write_freecut_project(tl_120_proxy, ws_120_proxy, "4K120 Proxy", proxies={src_4k120: proxy_120})
        workspaces["4k120_proxy"] = ws_120_proxy

    # c) Multi-clip timeline (>= 10 cuts mixing 4k60 H.264 and HEVC 10-bit) original vs proxies
    src_h264 = SAMPLES_DIR / "4k60_h264_8bit.mp4"
    src_hevc10 = SAMPLES_DIR / "4k60_hevc_10bit.mp4"
    if src_h264.exists() and src_hevc10.exists():
        proxy_h264 = PROXIES_DIR / "proxy_4k60_h264_gop1.mp4"
        run_single_encode(src_h264, proxy_h264, "h264_nvenc", gop=1, height=1080, fps=60.0)

        proxy_h264_g6 = PROXIES_DIR / "proxy_4k60_h264_gop6.mp4"
        run_single_encode(src_h264, proxy_h264_g6, "h264_nvenc", gop=6, height=1080, fps=60.0)

        proxy_hevc10 = PROXIES_DIR / "proxy_4k60_hevc10_gop1.mp4"
        run_single_encode(src_hevc10, proxy_hevc10, "h264_nvenc", gop=1, height=1080, fps=60.0)

        # 4k60 single-clip workspaces for scrub test
        ws_4k60_orig = ws_base / "ws_4k60_orig"
        tl_4k60 = EditTimeline(
            name="4K60 Original",
            fps=60.0,
            canvas_width=3840,
            canvas_height=2160,
            clips=[EditClip(source_path=src_h264, source_in=0.0, frames=1800, available=30.0, label="4K60 H264")],
        )
        write_freecut_project(tl_4k60, ws_4k60_orig, "4K60 Original")
        workspaces["4k60_orig"] = ws_4k60_orig

        ws_4k60_g1 = ws_base / "ws_4k60_proxy_g1"
        tl_4k60_g1 = EditTimeline(
            name="4K60 Proxy GOP1",
            fps=60.0,
            canvas_width=1920,
            canvas_height=1080,
            clips=[EditClip(source_path=src_h264, source_in=0.0, frames=1800, available=30.0, label="4K60 H264")],
        )
        write_freecut_project(tl_4k60_g1, ws_4k60_g1, "4K60 Proxy GOP1", proxies={src_h264: proxy_h264})
        workspaces["4k60_proxy_g1"] = ws_4k60_g1

        ws_4k60_g6 = ws_base / "ws_4k60_proxy_g6"
        tl_4k60_g6 = EditTimeline(
            name="4K60 Proxy GOP6",
            fps=60.0,
            canvas_width=1920,
            canvas_height=1080,
            clips=[EditClip(source_path=src_h264, source_in=0.0, frames=1800, available=30.0, label="4K60 H264")],
        )
        write_freecut_project(tl_4k60_g6, ws_4k60_g6, "4K60 Proxy GOP6", proxies={src_h264: proxy_h264_g6})
        workspaces["4k60_proxy_g6"] = ws_4k60_g6

        # 12 cuts alternating (150 frames each = 2.5s @ 60fps, total 1800 frames = 30s)
        clips_orig: list[EditClip] = []
        for i in range(12):
            is_hevc = (i % 2 == 1)
            src_file = src_hevc10 if is_hevc else src_h264
            start_sec = (i * 2.0) % 25.0
            clips_orig.append(
                EditClip(
                    source_path=src_file,
                    source_in=start_sec,
                    frames=150,
                    available=30.0,
                    label=f"Cut_{i+1}_{'HEVC10' if is_hevc else 'H264'}",
                )
            )

        ws_multi_orig = ws_base / "ws_multiclip_orig"
        tl_multi_orig = EditTimeline(
            name="MultiClip Original 4K60",
            fps=60.0,
            canvas_width=3840,
            canvas_height=2160,
            clips=clips_orig,
        )
        write_freecut_project(tl_multi_orig, ws_multi_orig, "MultiClip Original")
        workspaces["multiclip_orig"] = ws_multi_orig

        ws_multi_proxy = ws_base / "ws_multiclip_proxy"
        tl_multi_proxy = EditTimeline(
            name="MultiClip Proxy 1080p60",
            fps=60.0,
            canvas_width=1920,
            canvas_height=1080,
            clips=clips_orig,
        )
        write_freecut_project(
            tl_multi_proxy,
            ws_multi_proxy,
            "MultiClip Proxy",
            proxies={
                src_h264: proxy_h264,
                src_hevc10: proxy_hevc10,
            },
        )
        workspaces["multiclip_proxy"] = ws_multi_proxy

    print("\nGenerated Playback Workspaces:")
    for k, v in workspaces.items():
        print(f"  [{k}] -> {v}")
    return workspaces


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("bench", "all"):
        run_all_encode_benchmarks()
    if mode in ("workspaces", "all"):
        build_playback_workspaces()
