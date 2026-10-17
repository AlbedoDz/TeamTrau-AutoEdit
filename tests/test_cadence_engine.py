"""Phase 23 A1/A2: cadence types, presets, bar grid extraction and bar-locked cut generation."""

import json
import random
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from src.core.cadence import (
    PROFILE_MULTIPLES,
    assign_downbeats,
    cadence_cuts_with_stats,
    estimate_beats_per_bar,
    extract_musical_bar_grid,
    generate_cadence_cuts,
    grid_points,
    profile_multiples,
    snap_to_frame,
)
from src.core.config import PipelineConfig
from src.core.genre_presets import GENRE_PRESETS, ROUGH_CUT_PRESET
from src.core.project_io import (
    bar_grid_from_dict,
    bar_grid_to_dict,
    edit_clip_from_dict,
    edit_clip_to_dict,
    edit_timeline_from_dict,
    edit_timeline_to_dict,
)
from src.core.timebase import frame_rate_fraction
from src.core.types import (
    BeatMarker,
    CadenceProfile,
    EditClip,
    EditTimeline,
    MusicalBarGrid,
    SongSection,
    SongSectionType,
)
from src.modules.audio_analyzer import AudioAnalysisResult, AudioAnalyzer

HOP_SEC = 512 / 22050


def make_beats(
    bpm: float, duration: float, bpb: int = 4, jitter: float = 0.0, seed: int = 0, offset: float = 0.0
) -> list[BeatMarker]:
    """Tracked beats with a known truth: every bpb-th beat is a (strong) downbeat."""
    rng = random.Random(seed)
    period = 60.0 / bpm
    out: list[BeatMarker] = []
    i = 0
    while offset + i * period < duration:
        t = offset + i * period + (rng.uniform(-jitter, jitter) if jitter else 0.0)
        down = i % bpb == 0
        out.append(BeatMarker(max(t, 0.0), int(t * 22050 / 512), 1.0 if down else 0.5, down))
        i += 1
    return out


def sec(kind: SongSectionType, a: float, b: float, climax: bool = False) -> SongSection:
    return SongSection(kind, a, b, b - a, 0.5, is_climax=climax)


def four_sections(bar_len: float, bars: tuple[int, int, int, int] = (8, 16, 24, 8)) -> list[SongSection]:
    kinds = [
        SongSectionType.INTRO,
        SongSectionType.BUILDUP,
        SongSectionType.DROP,
        SongSectionType.OUTRO,
    ]
    out, t = [], 0.0
    for k, n in zip(kinds, bars):
        out.append(sec(k, t, t + n * bar_len, climax=k == SongSectionType.DROP))
        t += n * bar_len
    return out


def grid_for(bpm: float, duration: float, sections: list[SongSection], bpb: int = 4) -> MusicalBarGrid:
    return extract_musical_bar_grid(
        make_beats(bpm, duration, bpb), sections, bpm, duration, HOP_SEC, beats_per_bar=bpb
    )


# ------------------------------------------------------------------ bar grid


@pytest.mark.parametrize("bpm,bpb", [(96, 4), (120, 4), (140, 4), (120, 3)])
def test_bar_grid_matches_truth(bpm: float, bpb: int) -> None:
    duration = 60.0
    bar_len = bpb * 60.0 / bpm
    beats = make_beats(bpm, duration, bpb, jitter=0.008, seed=3)
    grid = extract_musical_bar_grid(beats, [], bpm, duration, HOP_SEC, beats_per_bar=bpb)
    assert grid.confidence >= 0.8
    assert grid.beats_per_bar == bpb
    truth = np.arange(0.0, duration, bar_len)
    assert abs(len(grid.bar_times) - len(truth)) <= 1
    for t, g in zip(truth, grid.bar_times):
        assert abs(t - g) <= 0.025
    assert grid.tempo_bpm == pytest.approx(bpm, rel=0.01)


