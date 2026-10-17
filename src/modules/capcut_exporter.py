"""CapCut Desktop native draft project exporter generating draft_content.json and draft_meta_info.json."""

import json
import logging
import time
import urllib.parse
import uuid
from pathlib import Path

import opentimelineio as otio

from src.core.config import PipelineConfig
from src.core.ffmpeg_utils import ffprobe_json

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


def _source_size(path: Path) -> tuple[int, int]:
    """Display width/height of a video file (rotation aware); (0, 0) when unknown."""
    from src.core.ffmpeg_utils import stream_rotation

    data = ffprobe_json(path)
    for st in (data or {}).get("streams", []):
        if st.get("codec_type") == "video" and (st.get("disposition", {}) or {}).get("attached_pic", 0) != 1:
            w, h = int(st.get("width") or 0), int(st.get("height") or 0)
            return (h, w) if stream_rotation(st) in (90, 270) else (w, h)
    return (0, 0)


def _fit_transform(
    crop: tuple[float, float, float, float] | None,
    src: tuple[int, int],
    canvas: tuple[int, int],
) -> dict | None:
    """CapCut clip transform for a 9:16 crop window.

    CapCut fits media inside the canvas (scale 1 = fitted). Units: ``transform`` is in half
    canvas widths/heights, +x right, +y up (pyCapCut convention). Returns None for plain fit.
    """
    sw, sh = src
    cw, ch = canvas
    if not crop or not sw or not sh:
        return None
    x, y, w, h = crop
    fit_k = min(cw / sw, ch / sh)
    k = ch / h  # source px -> canvas px so the crop window fills the canvas height
    scale = k / fit_k
    tx = ((sw / 2) - (x + w / 2)) * k / (cw / 2)
    ty = ((y + h / 2) - (sh / 2)) * k / (ch / 2)
    return {
        "alpha": 1.0,
        "flip": {"horizontal": False, "vertical": False},
        "rotation": 0.0,
        "scale": {"x": scale, "y": scale},
        "transform": {"x": tx, "y": ty},
    }


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

        speed_materials: list[dict] = []
        has_music = any(
            (c.metadata.get("AutoEdit", {}) or {}).get("role") == "music"
            for t in audio_tracks for c in t if isinstance(c, otio.schema.Clip)
        )
        cam_db = self.config.camera_audio_mix_db if self.config.keep_video_audio else -96.0
        cam_volume = 1.0 if not has_music else (0.0 if cam_db <= -90 else 10 ** (cam_db / 20.0))
        size_cache: dict[str, tuple[int, int]] = {}

        if video_tracks:
            v_track = video_tracks[0]
            for i, clip in enumerate(v_track):
                if isinstance(clip, otio.schema.Gap):
                    gap = clip.source_range.duration if clip.source_range else clip.duration()
                    current_v_target_us += _seconds_to_us(gap.to_seconds())
                    continue
                if not isinstance(clip, otio.schema.Clip):
                    continue

                media_url = getattr(clip.media_reference, "target_url", "")
                clip_file = _url_to_path(media_url)
                file_str = str(clip_file.resolve())
                if file_str not in size_cache:
                    size_cache[file_str] = _source_size(clip_file)
                src_w, src_h = size_cache[file_str]

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
                            "width": src_w or w,
                            "height": src_h or h,
                            "material_name": clip_file.stem,
                        }
                    )
                else:
                    mat_id = path_to_mat_id[file_str]

                meta = dict(clip.metadata.get("AutoEdit", {}) or {})
                speed = max(0.1, float(meta.get("speed", 1.0) or 1.0))
                src_start_sec = clip.source_range.start_time.to_seconds() if clip.source_range else 0.0
                out_dur_sec = clip.source_range.duration.to_seconds() if clip.source_range else 1.0

                dur_us = _seconds_to_us(out_dur_sec)  # timeline length
                src_dur_us = _seconds_to_us(out_dur_sec * speed)  # source consumed (retimed)
                src_start_us = _seconds_to_us(src_start_sec)

                refs: list[str] = []
                if abs(speed - 1.0) > 1e-6:
                    speed_id = str(uuid.uuid4()).upper()
                    speed_materials.append(
                        {"curve_speed": None, "id": speed_id, "mode": 0, "speed": speed, "type": "speed"}
                    )
                    refs.append(speed_id)

                crop = None
                if all(k in meta for k in ("crop_x", "crop_y", "crop_width", "crop_height")) and meta.get("tracking_subject") != "blur_backdrop":
                    crop = (meta["crop_x"], meta["crop_y"], meta["crop_width"], meta["crop_height"])

                seg_dict = {
                    "id": f"v_seg_{i:04d}_{str(uuid.uuid4())[:8]}",
                    "material_id": mat_id,
                    "source_timerange": {
                        "duration": src_dur_us,
                        "start": src_start_us,
                    },
                    "target_timerange": {
                        "duration": dur_us,
                        "start": current_v_target_us,
                    },
                    "speed": speed,
                    "volume": cam_volume,
                    "extra_material_refs": refs,
                }
                clip_tf = _fit_transform(crop, (src_w, src_h), (w, h))
                if clip_tf is not None:
                    seg_dict["clip"] = clip_tf
                video_segments.append(seg_dict)
                current_v_target_us += dur_us

        # 2. Process Audio Track Segments
        audio_segments: list[dict] = []
        current_a_target_us = 0

        if audio_tracks:
            a_track = audio_tracks[0]
            for j, a_clip in enumerate(a_track):
                if not isinstance(a_clip, otio.schema.Clip):
                    if isinstance(a_clip, otio.schema.Gap):
                        current_a_target_us += _seconds_to_us(
                            (a_clip.source_range.duration if a_clip.source_range else a_clip.duration()).to_seconds()
                        )
                    continue
                if (a_clip.metadata.get("AutoEdit", {}) or {}).get("role") == "camera_audio":
                    # the video segment already plays its own audio (see segment "volume")
                    current_a_target_us += _seconds_to_us(
                        a_clip.source_range.duration.to_seconds() if a_clip.source_range else 0.0
                    )
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
                    },
                    *speed_materials,
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
