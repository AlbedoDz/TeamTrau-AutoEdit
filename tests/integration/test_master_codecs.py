"""Phase 22 step 6: real-ffmpeg renders of the Deliver master (codecs, colour, canvases, dissolve, subs).

Every render uses the CPU encoders (``force_cpu=True``) so the suite does not need a GPU; ProRes
and DNxHR are CPU-only anyway. Outputs are verified with ffprobe / OpenCV, never with mocks.
"""

import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from src.core.config import PipelineConfig
from src.core.ffmpeg_utils import ffprobe_json
from src.core.types import DeliverSettings, EditClip, EditTimeline, SentenceSegment
from src.dispatchers.nvenc_renderer import NvencRenderer, available_filters
from src.modules.subtitle_export import burn_filter
from tests.integration.conftest import FFMPEG, stream_durations, stream_info

FPS_NTSC = 30000 / 1001


# --------------------------------------------------------------------------- helpers
def _solid_clip(
    out: Path, color: str, duration: float = 3.0, size: str = "160x90", freq: int = 440, audio: bool = True
) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30:d={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={duration}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-crf", "12", "-g", "10"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(out))
    subprocess.run(cmd, check=True)
    return out


def _timeline(clips: list[EditClip], settings: DeliverSettings, name: str = "t") -> EditTimeline:
    return EditTimeline(name, settings.fps, settings.canvas_width, settings.canvas_height, clips=clips)


def _settings(w: int = 320, h: int = 180, fps: float = 29.97, **kw) -> DeliverSettings:
    return DeliverSettings(canvas_width=w, canvas_height=h, fps=fps, **kw)


def _renderer(tmp_path: Path, **cfg) -> NvencRenderer:
    base = {"cache_dir": tmp_path / "cache", "keep_video_audio": True, "enable_teaser": False}
    base.update(cfg)
    return NvencRenderer(PipelineConfig(**base))


def _frame(path: Path, index: int) -> np.ndarray:
    cap = cv2.VideoCapture(str(path))
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, img = cap.read()
        assert ok, f"cannot read frame {index} of {path}"
        return img
    finally:
        cap.release()


def _frame_count(path: Path) -> int:
    cap = cv2.VideoCapture(str(path))
    try:
        return int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        cap.release()


def _assert_av(out: Path, expected: float, fps: float = FPS_NTSC) -> None:
    durs = stream_durations(out)
    assert "video" in durs and "audio" in durs, durs
    assert abs(durs["video"] - expected) <= 3 / fps, durs
    assert abs(durs["audio"] - expected) <= 3 / fps + 0.02, durs


# --------------------------------------------------------------------------- codec matrix
@pytest.mark.parametrize(
    ("master", "codec", "pix_fmt", "container", "audio_codec", "profile_hint"),
    [
        ("hevc", "hevc", "yuv420p", ".mp4", "aac", "Main"),
        ("h264", "h264", "yuv420p", ".mp4", "aac", "High"),
        ("prores_422", "prores", "yuv422p10le", ".mov", "pcm_s24le", "Standard"),
        ("prores_hq", "prores", "yuv422p10le", ".mov", "pcm_s24le", "HQ"),
        ("dnxhr_hqx", "dnxhd", "yuv422p10le", ".mov", "pcm_s24le", "DNXHR HQX"),
    ],
)
def test_master_codec_ffprobe(tmp_path, make_clip, master, codec, pix_fmt, container, audio_codec, profile_hint):
    a = make_clip("a.mp4", duration=3.0)
    b = make_clip("b.mp4", duration=3.0)
    st = _settings(master=master)  # 29.97 fps, rec709, 48k stereo
    tl = _timeline([EditClip(a, 0.5, 45, 2.5), EditClip(b, 0.5, 45, 2.5)], st)
    out = tmp_path / "out" / "master.mp4"  # suffix must be rewritten for mov masters
    res = _renderer(tmp_path).render_edit_timeline(tl, out, force_cpu=True, settings=st)

    assert res.output_path.suffix == container
    assert res.output_path.exists() and (container == ".mp4" or not out.exists())
    v = stream_info(res.output_path, "video")
    assert v["codec_name"] == codec
    assert v["pix_fmt"] == pix_fmt
    assert profile_hint in v.get("profile", "")
    assert (v["width"], v["height"]) == (320, 180)
    assert v["r_frame_rate"] == "30000/1001"
    assert (v["color_space"], v["color_transfer"], v["color_primaries"], v["color_range"]) == (
        "bt709", "bt709", "bt709", "tv",
    )
    au = stream_info(res.output_path, "audio")
    assert au["codec_name"] == audio_codec
    assert au["sample_rate"] == "48000" and au["channels"] == 2
    _assert_av(res.output_path, 90 / FPS_NTSC)
    assert res.encoder_used in ("libx265", "libx264", "prores_ks", "dnxhd")
    assert not list(res.output_path.parent.glob("*.part"))


def test_dnxhr_can_be_written_as_mxf(tmp_path, make_clip):
    a = make_clip("a.mp4", duration=3.0)
    st = _settings(fps=30.0, master="dnxhr_hqx")
    tl = _timeline([EditClip(a, 0.0, 60, 3.0)], st)
    res = _renderer(tmp_path).render_edit_timeline(tl, tmp_path / "out" / "master.mxf", force_cpu=True, settings=st)
    assert res.output_path.suffix == ".mxf"
    data = ffprobe_json(res.output_path)
    assert data["format"]["format_name"] == "mxf"
    assert stream_info(res.output_path, "video")["codec_name"] == "dnxhd"
    assert stream_info(res.output_path, "audio")["codec_name"] == "pcm_s24le"


@pytest.mark.parametrize(
    ("color", "trc", "space", "prim"),
    [
        ("rec2020_pq", "smpte2084", "bt2020nc", "bt2020"),
        ("rec2020_hlg", "arib-std-b67", "bt2020nc", "bt2020"),
    ],
)
def test_hdr_targets_are_10bit_main10_and_tagged(tmp_path, make_clip, color, trc, space, prim):
    a = make_clip("a.mp4", duration=2.0)
    st = _settings(fps=30.0, color=color, master="hevc")
    tl = _timeline([EditClip(a, 0.0, 30, 2.0)], st)
    res = _renderer(tmp_path).render_edit_timeline(tl, tmp_path / "out" / "hdr.mp4", force_cpu=True, settings=st)
    v = stream_info(res.output_path, "video")
    assert v["pix_fmt"] == "yuv420p10le" and "Main 10" in v["profile"]
    assert (v["color_transfer"], v["color_space"], v["color_primaries"]) == (trc, space, prim)


def test_audio_settings_mono_44k_and_limiter_with_soundtrack(tmp_path, make_clip, make_tone):
    a = make_clip("a.mp4", duration=3.0)
    music = make_tone("m.wav", duration=6.0, sample_rate=44100)
    st = _settings(fps=30.0, audio_rate=44100, audio_channels=1, master="h264")
    tl = _timeline([EditClip(a, 0.0, 60, 3.0)], st)
    tl.soundtrack_path = music
    res = _renderer(tmp_path).render_edit_timeline(tl, tmp_path / "out" / "mix.mp4", force_cpu=True, settings=st)
    au = stream_info(res.output_path, "audio")
    assert au["sample_rate"] == "44100" and au["channels"] == 1 and au["codec_name"] == "aac"
    _assert_av(res.output_path, 2.0, 30.0)


def test_narration_workflow_ducking_graph_renders(tmp_path, make_clip, make_tone):
    a = make_clip("voice.mp4", duration=3.0)
    music = make_tone("m.wav", duration=6.0)
    st = _settings(fps=30.0, master="hevc")
    tl = _timeline([EditClip(a, 0.0, 60, 3.0)], st)
    tl.soundtrack_path = music
    r = _renderer(tmp_path, workflow_mode="unboxing_narration", ducking_attenuation_db=-14.0)
    res = r.render_edit_timeline(tl, tmp_path / "out" / "narr.mp4", force_cpu=True, settings=st)
    _assert_av(res.output_path, 2.0, 30.0)


def test_legacy_render_without_settings_is_unchanged(tmp_path, make_clip):
    a = make_clip("a.mp4", duration=3.0)
    tl = EditTimeline("t", 30.0, 320, 180, clips=[EditClip(a, 0.0, 60, 3.0)])
    res = _renderer(tmp_path).render_edit_timeline(tl, tmp_path / "out" / "legacy.mp4", force_cpu=True)
    assert res.output_path.name == "legacy.mp4"
    v = stream_info(res.output_path, "video")
    assert v["codec_name"] == "hevc" and v["pix_fmt"] == "yuv420p" and v["r_frame_rate"] == "30/1"
    _assert_av(res.output_path, 2.0, 30.0)


def test_preview_with_settings_stays_540p_h264_mp4(tmp_path, make_clip):
    a = make_clip("a.mp4", duration=3.0)
    st = _settings(w=1920, h=1080, fps=30.0, master="prores_hq", color="rec2020_pq")
    tl = _timeline([EditClip(a, 0.0, 30, 3.0)], st)
    res = _renderer(tmp_path).render_edit_timeline(
        tl, tmp_path / "out" / "preview.mp4", force_cpu=True, preview=True, settings=st
    )
    assert res.output_path.suffix == ".mp4"
    v = stream_info(res.output_path, "video")
    assert v["codec_name"] == "h264" and (v["width"], v["height"]) == (960, 540)
    assert not v.get("color_transfer") or v["color_transfer"] != "smpte2084"


# --------------------------------------------------------------------------- canvases / fit modes
CANVASES = {"square": (360, 360), "4x5": (360, 450), "ultrawide": (840, 360)}


@pytest.mark.parametrize("canvas", list(CANVASES))
@pytest.mark.parametrize("fit", ["fit", "fill", "stretch", "auto"])
def test_canvas_shapes_and_fit_modes(tmp_path, canvas, fit):
    w, h = CANVASES[canvas]
    white = _solid_clip(tmp_path / "src" / "white.mp4", "white", duration=2.0)  # 16:9 source
    st = _settings(w=w, h=h, fps=30.0, fit_mode=fit, master="h264")
    tl = _timeline([EditClip(white, 0.0, 30, 2.0)], st)
    res = _renderer(tmp_path).render_edit_timeline(tl, tmp_path / "out" / f"{canvas}_{fit}.mp4", force_cpu=True, settings=st)
    v = stream_info(res.output_path, "video")
    assert (v["width"], v["height"]) == (w, h)
    img = _frame(res.output_path, 5)
    corners = [img[2, 2], img[2, w - 3], img[h - 3, 2], img[h - 3, w - 3]]
    centre = img[h // 2, w // 2]
    assert centre.min() > 200  # the picture itself is white
    if fit in ("fit", "auto"):
        # 16:9 inside a non-16:9 canvas leaves black bars: every corner is dark
        assert all(c.max() < 60 for c in corners), corners
    else:
        # fill covers the canvas (centre-crop) and stretch distorts it: no bars at all
        assert all(c.min() > 200 for c in corners), corners


# --------------------------------------------------------------------------- cross-dissolve
def _rb(img: np.ndarray) -> tuple[float, float]:
    """(red, blue) means of a BGR frame."""
    return float(img[..., 2].mean()), float(img[..., 0].mean())


@pytest.mark.parametrize("master", ["h264", "prores_422"])
@pytest.mark.parametrize("src_in_b", [1.0, 0.0])
def test_cross_dissolve_keeps_exact_duration_and_blends_across_the_cut(tmp_path, master, src_in_b):
    """A (red) -> B (blue) with a 10-frame dissolve centred on the cut at frame 45.

    ``src_in_b=0`` has no media before B's in-point: the pre-roll holds B's first frame.
    """
    red = _solid_clip(tmp_path / "src" / "red.mp4", "red", duration=3.0, freq=440)
    blue = _solid_clip(tmp_path / "src" / "blue.mp4", "blue", duration=3.0, freq=880)
    st = _settings(fps=30.0, master=master)
    n = 10
    clips = [EditClip(red, 0.5, 45, 2.5), EditClip(blue, src_in_b, 45, 3.0 - src_in_b, transition_in_frames=n)]
    tl = _timeline(clips, st)
    res = _renderer(tmp_path).render_edit_timeline(tl, tmp_path / "out" / f"dissolve_{master}.mp4", force_cpu=True, settings=st)

    _assert_av(res.output_path, 3.0, 30.0)
    assert _frame_count(res.output_path) == 90
    # outside the window the picture is pure A / pure B
    r39, b39 = _rb(_frame(res.output_path, 39))
    r50, b50 = _rb(_frame(res.output_path, 50))
    assert r39 > 150 and b39 < 60, (r39, b39)
    assert b50 > 150 and r50 < 60, (r50, b50)
    # inside it, blue grows monotonically from ~A to ~B and the cut frame is a real mix
    blues = [_rb(_frame(res.output_path, i))[1] for i in range(40, 50)]
    assert all(y >= x - 6 for x, y in zip(blues, blues[1:], strict=False)), blues
    r45, b45 = _rb(_frame(res.output_path, 45))
    assert r45 > 50 and b45 > 50, (r45, b45)
    # audio: the dissolve carries sound through (no gap at the cut)
    out_wav = tmp_path / "out" / "check.wav"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(res.output_path), "-ss", "1.40", "-t", "0.2",
                    "-vn", str(out_wav)], check=True)
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(out_wav), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    assert np.abs(np.frombuffer(raw, dtype=np.int16)).max() > 1000


