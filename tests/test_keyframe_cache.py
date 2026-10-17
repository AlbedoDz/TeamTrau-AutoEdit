"""Shared keyframe cache: one open per clip, sorted sweep, downscale, correct frames."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from src.core.config import PipelineConfig
from src.core.types import SceneCut
from src.modules import keyframe_cache as kc


def _make_clip(path: Path, frames: int = 90, size: tuple[int, int] = (1280, 720)) -> Path:
    w, h = size
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (w, h))
    for i in range(frames):
        img = np.full((h, w, 3), int(i * 255 / max(1, frames - 1)), dtype=np.uint8)
        out.write(img)
    out.release()
    return path


@pytest.fixture
def clips(tmp_path):
    return _make_clip(tmp_path / "a.mp4"), _make_clip(tmp_path / "b.mp4", frames=60)


def test_one_capture_per_clip_and_values(clips, monkeypatch):
    a, b = clips
    opened: list[str] = []
    real = cv2.VideoCapture

    def spy(path, *args):
        opened.append(str(path))
        return real(path, *args)

    monkeypatch.setattr(kc.cv2, "VideoCapture", spy)
    req = kc.FrameRequests()
    # Unsorted and duplicated on purpose.
    req.add_many(a, [2.0, 0.5, 1.0, 1.0], kc.PRIO_SCORER)
    req.add_many(b, [1.5, 0.2], kc.PRIO_BOUNDARY)
    store = kc.extract_keyframes(req, PipelineConfig(), workers=2)

    assert sorted(opened) == sorted([str(a), str(b)])
    assert set(store) == {(a, 0.5), (a, 1.0), (a, 2.0), (b, 0.2), (b, 1.5)}
    assert store.source_size(a) == (1280, 720)
    for frame in store.values():
        assert frame.shape[1] <= kc.MAX_FRAME_WIDTH
    # Brightness ramps with time: 0.5 s < 1 s < 2 s (within codec tolerance).
    m = {t: float(store[(a, t)].mean()) for t in (0.5, 1.0, 2.0)}
    assert m[0.5] < m[1.0] < m[2.0]
    assert m[1.0] == pytest.approx(30 * 255 / 89, abs=8)


def test_matches_direct_seek(clips):
    a, _ = clips
    store = kc.extract_keyframes({a: {1.2: 0, 0.4: 0}}, PipelineConfig())
    cap = cv2.VideoCapture(str(a))
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(1.2 * 30)))
    _, direct = cap.read()
    cap.release()
    small = cv2.resize(direct, (960, 540), interpolation=cv2.INTER_AREA)
    assert np.abs(store[(a, 1.2)].astype(int) - small.astype(int)).mean() < 3


def test_budget_drops_low_priority(clips):
    a, _ = clips
    per_frame = kc.MAX_FRAME_WIDTH * (kc.MAX_FRAME_WIDTH * 9 // 16) * 3
    store = kc.extract_keyframes(
        {a: {0.3: kc.PRIO_REFRAME, 1.0: kc.PRIO_SCORER, 2.0: kc.PRIO_REFRAME}},
        PipelineConfig(),
        budget_bytes=per_frame,  # room for a single frame
    )
    assert list(store) == [(a, 1.0)]


def test_missing_clip_is_empty(tmp_path):
    store = kc.extract_keyframes({tmp_path / "nope.mp4": {1.0: 0}}, PipelineConfig())
    assert len(store) == 0


def test_build_requests_covers_all_consumers(tmp_path):
    cfg = PipelineConfig(keyframes_per_scene=3, reframer_sample_frames=5)
    sc = SceneCut(tmp_path / "x.mp4", 2.0, 6.0, 60, 180, 4.0)
    req = kc.build_requests([sc], cfg, reframer=True, vlm=True)
    stamps = set(req.as_mapping()[tmp_path / "x.mp4"])
    assert {3.0, 4.0, 5.0} <= stamps  # scorer keyframes
    assert 4.0 in stamps  # VLM mid frame shared with scorer's middle keyframe
    assert 2.0 in stamps and 5.96 in stamps  # boundary frames
    assert len(stamps) == len({3.0, 4.0, 5.0, 2.0, 5.96, 3.0, 6.0})  # + reframer 2,3,4,5,6


def test_boundary_frames_attached_and_used_by_continuity(clips, monkeypatch):
    from src.core.types import ScoredScene
    from src.modules import visual_flow

    a, b = clips
    cfg = PipelineConfig()
    s1 = ScoredScene(SceneCut(a, 0.0, 1.0, 0, 30, 1.0), 1, 1, 1)
    s2 = ScoredScene(SceneCut(b, 0.5, 1.5, 15, 45, 1.0), 1, 1, 1)
    store = kc.extract_keyframes(kc.build_requests([s1.scene, s2.scene], cfg, scorer=False), cfg)
    assert kc.attach_boundary_frames([s1, s2], store) == 2
    assert s1.metadata["_boundary_frame_end"] is not None

    def boom(*args, **kwargs):
        raise AssertionError("clip re-opened for a boundary frame")

    monkeypatch.setattr(visual_flow, "extract_frame_at_timestamp", boom)
    dist, _gap = visual_flow.evaluate_cut_continuity(s1, s2)
    assert 0.0 <= dist <= 1.0


def test_vlm_keyframes_are_index_keyed(clips):
    from src.core.types import ScoredScene

    a, _ = clips
    cfg = PipelineConfig()
    sc = ScoredScene(SceneCut(a, 0.5, 2.5, 15, 75, 2.0), 1, 1, 1)
    store = kc.extract_keyframes(kc.build_requests([sc.scene], cfg, scorer=False, boundaries=False, vlm=True), cfg)
    frames = kc.vlm_keyframes([sc], store)
    assert list(frames) == [0] and frames[0].shape[1] <= kc.MAX_FRAME_WIDTH
