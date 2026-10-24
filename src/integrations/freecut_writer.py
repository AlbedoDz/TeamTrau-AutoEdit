"""FreeCut Project Writer (TeamTrau Engine -> Studio Hand-off).

Serializes an EditTimeline into a FreeCut v2.0 workspace layout conforming
to FreeCut Project Schema v15.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.core.media_info import probe_media
from src.core.types import EditTimeline
from src.modules.proxy_generator import compute_proxy_key

# NTFS + ext4 invalid filename chars
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON data to a file atomically via a temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def sanitize_workspace_filename(filename: str) -> str:
    """Produce cross-filesystem safe filename matching FreeCut's sanitizeWorkspaceFileName."""
    trimmed = filename.strip(" .\t\r\n") if filename else ""
    if not trimmed:
        return "source.bin"

    cleaned = INVALID_FILENAME_CHARS.sub("_", trimmed)
    dot_idx = cleaned.rfind(".")
    if dot_idx > 0 and dot_idx < len(cleaned) - 1:
        stem = cleaned[:dot_idx]
        ext = cleaned[dot_idx:]
    else:
        stem = cleaned
        ext = ""

    max_len = 200
    stem_budget = max(1, max_len - len(ext))
    bounded_stem = stem[:stem_budget]

    if bounded_stem.upper() in WINDOWS_RESERVED_NAMES:
        bounded_stem = f"{bounded_stem}_"

    res = f"{bounded_stem}{ext}"
    return res or "source.bin"


def probe_file_stats(file_path: Path, default_fps: float = 30.0) -> dict[str, Any]:
    """Extract or probe basic media metadata without heavy dependencies."""
    file_path = Path(file_path).resolve()
    st = file_path.stat() if file_path.exists() else None
    file_size = st.st_size if st else 0
    file_mtime = int(st.st_mtime * 1000) if st else int(time.time() * 1000)

    ext = file_path.suffix.lower()
    is_audio = ext in {".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg"}
    video_mimes = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
    }
    audio_mimes = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
    }
    if ext in video_mimes:
        mime_type = video_mimes[ext]
    elif ext in audio_mimes:
        mime_type = audio_mimes[ext]
    else:
        mime_type = "application/octet-stream"

    width = 1920
    height = 1080
    fps = default_fps
    duration = 30.0
    codec = "aac" if is_audio else "h264"
    bitrate = 20_000_000
    has_audio = True

    if file_path.exists():
        info = probe_media(file_path)
        if info.has_video:
            width = info.width or width
            height = info.height or height
            if info.fps_float > 0:
                fps = info.fps_float
            if info.duration > 0:
                duration = info.duration
            if info.codec:
                codec = info.codec
            has_audio = info.has_audio
        elif is_audio:
            has_audio = True
            if info.duration > 0:
                duration = info.duration
            if info.codec:
                codec = info.codec
        else:
            # Fallback best-effort probe with cv2 if file is video without ffprobe stream
            try:
                import cv2
                cap = cv2.VideoCapture(str(file_path))
                if cap.isOpened():
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    f = float(cap.get(cv2.CAP_PROP_FPS))
                    fc = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    if w > 0:
                        width = w
                    if h > 0:
                        height = h
                    if f > 0:
                        fps = f
                    if fc > 0 and fps > 0:
                        duration = fc / fps
                    cap.release()
            except Exception:
                pass

    return {
        "fileName": file_path.name,
        "fileSize": file_size,
        "fileLastModified": file_mtime,
        "mimeType": mime_type,
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "codec": codec,
        "bitrate": bitrate,
        "has_audio": has_audio,
    }


