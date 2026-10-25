"""P3.0e: composite scores are batch-normalised without zeroing a single or uniform batch."""

from pathlib import Path

import pytest

from src.core.types import SceneCut, ScoredScene
from src.modules.visual_scorer import normalize_composites


def _sc(sharp: float, aes: float) -> ScoredScene:
    cut = SceneCut(clip_path=Path("c.mp4"), start_time=0.0, end_time=1.0, start_frame=0,
                   end_frame=30, duration=1.0)
    return ScoredScene(scene=cut, sharpness_score=sharp, aesthetic_score=aes, composite_score=0.0)


def test_single_scene_is_not_zeroed():
    s = _sc(1132.66, 129.01)
    normalize_composites([s], sharpness_weight=0.5, aesthetic_weight=0.5)
    assert s.composite_score == pytest.approx(1.0)


def test_uniform_batch_scores_everyone_top():
    batch = [_sc(10.0, 3.0), _sc(10.0, 3.0)]
    normalize_composites(batch, sharpness_weight=0.6, aesthetic_weight=0.4)
    assert [s.composite_score for s in batch] == [pytest.approx(1.0), pytest.approx(1.0)]


def test_spread_batch_is_min_max_weighted():
    lo, mid, hi = _sc(0.0, 10.0), _sc(5.0, 10.0), _sc(10.0, 10.0)
    normalize_composites([lo, mid, hi], sharpness_weight=0.5, aesthetic_weight=0.5)
    # aesthetic is uniform -> contributes 1.0 * 0.5 to everyone; sharpness spreads 0..1
    assert lo.composite_score == pytest.approx(0.5)
    assert mid.composite_score == pytest.approx(0.75)
    assert hi.composite_score == pytest.approx(1.0)


def test_empty_batch_is_a_no_op():
    normalize_composites([], sharpness_weight=0.5, aesthetic_weight=0.5)