def test_back_to_back_dissolves_and_ntsc_rate_keep_duration(tmp_path):
    cols = ["red", "green", "blue"]
    clips = []
    for i, c in enumerate(cols):
        clip = _solid_clip(tmp_path / "src" / f"{c}.mp4", c, duration=3.0, freq=300 + i * 200)
        clips.append(EditClip(clip, 0.5, 30, 2.5, transition_in_frames=6 if i else 0))
    st = _settings(fps=29.97, master="hevc")
    tl = _timeline(clips, st)
    res = _renderer(tmp_path).render_edit_timeline(tl, tmp_path / "out" / "chain.mp4", force_cpu=True, settings=st)
    _assert_av(res.output_path, 90 / FPS_NTSC)
    assert _frame_count(res.output_path) == 90
    assert stream_info(res.output_path, "video")["r_frame_rate"] == "30000/1001"


def test_timeline_is_retargeted_to_deliver_fps(tmp_path, make_clip):
    """A 30 fps timeline delivered at 24 fps keeps its length in seconds."""
    a = make_clip("a.mp4", duration=4.0)
    tl = EditTimeline("t", 30.0, 320, 180, clips=[EditClip(a, 0.0, 60, 4.0), EditClip(a, 1.0, 30, 3.0)])
    st = _settings(fps=24.0, master="h264")
    res = _renderer(tmp_path).render_edit_timeline(tl, tmp_path / "out" / "r24.mp4", force_cpu=True, settings=st)
    assert stream_info(res.output_path, "video")["r_frame_rate"] == "24/1"
    _assert_av(res.output_path, 3.0, 24.0)


