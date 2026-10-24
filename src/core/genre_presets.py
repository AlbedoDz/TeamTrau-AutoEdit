"""Multi-genre preset matrix defining 4 major genres and 23 sub-genres.

Controls pacing, cut frequency, speed-ramping, beat snapping, transitions, and camera-role priorities.
"""

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Final

from src.core.cadence import PROFILE_MULTIPLES
from src.core.config import PRESETS, PipelineConfig
from src.core.types import CadenceProfile


class MajorGenre(str, Enum):
    """Four primary commercial video production genres."""

    ACTION_SPORTS = "Action & Sports"
    TRAVEL_LIFESTYLE = "Travel & Lifestyle"
    COMMERCIAL_SOCIAL = "Commercial & Social"
    DOCUMENTARY_NARRATIVE = "Documentary & Narrative"


@dataclass(frozen=True)
class SubGenrePreset:
    """Parametric editing configuration for a specialized video sub-genre."""

    key: str
    major_genre: MajorGenre
    display_name: str
    description: str
    min_scene_len_sec: float
    min_beat_interval: float
    standard_min_beat_interval: float
    climax_min_beat_interval: float
    enable_speed_ramping: bool
    max_speed_multiplier: float
    min_speed_multiplier: float
    enable_teaser: bool
    teaser_duration: float
    enable_exposure_guard: bool
    cross_dissolve_frames: int
    enable_ken_burns: bool
    ken_burns_scale_factor: float
    preferred_render_preset: str
    reframing_mode: str
    action_drop_threshold: float
    preferred_camera_roles: list[str] = field(default_factory=list)
    # Rhythmic cadence (Phase 23): see _CADENCE_BY_GROUP below for how presets map to profiles.
    cadence_profile: str = CadenceProfile.BALANCED_NARRATIVE.value
    target_bar_multiples: tuple[float, ...] = PROFILE_MULTIPLES[CadenceProfile.BALANCED_NARRATIVE]
    allow_extended_shots: bool = True
    golden_window_sec: tuple[float, float] = (3.0, 8.0)

    def apply(self, config: PipelineConfig) -> PipelineConfig:
        """Mutate a PipelineConfig with this preset's operational parameters."""
        config.min_scene_len_sec = self.min_scene_len_sec
        config.min_beat_interval = self.min_beat_interval
        config.standard_min_beat_interval = self.standard_min_beat_interval
        config.climax_min_beat_interval = self.climax_min_beat_interval
        config.enable_speed_ramping = self.enable_speed_ramping
        config.max_speed_multiplier = self.max_speed_multiplier
        config.min_speed_multiplier = self.min_speed_multiplier
        config.enable_teaser = self.enable_teaser
        config.teaser_duration = self.teaser_duration
        config.enable_exposure_guard = self.enable_exposure_guard
        config.cross_dissolve_frames = self.cross_dissolve_frames
        config.enable_ken_burns = self.enable_ken_burns
        config.ken_burns_scale_factor = self.ken_burns_scale_factor
        config.reframing_mode = self.reframing_mode
        config.action_drop_threshold = self.action_drop_threshold
        config.cadence_profile = self.cadence_profile
        config.cadence_multiples = list(self.target_bar_multiples)
        config.golden_window_min_sec = self.golden_window_sec[0]
        config.golden_window_max_sec = self.golden_window_sec[1]

        # Update resolution preset if valid
        if self.preferred_render_preset in PRESETS:
            preset = PRESETS[self.preferred_render_preset]
            config.preset_name = preset.name
            config.canvas_width = preset.width
            config.canvas_height = preset.height
            config.fps = preset.fps

        if self.key == "rough_cut_assembly":
            config.apply_workflow_mode("rough_cut")

        return config


