"""Qt-free (de)serialization helpers for edit timelines stored in .tteproj projects."""

from pathlib import Path
from typing import Any

from src.core.types import (
    CropWindow,
    DeliverSettings,
    EditClip,
    EditTimeline,
    MusicalBarGrid,
    SongSection,
    SongSectionType,
)


def _crop_to_dict(cw: CropWindow | None) -> dict[str, Any] | None:
    if cw is None:
        return None
    return {
        "x": cw.x,
        "y": cw.y,
        "width": cw.width,
        "height": cw.height,
        "confidence": cw.confidence,
        "tracking_subject": cw.tracking_subject,
    }


def _crop_from_dict(d: dict[str, Any] | None) -> CropWindow | None:
    if not d:
        return None
    return CropWindow(
        x=int(d["x"]),
        y=int(d["y"]),
        width=int(d["width"]),
        height=int(d["height"]),
        confidence=float(d.get("confidence", 1.0)),
        tracking_subject=str(d.get("tracking_subject", "person")),
    )


def section_to_dict(s: SongSection) -> dict[str, Any]:
    return {
        "section_type": s.section_type.value if hasattr(s.section_type, "value") else str(s.section_type),
        "start_time": s.start_time,
        "end_time": s.end_time,
        "duration": s.duration,
        "avg_energy": s.avg_energy,
        "chroma_profile": list(s.chroma_profile),
        "is_climax": s.is_climax,
    }


def section_from_dict(d: dict[str, Any]) -> SongSection:
    try:
        st = SongSectionType(d.get("section_type", "INTRO"))
    except ValueError:
        st = SongSectionType.INTRO
    return SongSection(
        section_type=st,
        start_time=float(d["start_time"]),
        end_time=float(d["end_time"]),
        duration=float(d["duration"]),
        avg_energy=float(d.get("avg_energy", 0.5)),
        chroma_profile=list(d.get("chroma_profile", [])),
        is_climax=bool(d.get("is_climax", False)),
    )


def bar_grid_to_dict(g: MusicalBarGrid | None) -> dict[str, Any] | None:
    if g is None:
        return None
    return {
        "tempo_bpm": g.tempo_bpm,
        "beats_per_bar": g.beats_per_bar,
        "bar_times": list(g.bar_times),
        "phrase_2": list(g.phrase_2),
        "phrase_4": list(g.phrase_4),
        "phrase_8": list(g.phrase_8),
        "confidence": g.confidence,
        "bar_confidence": list(g.bar_confidence),
    }


def bar_grid_from_dict(d: dict[str, Any] | None) -> MusicalBarGrid | None:
    if not d:
        return None
    return MusicalBarGrid(
        tempo_bpm=float(d.get("tempo_bpm", 0.0)),
        beats_per_bar=int(d.get("beats_per_bar", 4)),
        bar_times=[float(t) for t in d.get("bar_times", [])],
        phrase_2=[float(t) for t in d.get("phrase_2", [])],
        phrase_4=[float(t) for t in d.get("phrase_4", [])],
        phrase_8=[float(t) for t in d.get("phrase_8", [])],
        confidence=float(d.get("confidence", 0.0)),
        bar_confidence=[float(t) for t in d.get("bar_confidence", [])],
    )


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


def edit_clip_to_dict(c: EditClip) -> dict[str, Any]:
    return {
        "source_path": str(c.source_path),
        "source_in": c.source_in,
        "frames": c.frames,
        "available": c.available,
        "speed": c.speed,
        "crop_window": _crop_to_dict(c.crop_window),
        "ken_burns": c.ken_burns,
        "transition_in_frames": c.transition_in_frames,
        "is_teaser": c.is_teaser,
        "label": c.label,
        "markers": list(c.markers),
        "original_path": str(c.original_path) if c.original_path else None,
        "crop_needs_recompute": c.crop_needs_recompute,
        "scene_start": c.scene_start,
        "scene_end": c.scene_end,
    }


