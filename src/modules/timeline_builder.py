"""OpenTimelineIO sequence synthesizer exporting FCPXML and Premiere XML."""

import logging
from pathlib import Path

import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange

from src.core.config import PipelineConfig
from src.core.media_info import probe_media
from src.core.ontology import natural_sort_key
from src.core.types import (
    CleanedSegment,
    DefectType,
    DeliverSettings,
    EditTimeline,
    ScoredScene,
    SongSection,
    TimelineExportResult,
)
from src.modules.edit_timeline import select_scenes_for_cuts  # noqa: F401  (re-export)

logger = logging.getLogger(__name__)


def _media_reference(path: Path, fallback_duration: float, fps: float) -> otio.schema.ExternalReference:
    """External reference whose available_range covers the WHOLE source file at its own rate.

    NLEs treat a clip whose source_range falls outside available_range as offline/clamped,
    so this must describe the real media, not the scene slice.
    """
    duration, rate = media_duration_and_rate(path)
    if duration <= 0:
        duration = fallback_duration
    if rate <= 0:
        rate = fps
    return otio.schema.ExternalReference(
        target_url=Path(path).resolve().as_uri(),
        available_range=TimeRange(
            start_time=RationalTime(0, rate),
            duration=RationalTime(int(round(duration * rate)), rate),
        ),
    )


def media_duration_and_rate(path: Path) -> tuple[float, float]:
    """(duration_sec, video_fps) of a media file via the shared probe; zeros when unknown.

    A variable-frame-rate file has no meaningful media rate, so 0.0 is returned and callers
    fall back to the timeline rate.
    """
    info = probe_media(path)
    if info.is_vfr:
        return info.duration, 0.0
    rate = float(info.avg_fps or info.fps or 0)
    return info.duration, rate


def _probe_media_cached(path_str: str, size: int = 0, mtime_ns: int = 0) -> tuple[float, float]:
    """Backwards-compatible shim (probe results are cached in ``probe_media`` itself)."""
    return media_duration_and_rate(Path(path_str))


def _source_range(source_in: float, frames_out: int, timeline_fps: float, media_rate: float) -> TimeRange:
    """Source range expressed at the media's own rate (23.976/29.97/59.94...)."""
    rate = media_rate if media_rate > 0 else timeline_fps
    dur_sec = frames_out / timeline_fps
    return TimeRange(
        start_time=RationalTime(round(source_in * rate), rate),
        duration=RationalTime(max(1, round(dur_sec * rate)), rate),
    )


