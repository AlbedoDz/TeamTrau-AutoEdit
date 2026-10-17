"""CapCut Desktop native draft project exporter generating draft_content.json and draft_meta_info.json."""

import json
import logging
import time
import urllib.parse
import uuid
from pathlib import Path

import opentimelineio as otio

from src.core.config import PipelineConfig

logger = logging.getLogger(__name__)


def _seconds_to_us(seconds: float) -> int:
    """Convert float seconds to CapCut integer microseconds."""
    return int(round(seconds * 1_000_000))


def _url_to_path(target_url: str) -> Path:
    """Convert file:// URI or raw path to Path object."""
    if target_url.startswith("file://"):
        parsed = urllib.parse.urlparse(target_url)
        raw_path = urllib.parse.unquote(parsed.path)
        # Handle Windows drive letters like /E:/path -> E:/path
        if raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        return Path(raw_path)
    return Path(target_url)


class CapCutExporter:
    """Exports OpenTimelineIO timelines to native CapCut Desktop draft project folders."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def export_timeline(
        self,
        timeline: otio.schema.Timeline,
        output_draft_dir: Path,
        draft_name: str | None = None,
    ) -> Path:
        """Serialize an OTIO timeline into a complete CapCut Desktop draft folder."""
        output_draft_dir.mkdir(parents=True, exist_ok=True)
        proj_name = draft_name or timeline.name or "AutoEdit_CapCut_Draft"
        draft_id = str(uuid.uuid4()).upper()
        now_ts = int(time.time())

        # Determine canvas aspect ratio tag
        w = self.config.canvas_width
        h = self.config.canvas_height
        if w == 1080 and h == 1920:
            ratio_str = "9:16"
        elif w == 1920 and h == 1080:
            ratio_str = "16:9"
        elif w == 1080 and h == 1080:
            ratio_str = "1:1"
        else:
            ratio_str = "original"

        # Separate video and audio tracks
        video_tracks = [t for t in timeline.tracks if t.kind == otio.schema.TrackKind.Video]
        audio_tracks = [t for t in timeline.tracks if t.kind == otio.schema.TrackKind.Audio]

        video_materials: list[dict] = []
        audio_materials: list[dict] = []
        path_to_mat_id: dict[str, str] = {}

        # 1. Process Video Track Segments
        video_segments: list[dict] = []
        current_v_target_us = 0

        if video_tracks:
            v_track = video_tracks[0]
            for i, clip in enumerate(v_track):
                if not isinstance(clip, otio.schema.Clip):
                    continue

                media_url = getattr(clip.media_reference, "target_url", "")
                clip_file = _url_to_path(media_url)
                file_str = str(clip_file.resolve())

                # Register material if unique
                if file_str not in path_to_mat_id:
                    mat_id = str(uuid.uuid4()).upper()
                    path_to_mat_id[file_str] = mat_id
                    avail_dur_sec = (
                        clip.media_reference.available_range.duration.to_seconds()
                        if clip.media_reference and clip.media_reference.available_range
                        else 10.0
                    )
                    video_materials.append(
                        {
                            "id": mat_id,
                            "path": file_str,
                            "type": "video",
                            "duration": _seconds_to_us(avail_dur_sec),
                            "width": w,
                            "height": h,
                            "material_name": clip_file.stem,
                        }
                    )
                else:
                    mat_id = path_to_mat_id[file_str]

                src_start_sec = clip.source_range.start_time.to_seconds() if clip.source_range else 0.0
                src_dur_sec = clip.source_range.duration.to_seconds() if clip.source_range else 1.0

                src_start_us = _seconds_to_us(src_start_sec)
                dur_us = _seconds_to_us(src_dur_sec)

                seg_dict = {
                    "id": f"v_seg_{i:04d}_{str(uuid.uuid4())[:8]}",
                    "material_id": mat_id,
                    "source_timerange": {
                        "duration": dur_us,
                        "start": src_start_us,
                    },
                    "target_timerange": {
                        "duration": dur_us,
                        "start": current_v_target_us,
                    },
                    "speed": 1.0,
                    "volume": 1.0,
                    "extra_material_refs": [],
                }
                video_segments.append(seg_dict)
                current_v_target_us += dur_us

        # 2. Process Audio Track Segments
        audio_segments: list[dict] = []
        current_a_target_us = 0

        if audio_tracks:
            a_track = audio_tracks[0]
            for j, a_clip in enumerate(a_track):
                if not isinstance(a_clip, otio.schema.Clip):
                    continue

                a_url = getattr(a_clip.media_reference, "target_url", "")
                a_file = _url_to_path(a_url)
                a_file_str = str(a_file.resolve())

                if a_file_str not in path_to_mat_id:
                    a_mat_id = str(uuid.uuid4()).upper()
                    path_to_mat_id[a_file_str] = a_mat_id
                    a_dur_sec = (
                        a_clip.source_range.duration.to_seconds()
                        if a_clip.source_range
                        else (current_v_target_us / 1_000_000.0)
                    )
                    audio_materials.append(
                        {
                            "id": a_mat_id,
                            "path": a_file_str,
                            "type": "extract_music",
                            "duration": _seconds_to_us(a_dur_sec),
                            "name": a_file.stem,
                        }
                    )
                else:
                    a_mat_id = path_to_mat_id[a_file_str]

                a_src_start_sec = a_clip.source_range.start_time.to_seconds() if a_clip.source_range else 0.0
                a_src_dur_sec = a_clip.source_range.duration.to_seconds() if a_clip.source_range else (current_v_target_us / 1_000_000.0)

                a_dur_us = _seconds_to_us(a_src_dur_sec)
                a_seg_dict = {
                    "id": f"a_seg_{j:04d}_{str(uuid.uuid4())[:8]}",
                    "material_id": a_mat_id,
                    "source_timerange": {
                        "duration": a_dur_us,
                        "start": _seconds_to_us(a_src_start_sec),
                    },
                    "target_timerange": {
                        "duration": a_dur_us,
                        "start": current_a_target_us,
                    },
                    "speed": 1.0,
                    "volume": 1.0,
                }
                audio_segments.append(a_seg_dict)
                current_a_target_us += a_dur_us

        total_draft_dur_us = max(current_v_target_us, current_a_target_us)

        # Assemble draft_content.json
        draft_content = {
            "canvas_config": {
                "height": h,
                "ratio": ratio_str,
                "width": w,
            },
            "duration": total_draft_dur_us,
            "fps": float(self.config.fps),
            "materials": {
                "audios": audio_materials,
                "canvases": [],
                "speeds": [
                    {
                        "curve_speed": None,
                        "id": "speed_default_norm",
                        "mode": 0,
                        "speed": 1.0,
                        "type": "speed",
                    }
                ],
                "transitions": [],
                "videos": video_materials,
            },
            "tracks": [
                {
                    "attribute": 0,
                    "id": "track_video_main",
                    "is_default_name": True,
                    "name": "Video Track 1",
                    "segments": video_segments,
                    "type": "video",
                },
                {
                    "attribute": 0,
                    "id": "track_audio_main",
                    "is_default_name": True,
                    "name": "Audio Track 1",
                    "segments": audio_segments,
                    "type": "audio",
                },
            ],
            "version": 360000,
        }

        # Assemble draft_meta_info.json
        draft_meta_info = {
            "draft_id": draft_id,
            "draft_name": proj_name,
            "draft_root_path": str(output_draft_dir.resolve()),
            "draft_timeline_duration": total_draft_dur_us,
            "tm_draft_create": now_ts,
            "tm_draft_modified": now_ts,
            "draft_materials": [
                {"type": 0, "value": [m["path"] for m in video_materials]},
                {"type": 1, "value": [m["path"] for m in audio_materials]},
            ],
        }

        content_file = output_draft_dir / "draft_content.json"
        meta_file = output_draft_dir / "draft_meta_info.json"

        with content_file.open("w", encoding="utf-8") as f:
            json.dump(draft_content, f, indent=2, ensure_ascii=False)

        with meta_file.open("w", encoding="utf-8") as f:
            json.dump(draft_meta_info, f, indent=2, ensure_ascii=False)

        logger.info(
            "Exported CapCut Desktop draft to %s (%d video segments, %.2fs)",
            output_draft_dir,
            len(video_segments),
            total_draft_dur_us / 1_000_000.0,
        )
        return output_draft_dir
