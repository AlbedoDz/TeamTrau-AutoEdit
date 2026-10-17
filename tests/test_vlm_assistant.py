"""Unit tests for VLMAssistant module and heuristic fallback analysis."""

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene
from src.modules.vlm_assistant import VLMAssistant


def test_vlm_heuristic_high_motion():
    """Verify heuristic analysis correctly classifies high motion footage."""
    cfg = PipelineConfig(enable_vlm_tagging=True, vlm_provider="heuristic")
    vlm = VLMAssistant(cfg)

    analysis = vlm.analyze_scene(
        frame=None,
        scene_index=0,
        motion_energy=0.85,
        sharpness_score=0.75,
        duration=3.0,
    )

    assert analysis.scene_index == 0
    assert analysis.mood == "energetic"
    assert analysis.suggested_pacing == "fast"
    assert "action" in analysis.tags
    assert "dynamic" in analysis.tags
    assert "crisp_focus" in analysis.tags
    assert analysis.is_key_moment is True
    assert analysis.confidence == 0.60


def test_vlm_heuristic_scenic_calm():
    """Verify heuristic analysis identifies calm scenic slow-paced footage."""
    cfg = PipelineConfig(enable_vlm_tagging=True, vlm_provider="heuristic")
    vlm = VLMAssistant(cfg)

    analysis = vlm.analyze_scene(
        frame=None,
        scene_index=1,
        motion_energy=0.15,
        sharpness_score=0.45,
        duration=2.5,
    )

    assert analysis.scene_index == 1
    assert analysis.mood == "calm"
    assert analysis.suggested_pacing == "slow"
    assert "scenic" in analysis.tags
    assert "ambient" in analysis.tags
    assert analysis.is_key_moment is False


def test_vlm_encode_image_b64():
    """Verify base64 image encoding and downscaling for large frames."""
    cfg = PipelineConfig(enable_vlm_tagging=True, vlm_provider="heuristic")
    vlm = VLMAssistant(cfg)

    # 4K frame (3840x2160)
    frame = np.full((2160, 3840, 3), 120, dtype=np.uint8)
    b64_str = vlm.encode_image_b64(frame)

    assert isinstance(b64_str, str)
    assert len(b64_str) > 0


def test_vlm_tag_scored_scenes():
    """Verify batch scene tagging enriches metadata and composite scores."""
    cfg = PipelineConfig(enable_vlm_tagging=True, vlm_provider="heuristic")
    vlm = VLMAssistant(cfg)

    s1 = SceneCut(
        clip_path=Path("clip1.mp4"),
        start_time=0.0,
        end_time=2.0,
        start_frame=0,
        end_frame=60,
        duration=2.0,
    )
    s2 = SceneCut(
        clip_path=Path("clip1.mp4"),
        start_time=2.0,
        end_time=5.0,
        start_frame=60,
        end_frame=150,
        duration=3.0,
    )

    scored = [
        ScoredScene(
            scene=s1,
            sharpness_score=0.80,
            aesthetic_score=0.70,
            composite_score=0.75,
            motion_energy=0.80,
        ),
        ScoredScene(
            scene=s2,
            sharpness_score=0.40,
            aesthetic_score=0.50,
            composite_score=0.45,
            motion_energy=0.20,
        ),
    ]

    tagged = vlm.tag_scored_scenes(scored)

    assert len(tagged) == 2
    assert tagged[0].vlm_analysis is not None
    assert "vlm_tags" in tagged[0].metadata
    assert tagged[0].metadata["vlm_mood"] == "energetic"
    # Key moment bonus applied
    assert tagged[0].composite_score > 0.75

    assert tagged[1].vlm_analysis is not None
    assert tagged[1].metadata["vlm_mood"] == "calm"


def test_vlm_mock_openai_client_success():
    """Verify successful parsing of OpenAI vision response."""
    cfg = PipelineConfig(enable_vlm_tagging=True, vlm_provider="heuristic")
    vlm = VLMAssistant(cfg)

    # Mock client
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = (
        '{"description": "Bride walking down the aisle in sunlight.", '
        '"tags": ["wedding", "ceremony", "bride", "sunlight"], '
        '"mood": "joyful", "suggested_pacing": "medium", "is_key_moment": true}'
    )
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create.return_value = mock_response

    vlm.client = mock_client

    frame = np.full((100, 100, 3), 200, dtype=np.uint8)
    analysis = vlm.analyze_scene(frame=frame, scene_index=3)

    assert analysis.scene_index == 3
    assert analysis.description == "Bride walking down the aisle in sunlight."
    assert "wedding" in analysis.tags
    assert analysis.mood == "joyful"
    assert analysis.is_key_moment is True
    assert analysis.confidence == 0.95


def test_vlm_mock_openai_client_api_error_fallback():
    """Verify graceful fallback to heuristic when OpenAI REST API throws an error."""
    cfg = PipelineConfig(enable_vlm_tagging=True, vlm_provider="heuristic")
    vlm = VLMAssistant(cfg)

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("Connection timeout")
    vlm.client = mock_client

    frame = np.full((100, 100, 3), 50, dtype=np.uint8)
    # Should not raise exception
    analysis = vlm.analyze_scene(
        frame=frame,
        scene_index=4,
        motion_energy=0.25,
        sharpness_score=0.40,
    )

    assert analysis.scene_index == 4
    assert analysis.mood == "calm"
    assert "low_key" in analysis.tags
    assert analysis.confidence == 0.60
