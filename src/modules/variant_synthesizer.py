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

from src.core.config import PRESETS, PipelineConfig
from src.core.types import (
    BeatMarker,
    ScoredScene,
    SongSection,
    TimelineVariant,
)
from src.modules.timeline_builder import TimelineBuilder

logger = logging.getLogger(__name__)


class MultiVariantTimelineSynthesizer:
    """Orchestrates synthesis of multiple creative narrative timeline variations."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def synthesize_variants(
        self,
        scored_scenes: list[ScoredScene],
        beats: list[BeatMarker],
        soundtrack_path: Path,
        song_sections: list[SongSection] | None = None,
        variant_count: int = 3,
        output_dir: Path | None = None,
    ) -> dict[str, TimelineVariant]:
        """Synthesize 1, 2, or 3 distinct timeline cuts and return mapped by identifier."""
        if not scored_scenes:
            raise ValueError("Cannot synthesize timeline variants with empty scored_scenes.")

        variant_count = max(1, min(3, variant_count))
        variants: dict[str, TimelineVariant] = {}

        # -------------------------------------------------------------
        # 1. Variant 1: Beat-Driven / Energetic
        # -------------------------------------------------------------
        var_beat = self._build_beat_driven_variant(
            scored_scenes, beats, soundtrack_path, song_sections
        )
        variants["beat_driven"] = var_beat

        if variant_count >= 2:
            # ---------------------------------------------------------
            # 2. Variant 2: Cinematic / Story-Driven
            # ---------------------------------------------------------
            var_cine = self._build_cinematic_variant(
                scored_scenes, beats, soundtrack_path, song_sections
            )
            variants["cinematic"] = var_cine

        if variant_count >= 3:
            # ---------------------------------------------------------
            # 3. Variant 3: Social Viral Teaser (30s - 45s)
            # ---------------------------------------------------------
            var_social = self._build_social_teaser_variant(
                scored_scenes, beats, soundtrack_path, song_sections
            )
            variants["social_teaser"] = var_social

        # Optional: export NLE interchanges for all generated variants if output_dir provided
        if output_dir:
            self._export_variant_files(variants, output_dir)

        return variants

    def _build_beat_driven_variant(
        self,
        scored_scenes: list[ScoredScene],
        beats: list[BeatMarker],
        soundtrack_path: Path,
        song_sections: list[SongSection] | None,
    ) -> TimelineVariant:
        """Variant 1: High-energy cuts snapped to downbeats with motion acceleration."""
        cfg = copy.deepcopy(self.config)
        cfg.enable_teaser = True
        cfg.teaser_duration = min(5.0, self.config.teaser_duration)
        cfg.enable_speed_ramping = True
        cfg.enable_ken_burns = False

        builder = TimelineBuilder(cfg)

        # Use all strong beats, filter minimum interval to 0.4s
        cut_ts = self._extract_beat_timestamps(beats, min_interval=0.40)
        if len(cut_ts) < 2:
            cut_ts = [0.0, 30.0]

        # Prioritize high motion energy scenes
        scenes_pool = sorted(scored_scenes, key=lambda s: s.motion_energy, reverse=True)

        timeline = builder.build_timeline(
            scored_scenes=scenes_pool,
            cut_timestamps=cut_ts,
            soundtrack_path=soundtrack_path,
            timeline_name="Variant_1_Beat_Driven",
            include_teaser=True,
            sections=song_sections,
        )

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
            sequence_scenes=scenes_pool[: len(clips)],
        )

    def _build_cinematic_variant(
        self,
        scored_scenes: list[ScoredScene],
        beats: list[BeatMarker],
        soundtrack_path: Path,
        song_sections: list[SongSection] | None,
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

        # Relaxed pacing: snap only to major musical downbeats (interval >= 1.5s)
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

        timeline = builder.build_timeline(
            scored_scenes=scenes_pool,
            cut_timestamps=cut_ts,
            soundtrack_path=soundtrack_path,
            timeline_name="Variant_2_Cinematic",
            include_teaser=False,
            sections=song_sections,
        )

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
            sequence_scenes=scenes_pool[: len(clips)],
        )

    def _build_social_teaser_variant(
        self,
        scored_scenes: list[ScoredScene],
        beats: list[BeatMarker],
        soundtrack_path: Path,
        song_sections: list[SongSection] | None,
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
        all_cuts = self._extract_beat_timestamps(beats, min_interval=0.50)
        teaser_cuts = [t for t in all_cuts if t <= 35.0]
        if len(teaser_cuts) < 4:
            teaser_cuts = [0.0, 4.0, 8.0, 12.0, 18.0, 24.0, 30.0]

        # Prioritize top composite scoring scenes
        scenes_pool = sorted(scored_scenes, key=lambda s: s.composite_score, reverse=True)

        timeline = builder.build_timeline(
            scored_scenes=scenes_pool,
            cut_timestamps=teaser_cuts,
            soundtrack_path=soundtrack_path,
            timeline_name="Variant_3_Social_Teaser",
            include_teaser=True,
            sections=song_sections,
        )

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
            sequence_scenes=scenes_pool[: len(clips)],
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