def test_irregular_downbeats_are_regularised_with_lower_confidence() -> None:
    beats = make_beats(120, 60.0, jitter=0.004, seed=1)
    # Wreck the downbeat flags: only 2 irregularly spaced ones.
    broken = [
        BeatMarker(b.timestamp, b.frame_idx, b.strength, i in (3, 17)) for i, b in enumerate(beats)
    ]
    grid = extract_musical_bar_grid(broken, [], 120, 60.0, HOP_SEC)
    assert 0.4 <= grid.confidence <= 0.6
    gaps = np.diff(grid.bar_times)
    assert np.std(gaps) / np.mean(gaps) < 0.02
    assert np.mean(gaps) == pytest.approx(2.0, abs=0.02)


def test_best_onset_phase_is_chosen_when_regularising() -> None:
    beats = make_beats(120, 60.0, jitter=0.002, seed=2)
    wrong_flags = [
        BeatMarker(b.timestamp, b.frame_idx, b.strength, i % 4 == 1 and i < 8)
        for i, b in enumerate(beats)
    ]
    grid = extract_musical_bar_grid(wrong_flags, [], 120, 60.0, HOP_SEC)
    # strong beats (strength 1.0) are at multiples of 2.0 s: the phase must follow them
    for b in grid.bar_times[:10]:
        assert abs(b / 2.0 - round(b / 2.0)) * 2.0 < 0.03


def test_missing_beats_give_low_confidence_none() -> None:
    assert extract_musical_bar_grid([], [], 120, 30.0, HOP_SEC).confidence == 0.0
    few = make_beats(120, 3.0)
    grid = extract_musical_bar_grid(few, [], 120, 3.0, HOP_SEC)
    assert grid.confidence < 0.4
    assert cadence_cuts_with_stats(grid, [], "balanced_narrative", None, 3.0, 30) is None


def test_chaotic_beats_fall_back() -> None:
    rng = random.Random(5)
    t, beats = 0.0, []
    for i in range(80):
        t += rng.uniform(0.25, 0.9)
        beats.append(BeatMarker(t, 0, rng.random(), i % 4 == 0))
    grid = extract_musical_bar_grid(beats, [], 120, t + 1, HOP_SEC)
    assert grid.confidence < 0.4
    assert generate_cadence_cuts(grid, [], "balanced_narrative", None, t + 1, 30) is None
    assert generate_cadence_cuts(None, [], "balanced_narrative", None, 30.0, 30) is None


def test_phrases_never_cross_section_boundaries() -> None:
    bar_len = 2.0
    secs = four_sections(bar_len, (6, 10, 21, 5))  # deliberately not multiples of 8
    duration = 42 * bar_len
    grid = grid_for(120, duration, secs)
    starts = [s.start_time for s in secs[1:]]
    for phrase in (grid.phrase_2, grid.phrase_4, grid.phrase_8):
        assert phrase[0] == grid.bar_times[0]
        for i, p in enumerate(phrase):
            q = phrase[i + 1] if i + 1 < len(phrase) else duration
            for b in starts:
                assert not (p + 1e-6 < b < q - 1e-6), (p, q, b)
        for b in starts:  # every section opens a phrase
            assert any(abs(p - b) < 0.02 for p in phrase)
    # section with 6 bars -> phrase_8 has a single (short) phrase there
    assert [p for p in grid.phrase_8 if p < secs[0].end_time - 0.01] == pytest.approx([0.0], abs=1e-9)
    assert len(grid.phrase_4) > len(grid.phrase_8) >= len(secs)


def test_phrases_without_sections_group_from_start() -> None:
    grid = grid_for(120, 40.0, [])
    assert grid.phrase_2[:3] == pytest.approx([0.0, 4.0, 8.0])
    assert grid.phrase_4[:3] == pytest.approx([0.0, 8.0, 16.0])
    assert grid.phrase_8[:3] == pytest.approx([0.0, 16.0, 32.0])


def test_meter_estimation_distinguishes_3_and_4() -> None:
    four = [1.0 if i % 4 == 0 else 0.4 for i in range(64)]
    three = [1.0 if i % 3 == 0 else 0.4 for i in range(63)]
    assert estimate_beats_per_bar(four) == 4
    assert estimate_beats_per_bar(three) == 3
    assert estimate_beats_per_bar([0.5] * 40) == 4
    beats = make_beats(100, 30.0, 3)
    flat = [BeatMarker(b.timestamp, b.frame_idx, b.strength, i % 4 == 0) for i, b in enumerate(beats)]
    fixed = assign_downbeats(flat, 3)
    assert [i for i, b in enumerate(fixed) if b.is_downbeat][:3] == [0, 3, 6]


