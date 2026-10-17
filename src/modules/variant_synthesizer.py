"""Multi-Variant Timeline Synthesis Engine.

Generates 1 to 3 distinct narrative cuts from the same set of scored scenes and soundtrack:
- Variant 1: Beat-Driven (High-Octane, rapid downbeat cuts, speed-ramping)
- Variant 2: Cinematic (Story-Driven, aesthetic priority, ambient Ken Burns, smooth dissolves)
- Variant 3: Social Teaser (30s-45s compact viral cut, 5-clip retention hook, vertical crop)
"""

import copy
import logging
from pathlib import Path

import opentimelineio as otio

from src.core.cadence import MIN_GRID_CONFIDENCE, cadence_cuts_with_stats, profile_multiples
from src.core.config import PRESETS, PipelineConfig
from src.core.types import (
    BeatMarker,
    CadenceProfile,
    EditTimeline,
    MusicalBarGrid,
    ScoredScene,
    SongSection,
    TimelineVariant,
)
from src.modules.edit_timeline import build_edit_timeline
from src.modules.timeline_builder import TimelineBuilder

logger = logging.getLogger(__name__)


def _scenes_for_clips(edit_tl: EditTimeline, pool: list[ScoredScene]) -> list[ScoredScene]:
    """Map timeline clips back to the scored scenes they came from (in timeline order)."""
    by_key = {(s.scene.clip_path.resolve(), round(s.scene.start_time, 3)): s for s in pool}
    out: list[ScoredScene] = []
    for c in edit_tl.clips:
        start = c.scene_start if c.scene_start is not None else c.source_in
        sc = by_key.get((Path(c.source_path).resolve(), round(start, 3)))
        if sc is not None:
            out.append(sc)
    return out


