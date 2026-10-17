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



def _scorer_no_gpu(cfg):
    s = VisualScorer(cfg)
    s._torch_model = None  # deterministic OpenCV path
    return s


def test_deduplicate_matches_reference_hamming():
    import random

    from src.core.types import ScoredScene
    from src.modules.visual_scorer import hamming_distance

    rng = random.Random(1)
    cfg = PipelineConfig(duplicate_hash_threshold=6)
    scorer = _scorer_no_gpu(cfg)
    scenes = []
    for i in range(60):
        h = f"{rng.getrandbits(64):016x}"
        sc = ScoredScene(SceneCut(Path(f"c{i}.mp4"), 0, 1, 0, 30, 1.0), 1.0, 1.0, 1.0 - i / 100)
        sc.metadata["dhash"] = h
        scenes.append(sc)
    # near-duplicates of the first few
    for i in range(5):
        base = int(scenes[i].metadata["dhash"], 16) ^ 5
        sc = ScoredScene(SceneCut(Path(f"d{i}.mp4"), 0, 1, 0, 30, 1.0), 1.0, 1.0, 0.1)
        sc.metadata["dhash"] = f"{base:016x}"
        scenes.append(sc)

    expected = []
    for cand in scenes:
        if not any(hamming_distance(cand.metadata["dhash"], k.metadata["dhash"]) <= 6 for k in expected):
            expected.append(cand)
    assert scorer.deduplicate_scenes(scenes) == expected


def test_batch_aesthetic_matches_single_on_cpu_path():
    scorer = _scorer_no_gpu(PipelineConfig())
    imgs = [np.random.randint(0, 255, (90, 160, 3), dtype=np.uint8) for _ in range(5)]
    assert scorer._compute_aesthetic_batch(imgs) == [scorer._compute_aesthetic_opencv(i) for i in imgs]
    assert scorer._compute_aesthetic_batch([]) == []


def test_scores_come_from_keyframe_store_and_disk_cache(tmp_path):
    from src.modules import keyframe_cache as kc

    clip = tmp_path / "clip.mp4"
    out = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (640, 360))
    rng = np.random.default_rng(0)
    for _ in range(90):
        out.write(rng.integers(0, 255, (360, 640, 3), dtype=np.uint8))
    out.release()

    cfg = PipelineConfig(cache_dir=tmp_path / "cache", enable_deduplication=False)
    scene = SceneCut(clip, 0.0, 3.0, 0, 90, 3.0)
    store = kc.extract_keyframes(kc.build_requests([scene], cfg), cfg)

    first = _scorer_no_gpu(cfg).score_and_rank_scenes([scene], keyframes=store)
    assert first[0].metadata["keyframes_sampled"] == cfg.keyframes_per_scene

    # Second pass: results must come from disk, so neither frames nor decoding are touched.
    second_scorer = _scorer_no_gpu(cfg)

    def boom(*a, **k):
        raise AssertionError("frames were read despite a cache hit")

    second_scorer._extract_keyframes = boom  # type: ignore[method-assign]
    second = second_scorer.score_and_rank_scenes([scene], keyframes=kc.KeyframeStore())
    assert second[0].sharpness_score == first[0].sharpness_score
    assert second[0].metadata["dhash"] == first[0].metadata["dhash"]

    # enable_cache=False must bypass the stored entry.
    nocache = _scorer_no_gpu(cfg.model_copy(update={"enable_cache": False}))
    nocache._extract_keyframes = lambda *a, **k: []  # type: ignore[method-assign]
    res = nocache.score_and_rank_scenes([scene], keyframes=kc.KeyframeStore())
    assert res[0].sharpness_score == 0.0
