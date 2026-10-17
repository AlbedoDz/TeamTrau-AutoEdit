"""OpenTimelineIO sequence synthesizer exporting FCPXML and Premiere XML."""

import logging
from functools import lru_cache
from pathlib import Path

import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange

from src.core.config import PipelineConfig
from src.core.ffmpeg_utils import ffprobe_json
from src.core.ontology import natural_sort_key
from src.core.types import (
    CleanedSegment,
    DefectType,
    ScoredScene,
    SongSection,
    SongSectionType,
    TimelineExportResult,
)
from src.modules.visual_flow import assign_ambient_motion, evaluate_cut_continuity

logger = logging.getLogger(__name__)


def select_scenes_for_cuts(
    scored_scenes: list[ScoredScene],
    cut_timestamps: list[float],
    sections: list[SongSection] | None = None,
    action_drop_threshold: float = 0.70,
) -> list[ScoredScene]:
    """Align scored scenes to cut intervals based on music structural sections and motion energy.

    - DROP / CLIMAX sections: prioritizes high-action footage (motion_energy >= action_drop_threshold).
    - INTRO / OUTRO sections: prioritizes scenic/calm footage (motion_energy < 0.35).
    - BUILDUP sections: prioritizes dynamic footage (0.35 <= motion_energy < action_drop_threshold).
    - Gracefully falls back to ranked scenes if any pool is empty or sections is None.
    """
    num_slots = len(cut_timestamps) - 1
    if num_slots <= 0:
        return []
    if not scored_scenes:
        raise ValueError("Cannot select scenes from empty scored_scenes.")

    high_action = [s for s in scored_scenes if s.motion_energy >= action_drop_threshold]
    scenic = [s for s in scored_scenes if s.motion_energy < 0.35]
    dynamic = [s for s in scored_scenes if 0.35 <= s.motion_energy < action_drop_threshold]

    pool_drop = high_action if high_action else scored_scenes
    pool_scenic = scenic if scenic else scored_scenes
    pool_dynamic = dynamic if dynamic else scored_scenes

    cursors = {"drop": 0, "scenic": 0, "dynamic": 0, "default": 0}

    def pick(pool: list[ScoredScene], key: str, slot_dur: float) -> ScoredScene:
        """Round-robin through the pool, preferring a scene long enough to fill the slot.

        A scene shorter than its beat slot would otherwise leave a hole that pushes every
        later cut off the beat.
        """
        start = cursors[key]
        n = len(pool)
        for k in range(n):
            cand = pool[(start + k) % n]
            needed = slot_dur * max(0.1, float(cand.speed or 1.0))
            if cand.scene.duration + 1e-6 >= needed:
                cursors[key] = start + k + 1
                return cand
        cursors[key] = start + 1
        return max(pool, key=lambda c: c.scene.duration / max(0.1, float(c.speed or 1.0)))

    selected: list[ScoredScene] = []
    for i in range(num_slots):
        slot_start = cut_timestamps[i]
        slot_end = cut_timestamps[i + 1]
        slot_dur = slot_end - slot_start
        mid_t = (slot_start + slot_end) / 2.0

        if sections:
            sec: SongSection | None = None
            for s in sections:
                if s.start_time <= mid_t < s.end_time:
                    sec = s
                    break
            if sec is None:
                sec = min(
                    sections,
                    key=lambda s: min(abs(s.start_time - mid_t), abs(s.end_time - mid_t)),
                )

            if sec.section_type == SongSectionType.DROP or sec.is_climax:
                candidate = pick(pool_drop, "drop", slot_dur)
            elif sec.section_type in (SongSectionType.INTRO, SongSectionType.OUTRO):
                candidate = pick(pool_scenic, "scenic", slot_dur)
            else:
                candidate = pick(pool_dynamic, "dynamic", slot_dur)
        else:
            candidate = pick(scored_scenes, "default", slot_dur)

        selected.append(candidate)

    return selected


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
    """(duration_sec, video_fps) of a media file via cached ffprobe; zeros when unknown."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return 0.0, 0.0
    return _probe_media_cached(str(p.resolve()), st.st_size, st.st_mtime_ns)


@lru_cache(maxsize=1024)
def _probe_media_cached(path_str: str, size: int, mtime_ns: int) -> tuple[float, float]:
    data = ffprobe_json(Path(path_str))
    if not data:
        return 0.0, 0.0
    rate = 0.0
    duration = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    for st in data.get("streams", []):
        if st.get("codec_type") == "video":
            r = str(st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/1")
            try:
                n, d = (r.split("/", 1) + ["1"])[:2]
                rate = float(n) / float(d) if float(d) else 0.0
            except ValueError:
                rate = 0.0
            break
    return duration, rate


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
        if not scored_scenes:
            raise ValueError("Cannot build timeline with empty scored_scenes list.")
        if len(cut_timestamps) < 2:
            raise ValueError("cut_timestamps must have at least 2 points (start and end).")

        fps = self.config.fps
        timeline = otio.schema.Timeline(name=timeline_name)
        video_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
        audio_track = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)

        timeline.tracks.append(video_track)
        timeline.tracks.append(audio_track)

        should_teaser = (
            include_teaser if include_teaser is not None else self.config.enable_teaser
        )
        total_time = cut_timestamps[-1] - cut_timestamps[0]
        has_teaser = should_teaser and (total_time >= self.config.teaser_duration + 4.0)

        # 1. Teaser hook generation from top-scoring scenes
        if has_teaser:
            teaser_dur = self.config.teaser_duration
            num_teaser = min(5, max(3, len(scored_scenes)))
            sub_dur = teaser_dur / num_teaser
            for j in range(num_teaser):
                candidate = scored_scenes[j % len(scored_scenes)]
                sc = candidate.scene
                frames_out = max(1, int(round(min(sub_dur, max(1.0 / fps, sc.duration)) * fps)))
                media_ref = _media_reference(sc.clip_path, sc.end_time, fps)
                media_rate = media_ref.available_range.duration.rate
                clip = otio.schema.Clip(
                    name=f"TEASER_{sc.clip_path.stem}_{j:02d}",
                    media_reference=media_ref,
                    source_range=_source_range(sc.start_time, frames_out, fps, media_rate),
                )
                autoedit_meta: dict[str, object] = {"speed": candidate.speed}
                if candidate.crop_window is not None:
                    cw = candidate.crop_window
                    autoedit_meta.update({
                        "crop_x": cw.x,
                        "crop_y": cw.y,
                        "crop_width": cw.width,
                        "crop_height": cw.height,
                        "tracking_subject": cw.tracking_subject,
                    })
                clip.metadata["AutoEdit"] = autoedit_meta
                if candidate.speed != 1.0:
                    clip.effects.append(otio.schema.LinearTimeWarp(time_scalar=candidate.speed))
                video_track.append(clip)

            main_cuts = [teaser_dur] + [t for t in cut_timestamps if t > teaser_dur]
        else:
            main_cuts = cut_timestamps

        # 2. Main beat-matched sequence assembly with structural energy alignment
        main_scenes = select_scenes_for_cuts(
            scored_scenes=scored_scenes,
            cut_timestamps=main_cuts,
            sections=sections,
            action_drop_threshold=self.config.action_drop_threshold,
        )
        for i in range(len(main_cuts) - 1):
            slot_start = main_cuts[i]
            slot_end = main_cuts[i + 1]

            candidate = main_scenes[i]
            sc = candidate.scene
            # Frame-exact slot length; the clip fills what the source allows and any
            # remainder becomes a Gap so later cuts stay on the beat.
            slot_frames = int(round(slot_end * fps)) - int(round(slot_start * fps))
            speed = max(0.1, float(candidate.speed or 1.0))
            src_frames = int(round(sc.duration / speed * fps))
            clip_frames = max(1, min(slot_frames, src_frames)) if slot_frames > 0 else 0
            pad_frames = max(0, slot_frames - clip_frames)

            media_ref = _media_reference(sc.clip_path, sc.end_time, fps)
            media_rate = media_ref.available_range.duration.rate
            clip = otio.schema.Clip(
                name=f"{sc.clip_path.stem}_beat_{i:03d}",
                media_reference=media_ref,
                source_range=_source_range(sc.start_time, clip_frames, fps, media_rate),
            )
            autoedit_meta: dict[str, object] = {"speed": candidate.speed}
            if candidate.crop_window is not None:
                cw = candidate.crop_window
                autoedit_meta.update({
                    "crop_x": cw.x,
                    "crop_y": cw.y,
                    "crop_width": cw.width,
                    "crop_height": cw.height,
                    "tracking_subject": cw.tracking_subject,
                })

            # Ambient Motion (Ken Burns)
            if getattr(self.config, "enable_ken_burns", False):
                ambient_motion = assign_ambient_motion(
                    candidate,
                    clip_index=i,
                    motion_threshold=getattr(self.config, "ken_burns_motion_threshold", 0.20),
                )
                if ambient_motion:
                    autoedit_meta["ken_burns"] = ambient_motion
                    candidate.metadata["ken_burns"] = ambient_motion
                    scale_f = getattr(self.config, "ken_burns_scale_factor", 1.08)
                    clip.effects.append(
                        otio.schema.Effect(
                            effect_name="KenBurns",
                            metadata={"motion": ambient_motion, "scale_factor": scale_f},
                        )
                    )

            clip.metadata["AutoEdit"] = autoedit_meta
            if candidate.speed != 1.0:
                clip.effects.append(otio.schema.LinearTimeWarp(time_scalar=candidate.speed))

            # Exposure Continuity Guard (Insert Cross Dissolve on Exposure Gap)
            if i > 0 and getattr(self.config, "enable_exposure_guard", False):
                prev_candidate = main_scenes[i - 1]
                _, is_gap = evaluate_cut_continuity(
                    prev_candidate,
                    candidate,
                    threshold=getattr(self.config, "exposure_guard_threshold", 0.45),
                )
                if is_gap:
                    trans_frames = getattr(self.config, "cross_dissolve_frames", 10)
                    half_in = trans_frames // 2
                    half_out = trans_frames - half_in
                    trans = otio.schema.Transition(
                        name="Cross Dissolve",
                        transition_type=otio.schema.TransitionTypes.SMPTE_Dissolve,
                        in_offset=RationalTime(half_in, fps),
                        out_offset=RationalTime(half_out, fps),
                    )
                    video_track.append(trans)

            video_track.append(clip)
            if pad_frames > 0:
                video_track.append(otio.schema.Gap(duration=RationalTime(pad_frames, fps)))

        # Total timeline duration
        total_timeline_sec = cut_timestamps[-1] - cut_timestamps[0]
        total_audio_frames = int(round(total_timeline_sec * fps))

        # Build audio track for the background soundtrack
        audio_ref = otio.schema.ExternalReference(
            target_url=soundtrack_path.resolve().as_uri(),
            available_range=TimeRange(
                start_time=RationalTime(0, fps),
                duration=RationalTime(total_audio_frames, fps),
            ),
        )
        audio_clip = otio.schema.Clip(
            name=soundtrack_path.stem,
            media_reference=audio_ref,
            source_range=TimeRange(
                start_time=RationalTime(0, fps),
                duration=RationalTime(total_audio_frames, fps),
            ),
        )
        audio_track.append(audio_clip)

        logger.info(
            "Constructed OTIO timeline with %d video clips across %.2f seconds.",
            len(video_track),
            total_timeline_sec,
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
    ) -> TimelineExportResult:
        """Export timeline to DaVinci Resolve FCPXML, Premiere XML, and OTIO formats."""
        output_dir.mkdir(parents=True, exist_ok=True)

        fcpxml_path = output_dir / f"{base_name}.fcpxml"
        premiere_xml_path = output_dir / f"{base_name}_premiere.xml"
        otio_path = output_dir / f"{base_name}.otio"

        # 1. Native OTIO export
        try:
            otio.adapters.write_to_file(timeline, str(otio_path))
            logger.info("Exported OTIO timeline: %s", otio_path.name)
        except Exception as exc:
            logger.warning("Failed to export native OTIO (%s)", exc)

        # 2. Premiere Pro XML (using FCP 7 XML adapter)
        exported_premiere = None
        try:
            otio.adapters.write_to_file(timeline, str(premiere_xml_path), adapter_name="fcp_xml")
            logger.info("Exported Premiere Pro XML: %s", premiere_xml_path.name)
            exported_premiere = premiere_xml_path
        except Exception as exc:
            logger.warning("FCP 7 XML adapter export for Premiere failed (%s)", exc)

        # 3. DaVinci Resolve FCPXML (or FCP 7 XML fallback)
        exported_resolve = None
        try:
            # Try fcpx_xml adapter if available
            otio.adapters.write_to_file(timeline, str(fcpxml_path), adapter_name="fcpx_xml")
            logger.info("Exported DaVinci Resolve FCPXML: %s", fcpxml_path.name)
            exported_resolve = fcpxml_path
        except Exception:
            # Fallback to fcp_xml adapter for DaVinci Resolve
            try:
                otio.adapters.write_to_file(timeline, str(fcpxml_path), adapter_name="fcp_xml")
                logger.info("Exported DaVinci Resolve compatible XML: %s", fcpxml_path.name)
                exported_resolve = fcpxml_path
            except Exception as inner_exc:
                logger.warning("DaVinci Resolve FCPXML export failed (%s)", inner_exc)

        # 4. CapCut Desktop Native Draft Export (Phase 4)
        exported_capcut = None
        if self.config.enable_capcut_export:
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
            otio_path=otio_path if otio_path.is_file() else None,
            capcut_draft_path=exported_capcut,
        )