def edit_clip_from_dict(d: dict[str, Any]) -> EditClip:
    return EditClip(
        source_path=Path(d["source_path"]),
        source_in=float(d.get("source_in", 0.0)),
        frames=int(d["frames"]),
        available=float(d.get("available", 0.0)),
        speed=float(d.get("speed", 1.0)),
        crop_window=_crop_from_dict(d.get("crop_window")),
        ken_burns=d.get("ken_burns"),
        transition_in_frames=int(d.get("transition_in_frames", 0)),
        is_teaser=bool(d.get("is_teaser", False)),
        label=str(d.get("label", "")),
        markers=list(d.get("markers", [])),
        original_path=Path(d["original_path"]) if d.get("original_path") else None,
        crop_needs_recompute=bool(d.get("crop_needs_recompute", False)),
        scene_start=_opt_float(d.get("scene_start")),
        scene_end=_opt_float(d.get("scene_end")),
    )


def edit_timeline_to_dict(tl: EditTimeline) -> dict[str, Any]:
    return {
        "name": tl.name,
        "fps": tl.fps,
        "canvas_width": tl.canvas_width,
        "canvas_height": tl.canvas_height,
        "soundtrack_path": str(tl.soundtrack_path) if tl.soundtrack_path else None,
        "soundtrack_in": tl.soundtrack_in,
        "variant_key": tl.variant_key,
        "kind": tl.kind,
        "sections": [section_to_dict(s) for s in tl.sections],
        "clips": [edit_clip_to_dict(c) for c in tl.clips],
        "bar_grid": bar_grid_to_dict(tl.bar_grid),
        "cut_snap_error_max_sec": tl.cut_snap_error_max_sec,
    }


def edit_timeline_from_dict(d: dict[str, Any]) -> EditTimeline:
    return EditTimeline(
        name=str(d.get("name", "AutoEdit_Sequence")),
        fps=float(d.get("fps", 30.0)),
        canvas_width=int(d.get("canvas_width", 1920)),
        canvas_height=int(d.get("canvas_height", 1080)),
        clips=[edit_clip_from_dict(c) for c in d.get("clips", [])],
        soundtrack_path=Path(d["soundtrack_path"]) if d.get("soundtrack_path") else None,
        soundtrack_in=float(d.get("soundtrack_in", 0.0)),
        sections=[section_from_dict(s) for s in d.get("sections", [])],
        variant_key=str(d.get("variant_key", "master")),
        kind="rough_cut" if d.get("kind") == "rough_cut" else "music",
        bar_grid=bar_grid_from_dict(d.get("bar_grid")),
        cut_snap_error_max_sec=_opt_float(d.get("cut_snap_error_max_sec")),
    )


def deliver_settings_to_dict(s: DeliverSettings) -> dict[str, Any]:
    """JSON-serializable form of DeliverSettings (stored in .tteproj v2.2)."""
    return {
        "canvas_width": s.canvas_width,
        "canvas_height": s.canvas_height,
        "fps": s.fps,
        "fit_mode": s.fit_mode,
        "color": s.color,
        "audio_rate": s.audio_rate,
        "audio_channels": s.audio_channels,
        "master": s.master,
        "subtitles": s.subtitles,
        "watermark_in_nle": s.watermark_in_nle,
        "relink_original": s.relink_original,
        "variants": list(s.variants),
        "fcpxml_version": s.fcpxml_version,
    }


def deliver_settings_from_dict(d: dict[str, Any]) -> DeliverSettings:
    """Inverse of :func:`deliver_settings_to_dict`; missing keys take the dataclass defaults.

    Unknown keys are ignored (forward compatibility). Invalid values raise ValueError.
    """
    defaults = DeliverSettings(canvas_width=1920, canvas_height=1080, fps=30.0)
    return DeliverSettings(
        canvas_width=int(d.get("canvas_width", defaults.canvas_width)),
        canvas_height=int(d.get("canvas_height", defaults.canvas_height)),
        fps=float(d.get("fps", defaults.fps)),
        fit_mode=d.get("fit_mode", defaults.fit_mode),
        color=d.get("color", defaults.color),
        audio_rate=int(d.get("audio_rate", defaults.audio_rate)),
        audio_channels=int(d.get("audio_channels", defaults.audio_channels)),
        master=d.get("master", defaults.master),
        subtitles=d.get("subtitles", defaults.subtitles),
        watermark_in_nle=bool(d.get("watermark_in_nle", defaults.watermark_in_nle)),
        relink_original=bool(d.get("relink_original", defaults.relink_original)),
        variants=list(d.get("variants") or defaults.variants),
        fcpxml_version=str(d.get("fcpxml_version", defaults.fcpxml_version)),
    )
