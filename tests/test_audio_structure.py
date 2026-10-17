"""Unit tests for music structural energy-sync, section classification, and duration decoupling."""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from src.core.config import PipelineConfig
from src.core.types import SongSectionType
from src.modules.audio_analyzer import AudioAnalyzer


@pytest.fixture
def structured_audio_path(tmp_path: Path) -> Path:
    """Generate a 16-second synthetic song with clear INTRO, BUILDUP, DROP, and OUTRO sections."""
    sr = 22050
    duration = 16.0
    total_samples = int(sr * duration)
    t = np.linspace(0, duration, total_samples, endpoint=False)
    y = np.zeros(total_samples, dtype=np.float32)

    # 1. INTRO (0s - 4s): Soft gentle ambient pad (low amplitude)
    intro_mask = t < 4.0
    y[intro_mask] = 0.10 * np.sin(2 * np.pi * 220 * t[intro_mask])

    # 2. BUILDUP (4s - 8s): Rising volume and accelerated snare/click pulses
    buildup_mask = (t >= 4.0) & (t < 8.0)
    t_b = t[buildup_mask] - 4.0
    ramp = 0.15 + 0.55 * (t_b / 4.0)
    # Sine carrier with rising ramp
    y[buildup_mask] = ramp * np.sin(2 * np.pi * (220 + 50 * t_b) * t_b)
    # Add fast click pulses to simulate riser
    step = int(sr * 0.25)
    for sample_idx in range(int(4.0 * sr), int(8.0 * sr), step):
        p_len = min(sr // 50, len(y) - sample_idx)
        y[sample_idx : sample_idx + p_len] += 0.5 * np.ones(p_len)

    # 3. DROP (8s - 13s): High energy heavy kick pulses and high-amplitude chorus
    drop_mask = (t >= 8.0) & (t < 13.0)
    t_d = t[drop_mask]
    y[drop_mask] = 0.85 * np.sin(2 * np.pi * 110 * t_d) + 0.3 * np.sign(
        np.sin(2 * np.pi * 440 * t_d)
    )
    # Powerful beat pulses every 0.5s (120 BPM)
    beat_step = int(sr * 0.5)
    for sample_idx in range(int(8.0 * sr), int(13.0 * sr), beat_step):
        p_len = min(sr // 20, len(y) - sample_idx)
        p_t = np.linspace(0, 0.05, p_len, endpoint=False)
        y[sample_idx : sample_idx + p_len] += 0.9 * np.sin(2 * np.pi * 60 * p_t)

    # 4. OUTRO (13s - 16s): Decaying gentle release
    outro_mask = t >= 13.0
    t_o = t[outro_mask] - 13.0
    decay = 0.4 * np.exp(-t_o)
    y[outro_mask] = decay * np.sin(2 * np.pi * 220 * t_o)

    # Normalize to prevent hard clipping
    y = y / (np.max(np.abs(y)) + 1e-6)

    audio_file = tmp_path / "test_structured_song.wav"
    sf.write(str(audio_file), y.astype(np.float32), sr)
    return audio_file


def test_compute_energy_profile(structured_audio_path: Path):
    """Verify continuous normalized RMS, novelty profile, and chroma computation."""
    cfg = PipelineConfig()
    analyzer = AudioAnalyzer(cfg)
    y, sr = analyzer._load_audio(structured_audio_path)

    times, smooth_energy, norm_rms, norm_onset, chroma = analyzer.compute_energy_profile(y, sr)

    assert len(times) == len(smooth_energy) == len(norm_rms) == len(norm_onset)
    assert len(times) > 0
    assert chroma.shape[0] == 12
    assert chroma.shape[1] == len(times)
    assert 0.0 <= np.min(smooth_energy) <= np.max(smooth_energy) <= 1.0
    # Peak energy must occur during DROP window (8s - 13s)
    peak_time = times[np.argmax(smooth_energy)]
    assert 7.0 <= peak_time <= 14.0


def test_detect_song_sections_structure(structured_audio_path: Path):
    """Verify track is correctly partitioned into INTRO, BUILDUP, DROP, and OUTRO."""
    cfg = PipelineConfig()
    analyzer = AudioAnalyzer(cfg)

    sections = analyzer.detect_song_sections(structured_audio_path)

    assert len(sections) == 4
    assert sections[0].section_type == SongSectionType.INTRO
    assert sections[1].section_type == SongSectionType.BUILDUP
    assert sections[2].section_type == SongSectionType.DROP
    assert sections[3].section_type == SongSectionType.OUTRO

    # Sections must span [0, 16] continuously
    assert sections[0].start_time == pytest.approx(0.0, abs=0.1)
    assert sections[3].end_time == pytest.approx(16.0, abs=0.1)

    # Verify chronological sequence
    for i in range(len(sections) - 1):
        assert sections[i].end_time == pytest.approx(sections[i + 1].start_time, abs=0.05)

    # DROP must have higher average energy than INTRO and OUTRO
    drop_sec = sections[2]
    intro_sec = sections[0]
    outro_sec = sections[3]
    assert drop_sec.avg_energy > intro_sec.avg_energy
    assert drop_sec.avg_energy > outro_sec.avg_energy


def test_decoupled_duration_extraction(tmp_path: Path, structured_audio_path: Path):
    """Verify decoupled duration extraction captures peak musical segment with parametric fade-out."""
    cfg = PipelineConfig(cache_dir=tmp_path / "cache", audio_fade_out_sec=2.0)
    analyzer = AudioAnalyzer(cfg)

    target_dur = 6.0
    res = analyzer.analyze(structured_audio_path, target_duration=target_dur)

    assert res.duration == pytest.approx(target_dur, abs=0.2)
    assert res.extracted_audio_path is not None
    assert res.extracted_audio_path.is_file()

    # Verify cuts are bounded inside [0, target_dur]
    assert len(res.cut_timestamps) >= 2
    assert res.cut_timestamps[0] == pytest.approx(0.0, abs=0.1)
    assert res.cut_timestamps[-1] == pytest.approx(target_dur, abs=0.2)

    # Verify parametric fade-out on exported audio file
    data, sr = sf.read(str(res.extracted_audio_path))
    actual_dur = len(data) / sr
    assert actual_dur == pytest.approx(target_dur, abs=0.2)
    # The very last sample should be attenuated near zero due to cosine fade-out
    assert abs(data[-1]) < 0.05


def test_song_section_chroma_and_climax(structured_audio_path: Path):
    """Verify song sections contain 12-dim chroma profiles and climax flag on DROP."""
    cfg = PipelineConfig()
    analyzer = AudioAnalyzer(cfg)

    sections = analyzer.detect_song_sections(structured_audio_path)
    assert len(sections) == 4
    for sec in sections:
        assert len(sec.chroma_profile) == 12
        if sec.section_type == SongSectionType.DROP:
            assert sec.is_climax is True
        else:
            assert sec.is_climax is False


def test_filter_beats_by_structure():
    """Verify standard sections enforce 0.8s interval while DROP allows 0.4s interval."""
    from src.core.types import BeatMarker, SongSection, SongSectionType

    cfg = PipelineConfig(standard_min_beat_interval=0.80, climax_min_beat_interval=0.40)
    analyzer = AudioAnalyzer(cfg)

    sections = [
        SongSection(
            section_type=SongSectionType.INTRO,
            start_time=0.0,
            end_time=5.0,
            duration=5.0,
            avg_energy=0.2,
            is_climax=False,
        ),
        SongSection(
            section_type=SongSectionType.DROP,
            start_time=5.0,
            end_time=10.0,
            duration=5.0,
            avg_energy=0.9,
            is_climax=True,
        ),
    ]

    # Create raw beats spaced by 0.4s: [0.0, 0.4, 0.8, 1.2, 1.6, ..., 4.8, 5.0, 5.4, 5.8, ...]
    raw_markers = [
        BeatMarker(timestamp=round(t, 2), frame_idx=int(t * 30), strength=1.0, is_downbeat=(i % 4 == 0))
        for i, t in enumerate([x * 0.4 for x in range(25)])
    ]

    filtered = analyzer.filter_beats_by_structure(raw_markers, sections)

    # In INTRO (< 5.0s), intervals between consecutive filtered beats must be >= 0.8s
    intro_beats = [m.timestamp for m in filtered if m.timestamp < 5.0]
    for i in range(len(intro_beats) - 1):
        assert (intro_beats[i + 1] - intro_beats[i]) >= 0.79

    # In DROP (5.0s to 10.0s), intervals can be ~0.4s
    drop_beats = [m.timestamp for m in filtered if 5.0 <= m.timestamp <= 10.0]
    intervals = [drop_beats[i + 1] - drop_beats[i] for i in range(len(drop_beats) - 1)]
    assert any(pytest.approx(inter, abs=0.05) == 0.40 for inter in intervals)


def test_solve_footage_driven_duration():
    """Verify footage duration snaps to closest musical downbeat."""
    cfg = PipelineConfig()
    analyzer = AudioAnalyzer(cfg)

    # Downbeats every 2.0s
    downbeats = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0]
    total_audio_duration = 15.0

    # Case 1: 5.8s footage should snap to 6.0s downbeat
    dur1 = analyzer.solve_footage_driven_duration(5.8, downbeats, total_audio_duration)
    assert dur1 == 6.0

    # Case 2: 4.1s footage should snap to 4.0s downbeat
    dur2 = analyzer.solve_footage_driven_duration(4.1, downbeats, total_audio_duration)
    assert dur2 == 4.0

    # Case 3: 20.0s footage exceeds audio, should cap at audio duration
    dur3 = analyzer.solve_footage_driven_duration(20.0, downbeats, total_audio_duration)
    assert dur3 == 15.0
