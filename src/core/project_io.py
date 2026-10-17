"""Qt-free (de)serialization helpers for edit timelines stored in .tteproj projects."""

from pathlib import Path
from typing import Any

from src.core.types import CropWindow, EditClip, EditTimeline, SongSection, SongSectionType


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
    )
