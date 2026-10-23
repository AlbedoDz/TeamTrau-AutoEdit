"""window_cleaned_segments: roles decide which takes are windowed."""

from pathlib import Path

import numpy as np
import pytest

from src.core.ontology import ContentRole
from src.core.types import CleanedSegment, SceneCut
from src.modules.golden_takes import window_cleaned_segments
from src.modules.shot_windows import QualityCurve

CLIP_A = Path("A.mp4")
CLIP_B = Path("B.mp4")


def seg(path: Path, s: float, e: float) -> CleanedSegment:
    sc = SceneCut(clip_path=path, start_time=s, end_time=e, start_frame=int(s * 30),
                  end_frame=int(e * 30), duration=e - s)
    return CleanedSegment(scene=sc, clip_path=path, start_time=s, end_time=e, duration=e - s,
                          source_start_time=s, source_end_time=e)


def peak_curve(path: Path, start: float, end: float) -> QualityCurve:
    n = int(round((end - start) * 4))
    t = start + np.arange(n) / 4.0
    vals = np.where((t >= start + 8.0) & (t < start + 14.0), 0.9, 0.2)
    return QualityCurve(times=t, sharpness=vals, motion=np.zeros(n), jerk=np.zeros(n),
                        exposure_bad=np.zeros(n, dtype=bool), hz=4.0)


def no_curve(path, start, end):
    raise AssertionError("curve_fn must not be called")


def test_a_roll_is_kept_whole_and_tagged():
    s = seg(CLIP_A, 0.0, 20.0)
    (out,) = window_cleaned_segments([s], {CLIP_A: ContentRole.A_ROLL}, 2.5, 4.5, no_curve)
    assert (out.start_time, out.end_time) == (0.0, 20.0)
    assert out.metadata["content_role"] == "a_roll"
    assert s.metadata == {}


def test_b_roll_is_cut_to_its_best_window():
    s = seg(CLIP_B, 100.0, 120.0)
    (out,) = window_cleaned_segments([s], {CLIP_B: ContentRole.B_ROLL}, 2.5, 4.5, peak_curve)
    assert (out.start_time, out.end_time) == pytest.approx((108.0, 112.5))
    assert out.duration == pytest.approx(4.5)
    assert (out.source_start_time, out.source_end_time) == pytest.approx((108.0, 112.5))
    assert out.metadata["content_role"] == "b_roll"
    assert out.metadata["golden_window"] == {"span": [100.0, 120.0], "window": [108.0, 112.5]}
    assert (s.start_time, s.end_time) == (100.0, 120.0)


def test_short_b_roll_is_tagged_but_not_cut():
    s = seg(CLIP_B, 5.0, 8.0)
    (out,) = window_cleaned_segments([s], {CLIP_B: ContentRole.B_ROLL}, 2.5, 4.5, no_curve)
    assert (out.start_time, out.end_time) == (5.0, 8.0)
    assert out.metadata["content_role"] == "b_roll"


def test_unknown_role_is_untouched():
    s = seg(CLIP_B, 0.0, 20.0)
    (out,) = window_cleaned_segments([s], {}, 2.5, 4.5, no_curve)
    assert out is s


def test_long_ambient_take_gives_two_windows_in_order():
    s = seg(CLIP_B, 0.0, 40.0)

    def flat(path, start, end):
        n = int(round((end - start) * 4))
        return QualityCurve(times=start + np.arange(n) / 4.0, sharpness=np.full(n, 0.7),
                            motion=np.zeros(n), jerk=np.zeros(n),
                            exposure_bad=np.zeros(n, dtype=bool), hz=4.0)

    out = window_cleaned_segments([s], {CLIP_B: ContentRole.AMBIENT_B_ROLL}, 3.0, 8.0, flat)
    assert len(out) == 2
    assert out[0].end_time <= out[1].start_time
    assert all(o.metadata["content_role"] == "ambient_b_roll" for o in out)


def test_mixed_segments_keep_input_order():
    segs = [seg(CLIP_A, 0.0, 10.0), seg(CLIP_B, 100.0, 120.0), seg(CLIP_A, 30.0, 35.0)]
    roles = {CLIP_A: ContentRole.A_ROLL, CLIP_B: ContentRole.B_ROLL}
    out = window_cleaned_segments(segs, roles, 2.5, 4.5, peak_curve)
    assert [o.clip_path for o in out] == [CLIP_A, CLIP_B, CLIP_A]