# ------------------------------------------------------------------ cut generation


def point_error(cuts: list[float], pts: list[float]) -> float:
    return max(min(abs(c - p) for p in pts) for c in cuts[1:-1])


def test_cuts_only_on_bar_and_half_bar_points() -> None:
    duration = 90.0
    secs = four_sections(2.0, (8, 12, 20, 5))
    grid = grid_for(120, duration, secs)
    pts = grid_points(grid, duration)
    fps = 30.0
    res = cadence_cuts_with_stats(grid, secs, "balanced_narrative", None, duration, fps, 7)
    assert res is not None
    cuts, max_err, slots = res
    assert cuts[0] == 0.0
    assert cuts[-1] == pytest.approx(duration, abs=0.5 / fps + 1e-9)
    assert point_error(cuts, pts) <= 0.5 / fps + 1e-6
    assert all(s > 0 for s in slots)
    assert max_err <= 0.5 / fps + 1e-9


def test_cuts_are_deterministic_and_seed_dependent() -> None:
    duration = 90.0
    secs = four_sections(2.0, (8, 12, 20, 5))
    grid = grid_for(120, duration, secs)
    runs = {s: generate_cadence_cuts(grid, secs, "balanced_narrative", None, duration, 30, s) for s in range(8)}
    assert runs[7] == generate_cadence_cuts(grid, secs, "balanced_narrative", None, duration, 30, 7)
    distinct = {tuple(v) for v in runs.values() if v}
    assert len(distinct) >= 4
    assert runs[7] != runs[8 - 1 - 7] or runs[7] != runs[1]


@pytest.mark.parametrize("profile", list(CadenceProfile))
@pytest.mark.parametrize("seed", [0, 7, 42])
def test_no_three_consecutive_equal_slots(profile: CadenceProfile, seed: int) -> None:
    duration = 90.0
    secs = four_sections(2.0, (8, 12, 20, 5))
    grid = grid_for(120, duration, secs)
    cuts = generate_cadence_cuts(grid, secs, profile, None, duration, 30, seed)
    assert cuts is not None
    slots = np.diff(cuts)
    for a, b, c in zip(slots, slots[1:], slots[2:]):
        assert not (abs(a - b) < 0.05 and abs(b - c) < 0.05), slots


def test_slot_length_variety_per_profile() -> None:
    duration = 90.0
    secs = four_sections(2.0, (8, 12, 20, 5))
    grid = grid_for(120, duration, secs)
    for profile in CadenceProfile:
        res = cadence_cuts_with_stats(grid, secs, profile, None, duration, 30, 7)
        assert res is not None
        std = float(np.std(res[2]))
        print(f"slot std [{profile.value}] = {std:.2f}s over {len(res[2])} slots")
        if profile in (CadenceProfile.BALANCED_NARRATIVE, CadenceProfile.LINGERING_CINEMATIC):
            assert std >= 1.0
        else:
            assert std > 0.3


def test_section_curve_intro_longer_than_drop() -> None:
    duration = 120.0
    secs = four_sections(2.0, (16, 16, 24, 4))
    grid = grid_for(120, duration, secs)
    for seed in (1, 7, 99):
        cuts = generate_cadence_cuts(grid, secs, "balanced_narrative", None, duration, 30, seed)
        assert cuts is not None

        def mean_slot(s: SongSection) -> float:
            inside = [c for c in cuts if s.start_time - 0.05 <= c <= s.end_time + 0.05]
            return float(np.mean(np.diff(inside)))

        intro, drop = mean_slot(secs[0]), mean_slot(secs[2])
        assert intro > drop, (seed, intro, drop)


