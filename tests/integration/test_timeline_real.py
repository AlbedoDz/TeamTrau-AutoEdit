"""Real-media OTIO checks: ranges must describe the actual source file."""

from pathlib import Path

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene
from src.modules.timeline_builder import TimelineBuilder


def _scene(path: Path, start: float, dur: float) -> ScoredScene:
    return ScoredScene(
        scene=SceneCut(path, start, start + dur, 0, 0, dur),
        sharpness_score=1.0,
        aesthetic_score=1.0,
        composite_score=1.0,
    )


def test_source_range_inside_available_range_and_beats_kept(tmp_path, make_clip, make_tone):
    clip = make_clip("long.mp4", duration=4.0)
    short = make_clip("short.mp4", duration=1.0)
    music = make_tone(duration=8.0)
    cfg = PipelineConfig(fps=30.0, enable_teaser=False, enable_exposure_guard=False,
                         enable_ken_burns=False, cache_dir=tmp_path / "cache")
    tl = TimelineBuilder(cfg).build_timeline(
        [_scene(clip, 2.5, 1.5), _scene(short, 0.0, 1.0)], [0.0, 1.0, 3.0], music
    )
    v1 = tl.tracks[0]
    for item in v1:
        if hasattr(item, "media_reference") and item.media_reference is not None:
            avail = item.media_reference.available_range
            src = item.source_range
            assert avail.start_time.to_seconds() <= src.start_time.to_seconds()
            assert src.end_time_exclusive().to_seconds() <= avail.end_time_exclusive().to_seconds() + 1e-6
            assert abs(avail.duration.to_seconds() - 4.0) < 0.1 or abs(avail.duration.to_seconds() - 1.0) < 0.1
    assert abs(v1.duration().to_seconds() - 3.0) < 1e-6
