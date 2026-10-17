"""Unit tests for DurationPacker module and Knapsack dialogue/B-roll allocation."""

from pathlib import Path

import pytest

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene, SentenceSegment
from src.modules.duration_packer import DurationPacker


@pytest.fixture
def sample_sentences():
    """Create sample sentences with varying lengths and importance scores."""
    return [
        SentenceSegment(
            text="Introductory sentence.",
            start_time=0.0,
            end_time=3.0,
            duration=3.0,
            importance_score=0.7,
        ),
        SentenceSegment(
            text="Crucial key statement with high value.",
            start_time=5.0,
            end_time=9.0,
            duration=4.0,
            importance_score=0.95,
        ),
        SentenceSegment(
            text="Filler sentence with lower importance.",
            start_time=12.0,
            end_time=17.0,
            duration=5.0,
            importance_score=0.3,
        ),
        SentenceSegment(
            text="Concluding takeaway message.",
            start_time=20.0,
            end_time=24.0,
            duration=4.0,
            importance_score=0.85,
        ),
    ]


@pytest.fixture
def sample_scenes():
    """Create sample visual B-roll scenes."""
    return [
        ScoredScene(
            scene=SceneCut(Path("e:/footage/broll1.mp4"), 0.0, 4.0, 0, 120, 4.0),
            sharpness_score=90.0,
            aesthetic_score=0.85,
            composite_score=0.88,
        ),
        ScoredScene(
            scene=SceneCut(Path("e:/footage/broll2.mp4"), 0.0, 5.0, 0, 150, 5.0),
            sharpness_score=95.0,
            aesthetic_score=0.90,
            composite_score=0.92,
        ),
        ScoredScene(
            scene=SceneCut(Path("e:/footage/broll3.mp4"), 0.0, 6.0, 0, 180, 6.0),
            sharpness_score=80.0,
            aesthetic_score=0.75,
            composite_score=0.78,
        ),
    ]


def test_pack_timeline_without_sentences(sample_scenes):
    """Verify packer fills duration completely with visual B-roll when dialogue is absent."""
    cfg = PipelineConfig()
    packer = DurationPacker(cfg)

    target_dur = 10.0
    res = packer.pack_timeline(
        sentences=[],
        scored_scenes=sample_scenes,
        target_duration=target_dur,
    )

    assert len(res.selected_sentences) == 0
    assert len(res.selected_scenes) >= 2
    assert res.speech_ratio == 0.0
    assert res.b_roll_ratio == 1.0
    assert abs(res.total_duration - target_dur) <= 5.0


def test_pack_timeline_with_dialogue_knapsack(sample_sentences, sample_scenes):
    """Verify knapsack selects high-importance sentences and fits within target duration."""
    cfg = PipelineConfig(target_speech_ratio=0.60)
    packer = DurationPacker(cfg)

    # Target 15 seconds: ~9s dialogue, ~6s B-roll
    target_dur = 15.0
    res = packer.pack_timeline(
        sentences=sample_sentences,
        scored_scenes=sample_scenes,
        target_duration=target_dur,
        speech_ratio=0.60,
    )

    assert len(res.selected_sentences) >= 2
    # The highest value sentence (importance 0.95) must be included
    texts = [s.text for s in res.selected_sentences]
    assert "Crucial key statement with high value." in texts

    # Low value filler (importance 0.3) should be dropped
    assert "Filler sentence with lower importance." not in texts

    # Verify chronological sequence is maintained among selected sentences
    for i in range(len(res.selected_sentences) - 1):
        assert res.selected_sentences[i].start_time < res.selected_sentences[i + 1].start_time

    # B-roll scenes must be present
    assert len(res.selected_scenes) >= 1
    # Total duration should be close to target 15s (+- 4s)
    assert abs(res.total_duration - target_dur) <= 4.0


def test_pack_timeline_validates_positive_duration():
    """Verify ValueError is raised when target duration is non-positive."""
    cfg = PipelineConfig()
    packer = DurationPacker(cfg)

    with pytest.raises(ValueError, match="target_duration must be positive"):
        packer.pack_timeline([], [], target_duration=0.0)