def test_slots_never_cross_section_boundaries() -> None:
    duration = 100.0
    secs = four_sections(2.0, (7, 13, 22, 8))
    grid = grid_for(120, duration, secs)
    for profile in CadenceProfile:
        cuts = generate_cadence_cuts(grid, secs, profile, None, duration, 30, 7)
        assert cuts is not None
        for s in secs[1:]:
            assert any(abs(c - s.start_time) <= 0.5 / 30 + 1e-6 for c in cuts), (profile, s.start_time)


@pytest.mark.parametrize("profile", list(CadenceProfile))
def test_profile_multiples_respected(profile: CadenceProfile) -> None:
    bar_len = 2.0
    duration = 64 * bar_len
    secs = four_sections(bar_len, (16, 16, 24, 8))  # every section is a multiple of 8 bars
    grid = grid_for(120, duration, secs)
    allowed = PROFILE_MULTIPLES[profile]
    cuts = generate_cadence_cuts(grid, secs, profile, None, duration, 30, 7)
    assert cuts is not None
    for slot in np.diff(cuts)[1:-1]:
        bars = slot / bar_len
        assert any(abs(bars - m) <= 0.5 / 30 / bar_len + 1e-6 for m in allowed), (profile, bars)


def test_custom_multiples_override_profile() -> None:
    duration = 64 * 2.0
    secs = four_sections(2.0, (16, 16, 24, 8))
    grid = grid_for(120, duration, secs)
    cuts = generate_cadence_cuts(grid, secs, "viral_burst", [4.0], duration, 30, 7)
    assert cuts is not None
    assert set(np.round(np.diff(cuts) / 2.0, 1)) == {4.0}
    cfg = PipelineConfig(cadence_multiples=[3.0, 1.0, -2.0, 0.0])
    assert profile_multiples("viral_burst", cfg) == (1.0, 3.0)
    assert profile_multiples("viral_burst", PipelineConfig()) == (0.5, 1.0)
    assert profile_multiples("nonsense") == PROFILE_MULTIPLES[CadenceProfile.BALANCED_NARRATIVE]


@pytest.mark.parametrize("fps", [24.0, 25.0, 29.97, 30.0, 60.0])
def test_snap_error_at_most_half_frame(fps: float) -> None:
    rate = frame_rate_fraction(fps)
    duration = 97.3
    secs = four_sections(60.0 / 133.0 * 4, (8, 16, 24, 8))
    grid = grid_for(133, duration, secs)
    res = cadence_cuts_with_stats(grid, secs, "fast_syncopated", None, duration, fps, 5)
    assert res is not None
    cuts, max_err, _ = res
    half_frame = float(Fraction(1, 2) / rate)
    assert max_err <= half_frame + 1e-9
    for c in cuts:  # every cut is exactly on a frame boundary
        frames = c * float(rate)
        assert abs(frames - round(frames)) < 1e-6


def test_snap_to_frame_ntsc_exact() -> None:
    assert snap_to_frame(1.0, 29.97) == pytest.approx(30 * 1001 / 30000, abs=1e-12)
    assert snap_to_frame(0.0, 24) == 0.0
    assert snap_to_frame(0.0209, 24) == pytest.approx(1 / 24)


def test_last_slot_absorbs_remainder() -> None:
    duration = 31.2  # not a multiple of the 2 s bar
    grid = grid_for(120, duration, [])
    cuts = generate_cadence_cuts(grid, [], "balanced_narrative", None, duration, 30, 3)
    assert cuts is not None
    assert cuts[-1] == pytest.approx(duration, abs=1 / 60 + 1e-9)
    assert min(np.diff(cuts)) >= 1.0


# ------------------------------------------------------------------ real audio (synthetic)


def write_click_track(path: Path, bpm: float, bpb: int, seconds: float, sr: int = 22050) -> None:
    """Accented sine bursts: strong on beat 1 of the bar, soft on the others."""
    y = np.zeros(int(seconds * sr), dtype=np.float32)
    period = 60.0 / bpm
    burst_t = np.arange(int(0.05 * sr)) / sr
    i = 0
    while i * period < seconds - 0.1:
        down = i % bpb == 0
        freq = 1000.0 if down else 700.0
        amp = 0.9 if down else 0.35
        burst = amp * np.sin(2 * np.pi * freq * burst_t) * np.exp(-burst_t * 60.0)
        s = int(i * period * sr)
        y[s : s + burst.size] += burst[: max(0, min(burst.size, y.size - s))]
        i += 1
    sf.write(str(path), y, sr)