# Cadence mapping (slot lengths in bars, see src/core/cadence.py):
#
#   profile              multiples   sub-genres
#   -------------------  ----------  ---------------------------------------------------------
#   lingering_cinematic  2, 4, 8     travel_cinematic, travel_luxury, travel_nature,
#                                    commercial_realestate, doc_interview, narrative_shortfilm,
#                                    wedding_highlight
#   balanced_narrative   1, 2, 4     travel_roadtrip, travel_daily_vlog, travel_food,
#                                    commercial_product, commercial_brand_promo,
#                                    commercial_tech_review, doc_bts, rough_cut_assembly
#   fast_syncopated      0.5, 1, 2   sports_extreme, sports_racing, sports_combat,
#                                    sports_outdoor, sports_fpv_drone, sports_gaming
#   viral_burst          0.5, 1      social_tiktok_fast, social_reels_aesthetic,
#                                    commercial_fashion
_CADENCE_BY_KEY: Final[dict[str, CadenceProfile]] = {
    **dict.fromkeys(
        (
            "travel_cinematic",
            "travel_luxury",
            "travel_nature",
            "commercial_realestate",
            "doc_interview",
            "narrative_shortfilm",
            "wedding_highlight",
        ),
        CadenceProfile.LINGERING_CINEMATIC,
    ),
    **dict.fromkeys(
        (
            "travel_roadtrip",
            "travel_daily_vlog",
            "travel_food",
            "commercial_product",
            "commercial_brand_promo",
            "commercial_tech_review",
            "doc_bts",
            "rough_cut_assembly",
        ),
        CadenceProfile.BALANCED_NARRATIVE,
    ),
    **dict.fromkeys(
        (
            "sports_extreme",
            "sports_racing",
            "sports_combat",
            "sports_outdoor",
            "sports_fpv_drone",
            "sports_gaming",
        ),
        CadenceProfile.FAST_SYNCOPATED,
    ),
    **dict.fromkeys(
        ("social_tiktok_fast", "social_reels_aesthetic", "commercial_fashion"),
        CadenceProfile.VIRAL_BURST,
    ),
}


def _with_cadence(presets: dict[str, SubGenrePreset]) -> dict[str, SubGenrePreset]:
    """Fill cadence_profile / target_bar_multiples from _CADENCE_BY_KEY (KeyError = unmapped)."""
    return {
        key: replace(
            p,
            cadence_profile=_CADENCE_BY_KEY[key].value,
            target_bar_multiples=PROFILE_MULTIPLES[_CADENCE_BY_KEY[key]],
        )
        for key, p in presets.items()
    }


_CINEMATIC_OVERRIDE_KEYS: Final[frozenset[str]] = frozenset({
    "wedding_highlight",
    "travel_cinematic",
    "travel_luxury",
    "narrative_shortfilm",
    "social_reels_aesthetic",
    "commercial_realestate",
})


def _with_golden_windows(presets: dict[str, SubGenrePreset]) -> dict[str, SubGenrePreset]:
    """Fill golden_window_sec from major genre with cinematic overrides."""
    result: dict[str, SubGenrePreset] = {}
    for key, p in presets.items():
        if key in _CINEMATIC_OVERRIDE_KEYS:
            gw = (4.0, 8.0)
        elif p.major_genre == MajorGenre.DOCUMENTARY_NARRATIVE or key == "rough_cut_assembly":
            gw = (3.0, 8.0)
        elif p.major_genre in (
            MajorGenre.ACTION_SPORTS,
            MajorGenre.TRAVEL_LIFESTYLE,
            MajorGenre.COMMERCIAL_SOCIAL,
        ):
            gw = (2.5, 4.5)
        else:
            gw = (3.0, 8.0)
        result[key] = replace(p, golden_window_sec=gw)
    return result