# --------------------------------------------------------------------------- subtitles
VI_TEXT = "Xin chào các bạn, đây là bản thử nghiệm tiếng Việt"


def _sentences() -> list[SentenceSegment]:
    return [SentenceSegment(text=VI_TEXT, start_time=0.2, end_time=1.8, duration=1.6)]


def _grey_clip(tmp_path: Path, name: str = "grey.mp4", duration: float = 3.0) -> Path:
    return _solid_clip(tmp_path / "src" / name, "0x202020", duration=duration)


def _bottom_diff(a: np.ndarray, b: np.ndarray) -> float:
    h = a.shape[0]
    return float(np.abs(a[h // 2:].astype(int) - b[h // 2:].astype(int)).mean())


def test_burn_in_vietnamese_subtitles_renders_and_writes_srt(tmp_path):
    clip = _grey_clip(tmp_path)
    st = _settings(w=640, h=360, fps=30.0, master="h264", subtitles="burn")
    tl = _timeline([EditClip(clip, 0.0, 60, 3.0)], st)
    burned = _renderer(tmp_path).render_edit_timeline(
        tl, tmp_path / "out" / "burn.mp4", force_cpu=True, settings=st, sentences=_sentences()
    )
    st_plain = _settings(w=640, h=360, fps=30.0, master="h264", subtitles="none")
    plain = _renderer(tmp_path).render_edit_timeline(
        tl, tmp_path / "out" / "plain.mp4", force_cpu=True, settings=st_plain
    )
    _assert_av(burned.output_path, 2.0, 30.0)
    assert _bottom_diff(_frame(burned.output_path, 30), _frame(plain.output_path, 30)) > 0.5  # text visible at 1.0 s
    assert _bottom_diff(_frame(burned.output_path, 55), _frame(plain.output_path, 55)) < 0.5  # gone after 1.8 s
    srt = burned.output_path.with_suffix(".srt")
    assert srt.exists() and VI_TEXT in srt.read_text(encoding="utf-8")
    assert not plain.output_path.with_suffix(".srt").exists()


def test_burn_in_follows_timeline_time_across_clips(tmp_path):
    """A cue at 1.2-1.8 s lives in the second segment: its time must be offset by the first clip."""
    clip = _grey_clip(tmp_path, duration=4.0)
    st = _settings(w=640, h=360, fps=30.0, master="h264", subtitles="burn")
    tl = _timeline([EditClip(clip, 0.0, 30, 4.0), EditClip(clip, 1.0, 60, 3.0)], st)
    sent = [SentenceSegment(text="Phụ đề ở clip thứ hai", start_time=1.2, end_time=1.8, duration=0.6)]
    burned = _renderer(tmp_path).render_edit_timeline(
        tl, tmp_path / "out" / "b2.mp4", force_cpu=True, settings=st, sentences=sent
    )
    plain = _renderer(tmp_path).render_edit_timeline(
        tl, tmp_path / "out" / "p2.mp4", force_cpu=True, settings=_settings(w=640, h=360, fps=30.0, master="h264")
    )
    assert _bottom_diff(_frame(burned.output_path, 27), _frame(plain.output_path, 27)) < 0.5  # before the cue
    assert _bottom_diff(_frame(burned.output_path, 45), _frame(plain.output_path, 45)) > 0.5  # at 1.5 s
    assert _bottom_diff(_frame(burned.output_path, 70), _frame(plain.output_path, 70)) < 0.5  # after


def test_ass_mode_writes_sidecars_without_burning(tmp_path):
    clip = _grey_clip(tmp_path)
    st = _settings(w=640, h=360, fps=30.0, master="h264", subtitles="ass")
    tl = _timeline([EditClip(clip, 0.0, 60, 3.0)], st)
    res = _renderer(tmp_path).render_edit_timeline(
        tl, tmp_path / "out" / "ass.mp4", force_cpu=True, settings=st, sentences=_sentences()
    )
    ass = res.output_path.with_suffix(".ass")
    assert ass.exists() and "Dialogue" in ass.read_text(encoding="utf-8")
    assert res.output_path.with_suffix(".srt").exists()
    st_none = _settings(w=640, h=360, fps=30.0, master="h264")
    plain = _renderer(tmp_path).render_edit_timeline(tl, tmp_path / "out" / "plain.mp4", force_cpu=True, settings=st_none)
    assert _bottom_diff(_frame(res.output_path, 30), _frame(plain.output_path, 30)) < 0.5


@pytest.mark.parametrize("folder", ["plain dir", "it's a dir", "thư mục tiếng Việt", "semi;colon [x], y"])
def test_burn_filter_escaping_opens_awkward_paths(tmp_path, folder):
    """The generated filter string must let libass open files in Windows/Unicode/quoted folders."""
    from src.modules.subtitle_export import write_srt

    srt = write_srt(
        [SentenceSegment(text="Chào bạn", start_time=0.0, end_time=2.0, duration=2.0)],
        tmp_path / folder / "sub.srt",
    )
    flt = burn_filter(srt, (320, 180))
    base = ["-f", "lavfi", "-i", "color=c=0x202020:s=320x180:r=10:d=1"]
    with_sub = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", *base, "-vf", flt, "-frames:v", "5", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert with_sub.returncode == 0, with_sub.stderr[-400:]


def test_burn_filter_renders_text_pixels(tmp_path):
    from src.modules.subtitle_export import write_srt

    srt = write_srt([SentenceSegment(text="Hello", start_time=0.0, end_time=2.0, duration=2.0)], tmp_path / "s.srt")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=10:d=1",
           "-vf", burn_filter(srt, (320, 180)), "-frames:v", "1", str(tmp_path / "f.png")]
    subprocess.run(cmd, check=True)
    img = cv2.imread(str(tmp_path / "f.png"))
    assert img.max() > 200  # white text drawn on black


# --------------------------------------------------------------------------- HDR -> SDR
def _tagged_10bit_clip(out: Path, transfer: str, duration: float = 2.0, grey: str = "0x808080") -> Path:
    """10-bit HEVC clip whose bitstream VUI declares BT.2020 primaries/matrix and ``transfer``."""
    out.parent.mkdir(parents=True, exist_ok=True)
    params = f"log-level=error:colorprim=bt2020:transfer={transfer}:colormatrix=bt2020nc"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         f"color=c={grey}:s=160x90:r=30:d={duration}", "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
         "-x265-params", params, str(out)],
        check=True,
    )
    return out


