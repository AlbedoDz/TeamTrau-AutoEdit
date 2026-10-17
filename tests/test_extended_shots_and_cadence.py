"""Phase 23 A3/A4: long-shot sub-windows, scene-aware cadence slots, profile variants.

Footage is synthetic: lavfi ``testsrc2`` clips with a deliberately shaky head and a smooth rest
(real ffmpeg + OpenCV), plus fake scene lists on a click-track bar grid (120 BPM, 90 s).
"""

import copy
import shutil
import statistics
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from test_cadence_engine import four_sections, grid_for

from src.core.analysis_cache import AnalysisCache
from src.core.cadence import grid_points
from src.core.config import PipelineConfig
from src.core.timebase import frame_rate_fraction
from src.core.types import (
    BeatMarker,
    CadenceProfile,
    EditClip,
    SceneCut,
    ScoredScene,
)
from src.modules.edit_timeline import (
    apply_scene_curation,
    build_edit_timeline,
    clip_scene_key,
    retarget_timeline,
    scene_key,
)
from src.modules.shot_windows import (
    SubWindowPlanner,
    compute_quality_curve,
    extract_optimal_subwindow,
    plan_scene_segments,
    score_window,
)
from src.modules.variant_synthesizer import MultiVariantTimelineSynthesizer

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available on PATH")

BPM = 120.0
BAR = 4 * 60.0 / BPM  # 2.0 s
TRACK = 90.0
SECTIONS = four_sections(BAR, bars=(8, 16, 16, 5))  # 90 s


# ------------------------------------------------------------------ helpers


def make_shaky_then_smooth(out: Path, shaky: float, smooth: float) -> Path:
    """testsrc2 clip whose first ``shaky`` seconds jitter hard (random crop) and the rest is calm."""
    graph = (
        f"testsrc2=size=320x180:rate=30:duration={shaky},"
        "crop=288:162:x='16+14*(random(1)*2-1)':y='9+8*(random(2)*2-1)',scale=320:180,setsar=1[a];"
        f"testsrc2=size=320x180:rate=30:duration={smooth},crop=288:162:16:9,scale=320:180,setsar=1[b];"
        "[a][b]concat=n=2:v=1:a=0"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-filter_complex", graph,
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15", str(out)],
        check=True,
    )
    return out


