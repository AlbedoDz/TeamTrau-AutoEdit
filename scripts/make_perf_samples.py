"""Generate synthetic performance test media samples using FFmpeg lavfi filters.

Generates 6 standardized 30-second benchmark files into E:\\antigravity\\perf-samples\\:
1. 4k60_h264_8bit.mp4 (3840x2160 @ 60fps, 8-bit H.264)
2. 4k60_hevc_10bit.mp4 (3840x2160 @ 60fps, 10-bit HEVC 4:2:0)
3. 4k60_hevc_422_10bit.mov (3840x2160 @ 60fps, 10-bit HEVC 4:2:2)
4. 4k120_h264.mp4 (3840x2160 @ 120fps, 8-bit H.264)
5. phone_vfr.mp4 (Variable Frame Rate with simulated phone camera dropped frames)
6. 1080p60_h264_allintra.mp4 (1920x1080 @ 60fps, H.264 All-Intra GOP=1)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


def run_cmd(cmd: list[str]) -> None:
    print(f"Running: {' '.join(cmd)}")
    t0 = time.perf_counter()
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    dt = time.perf_counter() - t0
    if res.returncode != 0:
        print(f"Command failed with exit code {res.returncode}:\n{res.stderr[-1000:]}", file=sys.stderr)
        raise RuntimeError(f"FFmpeg failed: {res.returncode}")
    print(f"Done in {dt:.2f}s")


def generate_samples(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = "30"

    samples: list[tuple[str, list[str]]] = [
        # 1. 4k60_h264_8bit.mp4
        (
            "4k60_h264_8bit.mp4",
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=60",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", duration,
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                str(output_dir / "4k60_h264_8bit.mp4")
            ]
        ),
        # 2. 4k60_hevc_10bit.mp4
        (
            "4k60_hevc_10bit.mp4",
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=60",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", duration,
                "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
                "-c:a", "aac", "-b:a", "192k",
                str(output_dir / "4k60_hevc_10bit.mp4")
            ]
        ),
        # 3. 4k60_hevc_422_10bit.mov
        (
            "4k60_hevc_422_10bit.mov",
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=60",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", duration,
                "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv422p10le",
                "-c:a", "aac", "-b:a", "192k",
                str(output_dir / "4k60_hevc_422_10bit.mov")
            ]
        ),
        # 4. 4k120_h264.mp4
        (
            "4k120_h264.mp4",
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=120",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", duration,
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                str(output_dir / "4k120_h264.mp4")
            ]
        ),
        # 5. phone_vfr.mp4 (VFR simulation via drop filter and -fps_mode vfr)
        (
            "phone_vfr.mp4",
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", duration,
                "-vf", "select='not(eq(mod(n\\,13)\\,0))'",
                "-fps_mode", "vfr",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                str(output_dir / "phone_vfr.mp4")
            ]
        ),
        # 6. 1080p60_h264_allintra.mp4
        (
            "1080p60_h264_allintra.mp4",
            [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", duration,
                "-c:v", "libx264", "-preset", "veryfast", "-g", "1", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                str(output_dir / "1080p60_h264_allintra.mp4")
            ]
        ),
    ]

    for filename, cmd in samples:
        target = output_dir / filename
        print(f"\n--- Generating {filename} ---")
        run_cmd(cmd)
        if target.exists():
            size_mb = target.stat().st_size / (1024 * 1024)
            print(f"Created: {target} ({size_mb:.2f} MB)")
        else:
            print(f"ERROR: Expected target {target} was not created!", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic performance test media")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("E:/antigravity/perf-samples"),
        help="Target directory for generated perf samples",
    )
    args = parser.parse_args()
    generate_samples(args.output_dir)


if __name__ == "__main__":
    main()
