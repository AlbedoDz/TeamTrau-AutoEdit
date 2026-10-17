"""Unit tests for the Smart Pre-Recommendation Engine."""

from unittest.mock import patch

from src.core.config import PipelineConfig
from src.core.ontology import CameraRole
from src.modules.genre_recommender import GenreRecommender


def test_genre_recommender_dsp_high_bpm_action():
    """Verify high BPM with action cam footage classifies into extreme sports."""
    recommender = GenreRecommender()
    rec = recommender.recommend(
        bpm=138.0,
        duration=60.0,
        section_count=4,
        camera_roles=[CameraRole.ACTION_CAM, CameraRole.CAM_A],
        is_vertical=False,
    )
    assert rec.subgenre_key == "sports_extreme"
    assert rec.major_genre == "Action & Sports"
    assert rec.confidence >= 0.90
    assert "sports_extreme" in rec.preset.key


def test_genre_recommender_dsp_vertical_fast():
    """Verify high BPM with vertical preset classifies into social_tiktok_fast."""
    recommender = GenreRecommender()
    rec = recommender.recommend(
        bpm=132.0,
        duration=30.0,
        section_count=2,
        camera_roles=[CameraRole.CAM_A],
        is_vertical=True,
    )
    assert rec.subgenre_key == "social_tiktok_fast"
    assert rec.major_genre == "Commercial & Social"
    assert rec.confidence >= 0.90


def test_genre_recommender_dsp_slow_drone():
    """Verify slow BPM with drone aerials classifies into travel_nature."""
    recommender = GenreRecommender()
    rec = recommender.recommend(
        bpm=72.0,
        duration=90.0,
        section_count=3,
        camera_roles=[CameraRole.DRONE, CameraRole.B_ROLL],
        is_vertical=False,
    )
    assert rec.subgenre_key == "travel_nature"
    assert rec.major_genre == "Travel & Lifestyle"


def test_genre_recommender_mock_llm_success():
    """Verify LLM query when enabled and valid JSON is parsed."""
    config = PipelineConfig(enable_vlm_tagging=True)
    recommender = GenreRecommender(config)

    mock_json = '{"recommended_genre": "commercial_fashion", "confidence": 0.95, "reasoning": "High chic beat rhythm."}'
    with patch("src.modules.vlm_assistant.VLMAssistant.query_text", return_value=mock_json):
        rec = recommender.recommend(
            bpm=120.0,
            duration=45.0,
            camera_roles=[CameraRole.CAM_A],
            use_llm=True,
        )
        assert rec.subgenre_key == "commercial_fashion"
        assert rec.confidence == 0.95
        assert rec.reasoning == "High chic beat rhythm."
