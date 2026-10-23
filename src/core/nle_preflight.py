"""NLE pre-flight checks: inspect media files before NLE export or delivery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.core.media_info import MediaInfo, probe_media
from src.core.timebase import frame_rate_fraction
from src.core.types import EditTimeline
from src.modules.fcpxml_exporter import _uri_to_path

IssueCode = Literal[
    "MEDIA_MISSING",
    "MEDIA_UNREADABLE",
    "VFR_SOURCE",
    "FPS_MISMATCH",
    "AUDIO_MONO",
    "AUDIO_MULTICHANNEL",
    "AUDIO_RATE_MISMATCH",
    "URI_ROUNDTRIP",
]


@dataclass(frozen=True)
class PreflightIssue:
    code: IssueCode
    severity: Literal["error", "warning"]
    path: Path
    detail: str


@dataclass
class PreflightReport:
    issues: list[PreflightIssue]
    checked_files: int

    @property
    def ok(self) -> bool:
        """True when there are no 'error' severity issues."""
        return not any(i.severity == "error" for i in self.issues)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dictionary representation of the report."""
        return {
            "ok": self.ok,
            "checked_files": self.checked_files,
            "issues": [
                {
                    "code": i.code,
                    "severity": i.severity,
                    "path": str(i.path),
                    "detail": i.detail,
                }
                for i in self.issues
            ],
        }


def preflight_timeline(
    timeline: EditTimeline,
    audio_rate: int = 48000,
    probe: Callable[[Path], MediaInfo] = probe_media,
) -> PreflightReport:
    """Check timeline media files for missing files, unreadable media, VFR, fps/rate mismatch, URI issues."""
    unique_paths: list[Path] = []
    seen: set[Path] = set()

    for clip in timeline.clips:
        mp = clip.media_path()
        if mp not in seen:
            seen.add(mp)
            unique_paths.append(mp)

    soundtrack_path: Path | None = None
    if timeline.soundtrack_path is not None:
        soundtrack_path = Path(timeline.soundtrack_path)
        if soundtrack_path not in seen:
            seen.add(soundtrack_path)
            unique_paths.append(soundtrack_path)

    clip_paths = {clip.media_path() for clip in timeline.clips}
    issues: list[PreflightIssue] = []

    for path in unique_paths:
        if not path.exists():
            issues.append(
                PreflightIssue(
                    code="MEDIA_MISSING",
                    severity="error",
                    path=path,
                    detail=f"Media file not found: {path}",
                )
            )
            continue

        resolved = path.resolve()
        try:
            rt = _uri_to_path(resolved.as_uri())
            if rt != resolved:
                issues.append(
                    PreflightIssue(
                        code="URI_ROUNDTRIP",
                        severity="error",
                        path=path,
                        detail=f"URI round-trip mismatch: {resolved} != {rt}",
                    )
                )
        except Exception as exc:
            issues.append(
                PreflightIssue(
                    code="URI_ROUNDTRIP",
                    severity="error",
                    path=path,
                    detail=f"URI round-trip error: {exc}",
                )
            )

        try:
            info = probe(path)
        except Exception as exc:
            issues.append(
                PreflightIssue(
                    code="MEDIA_UNREADABLE",
                    severity="error",
                    path=path,
                    detail=f"Probe failed: {exc}",
                )
            )
            continue

        if info is None:
            issues.append(
                PreflightIssue(
                    code="MEDIA_UNREADABLE",
                    severity="error",
                    path=path,
                    detail="Media probe returned empty result",
                )
            )
            continue

        is_clip = path in clip_paths
        is_soundtrack = soundtrack_path is not None and path == soundtrack_path

        if is_clip and not info.has_video:
            issues.append(
                PreflightIssue(
                    code="MEDIA_UNREADABLE",
                    severity="error",
                    path=path,
                    detail="Clip has no video stream",
                )
            )

        if is_soundtrack and not info.has_audio:
            issues.append(
                PreflightIssue(
                    code="MEDIA_UNREADABLE",
                    severity="error",
                    path=path,
                    detail="Soundtrack has no audio stream",
                )
            )

        if is_clip:
            if info.is_vfr:
                issues.append(
                    PreflightIssue(
                        code="VFR_SOURCE",
                        severity="warning",
                        path=path,
                        detail="Variable frame rate source may cause sync drift in NLE",
                    )
                )

            if info.fps is not None and info.fps > 0:
                try:
                    clip_fps_frac = frame_rate_fraction(info.fps)
                    tl_fps_frac = frame_rate_fraction(timeline.fps)
                    if clip_fps_frac != tl_fps_frac:
                        issues.append(
                            PreflightIssue(
                                code="FPS_MISMATCH",
                                severity="warning",
                                path=path,
                                detail=f"Clip frame rate {clip_fps_frac} != timeline frame rate {tl_fps_frac}",
                            )
                        )
                except (ValueError, ZeroDivisionError):
                    pass

        if info.has_audio:
            if info.audio_channels == 1:
                issues.append(
                    PreflightIssue(
                        code="AUDIO_MONO",
                        severity="warning",
                        path=path,
                        detail="Mono audio channel layout",
                    )
                )
            elif info.audio_channels is not None and info.audio_channels > 2:
                issues.append(
                    PreflightIssue(
                        code="AUDIO_MULTICHANNEL",
                        severity="warning",
                        path=path,
                        detail=f"Multichannel audio ({info.audio_channels} channels)",
                    )
                )

            if info.audio_rate and info.audio_rate != audio_rate:
                issues.append(
                    PreflightIssue(
                        code="AUDIO_RATE_MISMATCH",
                        severity="warning",
                        path=path,
                        detail=f"Audio sample rate {info.audio_rate} Hz != expected {audio_rate} Hz",
                    )
                )

    return PreflightReport(issues=issues, checked_files=len(unique_paths))