GENRE_PRESETS: Final[dict[str, SubGenrePreset]] = _with_golden_windows(_with_cadence({
    # -------------------------------------------------------------
    # 1. Action & Sports (6 Sub-genres)
    # -------------------------------------------------------------
    "sports_extreme": SubGenrePreset(
        key="sports_extreme",
        major_genre=MajorGenre.ACTION_SPORTS,
        display_name="Extreme Sports (High-Octane)",
        description="Ultra-fast micro-cuts, aggressive speed-ramps on drops, downbeat snaps.",
        min_scene_len_sec=0.35,
        min_beat_interval=0.30,
        standard_min_beat_interval=0.60,
        climax_min_beat_interval=0.30,
        enable_speed_ramping=True,
        max_speed_multiplier=4.0,
        min_speed_multiplier=0.5,
        enable_teaser=True,
        teaser_duration=5.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=6,
        enable_ken_burns=False,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="1080p60",
        reframing_mode="smart_crop",
        action_drop_threshold=0.65,
        preferred_camera_roles=["action_cam", "drone", "cam_a"],
    ),
    "sports_racing": SubGenrePreset(
        key="sports_racing",
        major_genre=MajorGenre.ACTION_SPORTS,
        display_name="Motorsports & Racing",
        description="Fast kinetic pacing with dynamic whip acceleration ramps and high frame rates.",
        min_scene_len_sec=0.5,
        min_beat_interval=0.40,
        standard_min_beat_interval=0.80,
        climax_min_beat_interval=0.40,
        enable_speed_ramping=True,
        max_speed_multiplier=3.0,
        min_speed_multiplier=0.75,
        enable_teaser=True,
        teaser_duration=6.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=8,
        enable_ken_burns=False,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="1080p60",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.70,
        preferred_camera_roles=["action_cam", "b_roll", "drone"],
    ),
    "sports_combat": SubGenrePreset(
        key="sports_combat",
        major_genre=MajorGenre.ACTION_SPORTS,
        display_name="Combat Sports & Martial Arts",
        description="Punch-synchronized impact cuts, climax slow-motion, and close-angle emphasis.",
        min_scene_len_sec=0.40,
        min_beat_interval=0.35,
        standard_min_beat_interval=0.70,
        climax_min_beat_interval=0.35,
        enable_speed_ramping=True,
        max_speed_multiplier=2.5,
        min_speed_multiplier=0.4,
        enable_teaser=True,
        teaser_duration=4.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=4,
        enable_ken_burns=False,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="1080p60",
        reframing_mode="smart_crop",
        action_drop_threshold=0.60,
        preferred_camera_roles=["cam_a", "cam_b", "action_cam"],
    ),
    "sports_outdoor": SubGenrePreset(
        key="sports_outdoor",
        major_genre=MajorGenre.ACTION_SPORTS,
        display_name="Outdoor & Adventure Athletics",
        description="Balanced rhythm between sweeping aerial drone vistas and active motion tracks.",
        min_scene_len_sec=1.0,
        min_beat_interval=0.60,
        standard_min_beat_interval=1.20,
        climax_min_beat_interval=0.60,
        enable_speed_ramping=True,
        max_speed_multiplier=2.0,
        min_speed_multiplier=1.0,
        enable_teaser=True,
        teaser_duration=6.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=12,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.08,
        preferred_render_preset="4k30",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.75,
        preferred_camera_roles=["drone", "action_cam", "b_roll"],
    ),
    "sports_fpv_drone": SubGenrePreset(
        key="sports_fpv_drone",
        major_genre=MajorGenre.ACTION_SPORTS,
        display_name="FPV Drone Freestyle",
        description="Fluid continuous kinetic flight lines with smooth musical drop synchronization.",
        min_scene_len_sec=0.8,
        min_beat_interval=0.50,
        standard_min_beat_interval=1.00,
        climax_min_beat_interval=0.50,
        enable_speed_ramping=True,
        max_speed_multiplier=3.0,
        min_speed_multiplier=0.6,
        enable_teaser=True,
        teaser_duration=5.0,
        enable_exposure_guard=False,
        cross_dissolve_frames=6,
        enable_ken_burns=False,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="4k60",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.70,
        preferred_camera_roles=["drone", "action_cam"],
    ),
    "sports_gaming": SubGenrePreset(
        key="sports_gaming",
        major_genre=MajorGenre.ACTION_SPORTS,
        display_name="Esports & Gaming Highlights",
        description="Highlight burst kills, facecam reaction integration, and rapid hype pacing.",
        min_scene_len_sec=0.45,
        min_beat_interval=0.35,
        standard_min_beat_interval=0.70,
        climax_min_beat_interval=0.35,
        enable_speed_ramping=True,
        max_speed_multiplier=2.0,
        min_speed_multiplier=0.5,
        enable_teaser=True,
        teaser_duration=4.5,
        enable_exposure_guard=False,
        cross_dissolve_frames=6,
        enable_ken_burns=False,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="1080p60",
        reframing_mode="smart_crop",
        action_drop_threshold=0.60,
        preferred_camera_roles=["cam_a", "cam_b"],
    ),

    # -------------------------------------------------------------
    # 2. Travel & Lifestyle (6 Sub-genres)
    # -------------------------------------------------------------
    "travel_cinematic": SubGenrePreset(
        key="travel_cinematic",
        major_genre=MajorGenre.TRAVEL_LIFESTYLE,
        display_name="Cinematic Travel Film",
        description="Lush 2.5s-5.0s takes, ambient Ken Burns motion, 24fps filmic cadence.",
        min_scene_len_sec=2.2,
        min_beat_interval=0.80,
        standard_min_beat_interval=2.00,
        climax_min_beat_interval=0.80,
        enable_speed_ramping=False,
        max_speed_multiplier=1.0,
        min_speed_multiplier=1.0,
        enable_teaser=True,
        teaser_duration=6.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=16,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.08,
        preferred_render_preset="4k24",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.80,
        preferred_camera_roles=["drone", "b_roll", "cam_a"],
    ),
    "travel_luxury": SubGenrePreset(
        key="travel_luxury",
        major_genre=MajorGenre.TRAVEL_LIFESTYLE,
        display_name="Luxury Resort & Hospitality",
        description="Slow, elegant movements, pristine aesthetic priority, soft musical dissolves.",
        min_scene_len_sec=3.0,
        min_beat_interval=1.00,
        standard_min_beat_interval=2.50,
        climax_min_beat_interval=1.00,
        enable_speed_ramping=False,
        max_speed_multiplier=1.0,
        min_speed_multiplier=1.0,
        enable_teaser=False,
        teaser_duration=0.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=20,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.06,
        preferred_render_preset="4k24",
        reframing_mode="blur_backdrop",
        action_drop_threshold=0.85,
        preferred_camera_roles=["b_roll", "drone", "cam_a"],
    ),
    "travel_roadtrip": SubGenrePreset(
        key="travel_roadtrip",
        major_genre=MajorGenre.TRAVEL_LIFESTYLE,
        display_name="Roadtrip & Montage",
        description="Rhythmic, upbeat cut sequencing with highway timelapses and landscape reveals.",
        min_scene_len_sec=1.2,
        min_beat_interval=0.50,
        standard_min_beat_interval=1.20,
        climax_min_beat_interval=0.50,
        enable_speed_ramping=True,
        max_speed_multiplier=3.0,
        min_speed_multiplier=1.0,
        enable_teaser=True,
        teaser_duration=5.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=10,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.07,
        preferred_render_preset="1080p30",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.70,
        preferred_camera_roles=["cam_a", "drone", "action_cam"],
    ),
    "travel_daily_vlog": SubGenrePreset(
        key="travel_daily_vlog",
        major_genre=MajorGenre.TRAVEL_LIFESTYLE,
        display_name="Daily Travel Vlog",
        description="Host speech-driven timing with rapid B-roll cutaway inserts and ambient sound.",
        min_scene_len_sec=1.5,
        min_beat_interval=0.60,
        standard_min_beat_interval=1.50,
        climax_min_beat_interval=0.60,
        enable_speed_ramping=False,
        max_speed_multiplier=1.0,
        min_speed_multiplier=1.0,
        enable_teaser=True,
        teaser_duration=4.5,
        enable_exposure_guard=True,
        cross_dissolve_frames=8,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="1080p30",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.75,
        preferred_camera_roles=["cam_a", "b_roll", "action_cam"],
    ),
    "travel_food": SubGenrePreset(
        key="travel_food",
        major_genre=MajorGenre.TRAVEL_LIFESTYLE,
        display_name="Culinary & Street Food",
        description="Sensual close-ups, subtle sizzle slow-motion, warm exposure transitions.",
        min_scene_len_sec=1.8,
        min_beat_interval=0.70,
        standard_min_beat_interval=1.80,
        climax_min_beat_interval=0.70,
        enable_speed_ramping=True,
        max_speed_multiplier=1.5,
        min_speed_multiplier=0.75,
        enable_teaser=True,
        teaser_duration=5.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=12,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.06,
        preferred_render_preset="4k30",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.80,
        preferred_camera_roles=["b_roll", "cam_a", "cam_b"],
    ),
    "travel_nature": SubGenrePreset(
        key="travel_nature",
        major_genre=MajorGenre.TRAVEL_LIFESTYLE,
        display_name="Nature & Wilderness",
        description="Contemplative long takes, panoramic establishing aerials, serene dissolves.",
        min_scene_len_sec=3.5,
        min_beat_interval=1.20,
        standard_min_beat_interval=3.00,
        climax_min_beat_interval=1.20,
        enable_speed_ramping=False,
        max_speed_multiplier=1.0,
        min_speed_multiplier=1.0,
        enable_teaser=False,
        teaser_duration=0.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=24,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.08,
        preferred_render_preset="4k24",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.85,
        preferred_camera_roles=["drone", "b_roll"],
    ),

    # -------------------------------------------------------------
    # 3. Commercial & Social (7 Sub-genres)
    # -------------------------------------------------------------
    "social_tiktok_fast": SubGenrePreset(
        key="social_tiktok_fast",
        major_genre=MajorGenre.COMMERCIAL_SOCIAL,
        display_name="TikTok / Reels Viral Fast-Paced",
        description="Rapid 0.3s-0.8s micro-cuts, 9:16 vertical crop, explosive 5-clip teaser hook.",
        min_scene_len_sec=0.35,
        min_beat_interval=0.35,
        standard_min_beat_interval=0.60,
        climax_min_beat_interval=0.35,
        enable_speed_ramping=True,
        max_speed_multiplier=3.0,
        min_speed_multiplier=0.6,
        enable_teaser=True,
        teaser_duration=4.0,
        enable_exposure_guard=False,
        cross_dissolve_frames=4,
        enable_ken_burns=False,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="social_vertical",
        reframing_mode="smart_crop",
        action_drop_threshold=0.65,
        preferred_camera_roles=["cam_a", "action_cam", "b_roll"],
    ),
    "social_reels_aesthetic": SubGenrePreset(
        key="social_reels_aesthetic",
        major_genre=MajorGenre.COMMERCIAL_SOCIAL,
        display_name="Instagram Reels Aesthetic Mood",
        description="Vibrant color-graded lifestyle cuts synchronized with chill beat drops.",
        min_scene_len_sec=0.8,
        min_beat_interval=0.50,
        standard_min_beat_interval=1.00,
        climax_min_beat_interval=0.50,
        enable_speed_ramping=True,
        max_speed_multiplier=2.0,
        min_speed_multiplier=0.75,
        enable_teaser=True,
        teaser_duration=4.5,
        enable_exposure_guard=True,
        cross_dissolve_frames=8,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.06,
        preferred_render_preset="social_vertical",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.70,
        preferred_camera_roles=["b_roll", "cam_a", "drone"],
    ),
    "commercial_product": SubGenrePreset(
        key="commercial_product",
        major_genre=MajorGenre.COMMERCIAL_SOCIAL,
        display_name="Product Commercial & Promo",
        description="Punchy product macro detail focus with dynamic zoom ramps and stop-motion bursts.",
        min_scene_len_sec=1.0,
        min_beat_interval=0.50,
        standard_min_beat_interval=1.20,
        climax_min_beat_interval=0.50,
        enable_speed_ramping=True,
        max_speed_multiplier=2.5,
        min_speed_multiplier=0.5,
        enable_teaser=True,
        teaser_duration=5.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=8,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.08,
        preferred_render_preset="4k30",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.75,
        preferred_camera_roles=["b_roll", "cam_a"],
    ),
    "commercial_realestate": SubGenrePreset(
        key="commercial_realestate",
        major_genre=MajorGenre.COMMERCIAL_SOCIAL,
        display_name="Real Estate & Architecture",
        description="Smooth floating gimbal sweeps, architectural wide lenses, and slow dissolves.",
        min_scene_len_sec=2.2,
        min_beat_interval=0.90,
        standard_min_beat_interval=2.20,
        climax_min_beat_interval=0.90,
        enable_speed_ramping=False,
        max_speed_multiplier=1.0,
        min_speed_multiplier=1.0,
        enable_teaser=False,
        teaser_duration=0.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=16,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.06,
        preferred_render_preset="4k30",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.80,
        preferred_camera_roles=["drone", "b_roll", "cam_a"],
    ),
    "commercial_fashion": SubGenrePreset(
        key="commercial_fashion",
        major_genre=MajorGenre.COMMERCIAL_SOCIAL,
        display_name="Fashion & Lookbook",
        description="Rhythm chic editorial pacing, quick visual cuts on downbeats, high contrast.",
        min_scene_len_sec=0.7,
        min_beat_interval=0.45,
        standard_min_beat_interval=0.90,
        climax_min_beat_interval=0.45,
        enable_speed_ramping=True,
        max_speed_multiplier=2.0,
        min_speed_multiplier=0.5,
        enable_teaser=True,
        teaser_duration=4.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=6,
        enable_ken_burns=False,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="social_vertical",
        reframing_mode="smart_crop",
        action_drop_threshold=0.70,
        preferred_camera_roles=["cam_a", "cam_b", "b_roll"],
    ),
    "commercial_brand_promo": SubGenrePreset(
        key="commercial_brand_promo",
        major_genre=MajorGenre.COMMERCIAL_SOCIAL,
        display_name="Brand Manifesto & Corporate Promo",
        description="Inspiring narrative build from slow atmospheric beginnings to energetic climax.",
        min_scene_len_sec=1.2,
        min_beat_interval=0.60,
        standard_min_beat_interval=1.50,
        climax_min_beat_interval=0.50,
        enable_speed_ramping=True,
        max_speed_multiplier=2.0,
        min_speed_multiplier=1.0,
        enable_teaser=True,
        teaser_duration=6.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=12,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.07,
        preferred_render_preset="1080p30",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.75,
        preferred_camera_roles=["cam_a", "b_roll", "drone"],
    ),
    "commercial_tech_review": SubGenrePreset(
        key="commercial_tech_review",
        major_genre=MajorGenre.COMMERCIAL_SOCIAL,
        display_name="Tech Gadget Review",
        description="A-Roll talking head with precision macro B-roll cutaway overlays and speech ducking.",
        min_scene_len_sec=2.0,
        min_beat_interval=0.80,
        standard_min_beat_interval=2.00,
        climax_min_beat_interval=0.80,
        enable_speed_ramping=False,
        max_speed_multiplier=1.0,
        min_speed_multiplier=1.0,
        enable_teaser=True,
        teaser_duration=5.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=8,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="1080p30",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.80,
        preferred_camera_roles=["cam_a", "b_roll", "cam_b"],
    ),

    # -------------------------------------------------------------
    # 4. Documentary & Narrative (4 Sub-genres)
    # -------------------------------------------------------------
    "doc_bts": SubGenrePreset(
        key="doc_bts",
        major_genre=MajorGenre.DOCUMENTARY_NARRATIVE,
        display_name="Behind the Scenes (BTS)",
        description="Authentic documentary pacing, organic handheld camera motion, spontaneous cuts.",
        min_scene_len_sec=1.8,
        min_beat_interval=0.75,
        standard_min_beat_interval=1.80,
        climax_min_beat_interval=0.75,
        enable_speed_ramping=False,
        max_speed_multiplier=1.0,
        min_speed_multiplier=1.0,
        enable_teaser=True,
        teaser_duration=5.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=10,
        enable_ken_burns=False,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="1080p24",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.75,
        preferred_camera_roles=["cam_b", "cam_a", "b_roll"],
    ),
    "doc_interview": SubGenrePreset(
        key="doc_interview",
        major_genre=MajorGenre.DOCUMENTARY_NARRATIVE,
        display_name="Interview & Dialogue Podcast",
        description="Audio-driven multi-cam switcher between Cam A (Host) and Cam B (Guest) with J-cuts.",
        min_scene_len_sec=2.5,
        min_beat_interval=1.00,
        standard_min_beat_interval=2.50,
        climax_min_beat_interval=1.00,
        enable_speed_ramping=False,
        max_speed_multiplier=1.0,
        min_speed_multiplier=1.0,
        enable_teaser=False,
        teaser_duration=0.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=6,
        enable_ken_burns=False,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="1080p30",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.85,
        preferred_camera_roles=["cam_a", "cam_b", "b_roll"],
    ),
    "narrative_shortfilm": SubGenrePreset(
        key="narrative_shortfilm",
        major_genre=MajorGenre.DOCUMENTARY_NARRATIVE,
        display_name="Narrative Cinematic Drama",
        description="Story-centric scene flow with strict Bhattacharyya continuity protection and 24fps.",
        min_scene_len_sec=2.0,
        min_beat_interval=0.90,
        standard_min_beat_interval=2.20,
        climax_min_beat_interval=0.90,
        enable_speed_ramping=False,
        max_speed_multiplier=1.0,
        min_speed_multiplier=1.0,
        enable_teaser=False,
        teaser_duration=0.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=14,
        enable_ken_burns=False,
        ken_burns_scale_factor=1.05,
        preferred_render_preset="cinema_4k",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.80,
        preferred_camera_roles=["cam_a", "cam_b", "b_roll"],
    ),
    "wedding_highlight": SubGenrePreset(
        key="wedding_highlight",
        major_genre=MajorGenre.DOCUMENTARY_NARRATIVE,
        display_name="Wedding & Milestone Highlight",
        description="Romantic emotional crescendos, elegant 0.5x slow-motion vows, and golden-hour B-roll.",
        min_scene_len_sec=1.8,
        min_beat_interval=0.70,
        standard_min_beat_interval=1.80,
        climax_min_beat_interval=0.70,
        enable_speed_ramping=True,
        max_speed_multiplier=1.0,
        min_speed_multiplier=0.5,
        enable_teaser=True,
        teaser_duration=6.0,
        enable_exposure_guard=True,
        cross_dissolve_frames=18,
        enable_ken_burns=True,
        ken_burns_scale_factor=1.06,
        preferred_render_preset="4k24",
        reframing_mode="auto_hybrid",
        action_drop_threshold=0.80,
        preferred_camera_roles=["b_roll", "cam_a", "drone"],
    ),
}))

