"""Unit tests for concurrent VLM scene tagging and timeout guards."""

import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from src.core.config import PipelineConfig
from src.core.types import SceneCut, SceneVLMAnalysis, ScoredScene
from src.modules.vlm_assistant import VLMAssistant


@pytest.fixture
def sample_config() -> PipelineConfig:
    return PipelineConfig(vlm_concurrency=4, vlm_timeout_sec=5.0)


def create_dummy_scene(idx: int) -> ScoredScene:
    return ScoredScene(
        scene=SceneCut(
            clip_path=Path(f"clip_{idx}.mp4"),
            start_time=float(idx * 2),
            end_time=float(idx * 2 + 2),
            start_frame=idx * 60,
            end_frame=(idx + 1) * 60,
            duration=2.0,
        ),
        sharpness_score=0.6,
        aesthetic_score=0.7,
        composite_score=0.65,
        motion_energy=0.5,
    )


def test_vlm_tag_scored_scenes_concurrency_and_ordering(sample_config: PipelineConfig) -> None:
    """Test that tag_scored_scenes processes scenes concurrently and preserves order."""
    assistant = VLMAssistant(sample_config)
    scenes = [create_dummy_scene(i) for i in range(8)]
    keyframes = {i: np.zeros((100, 100, 3), dtype=np.uint8) for i in range(8)}

    def fake_analyze(frame, scene_index, motion_energy, sharpness_score, duration):
        # Simulate slight processing latency
        time.sleep(0.02)
        return SceneVLMAnalysis(
            scene_index=scene_index,
            description=f"Analyzed Scene {scene_index}",
            tags=["test", f"tag_{scene_index}"],
            mood="cinematic",
            suggested_pacing="medium",
            is_key_moment=False,
            confidence=0.9,
        )

    with patch.object(assistant, "analyze_scene", side_effect=fake_analyze):
        start_t = time.perf_counter()
        tagged = assistant.tag_scored_scenes(scenes, keyframes=keyframes, concurrency=4)
        elapsed = time.perf_counter() - start_t
        assert elapsed < 5.0

        # 8 items * 0.02s = 0.16s sequential. With concurrency=4, it should take ~0.04-0.08s
        assert len(tagged) == 8
        # Verify strict ordering preserved
        for i, sc in enumerate(tagged):
            assert sc.vlm_analysis is not None
            assert sc.vlm_analysis.scene_index == i
            assert f"tag_{i}" in sc.vlm_analysis.tags


def test_vlm_tag_scored_scenes_worker_exception_fallback(sample_config: PipelineConfig) -> None:
    """Test that when a worker raises an exception, it falls back gracefully without failing others."""
    assistant = VLMAssistant(sample_config)
    scenes = [create_dummy_scene(0), create_dummy_scene(1)]

    def fake_analyze(frame, scene_index, motion_energy, sharpness_score, duration):
        if scene_index == 1:
            raise RuntimeError("API Rate Limit 429")
        return SceneVLMAnalysis(
            scene_index=scene_index,
            description=f"Success Scene {scene_index}",
            tags=["ok"],
            mood="energetic",
            suggested_pacing="fast",
            is_key_moment=True,
            confidence=0.95,
        )

    with patch.object(assistant, "analyze_scene", side_effect=fake_analyze):
        tagged = assistant.tag_scored_scenes(scenes, concurrency=2)
        assert len(tagged) == 2
        # Scene 0 succeeded
        assert tagged[0].vlm_analysis is not None
        assert "ok" in tagged[0].vlm_analysis.tags

        # Scene 1 gracefully fell back to heuristic tags
        assert tagged[1].vlm_analysis is not None
        assert tagged[1].vlm_analysis.confidence == 0.60  # heuristic confidence