@pytest.mark.parametrize("bpm,bpb", [(96, 4), (120, 4), (140, 4), (120, 3)])
def test_analyzer_bar_grid_on_synthetic_music(tmp_path: Path, bpm: float, bpb: int) -> None:
    wav = tmp_path / f"click_{bpm}_{bpb}.wav"
    write_click_track(wav, bpm, bpb, 60.0)
    cfg = PipelineConfig(enable_cache=False, cache_dir=tmp_path / "cache")
    res = AudioAnalyzer(cfg).analyze(wav)
    grid = res.bar_grid
    assert grid is not None and grid.confidence >= 0.4
    assert grid.beats_per_bar == bpb
    bar_len = bpb * 60.0 / bpm
    truth = np.arange(0.0, res.duration - bar_len / 2, bar_len)
    errs = []
    for g in grid.bar_times:
        k = round(g / bar_len)
        errs.append(abs(g - k * bar_len))
        assert k * bar_len <= res.duration
    print(f"bar-time error vs truth @{bpm}bpm {bpb}/4: max={max(errs)*1000:.1f} ms bars={len(grid.bar_times)}")
    # beat trackers + onset envelopes work on 23 ms frames and lag the click by about one frame
    assert max(errs) <= 1.5 * HOP_SEC
    assert abs(len(grid.bar_times) - len(truth)) <= 2
    assert res.cadence_cuts is not None and res.cadence_cuts[0] == 0.0
    # legacy output untouched
    assert res.cut_timestamps[0] <= 0.1 and all(
        b > a for a, b in zip(res.cut_timestamps, res.cut_timestamps[1:])
    )
    assert all(b.frame_idx >= 0 for b in res.beats)


def test_engine_off_keeps_legacy_result(tmp_path: Path) -> None:
    wav = tmp_path / "c.wav"
    write_click_track(wav, 120, 4, 30.0)
    cfg = PipelineConfig(enable_cache=False, use_cadence_engine=False, cache_dir=tmp_path / "c")
    res = AudioAnalyzer(cfg).analyze(wav)
    assert res.bar_grid is None and res.cadence_cuts is None and res.cut_timestamps


def test_decoupled_frame_idx_is_hop_index(tmp_path: Path) -> None:
    wav = tmp_path / "long.wav"
    write_click_track(wav, 120, 4, 80.0)
    cfg = PipelineConfig(enable_cache=False, cache_dir=tmp_path / "cache", duration_mode="fixed")
    res = AudioAnalyzer(cfg).analyze(wav, target_duration=30.0)
    assert res.extracted_audio_path is not None
    assert res.beats
    for b in res.beats:
        assert b.frame_idx == pytest.approx(b.timestamp * res.sample_rate / 512, abs=1.0)
    assert res.bar_grid is not None
    assert res.bar_grid.bar_times[-1] <= res.duration + 1e-6


def test_analysis_cache_roundtrip_and_old_entries(tmp_path: Path) -> None:
    wav = tmp_path / "c.wav"
    write_click_track(wav, 120, 4, 40.0)
    cfg = PipelineConfig(cache_dir=tmp_path / "cache", enable_cache=True)
    first = AudioAnalyzer(cfg).analyze(wav)
    second = AudioAnalyzer(cfg).analyze(wav)  # served from cache
    assert second.bar_grid == first.bar_grid
    assert second.cadence_cuts == first.cadence_cuts
    # entry written by an older version (no bar_grid / cadence_cuts keys) still loads
    payload = AudioAnalyzer._result_to_json(first)
    payload.pop("bar_grid")
    payload.pop("cadence_cuts")
    old = AudioAnalyzer._result_from_json(json.loads(json.dumps(payload)))
    assert old.bar_grid is None and old.cadence_cuts is None
    assert old.cut_timestamps == first.cut_timestamps
    assert isinstance(old, AudioAnalysisResult)


