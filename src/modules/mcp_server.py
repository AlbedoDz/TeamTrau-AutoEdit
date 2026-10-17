"""Model Context Protocol (MCP) server exposing AutoEdit core capabilities to AI agents.

Complies with the standard Model Context Protocol (MCP 2.x) using MCPServer.
Exposes complete editing ecosystem tools, resources, and prompt templates to
external agents (Antigravity, Claude Desktop, Cursor, ChatGPT, DeepSeek v4, etc.).
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from src.core.config import (
    DEFAULT_PRESET_NAME,
    PRESETS,
    PipelineConfig,
    check_cuda_available,
    check_nvenc_available,
)
from src.core.service import StudioBackendAdapter

logger = logging.getLogger(__name__)

# Initialize central MCP server and backend adapter
mcp_server = MCPServer("teamtrau-autoedit")
adapter = StudioBackendAdapter()


# =============================================================================
# 1. System, Environment & Preset Tools
# =============================================================================


@mcp_server.tool()
def check_system_environment() -> dict[str, Any]:
    """Inspect system hardware acceleration, GPU, CUDA, NVENC, and NLE bindings.

    Returns:
        JSON structure with Python runtime, CUDA device, NVENC encoder availability,
        FFmpeg status, and DaVinci Resolve Studio API installation status.
    """
    cuda_ok = check_cuda_available()
    nvenc_ok = check_nvenc_available()
    cuda_device = "N/A"
    if cuda_ok:
        try:
            import torch

            if torch.cuda.is_available():
                cuda_device = torch.cuda.get_device_name(0)
        except Exception:
            pass

    cfg = PipelineConfig()
    resolve_installed = cfg.resolve_exe_path.is_file()
    resolve_api_ready = (cfg.resolve_script_path / "DaVinciResolveScript.py").is_file()

    return {
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "cuda_available": cuda_ok,
        "cuda_device_name": cuda_device,
        "nvenc_available": nvenc_ok,
        "resolve_installed": resolve_installed,
        "resolve_executable": str(cfg.resolve_exe_path) if resolve_installed else None,
        "resolve_api_ready": resolve_api_ready,
        "status": "ready" if (cuda_ok or nvenc_ok) else "cpu_fallback",
    }


@mcp_server.tool()
def list_editing_presets() -> dict[str, Any]:
    """List all supported video resolution, framerate, and aspect ratio presets.

    Returns:
        Dictionary of available presets (e.g. '1080p30', '4k60', 'social_vertical')
        with canvas dimensions, framerate, and description.
    """
    return {
        "default_preset": DEFAULT_PRESET_NAME,
        "presets": {
            name: {
                "name": p.name,
                "width": p.width,
                "height": p.height,
                "fps": p.fps,
                "aspect_ratio": f"{p.width}:{p.height}",
                "description": p.description,
            }
            for name, p in PRESETS.items()
        },
    }


@mcp_server.tool()
def list_workflow_presets() -> dict[str, Any]:
    """List specialized workflow presets for different video editing genres.

    Returns:
        Available workflow modes ('standard', 'build_timelapse', 'unboxing_narration')
        and their characteristic configurations.
    """
    return {
        "workflow_modes": {
            "standard": "General rhythm-synced edit with natural pacing and dialogue.",
            "build_timelapse": "Fast-paced assembly with 1x-8x speed ramping and stripped camera audio.",
            "unboxing_narration": "Voiceover-driven pacing with dead-air stripping and speech ducking.",
        }
    }


# =============================================================================
# 2. Ingestion & Media Analysis Tools
# =============================================================================


@mcp_server.tool()
def probe_media_file(file_path: str) -> dict[str, Any]:
    """Extract technical metadata from a video or audio file.

    Args:
        file_path: Absolute or relative path to media file.

    Returns:
        Resolution, framerate, duration, total frames, and file size.
    """
    p = Path(file_path)
    if not p.is_file():
        raise FileNotFoundError(f"Media file does not exist: {file_path}")

    meta = adapter.probe_media(p)
    return {
        "file_path": str(p.resolve()),
        "width": meta.width,
        "height": meta.height,
        "fps": round(meta.fps, 2),
        "total_frames": meta.total_frames,
        "duration": round(meta.duration, 3),
        "aspect_ratio": f"{meta.width}:{meta.height}",
        "file_size_bytes": p.stat().st_size,
    }


@mcp_server.tool()
def detect_scenes(
    video_path: str,
    threshold: float = 3.0,
    min_scene_len: float = 0.8,
) -> dict[str, Any]:
    """Detect non-destructive scene boundaries and shot intervals using PySceneDetect.

    Args:
        video_path: Path to the raw video footage file (MP4, MOV, MKV).
        threshold: Adaptive detector threshold (default: 3.0).
        min_scene_len: Minimum scene duration in seconds to avoid micro-cuts.

    Returns:
        JSON structure containing discovered scene intervals and frame boundaries.
    """
    p = Path(video_path)
    if not p.is_file():
        raise FileNotFoundError(f"Video file does not exist: {video_path}")

    cfg = PipelineConfig(
        adaptive_threshold=threshold,
        min_scene_len_sec=min_scene_len,
    )
    scenes = adapter.detect_scenes(p, config=cfg)

    return {
        "video_path": str(p.resolve()),
        "scene_count": len(scenes),
        "scenes": [
            {
                "scene_index": idx,
                "start_time": round(s.start_time, 3),
                "end_time": round(s.end_time, 3),
                "duration": round(s.duration, 3),
                "start_frame": s.start_frame,
                "end_frame": s.end_frame,
            }
            for idx, s in enumerate(scenes)
        ],
    }


@mcp_server.tool()
def batch_detect_scenes(
    input_dir: str,
    threshold: float = 3.0,
    min_scene_len: float = 0.8,
) -> dict[str, Any]:
    """Batch scan a folder of raw video footage to detect shot intervals across all files.

    Args:
        input_dir: Path to directory containing video footage files.
        threshold: Adaptive detector sensitivity.
        min_scene_len: Minimum shot duration in seconds.

    Returns:
        Summary of total scenes discovered across all processed video files.
    """
    p = Path(input_dir)
    if not p.is_dir():
        raise NotADirectoryError(f"Directory not found: {input_dir}")

    from src.modules.ingest_detector import IngestDetector

    cfg = PipelineConfig(
        adaptive_threshold=threshold,
        min_scene_len_sec=min_scene_len,
    )
    detector = IngestDetector(cfg)
    cuts = detector.batch_detect(p)

    return {
        "input_dir": str(p.resolve()),
        "total_scenes": len(cuts),
        "files_processed": len(set(c.clip_path for c in cuts)),
        "scenes": [
            {
                "clip_path": str(c.clip_path),
                "start_time": round(c.start_time, 2),
                "end_time": round(c.end_time, 2),
                "duration": round(c.duration, 2),
                "is_freeze": c.is_freeze,
                "is_silence": c.is_silence,
            }
            for c in cuts[:100]  # Cap to first 100 scenes for concise response
        ],
    }


# =============================================================================
# 3. Audio, Rhythm & Speech Tools
# =============================================================================


@mcp_server.tool()
def analyze_soundtrack(
    audio_path: str,
    target_duration: float | None = None,
    duration_mode: str = "fixed",
) -> dict[str, Any]:
    """Analyze soundtrack rhythm, BPM, downbeats, and structural sections (INTRO, DROP, OUTRO).

    Args:
        audio_path: Path to the audio file (MP3, WAV, AAC, FLAC).
        target_duration: Optional target duration in seconds for timeline trimming.
        duration_mode: Duration decoupling mode ('fixed', 'footage_driven', 'full_track').

    Returns:
        JSON structure with BPM, downbeats, energy curves, and detected musical sections.
    """
    p = Path(audio_path)
    if not p.is_file():
        raise FileNotFoundError(f"Audio file does not exist: {audio_path}")

    cfg = PipelineConfig(
        duration_mode=duration_mode,
        target_duration=target_duration,
    )
    res = adapter.quick_analyze_audio(p, config=cfg)

    return {
        "audio_path": str(p.resolve()),
        "bpm": round(float(res.tempo_bpm), 2),
        "duration": round(float(res.duration), 2),
        "beat_count": len(res.beats),
        "downbeat_count": len([b for b in res.beats if b.is_downbeat]),
        "cut_timestamps_count": len(res.cut_timestamps),
        "sections": [
            {
                "type": s.section_type.value,
                "start_time": round(s.start_time, 2),
                "end_time": round(s.end_time, 2),
                "duration": round(s.duration, 2),
                "avg_energy": round(s.avg_energy, 3),
                "is_climax": s.is_climax,
            }
            for s in res.sections
        ],
    }


@mcp_server.tool()
def normalize_audio_file(
    audio_path: str,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Strip Broadcast Wave Format (BWF) timecode chunks and normalize to clean 24-bit 48kHz WAV.

    Args:
        audio_path: Path to audio file.
        output_dir: Optional target cache directory for cleaned WAV.

    Returns:
        Cleaned audio path and normalization status.
    """
    from src.modules.audio_normalizer import AudioNormalizer

    p = Path(audio_path)
    if not p.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    out_p = Path(output_dir) if output_dir else None
    normalizer = AudioNormalizer(cache_dir=out_p)
    inspection = normalizer.inspect_audio(p)
    clean_path = normalizer.clean_audio(p)

    return {
        "original_path": str(p.resolve()),
        "clean_path": str(clean_path.resolve()),
        "is_dirty_bwf": inspection.is_dirty_bwf,
        "original_sample_rate": inspection.sample_rate,
        "original_channels": inspection.channels,
        "status": "normalized",
    }


