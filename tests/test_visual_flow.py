"""Unit tests for Visual Continuity, Exposure Guard, and Ken Burns Ambient Motion."""

from pathlib import Path

import numpy as np
import pytest

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene
from src.modules.visual_flow import (
    VisualFlowAnalyzer,
    assign_ambient_motion,
    build_zoompan_filter,
    compute_luminance_histogram,
    evaluate_cut_continuity,
)


def make_test_scene(
    clip_id: str,
    start: float = 0.0,
    end: float = 3.0,
    motion_energy: float = 0.5,
) -> ScoredScene:
    """Helper to construct dummy ScoredScene."""
    cut = SceneCut(
        clip_path=Path(f"test_{clip_id}.mp4"),
        start_time=start,
        end_time=end,
        start_frame=int(start * 30),
        end_frame=int(end * 30),
        duration=end - start,
    )
    return ScoredScene(
        scene=cut,
        sharpness_score=0.8,
        aesthetic_score=0.8,
        composite_score=0.8,
        motion_energy=motion_energy,
    )


def test_compute_luminance_histogram():
    """Verify luminance histogram returns normalized 64-bin array."""
    # Create simple solid color frame
    frame = np.full((100, 100, 3), 128, dtype=np.uint8)
    hist = compute_luminance_histogram(frame, bins=64)
    assert hist.shape == (64, 1)
    assert pytest.approx(float(np.sum(hist)), abs=1e-4) == 1.0

    with pytest.raises(ValueError):
        compute_luminance_histogram(None)  # type: ignore


def test_evaluate_cut_continuity_identical_vs_exposure_leap():
    """Verify Bhattacharyya distance correctly flags exposure gaps exceeding 0.45."""
    s1 = make_test_scene("clip1")
    s2 = make_test_scene("clip2")

    # 1. Identical frames: distance should be 0.0
    frame_identical = np.full((120, 120, 3), 128, dtype=np.uint8)
    dist, is_gap = evaluate_cut_continuity(
        s1, s2, threshold=0.45,
        frame_a_override=frame_identical,
        frame_b_override=frame_identical,
    )
    assert dist == pytest.approx(0.0, abs=1e-4)
    assert is_gap is False

    # 2. Exposure leap: dark scene (luminance 15) vs bright blown-out scene (luminance 240)
    frame_dark = np.full((120, 120, 3), 15, dtype=np.uint8)
    frame_bright = np.full((120, 120, 3), 240, dtype=np.uint8)
    dist_leap, is_gap_leap = evaluate_cut_continuity(
        s1, s2, threshold=0.45,
        frame_a_override=frame_dark,
        frame_b_override=frame_bright,
    )
    assert dist_leap > 0.45
    assert is_gap_leap is True


def test_assign_ambient_motion():
    """Verify ambient Ken Burns motion is assigned to low-motion scenes with alternating direction."""
    scene_static_1 = make_test_scene("static1", motion_energy=0.10)
    scene_static_2 = make_test_scene("static2", motion_energy=0.15)
    scene_dynamic = make_test_scene("dynamic", motion_energy=0.65)

    # Even index -> zoom_in
    m0 = assign_ambient_motion(scene_static_1, clip_index=0, motion_threshold=0.20)
    assert m0 == "zoom_in"

    # Odd index -> zoom_out
    m1 = assign_ambient_motion(scene_static_2, clip_index=1, motion_threshold=0.20)
    assert m1 == "zoom_out"

    # Above threshold -> None
    m_none = assign_ambient_motion(scene_dynamic, clip_index=2, motion_threshold=0.20)
    assert m_none is None


def test_build_zoompan_filter():
    """Verify FFmpeg zoompan filter string construction."""
    filt_in = build_zoompan_filter("zoom_in", duration=3.0, fps=30.0, width=1080, height=1920)
    assert "zoompan=" in filt_in
    assert "min(zoom+" in filt_in
    assert "s=1080x1920" in filt_in

    filt_out = build_zoompan_filter("zoom_out", duration=3.0, fps=30.0, width=1080, height=1920)
    assert "zoompan=" in filt_out
    assert "max(zoom-" in filt_out


def test_visual_flow_analyzer_sequence():
    """Verify VisualFlowAnalyzer orchestrates sequence continuity and motion assignment."""
    cfg = PipelineConfig(
        enable_exposure_guard=True,
        exposure_guard_threshold=0.45,
        enable_ken_burns=True,
        ken_burns_motion_threshold=0.20,
    )
    analyzer = VisualFlowAnalyzer(cfg)

    scenes = [
        make_test_scene("c1", motion_energy=0.08),
        make_test_scene("c2", motion_energy=0.75),
        make_test_scene("c3", motion_energy=0.12),
    ]

    assignments = analyzer.assign_sequence_motion(scenes)
    assert assignments[0] == "zoom_in"
    assert 1 not in assignments
    assert assignments[2] == "zoom_in"  # Index 2 is even -> zoom_in