@pytest.fixture(scope="module")
def long25(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_shaky_then_smooth(tmp_path_factory.mktemp("long") / "long25.mp4", 9.0, 16.0)


@pytest.fixture(scope="module")
def long40(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_shaky_then_smooth(tmp_path_factory.mktemp("long") / "long40.mp4", 12.0, 28.0)


def scene(path: Path | str, start: float, dur: float, motion: float = 0.2, speed: float = 1.0) -> ScoredScene:
    sc = SceneCut(
        clip_path=Path(path),
        start_time=start,
        end_time=start + dur,
        start_frame=int(start * 30),
        end_frame=int((start + dur) * 30),
        duration=dur,
    )
    return ScoredScene(
        scene=sc, sharpness_score=0.5, aesthetic_score=0.5, composite_score=0.5,
        motion_energy=motion, speed=speed,
    )


def mixed_pool(tmp_path: Path, long_path: Path | None = None) -> list[ScoredScene]:
    """Scenes from 1.5 s to 25 s on fake files (no curve) + optionally the real 25 s shot."""
    durs = [1.5, 2.5, 3.0, 4.5, 6.0, 7.0, 9.0, 11.0, 13.0, 16.0, 20.0, 3.5, 5.0, 8.0, 10.0]
    pool = [scene(tmp_path / f"fake_{i}.mp4", 0.0, d) for i, d in enumerate(durs)]
    if long_path is not None:
        pool.insert(4, scene(long_path, 0.0, 25.0))
    return pool


def cfg(fps: float = 30.0, **kw) -> PipelineConfig:
    c = PipelineConfig()
    c.fps = fps
    c.enable_teaser = False
    c.enable_ken_burns = False
    c.enable_exposure_guard = False
    c.enable_cache = False
    for k, v in kw.items():
        setattr(c, k, v)
    return c


GRID = grid_for(BPM, TRACK, SECTIONS)


def build(config: PipelineConfig, pool: list[ScoredScene], profile=None, seed=None, grid=GRID):
    return build_edit_timeline(
        config, pool, [0.0, TRACK], sections=SECTIONS, bar_grid=grid, profile=profile, cadence_seed=seed
    )


def clip_times(tl, fps: float) -> list[float]:
    rate = frame_rate_fraction(fps)
    out, acc = [0.0], 0
    for c in tl.clips:
        acc += c.frames
        out.append(float(Fraction(acc) / rate))
    return out


def slot_lengths(tl) -> list[float]:
    pts = clip_times(tl, tl.fps)
    return [b - a for a, b in zip(pts, pts[1:], strict=False)]


# ------------------------------------------------------------------ A3: quality curve


@needs_ffmpeg
def test_curve_separates_shaky_from_smooth(long25: Path) -> None:
    curve = compute_quality_curve(long25, 0.0, 25.0)
    assert curve is not None and len(curve) >= 90
    assert score_window(curve, 12.0, 20.0) > score_window(curve, 0.0, 8.0) + 0.3
    assert curve.motion[:30].mean() > 3 * curve.motion[40:].mean()
    assert not curve.exposure_bad.any()


@needs_ffmpeg
def test_curve_cache_namespace_and_unreadable_file(long25: Path, tmp_path: Path) -> None:
    config = cfg(cache_dir=tmp_path / "cache", enable_cache=True)
    cache = AnalysisCache(config)
    first = compute_quality_curve(long25, 0.0, 25.0, cache=cache)
    stored = list((tmp_path / "cache" / "analysis" / "curves").glob("*.json"))
    assert first is not None and len(stored) == 1
    second = compute_quality_curve(long25, 0.0, 25.0, cache=cache)
    assert second is not None and np.allclose(first.motion, second.motion, atol=1e-3)
    other = compute_quality_curve(long25, 0.0, 20.0, cache=cache)  # different range -> new entry
    assert other is not None and len(list((tmp_path / "cache" / "analysis" / "curves").glob("*.json"))) == 2
    assert compute_quality_curve(tmp_path / "missing.mp4", 0.0, 10.0) is None
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    assert compute_quality_curve(junk, 0.0, 10.0) is None


@needs_ffmpeg
def test_optimal_subwindow_beats_first_seconds_and_respects_used(long25: Path) -> None:
    sc = scene(long25, 0.0, 25.0)
    curve = compute_quality_curve(long25, 0.0, 25.0)
    win = extract_optimal_subwindow(sc, 8.0, curve, ())
    assert win is not None and win[0] >= 8.5 and win[1] <= 25.0 + 1e-6
    assert score_window(curve, *win) > score_window(curve, 0.0, 8.0)
    second = extract_optimal_subwindow(sc, 6.0, curve, [win], min_gap=0.5)
    assert second is not None
    assert second[1] <= win[0] - 0.5 + 1e-6 or second[0] >= win[1] + 0.5 - 1e-6
    assert extract_optimal_subwindow(sc, 30.0, curve, ()) is None  # longer than the scene


def test_subwindow_without_curve_is_earliest_free_and_clamped(tmp_path: Path) -> None:
    sc = scene(tmp_path / "x.mp4", 10.0, 20.0)  # scene covers 10..30 s of the file
    assert extract_optimal_subwindow(sc, 5.0, None, ()) == (10.0, 15.0)
    win = extract_optimal_subwindow(sc, 5.0, None, [(10.0, 15.0)], min_gap=0.5)
    assert win is not None and win[0] >= 15.5 and win[1] <= 30.0 + 1e-6


@needs_ffmpeg
def test_plan_segments_long_scene_gives_two_disjoint_windows(long40: Path) -> None:
    sc = scene(long40, 0.0, 40.0)
    curve = compute_quality_curve(long40, 0.0, 40.0)
    segs = plan_scene_segments(sc, curve, max_segments=2, slot_sec=8.0)
    assert len(segs) == 2
    (a0, a1), (b0, b1) = segs
    assert b0 - a1 >= 0.2 * 40.0 - 1e-6  # separated by >= 20 % of the scene
    assert all(s0 >= 0.0 and s1 <= 40.0 + 1e-6 for s0, s1 in segs)
    # one segment unless the scene is long and multi-segment is allowed
    assert len(plan_scene_segments(sc, curve, 2, 8.0, allow_multi_segment_for_long_shots=False)) == 1
    mid = scene(long40, 0.0, 28.0)
    assert len(plan_scene_segments(mid, curve, 2, 8.0)) == 1


@needs_ffmpeg
def test_planner_never_repeats_an_in_point(long25: Path) -> None:
    sc = scene(long25, 0.0, 25.0)
    planner = SubWindowPlanner(threshold=12.0)
    wins = [planner.choose(sc, 4.0) for _ in range(4)]
    assert all(w is not None for w in wins)
    starts = [w[0] for w in wins if w]
    assert len(set(round(s, 2) for s in starts)) == len(starts)
    spans = sorted(w for w in wins if w)
    assert all(b[0] >= a[1] for a, b in zip(spans, spans[1:], strict=False))
    assert planner.room(sc) < 25.0
    assert planner.choose(sc, 20.0) is None  # no room left for a long window


# ------------------------------------------------------------------ A3: allocation


@pytest.mark.parametrize("fps", [24.0, 25.0, 29.97, 30.0, 60.0])
def test_every_cut_on_bar_or_half_bar_within_half_frame(fps: float, tmp_path: Path) -> None:
    tl = build(cfg(fps), mixed_pool(tmp_path))
    rate = frame_rate_fraction(fps)
    half_frame = 0.5 / float(rate) + 1e-9
    grid = grid_points(GRID, TRACK)
    times = clip_times(tl, fps)[1:-1]
    assert len(times) > 10
    for t in times:
        assert min(abs(t - g) for g in grid) <= half_frame, (t, fps)
    assert tl.cut_snap_error_max_sec is not None and tl.cut_snap_error_max_sec <= half_frame
    assert tl.bar_grid is GRID
    assert tl.duration == pytest.approx(TRACK, abs=1.0 / fps + 1e-6)


@pytest.mark.parametrize(
    "profile",
    [CadenceProfile.BALANCED_NARRATIVE, CadenceProfile.LINGERING_CINEMATIC],
)
def test_slot_variety_and_no_three_equal(profile: CadenceProfile, tmp_path: Path) -> None:
    for seed in (1, 7, 42):
        lens = slot_lengths(build(cfg(), mixed_pool(tmp_path), profile, seed))
        assert statistics.pstdev(lens) >= 1.2, (profile, seed, statistics.pstdev(lens))
        for a, b, c in zip(lens, lens[1:], lens[2:], strict=False):
            assert not (abs(a - b) < 1e-6 and abs(b - c) < 1e-6)


def test_viral_and_fast_profiles_vary_without_three_equal(tmp_path: Path) -> None:
    for profile in (CadenceProfile.FAST_SYNCOPATED, CadenceProfile.VIRAL_BURST):
        lens = slot_lengths(build(cfg(), mixed_pool(tmp_path), profile))
        assert statistics.pstdev(lens) > 0.2
        assert not any(
            abs(a - b) < 1e-6 and abs(b - c) < 1e-6 for a, b, c in zip(lens, lens[1:], lens[2:], strict=False)
        )


def test_mean_slot_length_orders_by_profile(tmp_path: Path) -> None:
    means = {
        p: statistics.mean(slot_lengths(build(cfg(), mixed_pool(tmp_path), p)))
        for p in (
            CadenceProfile.FAST_SYNCOPATED,
            CadenceProfile.BALANCED_NARRATIVE,
            CadenceProfile.LINGERING_CINEMATIC,
        )
    }
    assert (
        means[CadenceProfile.FAST_SYNCOPATED]
        < means[CadenceProfile.BALANCED_NARRATIVE]
        < means[CadenceProfile.LINGERING_CINEMATIC]
    ), means


@needs_ffmpeg
def test_long_shot_gets_long_slot_and_smoothest_window(long25: Path, tmp_path: Path) -> None:
    pool = mixed_pool(tmp_path, long25)
    long_scene_key = scene_key(long25, 0.0)
    found = False
    for profile in (CadenceProfile.BALANCED_NARRATIVE, CadenceProfile.LINGERING_CINEMATIC):
        tl = build(cfg(), pool, profile)
        curve = compute_quality_curve(long25, 0.0, 25.0)
        mine = [c for c in tl.clips if clip_scene_key(c) == long_scene_key]
        assert mine, f"25 s shot unused with {profile}"
        long_ones = [c for c in mine if c.frames / tl.fps >= 8.0 - 1e-6]
        assert long_ones, [c.frames / tl.fps for c in mine]
        for c in long_ones:
            secs = c.frames / tl.fps
            assert secs <= 16.0 + 1e-6
            win = (c.source_in, c.source_in + secs)
            assert win[1] <= 25.0 + 1e-6
            assert score_window(curve, *win) > score_window(curve, 0.0, secs)
            found = True
        starts = [round(c.source_in, 2) for c in mine]
        assert len(set(starts)) == len(starts)  # never the same in-point twice
    assert found


@needs_ffmpeg
def test_forty_second_scene_yields_disjoint_segments_in_different_sections(
    long40: Path, tmp_path: Path
) -> None:
    pool = [scene(long40, 0.0, 40.0)] + [scene(tmp_path / f"s{i}.mp4", 0.0, 8.0 + i) for i in range(10)]
    config = cfg(enable_cache=False)
    tl = build(config, pool, CadenceProfile.LINGERING_CINEMATIC)
    key = scene_key(long40, 0.0)
    mine = [(i, c) for i, c in enumerate(tl.clips) if clip_scene_key(c) == key]
    assert len(mine) >= 2
    times = clip_times(tl, tl.fps)
    spans = sorted((c.source_in, c.source_in + c.frames / tl.fps) for _, c in mine)
    assert all(b[0] >= a[1] - 1e-6 for a, b in zip(spans, spans[1:], strict=False))
    sections_used = {
        next(k for k, s in enumerate(SECTIONS) if s.start_time <= times[i] < s.end_time) for i, _ in mine
    }
    assert len(sections_used) >= 2


def test_no_held_frames_or_gaps_when_pool_allows(tmp_path: Path) -> None:
    for fps in (24.0, 30.0):
        for profile in CadenceProfile:
            tl = build(cfg(fps), mixed_pool(tmp_path), profile)
            for c in tl.clips:
                assert c.frames <= c.available / c.speed * tl.fps + 1.0, (profile, c.label, c.frames, c.available)
                assert c.source_in >= c.scene_start - 1e-6
                assert c.source_in + c.available <= c.scene_end + 1e-3


def test_pool_shorter_than_smallest_slot_still_builds(tmp_path: Path) -> None:
    pool = [scene(tmp_path / f"t{i}.mp4", 0.0, 0.8) for i in range(3)]
    tl = build(cfg(), pool, CadenceProfile.LINGERING_CINEMATIC)
    assert len(tl.clips) >= 10  # unavoidable holds, but a valid frame-exact timeline


def test_deterministic_and_seed_sensitive(tmp_path: Path) -> None:
    pool = mixed_pool(tmp_path)
    a = build(cfg(), pool, seed=11)
    b = build(cfg(), copy.deepcopy(pool), seed=11)
    c = build(cfg(), pool, seed=12)

    def sig(tl):
        return [(Path(x.source_path).name, round(x.source_in, 3), x.frames) for x in tl.clips]

    assert sig(a) == sig(b)
    assert sig(a) != sig(c)


def test_teaser_ends_on_grid_and_uses_distinct_windows(tmp_path: Path) -> None:
    pool = [scene(tmp_path / "one.mp4", 0.0, 30.0), scene(tmp_path / "two.mp4", 0.0, 30.0)]
    config = cfg(enable_teaser=True, teaser_duration=5.0)
    tl = build(config, pool, CadenceProfile.BALANCED_NARRATIVE)
    teaser = [c for c in tl.clips if c.is_teaser]
    assert 3 <= len(teaser) <= 5
    teaser_end = sum(c.frames for c in teaser) / tl.fps
    assert min(abs(teaser_end - g) for g in grid_points(GRID, TRACK)) <= 0.5 / tl.fps + 1e-6
    keyed = [(Path(c.source_path).name, round(c.source_in, 2)) for c in tl.clips]
    assert len(set(keyed)) == len(keyed)


def test_legacy_path_unchanged_without_grid_or_engine_off(tmp_path: Path) -> None:
    pool = mixed_pool(tmp_path)
    cuts = [round(0.6 * i, 3) for i in range(0, 60)]  # deliberately off any musical grid
    legacy = build_edit_timeline(cfg(), pool, cuts, sections=SECTIONS)
    off = build_edit_timeline(
        cfg(use_cadence_engine=False), pool, cuts, sections=SECTIONS, bar_grid=GRID
    )
    low = copy.deepcopy(GRID)
    low.confidence = 0.1
    weak = build_edit_timeline(cfg(), pool, cuts, sections=SECTIONS, bar_grid=low)
    for other in (off, weak):
        assert other.clips == legacy.clips
        assert other.bar_grid is None and other.cut_snap_error_max_sec is None
    # legacy semantics: one clip per beat slot, always from the scene start
    assert len(legacy.clips) == len(cuts) - 1
    assert all(c.source_in == c.scene_start for c in legacy.clips)
    assert sum(c.frames for c in legacy.clips) == round((cuts[-1] - cuts[0]) * 30.0)


def test_retarget_preserves_new_fields(tmp_path: Path) -> None:
    from src.core.types import DeliverSettings

    tl = build(cfg(30.0), mixed_pool(tmp_path))
    out = retarget_timeline(tl, DeliverSettings(canvas_width=tl.canvas_width, canvas_height=tl.canvas_height, fps=24.0))
    assert out is not tl and out.fps == 24.0
    assert out.bar_grid is tl.bar_grid and out.cut_snap_error_max_sec == tl.cut_snap_error_max_sec
    assert [c.scene_start for c in out.clips] == [c.scene_start for c in tl.clips]
    assert [c.source_in for c in out.clips] == [c.source_in for c in tl.clips]


# ------------------------------------------------------------------ curation with sub-windows


def test_apply_scene_curation_matches_sub_window_clips_by_scene_start(tmp_path: Path) -> None:
    long_sc = scene(tmp_path / "long.mp4", 0.0, 25.0)
    others = [scene(tmp_path / f"o{i}.mp4", 0.0, 12.0 + i) for i in range(3)]
    tl = build(cfg(), [long_sc, *others], CadenceProfile.BALANCED_NARRATIVE)
    mine = [c for c in tl.clips if clip_scene_key(c) == scene_key(long_sc.scene.clip_path, 0.0)]
    assert mine and any(c.source_in > 0.0 for c in mine)  # sub-window clips exist
    new_tl, replaced = apply_scene_curation(tl, included=others, excluded=[long_sc])
    assert replaced == len(mine)
    assert not any(clip_scene_key(c) == scene_key(long_sc.scene.clip_path, 0.0) for c in new_tl.clips)
    assert [c.frames for c in new_tl.clips] == [c.frames for c in tl.clips]  # cuts preserved


def test_curation_falls_back_to_source_in_for_old_clips(tmp_path: Path) -> None:
    sc = scene(tmp_path / "a.mp4", 3.0, 5.0)
    other = scene(tmp_path / "b.mp4", 0.0, 9.0)
    old = EditClip(source_path=sc.scene.clip_path, source_in=3.0, frames=60, available=5.0)
    from src.core.types import EditTimeline

    tl = EditTimeline(name="old", fps=30.0, canvas_width=1920, canvas_height=1080, clips=[old])
    new_tl, replaced = apply_scene_curation(tl, included=[other], excluded=[sc])
    assert replaced == 1 and Path(new_tl.clips[0].source_path).name == "b.mp4"


# ------------------------------------------------------------------ A4: variants


def click_beats(bpm: float = BPM, duration: float = TRACK) -> list[BeatMarker]:
    period = 60.0 / bpm
    out: list[BeatMarker] = []
    i = 0
    while i * period < duration:
        out.append(BeatMarker(i * period, int(i * period * 43), 1.0 if i % 4 == 0 else 0.5, i % 4 == 0))
        i += 1
    return out


def variant_inputs(tmp_path: Path):
    soundtrack = tmp_path / "music.wav"
    soundtrack.touch()
    return mixed_pool(tmp_path), click_beats(), soundtrack


def test_variants_follow_profiles_and_none_is_dropped(tmp_path: Path) -> None:
    pool, beats, soundtrack = variant_inputs(tmp_path)
    config = cfg()
    config.enable_cache = False
    warnings: list[str] = []
    variants = MultiVariantTimelineSynthesizer(config).synthesize_variants(
        pool, beats, soundtrack, SECTIONS, variant_count=3,
        bar_grid=GRID, cut_timestamps=[0.0, TRACK], warnings=warnings,
    )
    assert set(variants) == {"beat_driven", "cinematic", "social_teaser"} and warnings == []
    fast = statistics.mean(slot_lengths(variants["beat_driven"].edit_timeline))
    cine = statistics.mean(slot_lengths(variants["cinematic"].edit_timeline))
    master = statistics.mean(slot_lengths(build(config, pool, CadenceProfile.BALANCED_NARRATIVE)))
    assert fast < master < cine, (fast, master, cine)
    social = variants["social_teaser"].edit_timeline
    assert social.duration <= 35.0 + 1.0 / social.fps
    assert (social.canvas_width, social.canvas_height) == (1080, 1920)
    assert social.bar_grid is GRID and variants["beat_driven"].edit_timeline.cut_snap_error_max_sec is not None
    # hard-coded legacy intervals are not applied on the cadence path
    assert min(slot_lengths(variants["beat_driven"].edit_timeline)) < 1.0 + 1e-6
    for v in variants.values():
        assert v.sequence_scenes  # clips map back to scenes through scene_start


def test_variants_without_grid_use_legacy_intervals(tmp_path: Path) -> None:
    pool, beats, soundtrack = variant_inputs(tmp_path)
    variants = MultiVariantTimelineSynthesizer(cfg()).synthesize_variants(
        pool, beats, soundtrack, SECTIONS, variant_count=2
    )
    assert set(variants) == {"beat_driven", "cinematic"}
    assert all(v.edit_timeline.bar_grid is None for v in variants.values())


def test_variant_failure_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pool, beats, soundtrack = variant_inputs(tmp_path)
    synth = MultiVariantTimelineSynthesizer(cfg())

    def boom(*_a, **_k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(synth, "_build_cinematic_variant", boom)
    warnings: list[str] = []
    with caplog.at_level("WARNING"):
        variants = synth.synthesize_variants(
            pool, beats, soundtrack, SECTIONS, bar_grid=GRID, cut_timestamps=[0.0, TRACK], warnings=warnings
        )
    assert set(variants) == {"beat_driven", "social_teaser"}
    assert len(warnings) == 1 and "cinematic" in warnings[0] and "kaput" in warnings[0]
    assert "kaput" in caplog.text


def test_pipeline_result_has_warnings_field() -> None:
    from src.core.types import PipelineExecutionResult

    res = PipelineExecutionResult(
        success=True, total_duration=1.0, clip_count=1, scenes_detected=1, audio_bpm=120.0,
        sections_detected=0, fcpxml_path=None, premiere_xml_path=None, otio_path=None,
        capcut_draft_path=None, render_path=None, encoder_used=None, resolve_status=None,
    )
    assert res.warnings == []


# ------------------------------------------------------------------ measurements (informational)


def test_curve_time_budget_is_reasonable(long25: Path) -> None:
    t = time.perf_counter()
    assert compute_quality_curve(long25, 0.0, 25.0) is not None
    assert time.perf_counter() - t < 30.0


@pytest.mark.parametrize(
    "profile", [CadenceProfile.LINGERING_CINEMATIC, CadenceProfile.BALANCED_NARRATIVE]
)
@pytest.mark.parametrize("fps", [24.0, 29.97, 30.0])
def test_teaser_length_follows_config_and_is_uneven(
    profile: CadenceProfile, fps: float, tmp_path: Path
) -> None:
    config = cfg(fps, enable_teaser=True, teaser_duration=6.0)
    tl = build(config, mixed_pool(tmp_path), profile)
    flags = [c.is_teaser for c in tl.clips]
    n = sum(flags)
    assert flags == [True] * n + [False] * (len(flags) - n)  # teaser clips only at the start
    assert 3 <= n <= 5
    rate = frame_rate_fraction(fps)
    lens = [float(Fraction(c.frames) / rate) for c in tl.clips[:n]]
    total = sum(lens)
    assert abs(total - 6.0) <= BAR + 1e-6 and total <= 0.25 * TRACK
    assert len({c.frames for c in tl.clips[:n]}) > 1  # not all equal
    beat = BAR / 4
    for t in clip_times(tl, fps)[: n + 1]:
        assert min(abs(t / beat - round(t / beat)) * beat, 1.0) <= 0.5 / float(rate) + 1e-6
    assert all(x >= beat - 1.0 / fps for x in lens)
