"""Spike S0.4: Workspace Generator & Engine Render-Preview Prototype.

Generates test workspaces for FreeCut playback:
- T1: 12 cuts @ 60 fps (1080p all-intra proxies from S0.3)
- T2: 30 cuts @ 60 fps (1s clips, 1080p all-intra proxies)
- T3: 12 cuts @ 120 fps (4k120 1080p proxy)

Task C:
- Renders T1 and T3 timelines to continuous 1080p all-intra H.264 files with NVENC.
- Reports render duration and real-time speed factor.
- Generates playback workspaces containing the rendered single-file preview.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.core.config import PipelineConfig  # noqa: E402
from src.core.types import DeliverSettings, EditClip, EditTimeline  # noqa: E402
from src.dispatchers.nvenc_renderer import NvencRenderer  # noqa: E402
from src.integrations.freecut_writer import probe_file_stats, write_freecut_project  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SAMPLES_DIR = Path(r"E:\antigravity\perf-samples")
PROXIES_DIR = Path(r"E:\antigravity\perf-proxies")
WS_DIR = Path(r"E:\antigravity\studio-workspaces-s04")
OUTPUT_DIR = Path(r"E:\antigravity\studio-workspaces-s04\rendered_previews")


def ensure_proxy(src: Path, proxy: Path, height: int = 1080, fps: float = 60.0) -> Path:
    """Ensure all-intra proxy exists, generating via NVENC if missing."""
    if proxy.exists() and proxy.stat().st_size > 0:
        return proxy
    proxy.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg_bin, "-y", "-hide_banner", "-nostdin",
        "-i", str(src),
        "-vf", f"scale=-2:{height},fps={fps}",
        "-c:v", "h264_nvenc", "-preset", "p7", "-cq", "23",
        "-g", "0", "-bf", "0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        str(proxy),
    ]
    logger.info("Generating proxy: %s -> %s", src.name, proxy.name)
    subprocess.run(cmd, check=True, capture_output=True)
    return proxy


def build_timelines() -> dict[str, tuple[EditTimeline, dict[Path, Path]]]:
    """Define T1, T2, T3 edit timelines and their proxy mappings."""
    src_h264 = SAMPLES_DIR / "4k60_h264_8bit.mp4"
    src_hevc10 = SAMPLES_DIR / "4k60_hevc_10bit.mp4"
    src_4k120 = SAMPLES_DIR / "4k120_h264.mp4"

    proxy_h264 = PROXIES_DIR / "proxy_4k60_h264_gop1.mp4"
    proxy_hevc10 = PROXIES_DIR / "proxy_4k60_hevc10_gop1.mp4"
    proxy_120 = PROXIES_DIR / "proxy_4k120_1080p.mp4"

    ensure_proxy(src_h264, proxy_h264, height=1080, fps=60.0)
    ensure_proxy(src_hevc10, proxy_hevc10, height=1080, fps=60.0)
    ensure_proxy(src_4k120, proxy_120, height=1080, fps=120.0)

    timelines: dict[str, tuple[EditTimeline, dict[Path, Path]]] = {}

    # T1: 12 cuts @ 60 fps (12 clips of 2.5s = 150 frames each, total 1800 frames = 30.0s)
    # Alternating between 4K60 H.264 and 4K60 HEVC 10-bit proxies
    clips_t1: list[EditClip] = []
    for i in range(12):
        is_hevc = (i % 2 == 1)
        src = src_hevc10 if is_hevc else src_h264
        start_sec = (i * 2.0) % 25.0
        clips_t1.append(
            EditClip(
                source_path=src,
                source_in=start_sec,
                frames=150,
                available=30.0,
                label=f"T1_Clip_{i+1}_{'HEVC10' if is_hevc else 'H264'}",
            )
        )
    tl_t1 = EditTimeline(
        name="T1_12cuts_60fps",
        fps=60.0,
        canvas_width=1920,
        canvas_height=1080,
        clips=clips_t1,
    )
    timelines["T1"] = (tl_t1, {src_h264: proxy_h264, src_hevc10: proxy_hevc10})

    # T2: 30 cuts @ 60 fps (30 clips of 1s = 60 frames each, total 1800 frames = 30.0s)
    clips_t2: list[EditClip] = []
    for i in range(30):
        is_hevc = (i % 2 == 1)
        src = src_hevc10 if is_hevc else src_h264
        start_sec = (i * 0.9) % 27.0
        clips_t2.append(
            EditClip(
                source_path=src,
                source_in=start_sec,
                frames=60,
                available=30.0,
                label=f"T2_Clip_{i+1}_{'HEVC10' if is_hevc else 'H264'}",
            )
        )
    tl_t2 = EditTimeline(
        name="T2_30cuts_60fps",
        fps=60.0,
        canvas_width=1920,
        canvas_height=1080,
        clips=clips_t2,
    )
    timelines["T2"] = (tl_t2, {src_h264: proxy_h264, src_hevc10: proxy_hevc10})

    # T3: 12 cuts @ 120 fps (12 clips of 2.5s = 300 frames each, total 3600 frames = 30.0s)
    # Using 4k120 proxy with varying in-points
    clips_t3: list[EditClip] = []
    for i in range(12):
        start_sec = (i * 2.0) % 25.0
        clips_t3.append(
            EditClip(
                source_path=src_4k120,
                source_in=start_sec,
                frames=300,
                available=30.0,
                label=f"T3_Clip_{i+1}_4K120",
            )
        )
    tl_t3 = EditTimeline(
        name="T3_12cuts_120fps",
        fps=120.0,
        canvas_width=1920,
        canvas_height=1080,
        clips=clips_t3,
    )
    timelines["T3"] = (tl_t3, {src_4k120: proxy_120})

    return timelines


def generate_workspaces() -> dict[str, Path]:
    """Generate workspaces for T1, T2, T3 in studio-workspaces-s04."""
    timelines = build_timelines()
    WS_DIR.mkdir(parents=True, exist_ok=True)
    ws_paths: dict[str, Path] = {}

    for name, (tl, proxies) in timelines.items():
        ws_path = WS_DIR / f"ws_{name.lower()}"
        shutil.rmtree(ws_path, ignore_errors=True)
        proj_id = write_freecut_project(
            timeline=tl,
            workspace=ws_path,
            project_name=tl.name,
            proxies=proxies,
        )
        ws_paths[name] = ws_path
        # Also write cut metadata JSON into workspace for the test harness to read
        cut_meta = {
            "name": name,
            "fps": tl.fps,
            "total_frames": tl.total_frames,
            "duration": tl.duration,
            "cut_points": tl.cut_points()[1:-1],  # internal cut timestamps
            "clips_count": len(tl.clips),
            "project_id": proj_id,
        }
        (ws_path / "spike_cuts_metadata.json").write_text(json.dumps(cut_meta, indent=2), encoding="utf-8")
        logger.info("Generated workspace [%s]: %s (Project: %s, %d cuts)", name, ws_path, proj_id, len(cut_meta["cut_points"]))

    return ws_paths


def run_render_preview() -> dict[str, dict[str, Any]]:
    """Task C: Render T1 and T3 timelines into continuous 1080p all-intra H.264 files.

    Uses NVENC (h264_nvenc with -g 0 / all-intra).
    Measures render time, real-time factor, and creates single-file playback workspaces.
    """
    timelines = build_timelines()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    results: dict[str, dict[str, Any]] = {}

    for name in ["T1", "T3"]:
        tl, proxies = timelines[name]
        out_file = OUTPUT_DIR / f"preview_{name.lower()}_allintra.mp4"
        out_file.unlink(missing_ok=True)

        logger.info("Rendering preview for %s (%d clips, %.1fs @ %.0ffps)...", name, len(tl.clips), tl.duration, tl.fps)

        # Build ffmpeg command with filter_complex to concatenate all clips seamlessly into one all-intra stream
        # Each clip uses its proxy or source with trim and setpts
        filter_complex_lines: list[str] = []
        input_args: list[str] = []

        for idx, clip in enumerate(tl.clips):
            # Use proxy file as source for render preview
            clip_src = proxies.get(clip.source_path, clip.source_path)
            clip_dur = clip.frames / tl.fps
            input_args.extend(["-ss", f"{clip.source_in:.3f}", "-t", f"{clip_dur:.3f}", "-i", str(clip_src)])
            filter_complex_lines.append(f"[{idx}:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={tl.fps}[v{idx}]")

        concat_inputs = "".join(f"[v{i}]" for i in range(len(tl.clips)))
        filter_complex_lines.append(f"{concat_inputs}concat=n={len(tl.clips)}:v=1:a=0[vout]")

        filter_script = OUTPUT_DIR / f"render_script_{name.lower()}.txt"
        filter_script.write_text(";\n".join(filter_complex_lines), encoding="utf-8")

        cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-nostdin",
            *input_args,
            "-filter_complex_script", str(filter_script),
            "-map", "[vout]",
            "-c:v", "h264_nvenc",
            "-preset", "p7",
            "-cq", "18",
            "-g", "0",  # All-Intra
            "-bf", "0",
            "-pix_fmt", "yuv420p",
            "-r", str(tl.fps),
            "-an",
            str(out_file),
        ]

        t0 = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        t1 = time.perf_counter()
        render_duration = t1 - t0

        if proc.returncode != 0 or not out_file.exists():
            logger.error("Render failed for %s:\n%s", name, proc.stderr[-1000:])
            raise RuntimeError(f"Render failed for {name}")

        speed_factor = tl.duration / render_duration if render_duration > 0 else 0.0
        file_size_mb = out_file.stat().st_size / (1024 * 1024)

        logger.info(
            "Rendered %s in %.2fs (%.2f× real-time, %.2f MB)",
            name, render_duration, speed_factor, file_size_mb,
        )

        # Create single-file playback workspace for FreeCut
        ws_preview = WS_DIR / f"ws_{name.lower()}_rendered"
        shutil.rmtree(ws_preview, ignore_errors=True)
        single_clip = EditClip(
            source_path=out_file,
            source_in=0.0,
            frames=tl.total_frames,
            available=tl.duration,
            label=f"{name}_Rendered_Preview",
        )
        tl_preview = EditTimeline(
            name=f"{name}_Rendered_Preview",
            fps=tl.fps,
            canvas_width=1920,
            canvas_height=1080,
            clips=[single_clip],
        )
        proj_id = write_freecut_project(
            timeline=tl_preview,
            workspace=ws_preview,
            project_name=tl_preview.name,
            proxies=None,
        )
        cut_meta = {
            "name": f"{name}_rendered",
            "fps": tl.fps,
            "total_frames": tl.total_frames,
            "duration": tl.duration,
            "cut_points": [],  # no cuts in continuous preview
            "clips_count": 1,
            "project_id": proj_id,
            "render_time_s": round(render_duration, 2),
            "speed_factor_x": round(speed_factor, 2),
        }
        (ws_preview / "spike_cuts_metadata.json").write_text(json.dumps(cut_meta, indent=2), encoding="utf-8")

        results[name] = {
            "render_time_s": round(render_duration, 2),
            "speed_factor_x": round(speed_factor, 2),
            "file_size_mb": round(file_size_mb, 2),
            "output_file": str(out_file),
            "workspace": str(ws_preview),
            "project_id": proj_id,
        }

    summary_file = OUTPUT_DIR / "render_preview_results.json"
    summary_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    logger.info("Render preview benchmark saved to %s", summary_file)
    return results


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "all"
    if action in ("workspaces", "all"):
        generate_workspaces()
    if action in ("render", "all"):
        run_render_preview()
