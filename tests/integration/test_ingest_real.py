"""Real-ffmpeg ingest tests: unicode paths, rotation, silent clips, CFR cache keys."""

from pathlib import Path

from src.core.config import PipelineConfig
from src.modules.ingest_detector import IngestDetector


def test_probe_reports_display_rotation_and_audio(tmp_path, make_clip):
    det = IngestDetector(PipelineConfig(cache_dir=tmp_path / "cache"))
    rotated = make_clip("iphone.mov", duration=1.0, rotation=90, size="160x90")
    meta = det.probe_video(rotated)
    assert meta.rotation in (90, 270)
    assert (meta.width, meta.height) == (90, 160)
    assert meta.has_audio is True
    assert abs(meta.duration - 1.0) < 0.1

    silent = make_clip("drone.mp4", duration=1.0, audio=False)
    assert det.probe_video(silent).has_audio is False


def test_silence_detection_survives_vietnamese_filename(tmp_path, make_clip):
    """ffmpeg echoes the unicode path on stderr; decoding must not crash."""
    det = IngestDetector(PipelineConfig(cache_dir=tmp_path / "cache", dead_air_min_duration=0.5))
    clip = make_clip("Phỏng vấn Đà Lạt.mp4", duration=1.5)
    assert det.detect_silence_ranges(clip) == []
    assert isinstance(det.detect_freeze_ranges(clip), list)


def test_cfr_cache_distinguishes_same_named_files(tmp_path, make_clip):
    det = IngestDetector(PipelineConfig(cache_dir=tmp_path / "cache"))
    a = make_clip("A/IMG_0001.mp4", duration=1.0)
    b = make_clip("B/IMG_0001.mp4", duration=1.5)
    out_a = det.normalize_to_cfr(a, 30.0)
    out_b = det.normalize_to_cfr(b, 30.0)
    assert out_a != out_b
    assert Path(out_a).is_relative_to(tmp_path / "cache")
    assert det.probe_video(out_b).duration > det.probe_video(out_a).duration
    assert not list((tmp_path / "cache").rglob("*.part*"))
