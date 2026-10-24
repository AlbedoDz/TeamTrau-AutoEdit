"""Spike S0.2 Runner: Curate perf samples and export FreeCut project workspace.

Sets up A-roll / B-roll / Music layout from E:/antigravity/perf-samples,
runs curation to generate an EditTimeline, and exports to E:/antigravity/studio-workspace-s02.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.core.curation import assemble_sequence, run_curation  # noqa: E402
from src.core.service import StudioBackendAdapter  # noqa: E402
from src.integrations.freecut_writer import write_freecut_project  # noqa: E402


def setup_sample_project(perf_samples_dir: Path, target_dir: Path) -> Path:
    """Set up A-roll, B-roll, and Music folder structure with hard links."""
    target_dir.mkdir(parents=True, exist_ok=True)
    aroll_dir = target_dir / "A-roll"
    broll_dir = target_dir / "B-roll"
    music_dir = target_dir / "Music"

    aroll_dir.mkdir(parents=True, exist_ok=True)
    broll_dir.mkdir(parents=True, exist_ok=True)
    music_dir.mkdir(parents=True, exist_ok=True)

    sample_1 = perf_samples_dir / "1080p60_h264_allintra.mp4"
    sample_2 = perf_samples_dir / "4k60_h264_8bit.mp4"

    if not sample_1.exists() or not sample_2.exists():
        raise FileNotFoundError(f"Missing required perf samples in {perf_samples_dir}")

    # Hardlink or copy sample 1 into A-roll
    clip1 = aroll_dir / "shot_aroll_01.mp4"
    if not clip1.exists():
        try:
            os.link(sample_1, clip1)
        except OSError:
            shutil.copy2(sample_1, clip1)

    # Hardlink or copy sample 2 into B-roll
    clip2 = broll_dir / "shot_broll_01.mp4"
    if not clip2.exists():
        try:
            os.link(sample_2, clip2)
        except OSError:
            shutil.copy2(sample_2, clip2)

    # Extract 15s audio as soundtrack in Music/
    soundtrack = music_dir / "soundtrack.mp3"
    if not soundtrack.exists():
        cmd = [
            "ffmpeg", "-y", "-i", str(sample_1),
            "-t", "15", "-vn", "-c:a", "libmp3lame", "-b:a", "192k",
            str(soundtrack)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0:
            # Fallback copy
            shutil.copy2(sample_1, soundtrack)

    return target_dir


def main() -> None:
    perf_samples_dir = Path("E:/antigravity/perf-samples")
    project_input_dir = perf_samples_dir / "project_s02"
    workspace_dir = Path("E:/antigravity/studio-workspace-s02")

    print("=== SPIKE S0.2: Curate & Export to FreeCut Workspace ===")
    print(f"Setting up sample project at: {project_input_dir}")
    setup_sample_project(perf_samples_dir, project_input_dir)

    adapter = StudioBackendAdapter()
    output_dir = project_input_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Running curation on {project_input_dir}...")
    session, summary = run_curation(
        adapter=adapter,
        input_dir=project_input_dir,
        genre="travel_daily_vlog",
        output_dir=output_dir,
        top_n=6,
    )
    print(f"Curation complete. Session ID: {session.session_id}, Total scenes: {summary.get('total_scenes')}")

    # Assemble sequence with soundtrack
    soundtrack_file = project_input_dir / "Music" / "soundtrack.mp3"
    print(f"Assembling cadence-driven sequence with {soundtrack_file}...")
    seq_res = assemble_sequence(
        adapter=adapter,
        session=session,
        soundtrack=soundtrack_file if soundtrack_file.exists() else None,
    )
    print(f"Sequence assembled: {seq_res}")

    # Retrieve the assembled EditTimeline
    active_name = session.sequence.active_variant_name or "master"
    timeline = session.sequence.edit_timelines.get(active_name)
    if not timeline and session.sequence.edit_timelines:
        timeline = next(iter(session.sequence.edit_timelines.values()))

    if not timeline:
        raise RuntimeError("No EditTimeline produced by curation/sequence assembly")

    print("\nTimeline details:")
    print(f"  Name: {timeline.name}")
    print(f"  FPS: {timeline.fps}")
    print(f"  Canvas: {timeline.canvas_width}x{timeline.canvas_height}")
    print(f"  Clips count: {len(timeline.clips)}")
    print(f"  Total frames: {timeline.total_frames} ({timeline.duration:.2f}s)")
    print(f"  Sections count: {len(timeline.sections)}")
    print(f"  Bar grid: {len(timeline.bar_grid.bar_times) if timeline.bar_grid else 0} bars")

    # Export to FreeCut workspace
    print(f"\nExporting to FreeCut workspace at {workspace_dir}...")
    project_id = write_freecut_project(
        timeline=timeline,
        workspace=workspace_dir,
        project_name="TeamTrau S02 Rough Cut",
    )

    print("\n[SUCCESS] Project written to FreeCut workspace!")
    print(f"  Project ID: {project_id}")
    print(f"  Project JSON: {workspace_dir / 'projects' / project_id / 'project.json'}")
    print(f"  Workspace Marker: {workspace_dir / '.freecut-workspace.json'}")
    print(f"  Index: {workspace_dir / 'index.json'}")

    # Print project.json summary
    pj_file = workspace_dir / "projects" / project_id / "project.json"
    pj = json.loads(pj_file.read_text(encoding="utf-8"))
    print("\nProject summary verification:")
    print(f"  Schema Version: {pj.get('schemaVersion')}")
    print(f"  Tracks: {[t['name'] for t in pj['timeline']['tracks']]}")
    print(f"  Items: {len(pj['timeline']['items'])} items")
    print(f"  Markers: {len(pj['timeline']['markers'])} markers")


if __name__ == "__main__":
    main()
