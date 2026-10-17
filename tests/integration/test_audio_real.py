"""Real-ffmpeg soundtrack tests: decoupled sections must keep native quality."""

from src.core.config import PipelineConfig
from src.modules.audio_analyzer import AudioAnalyzer
from tests.integration.conftest import stream_info


def test_coherent_section_keeps_native_rate_and_stereo(tmp_path, make_tone):
    music = make_tone("song.wav", duration=12.0, sample_rate=44100)
    an = AudioAnalyzer(PipelineConfig(cache_dir=tmp_path / "cache"))
    out, start, end, _sections, _cuts = an.extract_coherent_section(music, target_duration=5.0)
    info = stream_info(out, "audio")
    assert int(info["sample_rate"]) == 44100
    assert int(info["channels"]) == 2
    assert abs(float(info["duration"]) - 5.0) < 0.05
    assert abs((end - start) - 5.0) < 1e-6


def test_mp3_soundtrack_decodes_without_audioread(tmp_path, make_tone):
    import subprocess

    wav = make_tone("song.wav", duration=4.0)
    mp3 = wav.with_suffix(".mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), str(mp3)], check=True)
    an = AudioAnalyzer(PipelineConfig(cache_dir=tmp_path / "cache"))
    y, sr = an._load_audio(mp3)
    assert sr == 22050
    assert abs(len(y) / sr - 4.0) < 0.1
    assert list((tmp_path / "cache" / "analysis_audio").glob("*.wav"))
