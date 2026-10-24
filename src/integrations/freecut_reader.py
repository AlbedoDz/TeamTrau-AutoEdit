"""FreeCut Project Reader (Studio -> TeamTrau Engine Hand-off).

Deserializes a FreeCut v2.0 workspace project (schemaVersion <= 15) into
a FreeCutTimeline containing an EditTimeline and clip start frames.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.core.types import EditClip, EditTimeline
from src.integrations.freecut_writer import sanitize_workspace_filename


@dataclass(frozen=True)
class FreeCutTimeline:
    timeline: EditTimeline  # V1 (top-most video track) items in time order
    starts: tuple[int, ...]  # timeline start frame of each clip (gaps allowed)
    project_id: str


def read_freecut_project(workspace: Path, project_id: str) -> FreeCutTimeline:
    """Read a FreeCut project from workspace and convert to FreeCutTimeline.

    Args:
        workspace: Path to the FreeCut workspace directory.
        project_id: Unique project identifier.

    Returns:
        FreeCutTimeline with EditTimeline, clip starts, and project id.

    Raises:
        FileNotFoundError: If project.json does not exist.
        ValueError: If schemaVersion > 15, or if media referenced by items is missing.
    """
    workspace = Path(workspace)
    project_file = workspace / "projects" / project_id / "project.json"
    if not project_file.is_file():
        raise FileNotFoundError(f"FreeCut project file not found: {project_file}")

    proj = json.loads(project_file.read_text(encoding="utf-8"))

    schema_version = int(proj.get("schemaVersion", 1))
    if schema_version > 15:
        raise ValueError(
            f"unsupported schema version {schema_version} (max supported: 15)"
        )

    metadata = proj.get("metadata") or {}
    fps = float(metadata.get("fps", 30.0))
    canvas_width = int(metadata.get("width", 1920))
    canvas_height = int(metadata.get("height", 1080))
    project_name = str(proj.get("name") or project_id)

    timeline_data = proj.get("timeline") or {}
    tracks = timeline_data.get("tracks") or []
    items = timeline_data.get("items") or []

    # Find the first video track in track order
    video_tracks = [
        t
        for t in tracks
        if t.get("kind") == "video"
        or t.get("type") == "video"
        or str(t.get("id", "")).startswith("track-v")
    ]
    video_tracks.sort(key=lambda t: int(t.get("order", 0)))

    if video_tracks:
        first_video_track_id = video_tracks[0].get("id")
        video_items = [
            i
            for i in items
            if i.get("trackId") == first_video_track_id and i.get("type") == "video"
        ]
    else:
        video_items = [i for i in items if i.get("type") == "video"]

    # Sort video items by "from"
    video_items.sort(key=lambda i: int(i.get("from", 0)))

    starts: list[int] = []
    clips: list[EditClip] = []

    for item in video_items:
        start_frame = int(item.get("from", 0))
        starts.append(start_frame)

        media_id = item.get("mediaId")
        if not media_id:
            raise ValueError(f"Video item {item.get('id', 'unknown')} missing mediaId")

        media_dir = workspace / "media" / str(media_id)
        if not media_dir.exists():
            raise ValueError(f"media {media_id} is missing")

        # Resolve clip source_path: source.link.json path when present, else metadata.json fileName
        source_path: Path | None = None
        source_link_file = media_dir / "source.link.json"
        metadata_file = media_dir / "metadata.json"

        if source_link_file.exists():
            try:
                link_data = json.loads(source_link_file.read_text(encoding="utf-8"))
                link_path_str = link_data.get("path")
                if link_path_str:
                    source_path = Path(link_path_str)
            except Exception:
                pass

        if source_path is None:
            if metadata_file.exists():
                try:
                    meta_data = json.loads(metadata_file.read_text(encoding="utf-8"))
                    file_name = meta_data.get("fileName")
                    if file_name:
                        direct = media_dir / file_name
                        if direct.exists():
                            source_path = direct
                        else:
                            sanitized = media_dir / sanitize_workspace_filename(file_name)
                            if sanitized.exists():
                                source_path = sanitized
                            else:
                                source_path = direct
                except Exception:
                    pass

        if source_path is None:
            raise ValueError(f"media {media_id} source path could not be resolved")

        source_fps = float(item.get("sourceFps") or fps)
        source_start = float(item.get("sourceStart", 0))
        duration_frames = int(item.get("durationInFrames", 0))
        speed = float(item.get("speed", 1.0))

        if "sourceEnd" in item:
            source_end = float(item["sourceEnd"])
        else:
            source_end = source_start + duration_frames

        source_in = source_start / source_fps if source_fps > 0 else 0.0
        frames = duration_frames
        available = (source_end - source_start) / source_fps if source_fps > 0 else 0.0

        label = str(item.get("label", ""))

        clip = EditClip(
            source_path=source_path,
            source_in=source_in,
            frames=frames,
            available=available,
            speed=speed,
            label=label,
        )
        clips.append(clip)

    timeline = EditTimeline(
        name=project_name,
        fps=fps,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        clips=clips,
    )

    return FreeCutTimeline(
        timeline=timeline,
        starts=tuple(starts),
        project_id=project_id,
    )