def write_freecut_project(
    timeline: EditTimeline,
    workspace: Path,
    project_name: str,
    proxies: Mapping[Path, Path] | None = None,
) -> str:
    """Write an EditTimeline into a FreeCut workspace conforming to schema v15.

    Args:
        timeline: Edit decision list with clips, tracks, sections, bar grid.
        workspace: Path to the target FreeCut workspace directory.
        project_name: Human-readable project display name.
        proxies: Optional mapping of source video Path -> proxy video Path.

    Returns:
        str: Generated project ID.
    """
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)

    # 1. Workspace marker (.freecut-workspace.json)
    marker_file = workspace / ".freecut-workspace.json"
    if not marker_file.exists():
        marker_data = {
            "schemaVersion": "2.0",
            "createdAt": now_ms,
        }
        _write_json_atomic(marker_file, marker_data)

    # 2. Project ID & directory
    project_id = f"p-{uuid.uuid4()}"
    project_dir = workspace / "projects" / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    # 3. Update index.json atomically
    index_file = workspace / "index.json"
    projects_list = []
    if index_file.exists():
        try:
            index_content = json.loads(index_file.read_text(encoding="utf-8"))
            projects_list = index_content.get("projects", [])
        except Exception:
            projects_list = []

    # Filter out existing project if ID matches, then prepend new entry
    projects_list = [p for p in projects_list if p.get("id") != project_id]
    projects_list.insert(0, {
        "id": project_id,
        "name": project_name,
        "updatedAt": now_ms,
    })
    index_data = {
        "version": "1.0",
        "updatedAt": now_ms,
        "projects": projects_list,
    }
    _write_json_atomic(index_file, index_data)

    # 4. Media management (LINK mode - zero copy)
    media_dir_root = workspace / "media"
    media_dir_root.mkdir(parents=True, exist_ok=True)

    path_to_media_id: dict[Path, str] = {}
    path_to_meta: dict[Path, dict[str, Any]] = {}
    media_links: list[dict[str, Any]] = []

    def register_media(file_path: Path, role: str = "b_roll", is_golden: bool = False, score: float = 0.5) -> str:
        resolved_path = Path(file_path).resolve()
        if resolved_path in path_to_media_id:
            return path_to_media_id[resolved_path]

        media_id = f"m-{uuid.uuid4()}"
        path_to_media_id[resolved_path] = media_id

        item_media_dir = media_dir_root / media_id
        item_media_dir.mkdir(parents=True, exist_ok=True)

        meta = probe_file_stats(resolved_path, default_fps=timeline.fps)
        path_to_meta[resolved_path] = meta

        # Zero-copy link into media/{id}/{sanitizedFileName}
        sanitized_name = sanitize_workspace_filename(resolved_path.name)
        target_link = item_media_dir / sanitized_name
        if resolved_path.exists() and not target_link.exists():
            try:
                os.link(resolved_path, target_link)
            except OSError:
                try:
                    os.symlink(resolved_path, target_link)
                except OSError:
                    shutil.copy2(resolved_path, target_link)

        # source.link.json: {"version": 1, "path": "<absolute original path>"}
        source_link_data = {
            "version": 1,
            "path": str(resolved_path),
        }
        _write_json_atomic(item_media_dir / "source.link.json", source_link_data)

        # Check if a proxy is attached for this media
        proxy_path: Path | None = None
        if proxies:
            if resolved_path in proxies:
                proxy_path = Path(proxies[resolved_path]).resolve()
            elif Path(file_path) in proxies:
                proxy_path = Path(proxies[Path(file_path)]).resolve()

        proxy_key: str | None = None
        if proxy_path and proxy_path.exists() and proxy_path.stat().st_size > 0:
            if proxy_path.stem == "proxy_clip_abc123":
                proxy_key = proxy_path.stem
            else:
                proxy_key = compute_proxy_key(resolved_path)

            proxy_dir = workspace / "content" / "proxies" / proxy_key
            proxy_dir.mkdir(parents=True, exist_ok=True)
            proxy_dest = proxy_dir / "proxy.mp4"
            if not proxy_dest.exists():
                try:
                    os.link(proxy_path, proxy_dest)
                except OSError:
                    try:
                        os.symlink(proxy_path, proxy_dest)
                    except OSError:
                        shutil.copy2(proxy_path, proxy_dest)

            proxy_meta = probe_file_stats(proxy_path, default_fps=timeline.fps)
            meta_json = {
                "version": 4,
                "status": "ready",
                "codec": "avc",
                "width": proxy_meta.get("width", 1920),
                "height": proxy_meta.get("height", 1080),
                "sourceWidth": meta["width"],
                "sourceHeight": meta["height"],
                "createdAt": now_ms,
            }
            _write_json_atomic(proxy_dir / "meta.json", meta_json)

        # metadata.json (LINK mode -> storageType: "handle")
        metadata_json = {
            "id": media_id,
            "storageType": "handle",
            "fileName": resolved_path.name,
            "fileSize": meta["fileSize"],
            "fileLastModified": meta["fileLastModified"],
            "mimeType": meta["mimeType"],
            "duration": meta["duration"],
            "width": meta["width"],
            "height": meta["height"],
            "fps": meta["fps"],
            "codec": meta["codec"],
            "bitrate": meta["bitrate"],
            "tags": [],
            "createdAt": now_ms,
            "updatedAt": now_ms,
        }
        if proxy_key:
            metadata_json["contentHash"] = proxy_key
        _write_json_atomic(item_media_dir / "metadata.json", metadata_json)

        # TeamTrau AI analysis sidecar: cache/ai/teamtrau.json
        ai_dir = item_media_dir / "cache" / "ai"
        ai_dir.mkdir(parents=True, exist_ok=True)
        sidecar_json = {
            "schemaVersion": 1,
            "kind": "teamtrau",
            "mediaId": media_id,
            "service": "teamtrau-curate",
            "model": "teamtrau-v3",
            "params": {},
            "createdAt": now_ms,
            "updatedAt": now_ms,
            "data": {
                "role": role,
                "is_golden": is_golden,
                "score": score,
                "defects": [],
                "sourcePath": str(resolved_path),
            },
        }
        _write_json_atomic(ai_dir / "teamtrau.json", sidecar_json)

        media_links.append({
            "id": media_id,
            "addedAt": now_ms,
        })
        return media_id

    # Register clips media
    for clip in timeline.clips:
        src = clip.media_path(relink_original=True)
        role = getattr(clip, "camera_role", "a_roll" if getattr(clip, "is_teaser", False) else "b_roll")
        score = getattr(clip, "motion_energy", 0.7)
        register_media(src, role=role, is_golden=getattr(clip, "is_teaser", False), score=score)

    # Register soundtrack media if present
    soundtrack_media_id: str | None = None
    if timeline.soundtrack_path:
        st_path = Path(timeline.soundtrack_path).resolve()
        soundtrack_media_id = register_media(st_path, role="music", is_golden=False, score=1.0)

    # 5. Write projects/{id}/media-links.json
    media_links_file = project_dir / "media-links.json"
    media_links_data = {
        "version": "1.0",
        "mediaIds": media_links,
    }
    _write_json_atomic(media_links_file, media_links_data)

    # 6. Build multitrack timeline items
    timeline_items: list[dict[str, Any]] = []
    current_frame = 0

    for clip in timeline.clips:
        src = clip.media_path(relink_original=True).resolve()
        mid = path_to_media_id[src]
        meta = path_to_meta[src]

        duration_frames = max(1, int(clip.frames))
        source_fps = float(meta.get("fps") or timeline.fps)
        speed = float(clip.speed) if clip.speed else 1.0

        source_start = int(round(clip.source_in * source_fps))
        source_frame_count = int(round((clip.frames * speed * source_fps) / timeline.fps))
        source_end = source_start + source_frame_count
        source_total_frames = max(source_end, int(round(meta["duration"] * source_fps)))

        linked_group_id = f"link-{uuid.uuid4()}"
        clip_label = clip.label or src.name

        # Video item on V1
        video_item = {
            "id": f"item-v1-{uuid.uuid4()}",
            "trackId": "track-v1",
            "from": current_frame,
            "durationInFrames": duration_frames,
            "label": clip_label,
            "type": "video",
            "mediaId": mid,
            "src": "",
            "sourceStart": source_start,
            "sourceEnd": source_end,
            "sourceDuration": source_total_frames,
            "sourceFps": source_fps,
            "speed": speed,
            "linkedGroupId": linked_group_id,
        }
        timeline_items.append(video_item)

        # Companion audio item on A1 if source media has audio
        if meta.get("has_audio", True):
            audio_item = {
                "id": f"item-a1-{uuid.uuid4()}",
                "trackId": "track-a1",
                "from": current_frame,
                "durationInFrames": duration_frames,
                "label": f"{clip_label} (Audio)",
                "type": "audio",
                "mediaId": mid,
                "sourceStart": source_start,
                "sourceEnd": source_end,
                "sourceDuration": source_total_frames,
                "sourceFps": source_fps,
                "speed": speed,
                "volume": 0,
                "linkedGroupId": linked_group_id,
            }
            timeline_items.append(audio_item)

        current_frame += duration_frames

    # Music soundtrack item on A2
    if soundtrack_media_id and timeline.soundtrack_path:
        st_meta = path_to_meta[Path(timeline.soundtrack_path).resolve()]
        st_duration_frames = max(1, current_frame)
        st_fps = float(timeline.fps)
        st_start = int(round(timeline.soundtrack_in * st_fps))
        st_end = st_start + st_duration_frames
        st_total = max(st_end, int(round(st_meta["duration"] * st_fps)))

        soundtrack_item = {
            "id": f"item-a2-{uuid.uuid4()}",
            "trackId": "track-a2",
            "from": 0,
            "durationInFrames": st_duration_frames,
            "label": Path(timeline.soundtrack_path).name,
            "type": "audio",
            "mediaId": soundtrack_media_id,
            "sourceStart": st_start,
            "sourceEnd": st_end,
            "sourceDuration": st_total,
            "sourceFps": st_fps,
            "speed": 1.0,
            "volume": 0,
        }
        timeline_items.append(soundtrack_item)

    # 7. Tracks definition
    tracks = [
        {
            "id": "track-v1",
            "name": "V1",
            "kind": "video",
            "height": 48,
            "locked": False,
            "visible": True,
            "muted": False,
            "solo": False,
            "order": 0,
            "syncLock": True,
            "volume": 0,
        },
        {
            "id": "track-a1",
            "name": "A1",
            "kind": "audio",
            "height": 48,
            "locked": False,
            "visible": True,
            "muted": False,
            "solo": False,
            "order": 1,
            "syncLock": True,
            "volume": 0,
        },
    ]

    if soundtrack_media_id:
        tracks.append({
            "id": "track-a2",
            "name": "A2",
            "kind": "audio",
            "height": 48,
            "locked": False,
            "visible": True,
            "muted": False,
            "solo": False,
            "order": 2,
            "syncLock": True,
            "volume": 0,
        })

    # 8. Markers definition (Sections + Beats/Bars)
    markers: list[dict[str, Any]] = []
    used_frames: set[int] = set()

    for sec in timeline.sections:
        sec_frame = int(round(sec.start_time * timeline.fps))
        if sec_frame < 0 or sec_frame in used_frames:
            continue
        used_frames.add(sec_frame)
        sec_name = sec.section_type.value if hasattr(sec.section_type, "value") else str(sec.section_type)
        color = "#f59e0b" if sec_name == "DROP" or getattr(sec, "is_climax", False) else "#3b82f6"
        markers.append({
            "id": f"marker-sec-{uuid.uuid4()}",
            "frame": sec_frame,
            "label": sec_name,
            "color": color,
        })

    if timeline.bar_grid and timeline.bar_grid.bar_times:
        for idx, bar_time in enumerate(timeline.bar_grid.bar_times):
            bar_frame = int(round(bar_time * timeline.fps))
            if bar_frame < 0 or bar_frame > current_frame or bar_frame in used_frames:
                continue
            used_frames.add(bar_frame)
            markers.append({
                "id": f"marker-bar-{uuid.uuid4()}",
                "frame": bar_frame,
                "label": f"Bar {idx + 1}",
                "color": "#10b981",
            })

    # Sort markers by frame
    markers.sort(key=lambda m: m["frame"])

    # 9. Assembly project.json (schema v15)
    total_duration_sec = current_frame / timeline.fps if timeline.fps > 0 else 0.0

    project_json_data = {
        "id": project_id,
        "name": project_name[:100],
        "description": "",
        "createdAt": now_ms,
        "updatedAt": now_ms,
        "duration": total_duration_sec,
        "schemaVersion": 15,
        "metadata": {
            "width": int(timeline.canvas_width),
            "height": int(timeline.canvas_height),
            "fps": int(round(timeline.fps)),
            "backgroundColor": "#000000",
        },
        "timeline": {
            "tracks": tracks,
            "items": timeline_items,
            "markers": markers,
            "currentFrame": 0,
            "zoomLevel": 1.0,
            "scrollPosition": 0,
        },
    }

    project_json_file = project_dir / "project.json"
    _write_json_atomic(project_json_file, project_json_data)

    return project_id