@mcp_server.tool()
def transcribe_speech(
    media_path: str,
    model_size: str = "base",
    language: str | None = None,
) -> dict[str, Any]:
    """Transcribe spoken dialogue with word-level timestamps using Faster-Whisper.

    Args:
        media_path: Path to video or audio media file.
        model_size: Whisper model size ('tiny', 'base', 'small', 'medium', 'large-v3').
        language: Optional language code (e.g. 'en', 'vi'). Defaults to auto-detect.

    Returns:
        Transcription sentences, word segments, and ducking intervals for BGM mixing.
    """
    from src.modules.transcription_engine import TranscriptionEngine

    p = Path(media_path)
    if not p.is_file():
        raise FileNotFoundError(f"Media file not found: {media_path}")

    cfg = PipelineConfig(whisper_model_size=model_size)
    engine = TranscriptionEngine(cfg)
    sentences, ducking_ranges = engine.transcribe(p, language=language)

    return {
        "media_path": str(p.resolve()),
        "sentence_count": len(sentences),
        "sentences": [
            {
                "start": round(s.start_time, 2),
                "end": round(s.end_time, 2),
                "text": s.text,
                "importance": round(s.importance_score, 2),
                "word_count": len(s.words),
            }
            for s in sentences
        ],
        "ducking_ranges_count": len(ducking_ranges),
        "ducking_ranges": [
            [round(r[0], 2), round(r[1], 2)] for r in ducking_ranges
        ],
    }


# =============================================================================
# 4. Visual Quality, Continuity & Reframing Tools
# =============================================================================


@mcp_server.tool()
def score_visual_scenes(
    video_path_or_dir: str,
    max_scenes: int = 30,
) -> dict[str, Any]:
    """Score visual quality (sharpness, aesthetics) and perform 64-bit dHash deduplication.

    Args:
        video_path_or_dir: Video file or directory of raw footage.
        max_scenes: Maximum number of scenes to evaluate and return.

    Returns:
        Ranked scene scores, duplicate clusters dropped, and motion energy.
    """
    from src.modules.ingest_detector import IngestDetector
    from src.modules.visual_scorer import VisualScorer

    target = Path(video_path_or_dir)
    cfg = PipelineConfig()
    detector = IngestDetector(cfg)

    if target.is_dir():
        cuts = detector.batch_detect(target)
    elif target.is_file():
        cuts = detector.detect_scenes(target)
    else:
        raise FileNotFoundError(f"Path not found: {video_path_or_dir}")

    scorer = VisualScorer(cfg)
    scored = scorer.score_scenes(cuts[:max_scenes])

    return {
        "evaluated_scenes": len(scored),
        "top_scenes": [
            {
                "clip_path": str(s.clip_path.name),
                "start": round(s.start_time, 2),
                "end": round(s.end_time, 2),
                "duration": round(s.duration, 2),
                "composite_score": round(s.composite_score, 3),
                "sharpness": round(s.sharpness_score, 3),
                "aesthetic": round(s.aesthetic_score, 3),
                "motion": round(s.motion_score, 3),
                "is_duplicate": s.is_duplicate,
            }
            for s in scored[:15]
        ],
    }