@pytest.mark.skipif("zscale" not in available_filters(), reason="ffmpeg built without zscale")
def test_hdr_source_is_tone_mapped_for_rec709_master(tmp_path):
    """A PQ (smpte2084 / bt2020) clip delivered as Rec.709 goes through the zscale+tonemap chain."""
    hdr = _tagged_10bit_clip(tmp_path / "src" / "hdr_pq.mp4", "smpte2084")
    probe = stream_info(hdr, "video")
    assert probe["color_transfer"] == "smpte2084"

    st = _settings(fps=30.0, color="rec709", master="h264")
    tl = _timeline([EditClip(hdr, 0.0, 30, 2.0)], st)
    res = _renderer(tmp_path, keep_video_audio=False).render_edit_timeline(
        tl, tmp_path / "out" / "sdr.mp4", force_cpu=True, settings=st
    )
    v = stream_info(res.output_path, "video")
    assert v["pix_fmt"] == "yuv420p" and v["color_transfer"] == "bt709" and v["color_space"] == "bt709"
    # Naive tag-only conversion would leave PQ mid-grey (~ luma 126); tone-mapping maps ~92 nits to a bright SDR grey.
    sdr_luma = float(cv2.cvtColor(_frame(res.output_path, 5), cv2.COLOR_BGR2GRAY).mean())
    pq_luma = float(cv2.cvtColor(_frame(hdr, 5), cv2.COLOR_BGR2GRAY).mean())
    assert sdr_luma > pq_luma + 25, (sdr_luma, pq_luma)