class TimelineBuilder:
    """Builds multi-track non-destructive timelines and serializes to NLE interchange formats."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def build_timeline(
        self,
        scored_scenes: list[ScoredScene],
        cut_timestamps: list[float],
        soundtrack_path: Path,
        timeline_name: str = "AutoEdit_Sequence",
        include_teaser: bool | None = None,
        sections: list[SongSection] | None = None,
    ) -> otio.schema.Timeline:
        """Synthesize timeline aligning ranked clips to audio beat intervals with optional teaser."""
        from src.modules.edit_timeline import build_edit_timeline

        edit_tl = build_edit_timeline(
            self.config,
            scored_scenes,
            cut_timestamps,
            soundtrack_path=soundtrack_path,
            sections=sections,
            include_teaser=include_teaser,
            name=timeline_name,
        )
        return self.build_from_edit_timeline(edit_tl)

    def build_from_edit_timeline(
        self, edit_tl: EditTimeline, relink_original: bool = True
    ) -> otio.schema.Timeline:
        """Serialize an EditTimeline into an OTIO timeline (V1 picture, A1 music or camera audio).

        With ``relink_original`` (default) clips that were cut from a render proxy (VFR->CFR
        cache file) reference the user's original media, so NLE files never point into .cache.
        """
        fps = edit_tl.fps
        timeline = otio.schema.Timeline(name=edit_tl.name)
        video_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
        audio_track = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)
        timeline.tracks.append(video_track)
        timeline.tracks.append(audio_track)
        camera_audio = edit_tl.soundtrack_path is None

        for idx, ec in enumerate(edit_tl.clips):
            speed = max(0.1, float(ec.speed or 1.0))
            src_frames = int(round(ec.available / speed * fps))
            clip_frames = max(1, min(ec.frames, src_frames))
            pad_frames = max(0, ec.frames - clip_frames)

            media_ref = _media_reference(ec.media_path(relink_original), ec.source_in + ec.available, fps)
            media_rate = media_ref.available_range.duration.rate
            src_range = _source_range(ec.source_in, clip_frames, fps, media_rate)
            clip = otio.schema.Clip(
                name=ec.label or f"{Path(ec.source_path).stem}_{idx:03d}",
                media_reference=media_ref,
                source_range=src_range,
            )
            meta: dict[str, object] = {"speed": speed}
            if ec.crop_window is not None:
                cw = ec.crop_window
                meta.update({
                    "crop_x": cw.x,
                    "crop_y": cw.y,
                    "crop_width": cw.width,
                    "crop_height": cw.height,
                    "tracking_subject": cw.tracking_subject,
                })
            if ec.ken_burns:
                meta["ken_burns"] = ec.ken_burns
                clip.effects.append(
                    otio.schema.Effect(
                        effect_name="KenBurns",
                        metadata={
                            "motion": ec.ken_burns,
                            "scale_factor": self.config.ken_burns_scale_factor,
                        },
                    )
                )
            if edit_tl.kind == "rough_cut":
                meta["rough_cut"] = True
                meta["defects_removed_count"] = len(ec.markers)
            clip.metadata["AutoEdit"] = meta
            if speed != 1.0:
                clip.effects.append(otio.schema.LinearTimeWarp(time_scalar=speed))
            for mk in ec.markers:
                color = getattr(otio.schema.MarkerColor, str(mk.get("color", "YELLOW")), otio.schema.MarkerColor.YELLOW)
                clip.markers.append(
                    otio.schema.Marker(
                        name=str(mk.get("name", "Marker")),
                        marked_range=src_range,
                        color=color,
                        comment=str(mk.get("comment", "")),
                    )
                )

            if ec.transition_in_frames > 0 and len(video_track) > 0:
                half_in = ec.transition_in_frames // 2
                video_track.append(
                    otio.schema.Transition(
                        name="Cross Dissolve",
                        transition_type=otio.schema.TransitionTypes.SMPTE_Dissolve,
                        in_offset=RationalTime(half_in, fps),
                        out_offset=RationalTime(ec.transition_in_frames - half_in, fps),
                    )
                )
            video_track.append(clip)
            if camera_audio:
                cam = otio.schema.Clip(
                    name=f"{Path(ec.source_path).stem}_audio_{idx + 1:03d}",
                    media_reference=media_ref.clone(),
                    source_range=src_range,
                )
                cam.metadata["AutoEdit"] = {"role": "camera_audio"}
                audio_track.append(cam)
            if pad_frames > 0:
                video_track.append(otio.schema.Gap(duration=RationalTime(pad_frames, fps)))
                if camera_audio:
                    audio_track.append(otio.schema.Gap(duration=RationalTime(pad_frames, fps)))

        total_frames = edit_tl.total_frames
        if not camera_audio and edit_tl.soundtrack_path is not None:
            snd = Path(edit_tl.soundtrack_path)
            audio_ref = _media_reference(snd, edit_tl.soundtrack_in + total_frames / fps, fps)
            music = otio.schema.Clip(
                name=snd.stem,
                media_reference=audio_ref,
                source_range=TimeRange(
                    start_time=RationalTime(round(edit_tl.soundtrack_in * fps), fps),
                    duration=RationalTime(total_frames, fps),
                ),
            )
            music.metadata["AutoEdit"] = {"role": "music"}
            audio_track.append(music)

        logger.info(
            "Constructed OTIO timeline '%s' with %d clips across %.2f seconds.",
            edit_tl.name,
            len(edit_tl.clips),
            edit_tl.duration,
        )
        return timeline

    def build_rough_cut_timeline(
        self,
        cleaned_segments: list[CleanedSegment],
        timeline_name: str = "RoughCut_Assembly",
        fps: float | None = None,
    ) -> otio.schema.Timeline:
        """Synthesize chronological assembly rough-cut stringout sequence.

        Track Structure:
        - Track V1: All cleaned golden takes concatenated back-to-back in chronological order.
        - Track A1: Synchronized original camera audio (BWF metadata stripped) with identical SourceRange.
        - Track V2: Review markers indicating defect types removed (wobble, shake, defocus).
        """
        if not cleaned_segments:
            raise ValueError("Cannot build rough cut timeline with empty cleaned_segments list.")

        actual_fps = fps or self.config.fps or 30.0
        timeline = otio.schema.Timeline(name=timeline_name)
        video_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
        audio_track = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)
        review_track = otio.schema.Track(name="V2_Review_Markers", kind=otio.schema.TrackKind.Video)

        timeline.tracks.append(video_track)
        timeline.tracks.append(review_track)
        timeline.tracks.append(audio_track)

        # Enforce chronological ordering
        sorted_segments = sorted(
            cleaned_segments,
            key=lambda s: (natural_sort_key(s.clip_path), s.source_start_time),
        )

        for i, seg in enumerate(sorted_segments):
            dur_frames = max(1, int(round(seg.duration * actual_fps)))
            start_frame = int(round(seg.source_start_time * actual_fps))

            media_ref = otio.schema.ExternalReference(
                target_url=seg.clip_path.resolve().as_uri(),
                available_range=TimeRange(
                    start_time=RationalTime(0, actual_fps),
                    duration=RationalTime(
                        int(round(max(seg.scene.duration, seg.source_end_time) * actual_fps)),
                        actual_fps,
                    ),
                ),
            )

            # 1. Video Clip on V1
            v_clip = otio.schema.Clip(
                name=f"{seg.clip_path.stem}_take_{i+1:03d}",
                media_reference=media_ref,
                source_range=TimeRange(
                    start_time=RationalTime(start_frame, actual_fps),
                    duration=RationalTime(dur_frames, actual_fps),
                ),
            )
            v_clip.metadata["AutoEdit"] = {
                "rough_cut": True,
                "defects_removed_count": len(seg.defects_removed),
                "sharpness_score": seg.sharpness_score,
                "requires_deflicker": seg.requires_deflicker,
            }
            video_track.append(v_clip)

            # 2. Audio Clip on A1 (matching identical SourceRange for synchronous camera audio)
            a_clip = otio.schema.Clip(
                name=f"{seg.clip_path.stem}_audio_{i+1:03d}",
                media_reference=media_ref,
                source_range=TimeRange(
                    start_time=RationalTime(start_frame, actual_fps),
                    duration=RationalTime(dur_frames, actual_fps),
                ),
            )
            audio_track.append(a_clip)

            # 3. Review Marker on V2 / v_clip for any pruned defect spans
            for def_span in seg.defects_removed:
                marker_color = (
                    otio.schema.MarkerColor.RED
                    if def_span.defect_type in (DefectType.CAMERA_SHAKE, DefectType.WHIP_PAN)
                    else otio.schema.MarkerColor.ORANGE
                    if def_span.defect_type == DefectType.HEAD_TAIL_WOBBLE
                    else otio.schema.MarkerColor.YELLOW
                )
                v_clip.markers.append(
                    otio.schema.Marker(
                        name=f"Pruned: {def_span.defect_type.value}",
                        marked_range=TimeRange(
                            start_time=RationalTime(start_frame, actual_fps),
                            duration=RationalTime(dur_frames, actual_fps),
                        ),
                        color=marker_color,
                        comment=def_span.description,
                    )
                )

        # Gap filler for review track V2 matching total timeline duration
        total_dur = timeline.duration()
        if total_dur and total_dur.value > 0:
            review_track.append(
                otio.schema.Gap(
                    source_range=TimeRange(
                        start_time=RationalTime(0, actual_fps),
                        duration=total_dur,
                    )
                )
            )

        logger.info(
            "Constructed rough cut timeline '%s' with %d golden takes (total %.2f sec).",
            timeline_name,
            len(video_track),
            total_dur.to_seconds() if total_dur else 0.0,
        )
        return timeline

    def export_all(
        self,
        timeline: otio.schema.Timeline,
        output_dir: Path,
        base_name: str = "timeline",
        formats: set[str] | None = None,
        settings: DeliverSettings | None = None,
    ) -> TimelineExportResult:
        """Export timeline to DaVinci Resolve FCPXML, Premiere XML, OTIO and CapCut.

        ``formats`` limits which interchange files are written (None = all). ``settings``
        (Deliver) overrides the canvas and frame rate of the config and selects the FCPXML
        colour space and fit mode; without it the config values and legacy defaults apply.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        canvas = settings.canvas if settings else (self.config.canvas_width, self.config.canvas_height)
        fps = settings.fps if settings else self.config.fps
        color = settings.color if settings else None
        fit_mode = settings.fit_mode if settings else "auto"
        if settings and settings.fcpxml_version != "1.8":
            logger.warning(
                "FCPXML %s requested but only 1.8 (verified on DaVinci Resolve) is written.",
                settings.fcpxml_version,
            )
        wanted = set(formats) if formats is not None else {"otio", "premiere", "fcpxml", "capcut"}

        fcpxml_path = output_dir / f"{base_name}.fcpxml"
        premiere_xml_path = output_dir / f"{base_name}_premiere.xml"
        otio_path = output_dir / f"{base_name}.otio"

        def write(path: Path, adapter: str | None, label: str) -> Path | None:
            try:
                otio.adapters.write_to_file(timeline, str(path), adapter_name=adapter)
                logger.info("Exported %s: %s", label, path.name)
                return path
            except Exception as exc:
                logger.warning("%s export failed (%s)", label, exc)
                return None

        # 1. Native OTIO
        if "otio" in wanted:
            write(otio_path, None, "OTIO timeline")

        # 2. Premiere Pro XML (FCP 7 XML adapter)
        exported_premiere = (
            write(premiere_xml_path, "fcp_xml", "Premiere Pro XML") if "premiere" in wanted else None
        )
        if exported_premiere is not None:
            try:
                from src.modules.premiere_xml_fix import finalize_premiere_xml

                finalize_premiere_xml(
                    exported_premiere,
                    timeline=timeline,
                    canvas=canvas,
                    fps=fps,
                )  # real channel/size info, speed and fit for Premiere
            except Exception as exc:
                logger.warning("Premiere XML channel fix-up skipped (%s)", exc)

        # 3. DaVinci Resolve / Final Cut FCPXML 1.8 (own writer; OTIO has no FCPXML adapter here)
        exported_resolve = None
        if "fcpxml" in wanted:
            try:
                from src.modules.fcpxml_exporter import write_fcpxml

                camera_db = (
                    self.config.camera_audio_mix_db if self.config.keep_video_audio else -96.0
                )
                exported_resolve = write_fcpxml(
                    timeline,
                    fcpxml_path,
                    fps=fps,
                    width=canvas[0],
                    height=canvas[1],
                    camera_audio_db=camera_db,
                    project_name=base_name,
                    color=color,
                    fit_mode=fit_mode,
                )
                logger.info("Exported DaVinci Resolve FCPXML: %s", fcpxml_path.name)
            except Exception as exc:
                logger.warning("DaVinci Resolve FCPXML export failed (%s)", exc)

        # 4. CapCut Desktop Native Draft Export (Phase 4)
        exported_capcut = None
        if self.config.enable_capcut_export and "capcut" in wanted:
            try:
                from src.modules.capcut_exporter import CapCutExporter

                capcut_exporter = CapCutExporter(self.config)
                draft_dir = output_dir / f"{base_name}_capcut_draft"
                exported_capcut = capcut_exporter.export_timeline(
                    timeline, draft_dir, draft_name=base_name
                )
                logger.info("Exported CapCut Desktop draft: %s", draft_dir.name)
            except Exception as exc:
                logger.warning("CapCut draft export failed (%s)", exc)

        # Compute duration and clip count
        video_tracks = [t for t in timeline.tracks if t.kind == otio.schema.TrackKind.Video]
        clip_count = sum(
            len([c for c in t if isinstance(c, otio.schema.Clip)])
            for t in video_tracks
        )
        dur_rational = timeline.duration()
        total_duration = dur_rational.to_seconds() if dur_rational else 0.0

        return TimelineExportResult(
            fcpxml_path=exported_resolve,
            premiere_xml_path=exported_premiere,
            clip_count=clip_count,
            total_duration=total_duration,
            otio_path=otio_path if "otio" in wanted and otio_path.is_file() else None,
            capcut_draft_path=exported_capcut,
        )
