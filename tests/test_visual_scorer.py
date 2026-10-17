"""Unit tests for visual scorer, sharpness evaluation, and composite ranking."""

from pathlib import Path

import cv2
import numpy as np

from src.core.config import PipelineConfig
from src.core.types import SceneCut
from src.modules.visual_scorer import VisualScorer


def test_sharpness_differentiates_sharp_vs_blurred():
    """Verify that high-frequency sharp textures score higher than smooth/blurred images."""
    cfg = PipelineConfig()
    scorer = VisualScorer(cfg)

    # 1. Sharp high-frequency checkerboard pattern
    sharp_img = np.zeros((200, 200, 3), dtype=np.uint8)
    sharp_img[::20, :, :] = 255
    sharp_img[:, ::20, :] = 255

    # 2. Blurred low-frequency image
    blurred_img = cv2.GaussianBlur(sharp_img, (31, 31), 0)

    score_sharp = scorer._compute_sharpness(sharp_img)
    score_blurred = scorer._compute_sharpness(blurred_img)

    assert score_sharp > score_blurred * 2.0


def test_aesthetic_score_produces_valid_float():
    """Ensure aesthetic calculation produces non-negative numerical output."""
    cfg = PipelineConfig()
    scorer = VisualScorer(cfg)

    test_img = np.random.randint(0, 255, (200, 200, 3), dtype=np.uint8)
    score = scorer._compute_aesthetic(test_img)

    assert isinstance(score, float)
    assert score >= 0.0


def test_score_and_rank_scenes_descending_order(monkeypatch):
    """Verify that scenes are sorted descending by composite_score."""
    cfg = PipelineConfig()
    scorer = VisualScorer(cfg)

    # Mock _extract_keyframes to return mock images with distinct horizontal patterns
    sharp_img = np.zeros((100, 100, 3), dtype=np.uint8)
    sharp_img[::5, :50] = 255
    blur_img = np.full((100, 100, 3), 128, dtype=np.uint8)
    blur_img[:, 50:] = 240

    def mock_extract(video_path, start_sec, end_sec, count):
        if "sharp" in str(video_path):
            return [sharp_img, sharp_img]
        return [blur_img, blur_img]

    monkeypatch.setattr(scorer, "_extract_keyframes", mock_extract)

    scenes = [
        SceneCut(Path("blur.mp4"), 0.0, 2.0, 0, 60, 2.0),
        SceneCut(Path("sharp.mp4"), 0.0, 2.0, 0, 60, 2.0),
    ]

    ranked = scorer.score_and_rank_scenes(scenes)
    assert len(ranked) == 2
    assert ranked[0].scene.clip_path == Path("sharp.mp4")
    assert ranked[0].composite_score >= ranked[1].composite_score


def test_compute_dhash_and_hamming_distance():
    """Verify perceptual dHash calculation and Hamming distance behavior."""
    from src.modules.visual_scorer import compute_dhash, hamming_distance

    img1 = np.zeros((100, 100, 3), dtype=np.uint8)
    img1[:, 50:] = 255  # Left dark, right bright

    img2 = img1.copy()
    img3 = np.zeros((100, 100, 3), dtype=np.uint8)
    img3[50:, :] = 255  # Top dark, bottom bright

    h1 = compute_dhash(img1)
    h2 = compute_dhash(img2)
    h3 = compute_dhash(img3)

    assert isinstance(h1, str)
    assert len(h1) == 16
    assert hamming_distance(h1, h2) == 0
    assert hamming_distance(h1, h3) > 0


def test_deduplicate_scenes():
    """Verify deduplication removes scenes with similar perceptual hashes."""
    from src.core.types import ScoredScene

    cfg = PipelineConfig(enable_deduplication=True, duplicate_hash_threshold=6)
    scorer = VisualScorer(cfg)

    # Identical hash for scene1 and scene2, distinct hash for scene3
    s1 = ScoredScene(
        scene=SceneCut(Path("clip1.mp4"), 0.0, 3.0, 0, 90, 3.0),
        sharpness_score=100.0,
        aesthetic_score=10.0,
        composite_score=0.95,
        metadata={"dhash": "0123456789abcdef"},
    )
    s2 = ScoredScene(
        scene=SceneCut(Path("clip1_dup.mp4"), 3.0, 6.0, 90, 180, 3.0),
        sharpness_score=80.0,
        aesthetic_score=8.0,
        composite_score=0.75,
        metadata={"dhash": "0123456789abcdef"},  # Exactly same hash (dist=0 <= 6)
    )
    s3 = ScoredScene(
        scene=SceneCut(Path("clip2_diff.mp4"), 0.0, 3.0, 0, 90, 3.0),
        sharpness_score=90.0,
        aesthetic_score=9.0,
        composite_score=0.85,
        metadata={"dhash": "fedcba9876543210"},  # Very different hash
    )

    deduped = scorer.deduplicate_scenes([s1, s2, s3])
    assert len(deduped) == 2
    assert s1 in deduped
    assert s2 not in deduped
    assert s3 in deduped

