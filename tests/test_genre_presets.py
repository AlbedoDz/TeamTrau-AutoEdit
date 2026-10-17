"""Unit tests for the 4 Major Genres & 23 Sub-genres Preset Ontology Matrix."""

from src.core.config import PipelineConfig
from src.core.genre_presets import (
    GENRE_PRESETS,
    MajorGenre,
    get_all_preset_keys,
    get_preset_by_key,
    list_presets_by_major_genre,
)


def test_genre_presets_count_and_keys():
    """Verify exactly 23 sub-genres are registered across 4 major genres."""
    keys = get_all_preset_keys()
    assert len(keys) == 23
    assert len(GENRE_PRESETS) == 23

    grouped = list_presets_by_major_genre()
    assert len(grouped) == 4
    assert len(grouped[MajorGenre.ACTION_SPORTS.value]) == 6
    assert len(grouped[MajorGenre.TRAVEL_LIFESTYLE.value]) == 6
    assert len(grouped[MajorGenre.COMMERCIAL_SOCIAL.value]) == 7
    assert len(grouped[MajorGenre.DOCUMENTARY_NARRATIVE.value]) == 4


def test_get_preset_by_key():
    """Verify individual preset lookup and fallback handling."""
    preset = get_preset_by_key("sports_extreme")
    assert preset.key == "sports_extreme"
    assert preset.major_genre == MajorGenre.ACTION_SPORTS
    assert preset.preferred_render_preset == "1080p60"

    fallback = get_preset_by_key("non_existent_key")
    assert fallback.key == "travel_cinematic"


def test_apply_subgenre_preset_to_config():
    """Verify applying a sub-genre preset correctly mutates PipelineConfig parameters."""
    config = PipelineConfig()
    preset = get_preset_by_key("social_tiktok_fast")

    updated = preset.apply(config)
    assert updated.min_scene_len_sec == 0.35
    assert updated.preset_name == "social_vertical"
    assert updated.canvas_width == 1080
    assert updated.canvas_height == 1920
    assert updated.enable_speed_ramping is True
    assert updated.reframing_mode == "smart_crop"
    assert updated.enable_teaser is True