class MultiVariantTimelineSynthesizer:
    """Orchestrates synthesis of multiple creative narrative timeline variations."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.warnings: list[str] = []

    def synthesize_variants(
        self,
        scored_scenes: list[ScoredScene],
        beats: list[BeatMarker],
        soundtrack_path: Path,
        song_sections: list[SongSection] | None = None,
        variant_count: int = 3,
        output_dir: Path | None = None,
        *,
        bar_grid: MusicalBarGrid | None = None,
        cut_timestamps: list[float] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, TimelineVariant]:
        """Synthesize 1, 2, or 3 distinct timeline cuts and return mapped by identifier.

        With a trustworthy ``bar_grid`` each variant follows its own cadence profile
        (beat_driven = fast_syncopated, cinematic = lingering_cinematic, social_teaser =
        viral_burst); otherwise the legacy beat-interval filters run. A variant that fails is
        logged and reported in ``warnings`` (also kept in ``self.warnings``) while the others
        are still produced.
        """
        if not scored_scenes:
            raise ValueError("Cannot synthesize timeline variants with empty scored_scenes.")

        variant_count = max(1, min(3, variant_count))
        variants: dict[str, TimelineVariant] = {}
        self.warnings = warnings if warnings is not None else []
        duration = self._track_duration(beats, cut_timestamps)
        ctx = (bar_grid, duration, song_sections or [])

        builders = [
            ("beat_driven", self._build_beat_driven_variant),
            ("cinematic", self._build_cinematic_variant),
            ("social_teaser", self._build_social_teaser_variant),
        ][:variant_count]
        for key, build in builders:
            try:
                variants[key] = build(scored_scenes, beats, soundtrack_path, song_sections, ctx)
            except Exception as exc:
                msg = f"Variant '{key}' failed: {exc}"
                logger.warning(msg, exc_info=True)
                self.warnings.append(msg)

        # Optional: export NLE interchanges for all generated variants if output_dir provided
        if output_dir:
            self._export_variant_files(variants, output_dir)

        return variants

    @staticmethod
    def _track_duration(beats: list[BeatMarker], cut_timestamps: list[float] | None) -> float | None:
        if cut_timestamps and len(cut_timestamps) >= 2:
            return float(cut_timestamps[-1])
        if beats:
            return float(max(b.timestamp for b in beats))
        return None

    def _cadence_kwargs(
        self,
        cfg: PipelineConfig,
        profile: CadenceProfile,
        ctx: tuple[MusicalBarGrid | None, float | None, list[SongSection]],
        cap: float | None = None,
    ) -> tuple[list[float], dict] | None:
        """``(cut_timestamps, build_edit_timeline kwargs)`` for the cadence path, else ``None``."""
        bar_grid, duration, sections = ctx
        if (
            not cfg.use_cadence_engine
            or bar_grid is None
            or duration is None
            or bar_grid.confidence < MIN_GRID_CONFIDENCE
        ):
            return None
        end = min(duration, cap) if cap else duration
        cfg.cadence_multiples = None  # each variant uses its own profile's multiples
        probe = cadence_cuts_with_stats(
            bar_grid, sections, profile, profile_multiples(profile, cfg), end, cfg.fps, cfg.cadence_seed
        )
        if probe is None:
            return None
        return [0.0, end], {"bar_grid": bar_grid, "profile": profile}

    def _build_beat_driven_variant(
        self,
        scored_scenes: list[ScoredScene],
        beats: list[BeatMarker],
        soundtrack_path: Path,
        song_sections: list[SongSection] | None,
        ctx: tuple[MusicalBarGrid | None, float | None, list[SongSection]] = (None, None, []),
    ) -> TimelineVariant:
        """Variant 1: High-energy cuts snapped to downbeats with motion acceleration."""
        cfg = copy.deepcopy(self.config)
        cfg.enable_teaser = True
        cfg.teaser_duration = min(5.0, self.config.teaser_duration)
        cfg.enable_speed_ramping = True
        cfg.enable_ken_burns = False

        builder = TimelineBuilder(cfg)

        cadence = self._cadence_kwargs(cfg, CadenceProfile.FAST_SYNCOPATED, ctx)
        if cadence is not None:
            cut_ts, extra = cadence
        else:  # legacy fallback: all strong beats, minimum interval 0.4 s
            extra = {}
            cut_ts = self._extract_beat_timestamps(beats, min_interval=0.40)
            if len(cut_ts) < 2:
                cut_ts = [0.0, 30.0]

        # Prioritize high motion energy scenes
        scenes_pool = sorted(scored_scenes, key=lambda s: s.motion_energy, reverse=True)

        edit_tl = build_edit_timeline(
            cfg,
            scenes_pool,
            cut_ts,
            soundtrack_path=soundtrack_path,
            sections=song_sections,
            include_teaser=True,
            name="Variant_1_Beat_Driven",
            variant_key="beat_driven",
            **extra,
        )
        timeline = builder.build_from_edit_timeline(edit_tl)

        clips = self._extract_clips_from_timeline(timeline)
        total_dur = cut_ts[-1] - cut_ts[0]

        return TimelineVariant(
            name="beat_driven",
            display_name="Variant 1: Beat-Driven",
            description="High-energy rhythm synchronization with dynamic downbeat cuts and teaser hook.",
            target_pacing="fast",
            clip_count=len(clips),
            total_duration=total_dur,
            otio_timeline=timeline,
            sequence_scenes=_scenes_for_clips(edit_tl, scenes_pool),
            edit_timeline=edit_tl,
        )

    def _build_cinematic_variant(
        self,
        scored_scenes: list[ScoredScene],
        beats: list[BeatMarker],
        soundtrack_path: Path,
        song_sections: list[SongSection] | None,
        ctx: tuple[MusicalBarGrid | None, float | None, list[SongSection]] = (None, None, []),
    ) -> TimelineVariant:
        """Variant 2: Story-driven cuts prioritizing aesthetics, Ken Burns, and dissolves."""
        cfg = copy.deepcopy(self.config)
        cfg.enable_teaser = False
        cfg.enable_speed_ramping = False
        cfg.enable_ken_burns = True
        cfg.ken_burns_scale_factor = 1.07
        cfg.enable_exposure_guard = True
        cfg.cross_dissolve_frames = 14

        builder = TimelineBuilder(cfg)

        cadence = self._cadence_kwargs(cfg, CadenceProfile.LINGERING_CINEMATIC, ctx)
        if cadence is not None:
            cut_ts, extra = cadence
        else:  # legacy fallback: only major downbeats (interval >= 1.5 s)
            extra = {}
            downbeats = [b for b in beats if b.is_downbeat]
            beats_pool = downbeats if len(downbeats) >= 4 else beats
            cut_ts = self._extract_beat_timestamps(beats_pool, min_interval=1.50)
            if len(cut_ts) < 2:
                cut_ts = [0.0, 45.0]

        # Prioritize aesthetic and sharpness score
        scenes_pool = sorted(
            scored_scenes,
            key=lambda s: (s.aesthetic_score * 0.6 + s.sharpness_score * 0.4),
            reverse=True,
        )

        edit_tl = build_edit_timeline(
            cfg,
            scenes_pool,
            cut_ts,
            soundtrack_path=soundtrack_path,
            sections=song_sections,
            include_teaser=False,
            name="Variant_2_Cinematic",
            variant_key="cinematic",
            **extra,
        )
        timeline = builder.build_from_edit_timeline(edit_tl)

        clips = self._extract_clips_from_timeline(timeline)
        total_dur = cut_ts[-1] - cut_ts[0]

        return TimelineVariant(
            name="cinematic",
            display_name="Variant 2: Cinematic",
            description="Lush, lingering shots with high aesthetic scoring, ambient camera moves, and dissolves.",
            target_pacing="cinematic",
            clip_count=len(clips),
            total_duration=total_dur,
            otio_timeline=timeline,
            sequence_scenes=_scenes_for_clips(edit_tl, scenes_pool),
            edit_timeline=edit_tl,
        )

    def _build_social_teaser_variant(
        self,
        scored_scenes: list[ScoredScene],
        beats: list[BeatMarker],
        soundtrack_path: Path,
        song_sections: list[SongSection] | None,
        ctx: tuple[MusicalBarGrid | None, float | None, list[SongSection]] = (None, None, []),
    ) -> TimelineVariant:
        """Variant 3: Compact 30s-45s vertical viral cut with explosive teaser hook."""
        cfg = copy.deepcopy(self.config)
        cfg.enable_teaser = True
        cfg.teaser_duration = 4.0
        cfg.enable_speed_ramping = True
        cfg.max_speed_multiplier = 3.0
        cfg.enable_smart_reframing = True
        cfg.reframing_mode = "smart_crop"

        # Force 9:16 vertical resolution
        if "social_vertical" in PRESETS:
            preset = PRESETS["social_vertical"]
            cfg.canvas_width = preset.width
            cfg.canvas_height = preset.height
            cfg.preset_name = preset.name

        builder = TimelineBuilder(cfg)

        # Compact duration limit (maximum 35.0 seconds)
        cadence = self._cadence_kwargs(cfg, CadenceProfile.VIRAL_BURST, ctx, cap=35.0)
        if cadence is not None:
            teaser_cuts, extra = cadence
        else:  # legacy fallback: beats at least 0.5 s apart
            extra = {}
            all_cuts = self._extract_beat_timestamps(beats, min_interval=0.50)
            teaser_cuts = [t for t in all_cuts if t <= 35.0]
            if len(teaser_cuts) < 4:
                teaser_cuts = [0.0, 4.0, 8.0, 12.0, 18.0, 24.0, 30.0]

        # Prioritize top composite scoring scenes
        scenes_pool = sorted(scored_scenes, key=lambda s: s.composite_score, reverse=True)

        edit_tl = build_edit_timeline(
            cfg,
            scenes_pool,
            teaser_cuts,
            soundtrack_path=soundtrack_path,
            sections=song_sections,
            include_teaser=True,
            name="Variant_3_Social_Teaser",
            variant_key="social_teaser",
            **extra,
        )
        timeline = builder.build_from_edit_timeline(edit_tl)

        clips = self._extract_clips_from_timeline(timeline)
        total_dur = teaser_cuts[-1] - teaser_cuts[0]

        return TimelineVariant(
            name="social_teaser",
            display_name="Variant 3: Social Teaser",
            description="Punchy 30s-35s vertical social edit with explosive hook and 9:16 smart framing.",
            target_pacing="ultra_fast",
            clip_count=len(clips),
            total_duration=total_dur,
            otio_timeline=timeline,
            sequence_scenes=_scenes_for_clips(edit_tl, scenes_pool),
            edit_timeline=edit_tl,
        )

    def _extract_beat_timestamps(
        self,
        beats: list[BeatMarker],
        min_interval: float = 0.50,
    ) -> list[float]:
        """Filter beats ensuring minimum spacing and return sorted timestamps starting at 0.0."""
        if not beats:
            return [0.0, 30.0]

        cuts = [0.0]
        last_t = 0.0
        for b in sorted(beats, key=lambda x: x.timestamp):
            if b.timestamp - last_t >= min_interval:
                cuts.append(round(b.timestamp, 3))
                last_t = b.timestamp

        if len(cuts) == 1:
            cuts.append(round(cuts[0] + 30.0, 3))

        return cuts

    def _extract_clips_from_timeline(self, timeline: otio.schema.Timeline) -> list[otio.schema.Clip]:
        """Extract all video clips from track V1."""
        clips: list[otio.schema.Clip] = []
        for track in timeline.tracks:
            if track.kind == otio.schema.TrackKind.Video:
                for item in track:
                    if isinstance(item, otio.schema.Clip):
                        clips.append(item)
        return clips

    def _export_variant_files(
        self,
        variants: dict[str, TimelineVariant],
        output_dir: Path,
    ) -> None:
        """Export FCPXML, Premiere XML, CapCut draft, and OTIO files for each synthesized variant."""
        output_dir.mkdir(parents=True, exist_ok=True)
        builder = TimelineBuilder(self.config)

        for key, var in variants.items():
            if not var.otio_timeline:
                continue

            var_dir = output_dir / f"variant_{key}"
            result = builder.export_all(var.otio_timeline, var_dir, base_name=f"sequence_{key}")
            var.fcpxml_path = result.fcpxml_path
            var.premiere_xml_path = result.premiere_xml_path
            var.otio_path = result.otio_path
            var.capcut_draft_path = result.capcut_draft_path
