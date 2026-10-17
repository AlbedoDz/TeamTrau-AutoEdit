"""Cheap "will my footage be scaled / conformed?" check for the Deliver settings.

Probing is delegated to ``probe_media`` (cached by path, size and mtime), so repeated checks while
the user tweaks the canvas or frame rate cost nothing after the first pass.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.core.media_info import MediaInfo, probe_media
from src.core.timebase import frame_rate_fraction
from src.core.types import DeliverSettings, EditTimeline


@dataclass(frozen=True)
class SourceFormat:
    """Display size (after rotation) and frame rate of one source file."""

    width: int
    height: int
    fps: float


@dataclass(frozen=True)
class SourceMismatch:
    """How many timeline clips differ from the Deliver canvas / frame rate."""

    total: int = 0
    scaled: int = 0  # picture size differs from the canvas (fit / fill / stretch applies)
    conformed: int = 0  # frame rate differs (frames are dropped / repeated)
    affected: int = 0  # clips that are scaled and/or conformed

    @property
    def message(self) -> str:
        if not self.affected:
            return ""
        parts = []
        if self.scaled:
            parts.append(f"{self.scaled} scaled")
        if self.conformed:
            parts.append(f"{self.conformed} conformed")
        return (
            f"{self.affected} of {self.total} clips will be scaled/conformed to the Deliver canvas "
            f"and frame rate ({', '.join(parts)})."
        )


def clip_media_paths(timeline: EditTimeline) -> list[Path]:
    """Unique media files an NLE would reference (original media when known), in timeline order."""
    seen: dict[Path, None] = {}
    for clip in timeline.clips:
        seen.setdefault(clip.media_path(True), None)
    return list(seen)


def probe_source_formats(
    timeline: EditTimeline, probe: Callable[[Path], MediaInfo] = probe_media
) -> dict[Path, SourceFormat]:
    """Probe every distinct clip file once; files that cannot be probed are left out."""
    formats: dict[Path, SourceFormat] = {}
    for path in clip_media_paths(timeline):
        info = probe(path)
        if info.has_video and info.width > 0 and info.height > 0:
            formats[path] = SourceFormat(info.width, info.height, info.fps_float)
    return formats


def count_source_mismatch(
    timeline: EditTimeline, formats: dict[Path, SourceFormat], settings: DeliverSettings
) -> SourceMismatch:
    """Count clips of ``timeline`` whose probed source differs from ``settings`` (unknown files skipped)."""
    target_fps = frame_rate_fraction(settings.fps)
    scaled = conformed = affected = total = 0
    for clip in timeline.clips:
        fmt = formats.get(clip.media_path(True))
        if fmt is None:
            continue
        total += 1
        is_scaled = (fmt.width, fmt.height) != settings.canvas
        is_conformed = fmt.fps > 0 and abs(fmt.fps - float(target_fps)) > 0.01
        scaled += is_scaled
        conformed += is_conformed
        affected += is_scaled or is_conformed
    return SourceMismatch(total=total, scaled=scaled, conformed=conformed, affected=affected)
