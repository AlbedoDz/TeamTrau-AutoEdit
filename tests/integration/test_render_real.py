"""Real FFmpeg render tests (CPU encoder, tiny canvas) guarding audio/video integrity."""

from pathlib import Path

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene
from src.dispatchers.nvenc_renderer import NvencRenderer
from tests.integration.conftest import stream_durations, stream_info

FRAME = 1.0 / 30.0


def _cfg(**kw) -> PipelineConfig:
    base = dict(
        canvas_width=320,
        canvas_height=180,
        fps=30.0,
        enable_teaser=False,
        keep_video_audio=False,
        cache_dir=Path(kw.pop("cache_dir")),
    )
    base.update(kw)
    return PipelineConfig(**base)


def _scene(path: Path, start: float, dur: float, speed: float = 1.0) -> ScoredScene:
    return ScoredScene(
        scene=SceneCut(path, start, start + dur, int(start * 30), int((start + dur) * 30), dur),
        sharpness_score=1.0,
        aesthetic_score=1.0,
        composite_score=1.0,
        speed=speed,
    )


def _assert_av_aligned(out: Path, expected: float) -> None:
    durs = stream_durations(out)
    assert "video" in durs and "audio" in durs, f"missing stream: {durs}"
    assert abs(durs["video"] - expected) <= 3 * FRAME, durs
    assert abs(durs["audio"] - durs["video"]) <= 3 * FRAME + 0.05, durs


def test_render_clip_without_audio_and_no_soundtrack(tmp_path, make_clip):
    """Silent drone/screen clips must render with a generated silent track."""
    clip = make_clip("silent.mp4", duration=3.0, audio=False)
    out = tmp_path / "out" / "master.mp4"
    r = NvencRenderer(_cfg(cache_dir=tmp_path / "cache"))
    r.render_sequence([_scene(clip, 0.0, 3.0)], [0.0, 1.0, 2.0], None, out, force_cpu=True)
    _assert_av_aligned(out, 2.0)


def test_render_speed_ramp_keeps_camera_audio_in_sync(tmp_path, make_clip):
    """2x speed ramp must also speed up camera audio (atempo), not let it drift."""
    clip = make_clip("fast.mp4", duration=6.0)
    out = tmp_path / "out" / "master.mp4"
    r = NvencRenderer(_cfg(cache_dir=tmp_path / "cache"))
    r.render_sequence(
        [_scene(clip, 0.0, 6.0, speed=2.0)], [0.0, 1.0, 2.0], None, out, force_cpu=True
    )
    _assert_av_aligned(out, 2.0)


def test_render_unicode_path_and_mixed_audio_formats(tmp_path, make_clip):
    """Vietnamese file names plus 44.1k mono and 48k stereo sources concat cleanly."""
    a = make_clip("Đà Nẵng clip.mp4", duration=2.0, sample_rate=44100, channels=1)
    b = make_clip("Hội An.mp4", duration=2.0, sample_rate=48000, channels=2)
    out = tmp_path / "out" / "Kết quả.mp4"
    r = NvencRenderer(_cfg(cache_dir=tmp_path / "cache"))
    r.render_sequence(
        [_scene(a, 0.0, 2.0), _scene(b, 0.0, 2.0)], [0.0, 1.5, 3.0], None, out, force_cpu=True
    )
    _assert_av_aligned(out, 3.0)
    a_info = stream_info(out, "audio")
    assert int(a_info.get("sample_rate", 0)) == 48000
    assert int(a_info.get("channels", 0)) == 2


def test_short_clip_keeps_later_cuts_on_beat(tmp_path, make_clip, make_tone):
    """A scene shorter than its beat slot must not shorten the whole video."""
    short = make_clip("short.mp4", duration=1.0)
    out = tmp_path / "out" / "master.mp4"
    music = make_tone(duration=6.0)
    r = NvencRenderer(_cfg(cache_dir=tmp_path / "cache"))
    r.render_sequence([_scene(short, 0.0, 1.0)], [0.0, 2.0, 4.0], music, out, force_cpu=True)
    _assert_av_aligned(out, 4.0)


def test_render_with_soundtrack_keeps_native_quality(tmp_path, make_clip, make_tone):
    clip = make_clip("a.mp4", duration=3.0)
    music = make_tone(duration=5.0, sample_rate=44100)
    out = tmp_path / "out" / "master.mp4"
    r = NvencRenderer(_cfg(cache_dir=tmp_path / "cache"))
    r.render_sequence([_scene(clip, 0.0, 3.0)], [0.0, 1.5, 3.0], music, out, force_cpu=True)
    _assert_av_aligned(out, 3.0)
    assert int(stream_info(out, "audio").get("channels", 0)) == 2


def test_failed_render_leaves_no_partial_master(tmp_path, make_clip):
    """A failing render must not leave a corrupt master file behind."""
    clip = make_clip("a.mp4", duration=2.0)
    out = tmp_path / "out" / "master.mp4"
    r = NvencRenderer(_cfg(cache_dir=tmp_path / "cache", nvenc_pix_fmt="not_a_pix_fmt"))
    try:
        r.render_sequence([_scene(clip, 0.0, 2.0)], [0.0, 1.0], None, out, force_cpu=True)
    except RuntimeError:
        pass
    assert not out.exists()
    assert not list(out.parent.glob("*.part*"))