def test_hdr_source_falls_back_without_zscale_and_never_crashes(tmp_path, monkeypatch):
    import src.dispatchers.nvenc_renderer as nr

    hdr = _tagged_10bit_clip(tmp_path / "src" / "hdr_pq.mp4", "smpte2084", duration=1.0)
    monkeypatch.setattr(nr, "available_filters", lambda: frozenset())
    st = _settings(fps=30.0, color="rec709", master="h264")
    tl = _timeline([EditClip(hdr, 0.0, 30, 1.0)], st)
    res = _renderer(tmp_path, keep_video_audio=False).render_edit_timeline(
        tl, tmp_path / "out" / "fallback.mp4", force_cpu=True, settings=st
    )
    assert stream_info(res.output_path, "video")["color_transfer"] == "bt709"


def test_passthrough_10bit_source_stays_10bit_and_keeps_source_tags(tmp_path):
    src = _tagged_10bit_clip(tmp_path / "src" / "hlg.mp4", "arib-std-b67", duration=1.0)
    st = _settings(fps=30.0, color="passthrough", master="hevc")
    tl = _timeline([EditClip(src, 0.0, 30, 1.0)], st)
    res = _renderer(tmp_path, keep_video_audio=False).render_edit_timeline(
        tl, tmp_path / "out" / "pass.mp4", force_cpu=True, settings=st
    )
    v = stream_info(res.output_path, "video")
    assert v["pix_fmt"] == "yuv420p10le" and v["color_transfer"] == "arib-std-b67"


