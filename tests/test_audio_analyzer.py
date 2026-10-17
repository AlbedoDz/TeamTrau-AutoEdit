"""Unit tests for audio analyzer rhythm tracking and interval filtering."""

import numpy as np
import pytest
import soundfile as sf

from src.core.config import PipelineConfig
from src.core.types import BeatMarker
from src.modules.audio_analyzer import AudioAnalyzer


@pytest.fixture
def synthetic_audio_path(tmp_path):
    """Generate a clean 120 BPM synthetic click track for deterministic audio testing."""
    sr = 22050
    duration_sec = 4.0
    total_samples = int(sr * duration_sec)
    audio = np.zeros(total_samples, dtype=np.float32)

    # 120 BPM = 2 beats per second = clicks every 0.5s (11025 samples)
    beat_interval = int(sr * 0.5)
    for b in range(0, total_samples, beat_interval):
        # Click sound (decaying sine pulse)
        click_len = min(sr // 20, total_samples - b)
        t = np.linspace(0, 0.05, click_len, False)
        pulse = np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 100)
        audio[b : b + click_len] += pulse.astype(np.float32)

    audio_file = tmp_path / "test_120bpm.wav"
    sf.write(str(audio_file), audio, sr)
    return audio_file


def test_audio_analyzer_tempo_and_cuts(synthetic_audio_path):
    """Verify BPM detection and generated cut sequence on synthetic click track."""
    cfg = PipelineConfig()
    analyzer = AudioAnalyzer(cfg)
    res = analyzer.analyze(synthetic_audio_path)

    # Estimated tempo should be near 120 BPM (+- 10 BPM)
    assert 100.0 <= res.tempo_bpm <= 140.0
    assert len(res.cut_timestamps) >= 3
    assert res.duration == pytest.approx(4.0, abs=0.1)


def test_filter_micro_cuts_strictly_enforces_min_interval():
    """Verify that markers closer than min_beat_interval (0.4s) are pruned."""
    cfg = PipelineConfig(min_beat_interval=0.4)
    analyzer = AudioAnalyzer(cfg)

    raw_markers = [
        BeatMarker(timestamp=0.0, frame_idx=0, strength=1.0, is_downbeat=True),
        BeatMarker(timestamp=0.1, frame_idx=10, strength=0.3, is_downbeat=False),  # < 0.4s, drop
        BeatMarker(timestamp=0.25, frame_idx=25, strength=0.4, is_downbeat=False),  # < 0.4s, drop
        BeatMarker(timestamp=0.5, frame_idx=50, strength=0.8, is_downbeat=False),  # Keep
        BeatMarker(
            timestamp=0.6, frame_idx=60, strength=0.9, is_downbeat=True
        ),  # < 0.4s, but downbeat
        BeatMarker(timestamp=1.1, frame_idx=110, strength=0.8, is_downbeat=False),  # Keep
    ]

    filtered = analyzer._filter_micro_cuts(raw_markers, min_interval=0.4)

    # Check all consecutive intervals are >= 0.4s
    for i in range(len(filtered) - 1):
        interval = filtered[i + 1].timestamp - filtered[i].timestamp
        assert interval >= 0.4 - 1e-6


def test_onset_strength_computed_once_per_analysis(synthetic_audio_path, tmp_path, monkeypatch):
    import librosa

    calls = {"n": 0}
    real = librosa.onset.onset_strength

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(librosa.onset, "onset_strength", counting)
    cfg = PipelineConfig(cache_dir=tmp_path / "c", enable_cache=False)
    AudioAnalyzer(cfg).analyze(synthetic_audio_path)
    assert calls["n"] == 1


def test_audio_analysis_result_cached_on_disk(synthetic_audio_path, tmp_path, monkeypatch):
    cfg = PipelineConfig(cache_dir=tmp_path / "c")
    first = AudioAnalyzer(cfg).analyze(synthetic_audio_path)

    def boom(*a, **k):
        raise AssertionError("audio was re-analysed despite a cache hit")

    monkeypatch.setattr(AudioAnalyzer, "_load_audio", boom)
    second = AudioAnalyzer(cfg).analyze(synthetic_audio_path)
    assert second == first
    assert second.sections and second.sections[0].section_type == first.sections[0].section_type

    # A changed relevant parameter (duration mode/target) is a different cache key.
    with pytest.raises(AssertionError):
        AudioAnalyzer(cfg).analyze(synthetic_audio_path, target_duration=2.0, duration_mode="fixed")

    # Cache can be bypassed.
    with pytest.raises(AssertionError):
        AudioAnalyzer(cfg.model_copy(update={"enable_cache": False})).analyze(synthetic_audio_path)
