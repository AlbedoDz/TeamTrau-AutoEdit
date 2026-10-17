"""Unit tests for the segment-based NVENC renderer command and filter builders.

Real end-to-end ffmpeg execution is covered in tests/integration/test_render_real.py.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene, SongSection, SongSectionType
from src.dispatchers.nvenc_renderer import NvencRenderer, RenderSegment


@pytest.fixture
def sample_scenes():
    return [
        ScoredScene(
            scene=SceneCut(Path("e:/raw/clip1.mp4"), 0.0, 3.0, 0, 90, 3.0),
            sharpness_score=100.0,
            aesthetic_score=0.9,
            composite_score=0.95,
        ),
        ScoredScene(
            scene=SceneCut(Path("e:/raw/clip2.mp4"), 1.0, 4.0, 30, 120, 3.0),
            sharpness_score=90.0,
            aesthetic_score=0.8,
            composite_score=0.85,
        ),
    ]


def test_nvenc_renderer_validates_empty_inputs():
    """Verify ValueError is raised on empty scene or cut list."""
    renderer = NvencRenderer(PipelineConfig())

    with pytest.raises(ValueError, match="No scenes provided"):
        renderer.render_sequence([], [0.0, 2.0], Path("bgm.wav"), Path("out.mp4"))

    with pytest.raises(ValueError, match="at least 2 boundary"):
        renderer.render_sequence([MagicMock()], [0.0], Path("bgm.wav"), Path("out.mp4"))


def test_segments_are_frame_exact(sample_scenes):
    """Beat cuts are quantised to whole frames so long timelines never drift."""
    cfg = PipelineConfig(fps=30.0, enable_teaser=False)
    segs = NvencRenderer(cfg).build_segments(sample_scenes, [0.0, 0.517, 1.033, 3.0])
    assert sum(s.frames for s in segs) == 90
    assert [s.frames for s in segs] == [16, 15, 59]


def test_nvenc_encoder_args_use_config():
    cfg = PipelineConfig(nvenc_preset="p7", nvenc_cq=18, nvenc_bitrate="60M")
    enc, args = NvencRenderer(cfg).encoder_args(use_nvenc=True)
    assert enc == "hevc_nvenc"
    assert args[args.index("-preset") + 1] == "p7"
    assert args[args.index("-cq") + 1] == "18"
    assert args[args.index("-b:v") + 1] == "60M"


def test_cpu_fallback_encoder_is_libx265():
    enc, args = NvencRenderer(PipelineConfig()).encoder_args(use_nvenc=False)
    assert enc == "libx265"
    assert "libx265" in args


def test_preview_uses_fast_h264_at_540p():
    cfg = PipelineConfig(canvas_width=1920, canvas_height=1080)
    r = NvencRenderer(cfg)
    assert r.canvas_size(preview=True) == (960, 540)
    enc, _ = r.encoder_args(use_nvenc=True, preview=True)
    assert enc == "h264_nvenc"


def test_segment_command_fast_seeks_each_clip(sample_scenes):
    """Each segment seeks its own input instead of trimming from frame 0."""
    cfg = PipelineConfig(enable_teaser=False)
    r = NvencRenderer(cfg)
    seg = r.build_segments(sample_scenes, [0.0, 1.5, 3.0])[1]
    cmd = r.build_segment_command("ffmpeg", seg, Path("seg.mkv"), True, with_audio=False)
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == f"{seg.source_in:.3f}"
    assert "hevc_nvenc" in cmd
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert f"trim=end_frame={seg.frames}" in graph


def test_teaser_adds_leading_segments(sample_scenes):
    cfg = PipelineConfig(enable_teaser=True, teaser_duration=4.0, fps=30.0)
    segs = NvencRenderer(cfg).build_segments(
        sample_scenes, [0.0, 3.0, 6.0, 9.0, 12.0, 15.0], include_teaser=True
    )
    assert sum(s.frames for s in segs) == 450
    assert len(segs) >= 3 + 4


def test_sections_drive_scene_selection(sample_scenes):
    cfg = PipelineConfig(enable_teaser=False)
    sections = [
        SongSection(SongSectionType.INTRO, 0.0, 2.0, 2.0, 0.2, is_climax=False),
        SongSection(SongSectionType.DROP, 2.0, 4.0, 2.0, 0.9, is_climax=True),
    ]
    segs = NvencRenderer(cfg).build_segments(sample_scenes, [0.0, 2.0, 4.0], sections=sections)
    assert len(segs) == 2


def test_audio_filter_speed_ramp_and_silent_sources():
    cfg = PipelineConfig(fps=30.0)
    r = NvencRenderer(cfg)
    seg = RenderSegment(Path("a.mp4"), 0.0, frames=30, available=5.0, speed=3.0)
    graph = r.build_segment_filter(seg, (320, 180), with_audio=True, source_has_audio=True)
    assert "setpts=(PTS-STARTPTS)/3.0000" in graph
    assert "atempo=2.000000,atempo=1.500000" in graph
    assert "aresample=48000" in graph
    silent = r.build_segment_filter(seg, (320, 180), with_audio=True, source_has_audio=False)
    assert "anullsrc=r=48000:cl=stereo" in silent


def test_concat_command_mixes_soundtrack_and_copies_video():
    r = NvencRenderer(PipelineConfig(workflow_mode="standard"))
    cmd = r.build_concat_command(
        "ffmpeg", Path("list.txt"), Path("out.mp4.part"), 10.0, True, Path("music.wav")
    )
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in graph
    assert cmd[cmd.index("-f", cmd.index("-t")) + 1] == "mp4"


def test_default_nvenc_preset_is_p5_and_p7_selectable():
    assert PipelineConfig().nvenc_preset == "p5"
    _, args = NvencRenderer(PipelineConfig(nvenc_preset="p7")).encoder_args(use_nvenc=True)
    assert args[args.index("-preset") + 1] == "p7"


def _one_segment(sample_scenes):
    cfg = PipelineConfig(fps=30.0, enable_teaser=False)
    seg = NvencRenderer(cfg).build_segments(sample_scenes, [0.0, 2.0])[0]
    return cfg, seg


def test_hwdecode_flag_only_on_nvenc_path(sample_scenes):
    cfg, seg = _one_segment(sample_scenes)
    r = NvencRenderer(cfg)
    hw = r.build_segment_command("ffmpeg", seg, Path("o.mkv"), True, False, hwdecode=True)
    assert hw[hw.index("-hwaccel") + 1] == "cuda"
    # Frames must stay in system memory: CPU filters (zoompan/overlay/...) rely on it.
    assert "-hwaccel_output_format" not in hw
    assert hw.index("-hwaccel") < hw.index("-i")
    assert "-hwaccel" not in r.build_segment_command("ffmpeg", seg, Path("o.mkv"), True, False)
    assert "-hwaccel" not in r.build_segment_command("ffmpeg", seg, Path("o.mkv"), False, False, hwdecode=True)


# ---------------------------------------------------------------------------
# Phase 22 step 6: Deliver master (settings-driven)
# ---------------------------------------------------------------------------
import dataclasses  # noqa: E402
from fractions import Fraction  # noqa: E402

from src.core.media_info import MediaInfo  # noqa: E402
from src.core.types import CropWindow, DeliverSettings, EditClip  # noqa: E402
from src.dispatchers.nvenc_renderer import (  # noqa: E402
    COLOR_TAGS,
    color_conversion_chain,
    color_tag_args,
    master_container,
    plan_units,
)


def _settings(**kw) -> DeliverSettings:
    base = {"canvas_width": 1920, "canvas_height": 1080, "fps": 30.0}
    base.update(kw)
    return DeliverSettings(**base)


def _renderer(settings: DeliverSettings | None = None, **cfg) -> NvencRenderer:
    cfg.setdefault("fps", settings.fps if settings else 30.0)
    if settings:
        cfg.setdefault("canvas_width", settings.canvas_width)
        cfg.setdefault("canvas_height", settings.canvas_height)
    r = NvencRenderer(PipelineConfig(**cfg), settings)
    r.nvenc_available = True
    return r


def _arg(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def _muxer(cmd: list[str]) -> str:
    """Output muxer = the last ``-f`` (the first one selects the concat demuxer)."""
    return cmd[len(cmd) - 1 - cmd[::-1].index("-f") + 1]


def test_master_hevc_main_8bit_and_main10_for_hdr():
    s = _settings(master="hevc", color="rec709")
    enc, args = _renderer(s).master_encoder_args(s, True)
    assert enc == "hevc_nvenc"
    assert _arg(args, "-profile:v") == "main" and _arg(args, "-pix_fmt") == "yuv420p"
    assert _arg(args, "-colorspace") == "bt709" and _arg(args, "-color_trc") == "bt709"

    hdr = _settings(master="hevc", color="rec2020_pq")
    enc, args = _renderer(hdr).master_encoder_args(hdr, True)
    assert _arg(args, "-profile:v") == "main10" and _arg(args, "-pix_fmt") == "yuv420p10le"
    assert _arg(args, "-color_trc") == "smpte2084" and _arg(args, "-colorspace") == "bt2020nc"
    enc, args = _renderer(hdr).master_encoder_args(hdr, False)
    assert enc == "libx265" and _arg(args, "-pix_fmt") == "yuv420p10le"


def test_master_h264_is_always_8bit():
    s = _settings(master="h264", color="rec2020_hlg")
    enc, args = _renderer(s).master_encoder_args(s, True)
    assert enc == "h264_nvenc" and _arg(args, "-pix_fmt") == "yuv420p"
    enc, args = _renderer(s).master_encoder_args(s, False)
    assert enc == "libx264" and _arg(args, "-color_trc") == "arib-std-b67"


@pytest.mark.parametrize(("master", "profile"), [("prores_422", "2"), ("prores_hq", "3")])
def test_master_prores(master, profile):
    s = _settings(master=master)
    for nvenc in (True, False):  # ProRes is CPU-only whatever the GPU says
        enc, args = _renderer(s).master_encoder_args(s, nvenc)
        assert enc == "prores_ks"
        assert _arg(args, "-profile:v") == profile and _arg(args, "-vendor") == "apl0"
        assert _arg(args, "-pix_fmt") == "yuv422p10le"


def test_master_dnxhr_hqx():
    s = _settings(master="dnxhr_hqx")
    enc, args = _renderer(s).master_encoder_args(s, True)
    assert enc == "dnxhd" and _arg(args, "-profile:v") == "dnxhr_hqx"
    assert _arg(args, "-pix_fmt") == "yuv422p10le"


def test_master_encoder_args_none_or_preview_is_legacy():
    r = _renderer()
    assert r.master_encoder_args(None, True) == r.encoder_args(True)
    s = _settings(master="prores_hq")
    assert _renderer(s).master_encoder_args(s, True, preview=True)[0] == "h264_nvenc"


def test_master_container_follows_codec():
    assert master_container(_settings(master="hevc")) == (".mp4", "mp4")
    assert master_container(_settings(master="h264")) == (".mp4", "mp4")
    assert master_container(_settings(master="prores_hq")) == (".mov", "mov")
    assert master_container(_settings(master="dnxhr_hqx")) == (".mov", "mov")
    assert master_container(_settings(master="dnxhr_hqx"), "x/out.mxf") == (".mxf", "mxf")
    assert master_container(None) == (".mp4", "mp4")


def test_color_tags():
    assert color_tag_args("rec709") == [
        "-colorspace", "bt709", "-color_trc", "bt709", "-color_primaries", "bt709", "-color_range", "tv"
    ]
    assert COLOR_TAGS["rec2020_hlg"] == ("bt2020nc", "arib-std-b67", "bt2020", "tv")
    assert COLOR_TAGS["rec2020_pq"] == ("bt2020nc", "smpte2084", "bt2020", "tv")
    assert color_tag_args("passthrough") == []
    info = MediaInfo(
        path=Path("a.mp4"), has_video=True, color_space="bt2020nc",
        color_transfer="smpte2084", color_primaries="bt2020", color_range="tv",
    )
    assert "smpte2084" in color_tag_args("passthrough", info)


def _hdr_info(**kw) -> MediaInfo:
    base = {
        "path": Path("hdr.mp4"), "has_video": True, "is_hdr": True, "color_space": "bt2020nc",
        "color_transfer": "smpte2084", "color_primaries": "bt2020", "color_range": "tv",
    }
    base.update(kw)
    return MediaInfo(**base)


def test_tonemap_chain_prefers_zscale_then_tonemapx_then_nothing():
    chain = color_conversion_chain(_hdr_info(), "rec709", "yuv420p", frozenset({"zscale", "tonemap"}))
    joined = ",".join(chain)
    assert "t=linear:npl=100" in joined and "tonemap=tonemap=hable" in joined
    assert "zscale=p=709:t=709:m=709:r=tv" in joined and chain[-1] == "format=yuv420p"
    x = color_conversion_chain(_hdr_info(), "rec709", "yuv420p", frozenset({"tonemapx"}))
    assert x and x[0].startswith("tonemapx=")
    assert color_conversion_chain(_hdr_info(), "rec709", "yuv420p", frozenset()) == []  # never raises
    # SDR source or passthrough: untouched
    assert color_conversion_chain(_hdr_info(is_hdr=False), "rec709", "yuv420p", frozenset({"zscale"})) == []
    assert color_conversion_chain(_hdr_info(), "passthrough", "yuv420p10le", frozenset({"zscale"})) == []
    assert color_conversion_chain(None, "rec709", "yuv420p") == []


def test_hdr_target_conversions():
    sdr = _hdr_info(is_hdr=False, color_transfer="bt709", color_primaries="bt709", color_space="bt709")
    chain = color_conversion_chain(sdr, "rec2020_hlg", "yuv420p10le", frozenset({"zscale"}))
    joined = ",".join(chain)
    assert "t=linear:npl=100:p=2020" in joined and "zscale=t=arib-std-b67" in joined
    assert "zscale=m=2020_ncl:r=tv" in joined and chain[-1] == "format=yuv420p10le"
    same = _hdr_info(color_transfer="arib-std-b67")
    assert color_conversion_chain(same, "rec2020_hlg", "yuv420p10le", frozenset({"zscale"})) == []
    pq_to_hlg = color_conversion_chain(_hdr_info(), "rec2020_hlg", "yuv420p10le", frozenset({"zscale"}))
    assert any("npl=1000" in c for c in pq_to_hlg)


def test_fps_is_exact_rational_in_filters_and_output_args():
    s = _settings(fps=29.97)
    r = _renderer(s)
    seg = EditClip(Path("a.mp4"), 0.0, frames=30, available=5.0)
    graph = r.build_segment_filter(seg, (320, 180), with_audio=True, source_has_audio=True)
    assert "fps=30000/1001" in graph
    assert "fps=29.97" not in graph
    cmd = r.build_segment_command("ffmpeg", seg, Path("o.mov"), False, False)
    assert cmd[cmd.index("-r") + 1] == "30000/1001"
    # integer rates stay integers
    r30 = _renderer(_settings(fps=30.0))
    assert "fps=30," in r30.build_segment_filter(seg, (320, 180), False, False)


def test_audio_rate_and_channels_follow_settings():
    s = _settings(audio_rate=44100, audio_channels=1)
    r = _renderer(s)
    seg = EditClip(Path("a.mp4"), 0.0, frames=30, available=5.0)
    graph = r.build_segment_filter(seg, (320, 180), True, True)
    assert "aresample=44100" in graph and "channel_layouts=mono" in graph
    assert "anullsrc=r=44100:cl=mono" in r.build_segment_filter(seg, (320, 180), True, False)
    cmd = r.build_concat_command("ffmpeg", Path("l.txt"), Path("o.mp4.part"), 1.0, True, Path("m.wav"))
    assert _arg(cmd, "-ar") == "44100" and _arg(cmd, "-ac") == "1"


def test_concat_audio_codec_and_container_follow_master():
    mp4 = _renderer(_settings(master="hevc"))
    cmd = mp4.build_concat_command("ffmpeg", Path("l.txt"), Path("o.mp4.part"), 2.0, True, Path("m.wav"))
    assert _arg(cmd, "-c:a") == "aac" and _muxer(cmd) == "mp4" and "+faststart" in cmd
    assert "-tag:v" in cmd
    graph = _arg(cmd, "-filter_complex")
    assert "alimiter=limit=0.95:level=disabled" in graph
    assert graph.index("amix") < graph.index("alimiter")
    pro = _renderer(_settings(master="prores_hq"))
    cmd = pro.build_concat_command(
        "ffmpeg", Path("l.txt"), Path("o.mov.part"), 2.0, True, Path("m.wav"), encoder="prores_ks"
    )
    assert _arg(cmd, "-c:a") == "pcm_s24le" and _muxer(cmd) == "mov"
    assert "sample_fmts=s32" in _arg(cmd, "-filter_complex") and "-tag:v" not in cmd
    mxf = _renderer(_settings(master="dnxhr_hqx"))
    cmd = mxf.build_concat_command("ffmpeg", Path("l.txt"), Path("o.mxf.part"), 2.0, True, None, encoder="dnxhd")
    assert _muxer(cmd) == "mxf"
    assert _arg(cmd, "-colorspace") == "bt709"  # colour tags reach the muxer too


def test_preview_concat_ignores_settings():
    s = _settings(master="prores_hq", audio_rate=96000)
    r = _renderer(s)
    cmd = r.build_concat_command(
        "ffmpeg", Path("l.txt"), Path("o.mp4.part"), 2.0, True, Path("m.wav"), encoder="h264_nvenc", preview=True
    )
    assert _arg(cmd, "-c:a") == "aac" and _muxer(cmd) == "mp4" and "-colorspace" not in cmd
    assert "aresample=48000" in _arg(cmd, "-filter_complex")


def test_narration_ducking_uses_configured_attenuation():
    r = _renderer(workflow_mode="unboxing_narration", ducking_attenuation_db=-14.0)
    cmd = r.build_concat_command("ffmpeg", Path("l.txt"), Path("o.mp4.part"), 5.0, True, Path("m.wav"))
    graph = _arg(cmd, "-filter_complex")
    assert "sidechaincompress=threshold=0.05:ratio=3.33" in graph
    assert "alimiter" in graph
    deeper = _renderer(workflow_mode="unboxing_narration", ducking_attenuation_db=-18.0)
    cmd = deeper.build_concat_command("ffmpeg", Path("l.txt"), Path("o.mp4.part"), 5.0, True, Path("m.wav"))
    assert "ratio=10.00" in _arg(cmd, "-filter_complex")


def test_legacy_standard_mix_gets_limiter_and_keeps_aac():
    r = _renderer(workflow_mode="standard")
    cmd = r.build_concat_command("ffmpeg", Path("l.txt"), Path("o.mp4.part"), 5.0, True, Path("m.wav"))
    assert "alimiter=limit=0.95" in _arg(cmd, "-filter_complex") and _arg(cmd, "-c:a") == "aac"


# -- fit modes ---------------------------------------------------------------
def _geo(r: NvencRenderer, w: int, h: int, seg: EditClip | None = None) -> str:
    seg = seg or EditClip(Path("a.mp4"), 0.0, frames=30, available=5.0)
    return ";".join(r._geometry_filters(seg, w, h, "in", "out"))


@pytest.mark.parametrize("size", [(1080, 1080), (1080, 1350), (2560, 1080), (2048, 1080)])
def test_fit_fill_stretch_work_on_any_canvas(size):
    w, h = size
    fit = _geo(_renderer(_settings(canvas_width=w, canvas_height=h, fit_mode="fit")), w, h)
    assert f"scale={w}:{h}:force_original_aspect_ratio=decrease" in fit and f"pad={w}:{h}" in fit
    fill = _geo(_renderer(_settings(canvas_width=w, canvas_height=h, fit_mode="fill")), w, h)
    assert "force_original_aspect_ratio=increase" in fill and f"crop={w}:{h}" in fill and "pad=" not in fill
    stretch = _geo(_renderer(_settings(canvas_width=w, canvas_height=h, fit_mode="stretch")), w, h)
    assert stretch == f"[in]scale={w}:{h}[out]"


def test_explicit_fit_ignores_portrait_crop_window_but_auto_uses_it():
    cw = CropWindow(x=10, y=0, width=300, height=540)
    seg = EditClip(Path("a.mp4"), 0.0, frames=30, available=5.0, crop_window=cw)
    auto = _renderer(_settings(canvas_width=1080, canvas_height=1920, fit_mode="auto"))
    assert "crop=300:540:10:0" in _geo(auto, 1080, 1920, seg)
    fit = _renderer(_settings(canvas_width=1080, canvas_height=1920, fit_mode="fit"))
    assert "crop=300" not in _geo(fit, 1080, 1920, seg)
    stale = dataclasses.replace(seg, crop_needs_recompute=True)
    assert "crop=300" not in _geo(auto, 1080, 1920, stale)


def test_auto_on_square_canvas_is_letterbox():
    sq = _renderer(_settings(canvas_width=1080, canvas_height=1080, fit_mode="auto"))
    cw = CropWindow(x=0, y=0, width=300, height=300)
    seg = EditClip(Path("a.mp4"), 0.0, frames=30, available=5.0, crop_window=cw)
    geo = _geo(sq, 1080, 1080, seg)
    assert "pad=1080:1080" in geo and "crop=300" not in geo


def test_ken_burns_uses_rational_fps_and_clip_relative_progress():
    r = _renderer(_settings(fps=29.97))
    seg = EditClip(Path("a.mp4"), 0.0, frames=60, available=5.0, ken_burns="zoom_in")
    graph = r.build_segment_filter(seg, (320, 180), False, False)
    assert "d=1:s=320x180:fps=30000/1001" in graph and "*on/60" in graph


# -- dissolve planning -------------------------------------------------------
def _clips(frames: list[int], trans: list[int], src_in: float = 5.0, speed: float = 1.0) -> list[EditClip]:
    return [
        EditClip(Path(f"c{i}.mp4"), src_in, f, available=20.0, speed=speed, transition_in_frames=t)
        for i, (f, t) in enumerate(zip(frames, trans, strict=True))
    ]


def test_plan_units_without_transitions_is_one_unit_per_clip():
    units = plan_units(_clips([30, 20, 10], [0, 0, 0]), Fraction(30))
    assert [u.frames for u in units] == [30, 20, 10]
    assert [u.start_frame for u in units] == [0, 30, 50]
    assert not any(u.is_transition for u in units)


@pytest.mark.parametrize("n", [2, 3, 8, 9, 15])
def test_plan_units_keep_total_length_and_centre_dissolve_on_cut(n):
    fps = Fraction(30)
    clips = _clips([30, 45, 30], [0, n, n])
    units = plan_units(clips, fps)
    assert sum(u.frames for u in units) == 105
    # contiguous, no overlap, no gap on the timeline
    cursor = 0
    for u in units:
        assert u.start_frame == cursor
        cursor += u.frames
    trans = [u for u in units if u.is_transition]
    assert [u.frames for u in trans] == [n, n]
    # dissolve covers [cut - n//2, cut + n - n//2): first cut at 30, second at 75
    assert trans[0].start_frame == 30 - n // 2 and trans[1].start_frame == 75 - n // 2
    a, b = trans[0].slices
    # outgoing slice starts n//2 frames before A's end; incoming starts n//2 frames before B's in-point
    assert a.seg.source_in == pytest.approx(5.0 + (30 - n // 2) / 30)
    assert b.seg.source_in == pytest.approx(5.0 - (n // 2) / 30)
    assert a.frames == b.frames == n and b.lead_frames == 0


def test_plan_units_dissolve_preroll_clamps_at_media_start():
    clips = _clips([30, 30], [0, 10], src_in=0.0)
    units = plan_units(clips, Fraction(30))
    _a, b = next(u for u in units if u.is_transition).slices
    assert b.seg.source_in == 0.0 and b.lead_frames == 5  # 5 held frames before the media starts
    assert sum(u.frames for u in units) == 60


def test_plan_units_caps_and_ignores_degenerate_transitions():
    units = plan_units(_clips([6, 6], [0, 40]), Fraction(30))  # capped at min(len)
    assert sum(u.frames for u in units) == 12
    assert [u.frames for u in units if u.is_transition] == [6]
    one = plan_units(_clips([30, 30], [0, 1]), Fraction(30))  # 1-frame dissolve = hard cut
    assert not any(u.is_transition for u in one)
    first = plan_units(_clips([30, 30], [12, 0]), Fraction(30))  # nothing before the first clip
    assert not any(u.is_transition for u in first)


def test_plan_units_speed_scales_source_offsets():
    clips = _clips([30, 30], [0, 10], src_in=5.0, speed=2.0)
    a, b = next(u for u in plan_units(clips, Fraction(30)) if u.is_transition).slices
    assert a.seg.source_in == pytest.approx(5.0 + (30 - 5) / 30 * 2.0)
    assert b.seg.source_in == pytest.approx(5.0 - 5 / 30 * 2.0)


def test_transition_filter_uses_xfade_and_acrossfade():
    r = _renderer(_settings())
    clips = _clips([30, 30], [0, 10])
    unit = next(u for u in plan_units(clips, Fraction(30)) if u.is_transition)
    graph = r._unit_filter(unit, (320, 180), True, [True, True], None, [None, None])
    assert "xfade=transition=fade:duration=0.333333:offset=0" in graph
    assert "acrossfade=d=0.333333" in graph
    cmd = r._unit_command("ffmpeg", unit, Path("t.mov"), False, True)
    assert cmd.count("-i") == 2 and cmd.count("-ss") == 2


def test_segment_ss_is_floored_to_millisecond():
    r = _renderer(_settings())
    seg = EditClip(Path("a.mp4"), 1.0 / 15.0, frames=30, available=5.0)  # 0.066666...
    cmd = r.build_segment_command("ffmpeg", seg, Path("o.mov"), False, False)
    assert cmd[cmd.index("-ss") + 1] == "0.066"