# ------------------------------------------------------------------ presets / config


def test_every_subgenre_has_profile_and_multiples() -> None:
    assert len(GENRE_PRESETS) == 23
    for key, p in list(GENRE_PRESETS.items()) + [("rough", ROUGH_CUT_PRESET)]:
        profile = CadenceProfile(p.cadence_profile)
        assert p.target_bar_multiples == PROFILE_MULTIPLES[profile], key
        assert p.allow_extended_shots is True
    assert GENRE_PRESETS["travel_cinematic"].cadence_profile == "lingering_cinematic"
    assert GENRE_PRESETS["sports_extreme"].target_bar_multiples == (0.5, 1.0, 2.0)
    assert GENRE_PRESETS["social_tiktok_fast"].cadence_profile == "viral_burst"
    assert GENRE_PRESETS["travel_daily_vlog"].cadence_profile == "balanced_narrative"
    assert {p.cadence_profile for p in GENRE_PRESETS.values()} == {c.value for c in CadenceProfile}


def test_apply_copies_cadence_fields_and_keeps_legacy() -> None:
    p = GENRE_PRESETS["sports_racing"]
    cfg = p.apply(PipelineConfig())
    assert cfg.cadence_profile == "fast_syncopated"
    assert cfg.cadence_multiples == [0.5, 1.0, 2.0]
    assert cfg.standard_min_beat_interval == p.standard_min_beat_interval
    assert cfg.climax_min_beat_interval == p.climax_min_beat_interval


def test_config_defaults() -> None:
    c = PipelineConfig()
    assert (c.cadence_profile, c.use_cadence_engine, c.cadence_seed) == ("balanced_narrative", True, 7)
    assert c.enable_rough_cut_pruning is True
    assert c.extended_shot_threshold_sec == 12.0 and c.max_scene_len_sec == 16.0
    assert c.allow_multi_segment_for_long_shots is True and c.cadence_multiples is None
    assert c.standard_min_beat_interval == 0.80 and c.climax_min_beat_interval == 0.40


# ------------------------------------------------------------------ serialization


def test_bar_grid_dict_roundtrip() -> None:
    secs = four_sections(2.0)
    grid = grid_for(120, 112.0, secs)
    again = bar_grid_from_dict(json.loads(json.dumps(bar_grid_to_dict(grid))))
    assert again == grid
    assert bar_grid_to_dict(None) is None and bar_grid_from_dict(None) is None


def test_edit_types_defaults_and_positional_construction() -> None:
    clip = EditClip(Path("a.mp4"), 1.0, 30, 5.0)
    assert clip.scene_start is None and clip.scene_end is None
    tl = EditTimeline("t", 30.0, 1920, 1080)
    assert tl.bar_grid is None and tl.cut_snap_error_max_sec is None
    assert MusicalBarGrid(120.0).beats_per_bar == 4


def test_timeline_roundtrip_with_new_fields() -> None:
    grid = grid_for(120, 30.0, [])
    clip = EditClip(Path("a.mp4"), 2.0, 60, 9.0, scene_start=1.5, scene_end=11.0)
    tl = EditTimeline("t", 30.0, 1920, 1080, clips=[clip], bar_grid=grid, cut_snap_error_max_sec=0.012)
    back = edit_timeline_from_dict(json.loads(json.dumps(edit_timeline_to_dict(tl))))
    assert back.bar_grid == grid and back.cut_snap_error_max_sec == 0.012
    assert (back.clips[0].scene_start, back.clips[0].scene_end) == (1.5, 11.0)


def test_old_project_dicts_load_unchanged() -> None:
    old_clip = {"source_path": "a.mp4", "source_in": 1.0, "frames": 30, "available": 4.0}
    c = edit_clip_from_dict(old_clip)
    assert c.scene_start is None and c.scene_end is None
    assert "scene_start" in edit_clip_to_dict(c)
    tl = edit_timeline_from_dict({"name": "x", "fps": 24.0, "clips": [old_clip]})
    assert tl.bar_grid is None and tl.cut_snap_error_max_sec is None and len(tl.clips) == 1