# --------------------------------------------------------------------------- extra graph coverage
def test_dissolve_with_ken_burns_and_watermark_renders_exact_length(tmp_path, make_clip):
    wm = tmp_path / "wm.png"
    cv2.imwrite(str(wm), np.full((20, 40, 4), 255, dtype=np.uint8))
    a = make_clip("a.mp4", duration=3.0)
    b = make_clip("b.mp4", duration=3.0)
    st = _settings(fps=30.0, master="h264")
    clips = [
        EditClip(a, 0.5, 40, 2.5, ken_burns="zoom_in"),
        EditClip(b, 0.0, 40, 3.0, ken_burns="zoom_out", transition_in_frames=8),
    ]
    tl = _timeline(clips, st)
    r = _renderer(tmp_path, watermark_enabled=True, watermark_path=wm)
    res = r.render_edit_timeline(tl, tmp_path / "out" / "kbwm.mp4", force_cpu=True, settings=st)
    _assert_av(res.output_path, 80 / 30.0, 30.0)
    assert _frame_count(res.output_path) == 80


def test_hdr_to_hdr_and_sdr_to_pq_conversions_run(tmp_path, make_clip):
    if "zscale" not in available_filters():
        pytest.skip("ffmpeg built without zscale")
    pq = _tagged_10bit_clip(tmp_path / "src" / "pq.mp4", "smpte2084", duration=1.0)
    st = _settings(fps=30.0, color="rec2020_hlg", master="hevc")
    tl = _timeline([EditClip(pq, 0.0, 30, 1.0)], st)
    res = _renderer(tmp_path, keep_video_audio=False).render_edit_timeline(
        tl, tmp_path / "out" / "pq2hlg.mp4", force_cpu=True, settings=st
    )
    assert stream_info(res.output_path, "video")["color_transfer"] == "arib-std-b67"

    sdr = make_clip("sdr.mp4", duration=2.0)
    st = _settings(fps=30.0, color="rec2020_pq", master="hevc")
    tl = _timeline([EditClip(sdr, 0.0, 30, 2.0)], st)
    res = _renderer(tmp_path, keep_video_audio=False).render_edit_timeline(
        tl, tmp_path / "out" / "sdr2pq.mp4", force_cpu=True, settings=st
    )
    v = stream_info(res.output_path, "video")
    assert v["color_transfer"] == "smpte2084" and v["pix_fmt"] == "yuv420p10le"


def test_nvenc_masters_when_gpu_available(tmp_path, make_clip):
    from src.core.config import check_nvenc_available

    if not check_nvenc_available():
        pytest.skip("NVENC not available")
    a = make_clip("a.mp4", duration=3.0)
    b = make_clip("b.mp4", duration=3.0)
    st = _settings(w=640, h=360, fps=29.97, color="rec2020_hlg", master="hevc")
    tl = _timeline([EditClip(a, 0.5, 45, 2.5), EditClip(b, 0.5, 45, 2.5, transition_in_frames=8)], st)
    res = _renderer(tmp_path).render_edit_timeline(tl, tmp_path / "out" / "nv.mp4", settings=st)
    assert res.encoder_used == "hevc_nvenc"
    v = stream_info(res.output_path, "video")
    assert v["profile"] == "Main 10" and v["pix_fmt"] == "yuv420p10le" and v["r_frame_rate"] == "30000/1001"
    _assert_av(res.output_path, 90 / FPS_NTSC)