@mcp_server.tool()
def reframe_video(
    video_path: str,
    target_preset: str = "social_vertical",
    layout_mode: str = "auto_hybrid",
    confidence_threshold: float = 0.35,
) -> dict[str, Any]:
    """Calculate AI smart 9:16 reframing with YOLO subject tracking or blurred backdrop.

    Args:
        video_path: Path to the 16:9 source video file.
        target_preset: Named target canvas preset (default: 'social_vertical').
        layout_mode: Reframing mode ('smart_crop', 'blur_backdrop', 'auto_hybrid').
        confidence_threshold: Minimum YOLO subject detection confidence.

    Returns:
        Reframing coordinates, crop box, and layout strategy.
    """
    p = Path(video_path)
    if not p.is_file():
        raise FileNotFoundError(f"Video file does not exist: {video_path}")

    if target_preset not in PRESETS:
        raise ValueError(
            f"Unknown preset '{target_preset}'. Available: {list(PRESETS.keys())}"
        )

    cfg = PipelineConfig.from_preset(
        target_preset,
        reframing_mode=layout_mode,
        reframer_confidence_threshold=confidence_threshold,
    )
    crop = adapter.reframe_video(p, target_preset=target_preset, config=cfg)

    return {
        "video_path": str(p.resolve()),
        "target_preset": target_preset,
        "layout_mode": layout_mode,
        "canvas_width": cfg.canvas_width,
        "canvas_height": cfg.canvas_height,
        "crop_window": {
            "x": crop.x,
            "y": crop.y,
            "width": crop.width,
            "height": crop.height,
            "confidence": round(crop.confidence, 3),
            "tracking_subject": crop.tracking_subject,
        },
    }


@mcp_server.tool()
def evaluate_cut_continuity(
    scene_a_path: str,
    scene_b_path: str,
) -> dict[str, Any]:
    """Evaluate visual continuity & exposure jump between two adjacent video clips.

    Computes Bhattacharyya distance on normalized 64-bin Y-channel histograms.

    Args:
        scene_a_path: First video clip.
        scene_b_path: Second video clip.

    Returns:
        Bhattacharyya distance and cross-dissolve recommendation.
    """
    from src.modules.visual_flow import VisualFlowAnalyzer

    p_a = Path(scene_a_path)
    p_b = Path(scene_b_path)
    if not p_a.is_file() or not p_b.is_file():
        raise FileNotFoundError("One or both scene video files not found.")

    analyzer = VisualFlowAnalyzer()
    dist, needs_transition = analyzer.evaluate_cut_continuity_from_files(p_a, p_b)

    return {
        "scene_a": str(p_a.resolve()),
        "scene_b": str(p_b.resolve()),
        "bhattacharyya_distance": round(dist, 4),
        "exposure_jump_detected": needs_transition,
        "recommended_transition": "10-frame cross-dissolve" if needs_transition else "hard_cut",
    }


