"""Stage 2 golden windows: genre ranges and window selection on quality curves."""

import numpy as np
import pytest

from src.core.config import PipelineConfig
from src.core.genre_presets import GENRE_PRESETS, ROUGH_CUT_PRESET
from src.core.stage_options import apply_staged_options
from src.modules.shot_windows import QualityCurve, select_golden_windows


def make_curve(values, hz: float = 4.0, t0: float = 0.0, bad=None) -> QualityCurve:
    n = len(values)
    return QualityCurve(
        times=t0 + np.arange(n) / hz,
        sharpness=np.asarray(values, dtype=float),
        motion=np.zeros(n),
        jerk=np.zeros(n),
        exposure_bad=np.zeros(n, dtype=bool) if bad is None else np.asarray(bad, dtype=bool),
        hz=hz,
    )


def test_default_golden_window_range():
    cfg = PipelineConfig()
    assert (cfg.golden_window_min_sec, cfg.golden_window_max_sec) == (3.0, 8.0)
    assert ROUGH_CUT_PRESET.golden_window_sec == (3.0, 8.0)


@pytest.mark.parametrize(
    "key, expected",
    [
        ("wedding_highlight", (4.0, 8.0)),
        ("travel_cinematic", (4.0, 8.0)),
        ("travel_daily_vlog", (2.5, 4.5)),
        ("sports_extreme", (2.5, 4.5)),
        ("social_tiktok_fast", (2.5, 4.5)),
        ("doc_interview", (3.0, 8.0)),
    ],
)
def test_genre_sets_golden_window(key, expected):
    cfg = PipelineConfig()
    apply_staged_options(cfg, genre=key)
    assert (cfg.golden_window_min_sec, cfg.golden_window_max_sec) == expected


def test_every_preset_has_a_valid_range():
    for preset in GENRE_PRESETS.values():
        lo, hi = preset.golden_window_sec
        assert 0.0 < lo <= hi


def test_short_span_is_kept_whole():
    curve = make_curve([0.5] * 16)
    assert select_golden_windows(curve, 0.0, 4.0, 2.5, 4.5) == [(0.0, 4.0)]


def test_without_curve_takes_earliest_window():
    assert select_golden_windows(None, 10.0, 40.0, 3.0, 8.0) == [(10.0, 18.0)]


def test_peak_prefers_longest_window_within_tolerance():
    curve = make_curve([0.2] * 12 + [0.9] * 16 + [0.2] * 12)  # peak 3.0 .. 6.75 s
    (w,) = select_golden_windows(curve, 0.0, 10.0, 2.5, 4.5)
    assert w == pytest.approx((3.0, 6.5))


def test_exposure_bad_region_is_avoided():
    bad = [True] * 20 + [False] * 20
    curve = make_curve([0.8] * 40, bad=bad)
    (w,) = select_golden_windows(curve, 0.0, 10.0, 2.5, 4.5)
    assert w[0] >= 5.0 - 1e-6


def test_two_takes_do_not_overlap_and_keep_a_gap():
    curve = make_curve([0.7] * 160)  # 40 s flat
    takes = select_golden_windows(curve, 0.0, 40.0, 3.0, 8.0, max_takes=2)
    assert len(takes) == 2
    (a0, a1), (b0, b1) = takes
    assert a1 - a0 == pytest.approx(8.0) and b1 - b0 == pytest.approx(8.0)
    assert b0 >= a1 + 3.0 - 1e-6


def test_windows_stay_inside_span_and_length_range():
    rng = np.random.default_rng(7)
    for _ in range(25):
        n = int(rng.integers(60, 200))
        curve = make_curve(rng.random(n), t0=100.0)
        end = 100.0 + (n - 1) / 4.0
        for w0, w1 in select_golden_windows(curve, 100.0, end, 2.5, 4.5, max_takes=2):
            assert 100.0 - 1e-6 <= w0 < w1 <= end + 1e-6
            assert 2.5 - 1e-6 <= w1 - w0 <= 4.5 + 1e-6
