"""NLE exports must link to the user's original media, not the CFR proxy in .cache."""

import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

from src.core.config import PipelineConfig
from src.core.media_info import probe_media
from src.core.project_io import edit_timeline_from_dict, edit_timeline_to_dict
from src.core.types import EditClip, EditTimeline, SceneCut, ScoredScene
from src.modules.edit_timeline import clip_from_scene
from src.modules.ingest_detector import IngestDetector
from src.modules.timeline_builder import TimelineBuilder
from tests.integration.conftest import FFMPEG


def _make_vfr(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=160x90:rate=30:duration=3",
         "-vf", "select='lt(mod(n,5),3)'", "-fps_mode", "vfr",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out)],
        check=True,
    )
    return out


def _url_path(url: str) -> Path:
    raw = unquote(urlparse(url).path)
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


def _video_urls(otio_tl) -> list[str]:
    return [c.media_reference.target_url for c in otio_tl.video_tracks()[0] if hasattr(c, "media_reference")]


def test_vfr_proxy_maps_back_to_original_through_ingest_and_otio(tmp_path):
    original = _make_vfr(tmp_path / "footage" / "phone.mp4")
    assert probe_media(original).is_vfr
    cfg = PipelineConfig(cache_dir=tmp_path / ".cache", enable_dead_air_detection=False, max_workers=1)
    det = IngestDetector(cfg)
    proxy = det.normalize_to_cfr(original, 30.0)
    assert proxy != original and not probe_media(proxy).is_vfr
    assert det.proxy_to_original[proxy] == original

    scenes = det.batch_process_videos([proxy])
    assert scenes, "synthetic clip should yield at least one scene"
    assert all(s.clip_path == proxy and s.original_path == original for s in scenes)

    sc = ScoredScene(scene=scenes[0], sharpness_score=1.0, aesthetic_score=1.0, composite_score=1.0)
    clip = clip_from_scene(sc, 30)
    assert clip.source_path == proxy and clip.original_path == original

    tl = EditTimeline("T", 30.0, 1920, 1080, [clip])
    # survives project save/load
    again = edit_timeline_from_dict(edit_timeline_to_dict(tl))
    assert again.clips[0].original_path == original

    builder = TimelineBuilder(cfg)
    linked = _video_urls(builder.build_from_edit_timeline(tl))
    assert [_url_path(u) for u in linked] == [original.resolve()]
    assert ".cache" not in linked[0]
    default = _video_urls(builder.build_from_edit_timeline(tl, relink_original=True))
    assert default == linked
    proxied = _video_urls(builder.build_from_edit_timeline(tl, relink_original=False))
    assert [_url_path(u) for u in proxied] == [proxy.resolve()]


def test_clips_without_original_are_unchanged(tmp_path, make_clip):
    plain = make_clip("plain.mp4", duration=1.0)
    scene = SceneCut(plain, 0.0, 1.0, 0, 30, 1.0)
    assert scene.original_path is None  # default keeps positional constructors working
    clip = clip_from_scene(ScoredScene(scene, 1.0, 1.0, 1.0), 30)
    assert clip.original_path is None
    tl = EditTimeline("T", 30.0, 1920, 1080, [clip])
    urls = _video_urls(TimelineBuilder(PipelineConfig()).build_from_edit_timeline(tl))
    assert _url_path(urls[0]) == plain.resolve()


def test_relink_applies_to_camera_audio_track(tmp_path, make_clip):
    proxy = make_clip("proxy_cfr.mp4", duration=1.0)
    original = make_clip("orig.mp4", duration=1.0)
    tl = EditTimeline("T", 30.0, 1920, 1080, [EditClip(proxy, 0.0, 30, 1.0, original_path=original)])
    otio_tl = TimelineBuilder(PipelineConfig()).build_from_edit_timeline(tl)
    audio_urls = [c.media_reference.target_url for c in otio_tl.audio_tracks()[0] if hasattr(c, "media_reference")]
    assert [_url_path(u) for u in audio_urls] == [original.resolve()]


def test_rough_cut_clips_keep_original(tmp_path):
    from src.core.types import CleanedSegment
    from src.modules.edit_timeline import build_rough_cut_edit_timeline

    proxy, original = tmp_path / "cache" / "a_cfr30.mkv", tmp_path / "a.mov"
    scene = SceneCut(proxy, 0.0, 2.0, 0, 60, 2.0, original_path=original)
    seg = CleanedSegment(scene=scene, clip_path=proxy, start_time=0.0, end_time=2.0, duration=2.0,
                         source_start_time=0.0, source_end_time=2.0)
    tl = build_rough_cut_edit_timeline(PipelineConfig(), [seg])
    assert tl.clips[0].source_path == proxy and tl.clips[0].original_path == original