@mcp_server.tool()
def generate_kinetic_subtitles(
    transcription_json: str,
    output_ass_path: str,
    style: str = "kinetic_ass",
    target_preset: str = "social_vertical",
) -> dict[str, Any]:
    """Generate kinetic karaoke ASS or clean SRT subtitles with safe-zone margin.

    Args:
        transcription_json: JSON string of transcribed sentence segments with word timestamps.
        output_ass_path: Output subtitle file path (.ass or .srt).
        style: Subtitle style ('kinetic_ass' or 'clean_srt').
        target_preset: Target preset to determine vertical safe zone margin.

    Returns:
        Exported subtitle path and entry count.
    """
    from src.core.types import SentenceSegment, WordSegment
    from src.modules.kinetic_subtitle import export_kinetic_ass

    raw = json.loads(transcription_json)
    sentences: list[SentenceSegment] = []
    for item in raw:
        words = [
            WordSegment(
                word=w.get("word", ""),
                start_time=float(w.get("start", 0.0)),
                end_time=float(w.get("end", 0.0)),
                confidence=float(w.get("confidence", 1.0)),
            )
            for w in item.get("words", [])
        ]
        sentences.append(
            SentenceSegment(
                text=item.get("text", ""),
                start_time=float(item.get("start", 0.0)),
                end_time=float(item.get("end", 0.0)),
                words=words,
                importance_score=float(item.get("importance", 1.0)),
            )
        )

    cfg = PipelineConfig.from_preset(target_preset, subtitle_style=style)
    out_p = Path(output_ass_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    res_path = export_kinetic_ass(sentences, out_p, config=cfg)

    return {
        "subtitle_path": str(res_path.resolve()),
        "style": style,
        "sentence_count": len(sentences),
        "status": "exported",
    }


# =============================================================================
# 5. Timeline Synthesis, QA & NLE Handoff Tools
# =============================================================================


_DELIVER_SETTING_KEYS = (
    "deliver_preset", "canvas", "fps", "fit", "color", "master", "subtitles",
    "audio_rate", "audio_channels", "variants", "relink_original", "watermark_in_nle",
)


def _deliver_from_project(
    project_path: str,
    formats: set[str],
    output_dir: Path | None = None,
    variant: str | None = None,
    force_cpu: bool = False,
    base_name: str | None = None,
    settings_args: dict[str, Any] | None = None,
) -> Any:
    """Deliver an already-edited timeline from a saved .tteproj (no re-analysis).

    ``settings_args`` are the optional Deliver settings given to the tool (see
    ``_DELIVER_SETTING_KEYS``); they override the project's saved Deliver settings (or the
    timeline's own canvas / fps). Variants default to the active timeline only.
    """
    from src.core.deliver_options import build_deliver_settings
    from src.core.types import DeliverSettings
    from src.ui.controllers.project_manager import ProjectManager, load_project_file

    proj = Path(project_path)
    data = load_project_file(proj)
    timelines = data.get_edit_timelines()
    key = variant or data.active_timeline_key
    if key not in timelines:
        raise ValueError(f"Timeline '{key}' not found. Available: {sorted(timelines)}")
    cfg = ProjectManager().deserialize_pipeline_config(data.config) if data.config else PipelineConfig()
    out_dir = output_dir or Path(data.output_dir or proj.parent)

    args = {k: v for k, v in (settings_args or {}).items() if v is not None and v != ""}
    base = data.get_deliver_settings() or DeliverSettings.from_timeline(timelines[key], cfg)
    base.variants = ["active"]  # tools export the active timeline unless variants is given
    try:
        settings = build_deliver_settings(
            base,
            preset=args.get("deliver_preset"),
            canvas=args.get("canvas"),
            fps=args.get("fps"),
            fit=args.get("fit"),
            color=args.get("color"),
            master=args.get("master"),
            subtitles=args.get("subtitles"),
            audio_rate=args.get("audio_rate"),
            audio_channels=args.get("audio_channels"),
            variants=args.get("variants"),
            relink=args.get("relink_original"),
            watermark_in_nle=args.get("watermark_in_nle"),
        )
        keys = adapter.resolve_variant_keys(key, settings, timelines)
    except ValueError as exc:
        raise ValueError(f"Invalid deliver settings: {exc}") from exc

    handle = adapter.submit_deliver_job(
        timelines[key],
        out_dir,
        cfg,
        formats=formats,  # type: ignore[arg-type]
        force_cpu=force_cpu,
        base_name=(base_name or (f"{proj.stem}_{key}" if len(keys) == 1 else proj.stem)),
        settings=settings,
        variant_timelines=timelines,
    )
    res = handle.result
    if not res or not res.success:
        raise ValueError(res.error_message if res else "Deliver failed")
    res.clip_count = len(timelines[key].clips)
    return res


def _reject_settings_without_project(settings_args: dict[str, Any]) -> None:
    given = sorted(k for k, v in settings_args.items() if v is not None and v != "")
    if given:
        raise ValueError(f"Deliver settings ({', '.join(given)}) need project_path (a saved .tteproj).")


def _result_paths(res: Any) -> dict[str, Any]:
    """Paths produced by a DeliverResult, per variant when several were delivered."""

    def one(r: Any) -> dict[str, Any]:
        return {
            "master_path": str(r.render_path) if r.render_path else None,
            "master_codec": r.master_codec,
            "fcpxml_path": str(r.fcpxml_path) if r.fcpxml_path else None,
            "premiere_xml_path": str(r.premiere_xml_path) if r.premiere_xml_path else None,
            "otio_path": str(r.otio_path) if r.otio_path else None,
            "capcut_draft_path": str(r.capcut_draft_path) if r.capcut_draft_path else None,
            "subtitle_paths": [str(x) for x in r.subtitle_paths],
            "manifest_path": str(r.manifest_path) if r.manifest_path else None,
        }

    out = one(res)
    if res.variant_results:
        out["variants"] = {k: one(v) for k, v in res.variant_results.items()}
    return out


def _analysis_config(
    preset: str,
    duration_mode: str = "fixed",
    target_duration: float | None = None,
    enable_teaser: bool = True,
    speed_ramping: bool = False,
    watermark_path: str | None = None,
    layout_mode: str = "auto_hybrid",
    subtitle_style: str = "kinetic_ass",
    enable_transcription: bool = False,
) -> PipelineConfig:
    return PipelineConfig.from_preset(
        preset,
        duration_mode=duration_mode,
        target_duration=target_duration,
        enable_teaser=enable_teaser,
        enable_speed_ramping=speed_ramping,
        watermark_path=Path(watermark_path) if watermark_path else None,
        watermark_enabled=bool(watermark_path),
        reframing_mode=layout_mode,
        subtitle_style=subtitle_style,
        enable_transcription=enable_transcription,
    )


@mcp_server.tool()
def export_timeline(
    input_dir: str = "",
    soundtrack: str = "",
    output_dir: str = "./output",
    preset: str = "1080p30",
    duration_mode: str = "fixed",
    target_duration: float | None = None,
    enable_teaser: bool = True,
    speed_ramping: bool = False,
    watermark_path: str | None = None,
    layout_mode: str = "auto_hybrid",
    subtitle_style: str = "kinetic_ass",
    project_path: str | None = None,
    variant: str | None = None,
    deliver_preset: str | None = None,
    canvas: str | None = None,
    fps: str | None = None,
    fit: str | None = None,
    color: str | None = None,
    master: str | None = None,
    subtitles: str | None = None,
    audio_rate: int | None = None,
    audio_channels: int | None = None,
    variants: str | None = None,
    relink_original: bool | None = None,
    watermark_in_nle: bool | None = None,
) -> dict[str, Any]:
    """Export NLE project files (DaVinci FCPXML, Premiere XML, OTIO, CapCut draft).

    ONLY call this when the user explicitly asks to export. Prefer ``project_path``
    (a .tteproj from execute_pipeline_job) so the exported timeline is exactly the
    one already edited; passing input_dir + soundtrack re-analyzes the footage.

    Args:
        input_dir: Directory containing raw video footage files (when no project_path).
        soundtrack: Path to the background audio soundtrack file (when no project_path).
        output_dir: Target directory for exported project files.
        preset: Target canvas resolution and framerate preset.
        duration_mode: Duration decoupling mode ('fixed', 'footage_driven', 'full_track').
        target_duration: Target duration in seconds (optional).
        enable_teaser: Include rapid 3-5 cut retention teaser hook.
        speed_ramping: Enable dynamic 1x-8x speed ramping mapped to visual score.
        watermark_path: Optional path to logo watermark image.
        layout_mode: Reframing strategy ('smart_crop', 'blur_backdrop', 'auto_hybrid').
        subtitle_style: Subtitle format ('none', 'clean_srt', 'kinetic_ass').
        project_path: Saved .tteproj to export without re-analysis.
        variant: Timeline key inside the project (master, beat_driven, cinematic, social_teaser).
        deliver_preset: Deliver canvas + frame rate from a preset name (needs project_path).
        canvas: Deliver canvas as "WxH", e.g. "1080x1920" (needs project_path).
        fps: Deliver frame rate, e.g. "29.97", "25" or "30000/1001" (needs project_path).
        fit: Fit mode 'auto', 'fit', 'fill' or 'stretch' (needs project_path).
        color: 'rec709', 'rec2020_hlg', 'rec2020_pq' or 'passthrough' (needs project_path).
        master: Master codec 'hevc', 'h264', 'prores_422', 'prores_hq' or 'dnxhr_hqx' (recorded in the manifest).
        subtitles: 'none', 'srt', 'ass' or 'burn'.
        audio_rate: Audio sample rate 44100, 48000 or 96000.
        audio_channels: 1 or 2.
        variants: 'all' or comma separated timeline keys to export into one subfolder each (default: active only).
        relink_original: Link NLE files to the original media instead of render proxies (default true).
        watermark_in_nle: Request the watermark in NLE files (not implemented yet; only recorded).

    Returns:
        JSON structure with exported project file paths (also manifest_path) and timeline metrics.
    """
    out_path = Path(output_dir)
    nle = {"fcpxml", "premiere", "otio", "capcut"}
    settings_args = {
        "deliver_preset": deliver_preset, "canvas": canvas, "fps": fps, "fit": fit, "color": color,
        "master": master, "subtitles": subtitles, "audio_rate": audio_rate,
        "audio_channels": audio_channels, "variants": variants,
        "relink_original": relink_original, "watermark_in_nle": watermark_in_nle,
    }
    paths: dict[str, Any] = {}
    if project_path:
        res = _deliver_from_project(project_path, nle, out_path, variant, settings_args=settings_args)
        clip_count, total = res.clip_count, res.duration
        paths = _result_paths(res)
    else:
        _reject_settings_without_project(settings_args)
        in_path, audio_path = Path(input_dir), Path(soundtrack)
        if not in_path.is_dir():
            raise NotADirectoryError(f"Input directory does not exist: {input_dir}")
        if not audio_path.is_file():
            raise FileNotFoundError(f"Soundtrack audio not found: {soundtrack}")
        cfg = _analysis_config(
            preset, duration_mode, target_duration, enable_teaser, speed_ramping,
            watermark_path, layout_mode, subtitle_style,
        )
        handle = adapter.submit_job(
            input_dir=in_path, soundtrack=audio_path, output_dir=out_path, config=cfg,
            run_async=False, no_render=True, export_nle=True,
        )
        res = handle.result
        if not res or not res.success:
            raise ValueError(res.error_message if res else "Timeline export failed")
        clip_count, total = res.clip_count, res.total_duration

    return {
        **paths,
        "clip_count": clip_count,
        "total_duration": round(total, 2),
        "fcpxml_path": str(res.fcpxml_path) if res.fcpxml_path else None,
        "premiere_xml_path": str(res.premiere_xml_path) if res.premiere_xml_path else None,
        "otio_path": str(res.otio_path) if res.otio_path else None,
        "capcut_draft_path": str(res.capcut_draft_path) if res.capcut_draft_path else None,
    }


@mcp_server.tool()
def render_master(
    input_dir: str = "",
    soundtrack: str = "",
    output_file: str = "./output/master.mp4",
    preset: str = "1080p30",
    no_nvenc: bool = False,
    speed_ramping: bool = False,
    watermark_path: str | None = None,
    layout_mode: str = "auto_hybrid",
    subtitle_style: str = "kinetic_ass",
    project_path: str | None = None,
    variant: str | None = None,
    deliver_preset: str | None = None,
    canvas: str | None = None,
    fps: str | None = None,
    fit: str | None = None,
    color: str | None = None,
    master: str | None = None,
    subtitles: str | None = None,
    audio_rate: int | None = None,
    audio_channels: int | None = None,
    variants: str | None = None,
    relink_original: bool | None = None,
    watermark_in_nle: bool | None = None,
) -> dict[str, Any]:
    """Render the master video with FFmpeg NVENC (CPU fallback) or ProRes / DNxHR / H.264.

    ONLY call this when the user explicitly asks to render/export the video. Prefer
    ``project_path`` so the render matches the edited timeline exactly; passing
    input_dir + soundtrack re-analyzes the footage first.

    Args:
        input_dir: Directory containing raw video footage files (when no project_path).
        soundtrack: Path to the background soundtrack audio (when no project_path).
        output_file: Master deliverable output video file path.
        preset: Target canvas resolution and framerate preset.
        no_nvenc: Force CPU libx265 rendering instead of NVIDIA NVENC hardware acceleration.
        speed_ramping: Enable aesthetic speed ramping.
        watermark_path: Optional path to logo watermark image.
        layout_mode: Reframing mode ('smart_crop', 'blur_backdrop', 'auto_hybrid').
        subtitle_style: Subtitle format ('none', 'clean_srt', 'kinetic_ass').
        project_path: Saved .tteproj to render without re-analysis.
        variant: Timeline key inside the project.
        deliver_preset: Deliver canvas + frame rate from a preset name (needs project_path).
        canvas: Deliver canvas as "WxH", e.g. "1080x1920" (needs project_path).
        fps: Deliver frame rate, e.g. "29.97", "25" or "30000/1001" (needs project_path).
        fit: Fit mode 'auto', 'fit', 'fill' or 'stretch'.
        color: 'rec709', 'rec2020_hlg', 'rec2020_pq' or 'passthrough'.
        master: Master codec 'hevc', 'h264', 'prores_422', 'prores_hq' or 'dnxhr_hqx'; ProRes/DNxHR
            write a .mov, so the output file suffix follows the codec.
        subtitles: 'none', 'srt', 'ass' or 'burn' (sidecars need a transcript).
        audio_rate: Audio sample rate 44100, 48000 or 96000.
        audio_channels: 1 or 2.
        variants: 'all' or comma separated timeline keys; each goes to its own subfolder of the output folder.
        relink_original: Link NLE files to the original media instead of render proxies.
        watermark_in_nle: Request the watermark in NLE files (not implemented yet).

    Returns:
        JSON structure with output file path, encoder used, master codec, duration, file size and manifest_path.
    """
    out_video_path = Path(output_file)
    out_dir = out_video_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    settings_args = {
        "deliver_preset": deliver_preset, "canvas": canvas, "fps": fps, "fit": fit, "color": color,
        "master": master, "subtitles": subtitles, "audio_rate": audio_rate,
        "audio_channels": audio_channels, "variants": variants,
        "relink_original": relink_original, "watermark_in_nle": watermark_in_nle,
    }
    paths: dict[str, Any] = {}
    if project_path:
        res = _deliver_from_project(
            project_path, {"mp4"}, out_dir, variant, no_nvenc,
            base_name=out_video_path.stem if not variants else None, settings_args=settings_args,
        )
        render_p, encoder, duration = res.render_path, res.encoder_used, res.duration
        paths = _result_paths(res)
    else:
        _reject_settings_without_project(settings_args)
        in_path, audio_path = Path(input_dir), Path(soundtrack)
        if not in_path.is_dir():
            raise NotADirectoryError(f"Input directory does not exist: {input_dir}")
        if not audio_path.is_file():
            raise FileNotFoundError(f"Soundtrack audio not found: {soundtrack}")
        cfg = _analysis_config(
            preset, speed_ramping=speed_ramping, watermark_path=watermark_path,
            layout_mode=layout_mode, subtitle_style=subtitle_style,
        )
        handle = adapter.submit_job(
            input_dir=in_path, soundtrack=audio_path, output_dir=out_dir, config=cfg,
            run_async=False, no_render=False, force_cpu=no_nvenc,
        )
        res = handle.result
        if not res or not res.success:
            raise ValueError(res.error_message if res else "Master render failed")
        render_p = res.render_path or (out_dir / f"master_{cfg.preset_name}.mp4")
        encoder, duration = res.encoder_used, res.total_duration

    if render_p is not None and not paths.get("variants") and render_p.is_file():
        # ProRes / DNxHR masters are .mov: the requested file name follows the real container.
        target = out_video_path.with_suffix(render_p.suffix) if render_p.suffix != out_video_path.suffix else out_video_path
        if render_p.resolve() != target.resolve():
            shutil.move(str(render_p), str(target))
            render_p = target
            paths["master_path"] = str(target)

    file_size = render_p.stat().st_size if render_p and render_p.is_file() else 0
    return {
        **paths,
        "output_path": str(render_p),
        "encoder_used": encoder or "unknown",
        "duration": round(duration, 2),
        "file_size_bytes": file_size,
        "return_code": 0,
    }


@mcp_server.tool()
def validate_timeline_qa(
    timeline_path: str,
    min_clip_frames: int = 6,
) -> dict[str, Any]:
    """Run timeline quality assurance checks to sanitize flash-frames and pop noises.

    Args:
        timeline_path: Path to OpenTimelineIO (.otio) file.
        min_clip_frames: Minimum frame length threshold to prevent flash-frame cuts.

    Returns:
        Sanitization report and count of adjustments made.
    """
    import opentimelineio as otio

    from src.modules.timeline_qa import TimelineQA

    p = Path(timeline_path)
    if not p.is_file():
        raise FileNotFoundError(f"Timeline file not found: {timeline_path}")

    timeline = otio.adapters.read_from_file(str(p))
    cfg = PipelineConfig(min_clip_frames_threshold=min_clip_frames)
    qa = TimelineQA(cfg)
    clean_tl = qa.validate_and_sanitize(timeline)

    out_p = p.with_name(f"{p.stem}_sanitized.otio")
    otio.adapters.write_to_file(clean_tl, str(out_p))

    return {
        "original_path": str(p.resolve()),
        "sanitized_path": str(out_p.resolve()),
        "status": "sanitized",
    }


# =============================================================================
# 6. Pipeline Job Management & Project Persistence Tools
# =============================================================================


@mcp_server.tool()
def execute_pipeline_job(
    input_dir: str,
    soundtrack: str | None = None,
    output_dir: str = "./output",
    preset: str = "1080p30",
    duration_mode: str = "fixed",
    target_duration: float | None = None,
    enable_transcription: bool = False,
    enable_teaser: bool = True,
    speed_ramping: bool = False,
    watermark_path: str | None = None,
    layout_mode: str = "auto_hybrid",
    subtitle_style: str = "kinetic_ass",
    no_render: bool = True,
    run_async: bool = False,
    stage: str = "all",
    genre: str | None = None,
    cadence: str | None = None,
    rough_cut: bool | None = None,
) -> dict[str, Any]:
    """Analyze footage and auto-edit a timeline, saved as a .tteproj project.

    Analysis only: stops at "Timeline ready" - nothing is exported. Use render_master /
    export_timeline with the returned project_path ONLY when the user asks to export.

    Args:
        input_dir: Path to directory of raw video footage.
        soundtrack: Background soundtrack audio file (optional only when stage is 'rough_cut').
        stage: 'rough_cut' (defect pruning only, saved as the 'rough_cut' timeline, no soundtrack),
            'sequence' (music edit) or 'all' (default, classic one-pass analysis).
        genre: Genre preset key (e.g. 'travel_cinematic', 'sports_extreme'); drives pacing.
        cadence: Cadence profile: 'fast_syncopated', 'balanced_narrative', 'lingering_cinematic', 'viral_burst'.
        rough_cut: With stage 'all', run the defect-pruning rough cut first (default off).
        output_dir: Project / deliverables directory.
        preset: Target preset (e.g. '1080p30', 'social_vertical', '4k30').
        duration_mode: Decoupling mode ('fixed', 'footage_driven', 'full_track').
        target_duration: Target duration in seconds (optional).
        enable_transcription: Run speech-to-text and subtitle generation.
        enable_teaser: Generate retention teaser hook.
        speed_ramping: Apply dynamic speed ramping.
        watermark_path: Path to watermark image overlay.
        layout_mode: 'smart_crop', 'blur_backdrop', 'auto_hybrid'.
        subtitle_style: 'none', 'clean_srt', 'kinetic_ass'.
        no_render: Keep True unless the user explicitly asked for the MP4 now.
        run_async: Run job in background thread and return job_id immediately.

    Returns:
        Job handle status, timeline summary and the saved project path.
    """
    from src.core.stage_options import STAGES, apply_staged_options
    from src.ui.controllers.project_manager import save_result_as_project

    stage = (stage or "all").lower()
    if stage not in STAGES:
        raise ValueError(f"Unknown stage '{stage}'. Use one of: {', '.join(STAGES)}")
    in_p = Path(input_dir)
    audio_p = Path(soundtrack) if soundtrack else None
    out_p = Path(output_dir)
    project_p = out_p / f"{in_p.resolve().name or 'project'}.tteproj"

    cfg = _analysis_config(
        preset, duration_mode, target_duration, enable_teaser, speed_ramping,
        watermark_path, layout_mode, subtitle_style, enable_transcription,
    )
    apply_staged_options(cfg, genre=genre, cadence=cadence, rough_cut=rough_cut)
    if audio_p is None and stage != "rough_cut" and cfg.workflow_mode != "rough_cut":
        raise ValueError("soundtrack is required unless stage is 'rough_cut'.")

    def _persist(handle) -> None:
        res = handle.result
        if res is not None and getattr(res, "success", False):
            save_result_as_project(res, cfg, project_p, [in_p], audio_p, out_p)

    handle = adapter.submit_job(
        input_dir=in_p,
        soundtrack=audio_p,
        output_dir=out_p,
        config=cfg,
        run_async=run_async,
        no_render=no_render,
        stage=stage,  # type: ignore[arg-type]
    )

    if run_async:
        import threading

        threading.Thread(
            target=lambda: (handle.wait(), _persist(handle)), daemon=True
        ).start()
        return {
            "job_id": handle.job_id,
            "status": "running",
            "project_path": str(project_p),
            "message": "Job submitted asynchronously. Check status via get_pipeline_job_status.",
        }

    _persist(handle)
    res = handle.result
    ok = bool(res and res.success)
    return {
        "job_id": handle.job_id,
        "success": ok,
        "status": "timeline_ready" if ok else "failed",
        "stage": res.stage if res else stage,
        "project_path": str(project_p) if ok else None,
        "timelines": sorted(res.edit_timelines) if ok else [],
        "duration": round(res.total_duration, 2) if res else 0.0,
        "clips": res.clip_count if res else 0,
        "render_path": str(res.render_path) if res and res.render_path else None,
        "error": res.error_message if res and not res.success else None,
    }


@mcp_server.tool()
def get_pipeline_job_status(job_id: str) -> dict[str, Any]:
    """Check the running status and progress of an active asynchronous editing job.

    Args:
        job_id: Unique job identifier returned by execute_pipeline_job.

    Returns:
        Current execution state (running, completed, cancelled) and result if finished.
    """
    handle = adapter.get_job(job_id)
    if not handle:
        return {"job_id": job_id, "status": "not_found"}

    is_running = handle.is_running()
    is_cancelled = handle.is_cancelled()
    res = handle.result

    return {
        "job_id": job_id,
        "is_running": is_running,
        "is_cancelled": is_cancelled,
        "status": "running" if is_running else ("cancelled" if is_cancelled else "completed"),
        "success": res.success if res else None,
        "total_duration": round(res.total_duration, 2) if res else None,
        "render_path": str(res.render_path) if res and res.render_path else None,
        "error_message": res.error_message if res and not res.success else None,
    }


@mcp_server.tool()
def cancel_pipeline_job(job_id: str) -> dict[str, Any]:
    """Cancel an active background video editing pipeline job.

    Args:
        job_id: Unique job identifier to abort.

    Returns:
        Cancellation confirmation status.
    """
    success = adapter.cancel_job(job_id)
    return {
        "job_id": job_id,
        "cancelled": success,
        "message": "Job cancellation signal transmitted." if success else "Job not found or already completed.",
    }


@mcp_server.tool()
def save_project(
    project_path: str,
    input_dir: str | None = None,
    soundtrack_path: str | None = None,
    output_dir: str | None = None,
    preset: str = "1080p30",
    project_name: str = "Untitled Project",
) -> dict[str, Any]:
    """Save the current editing state to a TeamTrau project file (.tteproj).

    Args:
        project_path: File path to save (.tteproj).
        input_dir: Footage directory.
        soundtrack_path: Background audio path.
        output_dir: Export directory.
        preset: Named preset.
        project_name: Human-readable project title.

    Returns:
        Saved project file path and metadata.
    """
    from src.ui.controllers.project_manager import ProjectData, ProjectManager

    p = Path(project_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.suffix:
        p = p.with_suffix(".tteproj")

    data = ProjectData(
        project_name=project_name,
        input_dir=input_dir,
        soundtrack_path=soundtrack_path,
        output_dir=output_dir,
        preset_name=preset,
    )
    mgr = ProjectManager()
    mgr.save_project(p, data)

    return {
        "project_path": str(p.resolve()),
        "project_name": project_name,
        "preset": preset,
        "status": "saved",
    }


@mcp_server.tool()
def load_project(project_path: str) -> dict[str, Any]:
    """Load and parse an existing TeamTrau project file (.tteproj).

    Args:
        project_path: Path to .tteproj file.

    Returns:
        Restored project data dictionary.
    """
    from src.ui.controllers.project_manager import ProjectManager

    p = Path(project_path)
    if not p.is_file():
        raise FileNotFoundError(f"Project file not found: {project_path}")

    mgr = ProjectManager()
    data = mgr.load_project(p)
    return {
        "project_path": str(p.resolve()),
        "project_data": data.to_dict(),
        "status": "loaded",
    }


@mcp_server.tool()
def tag_scene_with_vlm(
    image_path: str,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Analyze a video keyframe image using VLM (OpenRouter, Ollama, LM Studio, OpenAI).

    Args:
        image_path: Path to keyframe image file (PNG, JPG).
        prompt: Optional query prompt for the vision model.

    Returns:
        Semantic tags, detected mood, visual description, and key moment flags.
    """
    from src.modules.vlm_assistant import VLMAssistant

    p = Path(image_path)
    if not p.is_file():
        raise FileNotFoundError(f"Keyframe image not found: {image_path}")

    cfg = PipelineConfig(enable_vlm_tagging=True)
    assistant = VLMAssistant(cfg)
    analysis = assistant.analyze_scene(p, custom_prompt=prompt)

    return {
        "image_path": str(p.resolve()),
        "description": analysis.description,
        "tags": analysis.tags,
        "mood": analysis.mood,
        "pacing": analysis.pacing,
        "is_key_moment": analysis.is_key_moment,
        "model_used": analysis.model_used,
    }


@mcp_server.tool()
def generate_mcp_config(
    client_name: str = "all",
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> dict[str, Any]:
    """Generate ready-to-paste MCP client configuration JSON for AI agents.

    Supports Antigravity, Claude Desktop, Cursor, ChatGPT, and custom LLM clients.

    Args:
        client_name: 'antigravity', 'claude_desktop', 'cursor', or 'all'.
        transport: 'stdio' or 'sse'.
        host: Host IP for SSE server (default: 127.0.0.1).
        port: Port number for SSE server (default: 8000).

    Returns:
        JSON snippets formatted according to client-specific configuration schemas.
    """
    python_exe = sys.executable
    server_module = "src.modules.mcp_server"
    root_dir = str(Path(__file__).resolve().parent.parent.parent)

    stdio_server_entry = {
        "command": python_exe,
        "args": ["-m", server_module],
        "cwd": root_dir,
        "env": {"PYTHONUNBUFFERED": "1"},
    }

    sse_server_entry = {
        "url": f"http://{host}:{port}/sse",
    }

    active_entry = sse_server_entry if transport == "sse" else stdio_server_entry

    claude_config = {
        "mcpServers": {
            "teamtrau-autoedit": active_entry
        }
    }

    antigravity_config = {
        "mcpServers": {
            "teamtrau-autoedit": active_entry
        }
    }

    cursor_config = {
        "mcpServers": {
            "teamtrau-autoedit": active_entry
        }
    }

    configs = {
        "claude_desktop": claude_config,
        "antigravity": antigravity_config,
        "cursor": cursor_config,
        "sse_endpoint": f"http://{host}:{port}/sse",
    }

    if client_name in configs:
        return {"client": client_name, "config": configs[client_name]}
    return {"clients": configs}


# =============================================================================
# 7. MCP Resources & Prompt Templates
# =============================================================================


@mcp_server.resource("system://environment")
def resource_system_environment() -> str:
    """Live diagnostic snapshot of hardware, CUDA, NVENC, and NLE environment."""
    return json.dumps(check_system_environment(), indent=2)


@mcp_server.resource("config://presets")
def resource_presets() -> str:
    """Pre-configured resolution, framerate, and aspect ratio canvas presets."""
    return json.dumps(list_editing_presets(), indent=2)


@mcp_server.resource("config://workflows")
def resource_workflows() -> str:
    """Supported high-level video editing workflow modes."""
    return json.dumps(list_workflow_presets(), indent=2)


@mcp_server.prompt("autoedit_high_retention")
def prompt_high_retention_video() -> str:
    """Prompt template for orchestrating high-retention 9:16 short-form video synthesis."""
    return (
        "You are the Lead Video Editor for TeamTrau AutoEdit. Your goal is to assemble "
        "a viral, high-retention 9:16 vertical video from raw footage.\n"
        "Instructions:\n"
        "1. Detect shot intervals and eliminate dead-air and frozen frames.\n"
        "2. Score scenes by sharpness and aesthetic appeal, dropping duplicate camera angles via dHash.\n"
        "3. Synchronize cuts to the soundtrack downbeats with action-to-drop alignment.\n"
        "4. Place a rapid 3-5 cut retention teaser hook in the first 3 seconds.\n"
        "5. Apply AI smart reframing (blur backdrop or YOLO crop) and format kinetic ASS subtitles."
    )


@mcp_server.prompt("autoedit_cinematic")
def prompt_cinematic_edit() -> str:
    """Prompt template for orchestrating 4K landscape cinematic video synthesis."""
    return (
        "You are the Lead Video Editor for TeamTrau AutoEdit. Assemble a cinematic 4K landscape film.\n"
        "Instructions:\n"
        "1. Check visual continuity: compute Bhattacharyya luminance distance between cuts.\n"
        "2. Insert 10-frame cross-dissolves at high-contrast exposure boundaries.\n"
        "3. Apply subtle Ken Burns ambient zoom on static scenery clips (motion < 0.20).\n"
        "4. Export lossless project handoffs to DaVinci Resolve FCPXML and Premiere Pro XML."
    )


# =============================================================================
# 8. Server Runners (stdio and SSE)
# =============================================================================


def run_mcp_stdio() -> None:
    """Run the MCP server over standard input/output (stdio)."""
    mcp_server.run(transport="stdio")


def run_mcp_sse(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the MCP server over Server-Sent Events (SSE) for network/cloud agents."""
    logger.info("Starting TeamTrau AutoEdit MCP server over SSE at http://%s:%s/sse", host, port)
    mcp_server.run(transport="sse", host=host, port=port)


if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "sse":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
        run_mcp_sse(port=port)
    else:
        run_mcp_stdio()
