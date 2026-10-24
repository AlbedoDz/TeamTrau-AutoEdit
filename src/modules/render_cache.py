"""Render cache planning, keys, status, and intra-only rendering orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.core.config import PipelineConfig
from src.core.ffmpeg_utils import run_tool
from src.core.types import DeliverSettings, EditClip, EditTimeline
from src.dispatchers.nvenc_renderer import NvencRenderer

RENDER_CACHE_VERSION = 1
MANIFEST_NAME = "render_cache.json"


@dataclass(frozen=True)
class Segment:
    """A contiguous run of edit timeline clips rendered to an all-intra segment cache file."""

    index: int
    start_frame: int
    frames: int
    clip_indices: tuple[int, ...]
    key: str


@dataclass(frozen=True)
class CacheStatus:
    """On-disk status of a render cache segment."""

    segment: Segment
    path: Path
    ready: bool


def _clip_dict(c: EditClip) -> dict[str, Any]:
    resolved = Path(c.source_path).resolve()
    try:
        st = resolved.stat()
        st_size = st.st_size
        st_mtime_ns = st.st_mtime_ns
    except OSError:
        st_size = 0
        st_mtime_ns = 0

    crop_val = asdict(c.crop_window) if c.crop_window is not None else None
    return {
        "crop": crop_val,
        "frames": int(c.frames),
        "in": round(float(c.source_in), 6),
        "kb": c.ken_burns,
        "mtime_ns": st_mtime_ns,
        "path": str(resolved),
        "size": st_size,
        "speed": float(c.speed),
        "tr": int(c.transition_in_frames),
    }


def segment_key(timeline: EditTimeline, clips: Sequence[EditClip]) -> str:
    """Compute 40-hex SHA1 content hash for a segment of edit clips."""
    payload = {
        "canvas": [int(timeline.canvas_width), int(timeline.canvas_height)],
        "clips": [_clip_dict(c) for c in clips],
        "fps": float(timeline.fps),
        "v": RENDER_CACHE_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def plan_segments(
    timeline: EditTimeline,
    max_segment_sec: float = 10.0,
    starts: Sequence[int] | None = None,
) -> list[Segment]:
    """Group timeline clips into cache segments up to max_segment_sec without breaking dissolves."""
    clips = timeline.clips
    if not clips:
        return []

    fps = float(timeline.fps)
    max_frames = max_segment_sec * fps

    if starts is None:
        starts_list: list[int] = []
        cur = 0
        for c in clips:
            starts_list.append(cur)
            cur += c.frames
    else:
        starts_list = list(starts)

    segments_info: list[tuple[int, list[int]]] = []
    curr_indices: list[int] = []
    curr_start: int = 0
    curr_frames: int = 0

    for i, c in enumerate(clips):
        c_start = starts_list[i]
        c_frames = c.frames

        if not curr_indices:
            curr_indices = [i]
            curr_start = c_start
            curr_frames = c_frames
            continue

        prev_idx = curr_indices[-1]
        prev_end = starts_list[prev_idx] + clips[prev_idx].frames
        is_gap = c_start != prev_end

        if is_gap:
            segments_info.append((curr_start, curr_indices))
            curr_indices = [i]
            curr_start = c_start
            curr_frames = c_frames
            continue

        if curr_frames + c_frames <= max_frames:
            curr_indices.append(i)
            curr_frames += c_frames
            continue

        if c.transition_in_frames <= 0:
            segments_info.append((curr_start, curr_indices))
            curr_indices = [i]
            curr_start = c_start
            curr_frames = c_frames
            continue

        chain: list[int] = [prev_idx]
        for k in range(len(curr_indices) - 1, 0, -1):
            if clips[curr_indices[k]].transition_in_frames > 0:
                chain.append(curr_indices[k - 1])
            else:
                break
        chain.reverse()

        if len(chain) == len(curr_indices):
            curr_indices.append(i)
            curr_frames += c_frames
        else:
            remaining = curr_indices[: len(curr_indices) - len(chain)]
            segments_info.append((curr_start, remaining))
            curr_indices = chain + [i]
            curr_start = starts_list[chain[0]]
            curr_frames = sum(clips[idx].frames for idx in curr_indices)

    if curr_indices:
        segments_info.append((curr_start, curr_indices))

    result: list[Segment] = []
    for idx, (seg_start, indices) in enumerate(segments_info):
        seg_clips = [clips[j] for j in indices]
        seg_frames = sum(c.frames for c in seg_clips)
        k = segment_key(timeline, seg_clips)
        result.append(
            Segment(
                index=idx,
                start_frame=seg_start,
                frames=seg_frames,
                clip_indices=tuple(indices),
                key=k,
            )
        )
    return result


def cache_status(
    timeline: EditTimeline,
    cache_dir: Path,
    max_segment_sec: float = 10.0,
    starts: Sequence[int] | None = None,
) -> list[CacheStatus]:
    """Check readiness of each cache segment file on disk."""
    cache_dir = Path(cache_dir)
    segments = plan_segments(timeline, max_segment_sec=max_segment_sec, starts=starts)
    statuses: list[CacheStatus] = []
    for s in segments:
        p = cache_dir / f"{s.key}.mp4"
        ready = p.is_file() and p.stat().st_size > 0
        statuses.append(CacheStatus(segment=s, path=p, ready=ready))
    return statuses


def render_missing(
    timeline: EditTimeline,
    cache_dir: Path,
    *,
    renderer: Any = None,
    config: PipelineConfig | None = None,
    max_segment_sec: float = 10.0,
    starts: Sequence[int] | None = None,
    force_cpu: bool = False,
    progress: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[Path]:
    """Render missing segment MP4s as all-intra H.264 into cache_dir."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    statuses = cache_status(timeline, cache_dir, max_segment_sec=max_segment_sec, starts=starts)
    missing = [st for st in statuses if not st.ready]
    if not missing:
        return []

    # Segments are video-only (Studio plays the timeline's own audio items). The renderer
    # built here gets its audio stripped by a remux; an injected renderer owns its output.
    strip_audio = renderer is None
    if renderer is None:
        settings = DeliverSettings(
            canvas_width=timeline.canvas_width,
            canvas_height=timeline.canvas_height,
            fps=timeline.fps,
            master="h264",
        )
        renderer = NvencRenderer(config or PipelineConfig(), settings=settings, intra_only=True)

    rendered: list[Path] = []
    total = len(missing)

    for idx, item in enumerate(missing):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Render cache cancelled by user.")

        sub_clips = [timeline.clips[i] for i in item.segment.clip_indices]
        sub = EditTimeline(
            name=f"{timeline.name}_seg_{item.segment.index}",
            fps=timeline.fps,
            canvas_width=timeline.canvas_width,
            canvas_height=timeline.canvas_height,
            clips=sub_clips,
            soundtrack_path=None,
            soundtrack_in=0.0,
            sections=[],
            kind=timeline.kind,
        )

        part_file = cache_dir / f"{item.segment.key}.part.mp4"
        v_part_file = cache_dir / f"{item.segment.key}.v.part.mp4"
        final_file = cache_dir / f"{item.segment.key}.mp4"

        try:
            renderer.render_edit_timeline(
                sub,
                part_file,
                force_cpu=force_cpu,
                cancel_event=cancel_event,
            )
            if not strip_audio:
                os.replace(part_file, final_file)
            else:
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(part_file),
                    "-map",
                    "0:v:0",
                    "-c",
                    "copy",
                    "-an",
                    str(v_part_file),
                ]
                run_tool(cmd, timeout=120.0, check=True)
                os.replace(v_part_file, final_file)
            rendered.append(final_file)
            if progress is not None:
                progress(100.0 * (idx + 1) / total, f"Rendered segment {idx + 1}/{total}")
        finally:
            if part_file.exists():
                part_file.unlink(missing_ok=True)
            if v_part_file.exists():
                v_part_file.unlink(missing_ok=True)

    return rendered


def write_manifest(
    timeline: EditTimeline,
    cache_dir: Path,
    max_segment_sec: float = 10.0,
    starts: Sequence[int] | None = None,
    item_sigs: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """Write atomic JSON manifest of cache status for timeline."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    statuses = cache_status(timeline, cache_dir, max_segment_sec=max_segment_sec, starts=starts)
    segments_payload: list[dict[str, Any]] = []
    for s in statuses:
        seg_entry: dict[str, Any] = {
            "file": f"{s.segment.key}.mp4",
            "frames": s.segment.frames,
            "index": s.segment.index,
            "key": s.segment.key,
            "ready": s.ready,
            "start_frame": s.segment.start_frame,
        }
        if item_sigs is not None:
            seg_entry["items"] = [item_sigs[i] for i in s.segment.clip_indices]
        segments_payload.append(seg_entry)

    payload = {
        "canvas": [int(timeline.canvas_width), int(timeline.canvas_height)],
        "fps": float(timeline.fps),
        "segments": segments_payload,
        "version": RENDER_CACHE_VERSION,
    }
    target = cache_dir / MANIFEST_NAME
    temp_target = cache_dir / f"{MANIFEST_NAME}.tmp"
    temp_target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp_target, target)
    return target