ROUGH_CUT_PRESET: Final[SubGenrePreset] = _with_golden_windows(
    _with_cadence(
    {"rough_cut_assembly": SubGenrePreset(
    key="rough_cut_assembly",
    major_genre=MajorGenre.DOCUMENTARY_NARRATIVE,
    display_name="Rough Cut Stringout & Defect Pruning",
    description="Strict chronological stringout. Auto-excises wobbles, wild shakes, out-of-focus hunting, and occlusions.",
    min_scene_len_sec=1.5,
    min_beat_interval=1.0,
    standard_min_beat_interval=1.5,
    climax_min_beat_interval=1.0,
    enable_speed_ramping=False,
    max_speed_multiplier=1.0,
    min_speed_multiplier=1.0,
    enable_teaser=False,
    teaser_duration=0.0,
    enable_exposure_guard=False,
    cross_dissolve_frames=0,
    enable_ken_burns=False,
    ken_burns_scale_factor=1.0,
    preferred_render_preset="1080p30",
    reframing_mode="auto_hybrid",
    action_drop_threshold=0.70,
    preferred_camera_roles=["cam_a", "cam_b", "drone", "action_cam", "b_roll"],
)}
))["rough_cut_assembly"]


def get_preset_by_key(key: str) -> SubGenrePreset:
    """Retrieve sub-genre preset by key, defaulting to 'travel_cinematic' if not found."""
    if key == "rough_cut_assembly":
        return ROUGH_CUT_PRESET
    return GENRE_PRESETS.get(key, GENRE_PRESETS["travel_cinematic"])


def list_presets_by_major_genre() -> dict[str, list[SubGenrePreset]]:
    """Group all 23 sub-genre presets by their parent MajorGenre."""
    grouped: dict[str, list[SubGenrePreset]] = {g.value: [] for g in MajorGenre}
    for preset in GENRE_PRESETS.values():
        grouped[preset.major_genre.value].append(preset)
    return grouped


def get_all_preset_keys() -> list[str]:
    """Return an ordered list of all 23 sub-genre preset keys."""
    return list(GENRE_PRESETS.keys())
