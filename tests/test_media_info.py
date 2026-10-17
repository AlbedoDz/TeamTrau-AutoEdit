"""Phase 22 step 2: probe_media built from ffprobe JSON (mocked, headless-friendly)."""

from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core import ffmpeg_utils, media_info
from src.core.media_info import MediaInfo, clear_media_cache, parse_rate, probe_media


def _video(**kw) -> dict:
    base = {
        "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
        "r_frame_rate": "30/1", "avg_frame_rate": "30/1", "pix_fmt": "yuv420p",
    }
    base.update(kw)
    return base


def _audio(**kw) -> dict:
    base = {"codec_type": "audio", "channels": 2, "sample_rate": "48000", "channel_layout": "stereo"}
    base.update(kw)
    return base


def _probe(tmp_path: Path, payload: dict | None) -> MediaInfo:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")
    clear_media_cache()
    with patch.object(media_info, "ffprobe_json", return_value=payload):
        return probe_media(f)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("30000/1001", Fraction(30000, 1001)), ("25/1", Fraction(25)), ("27.42/1", Fraction("27.42")),
     ("0/0", None), ("0/1", None), ("", None), (None, None), ("abc", None), ("24", Fraction(24))],
)
def test_parse_rate(text, expected):
    assert parse_rate(text) == expected


def test_basic_video_and_audio(tmp_path):
    info = _probe(tmp_path, {"streams": [_video(), _audio()], "format": {"duration": "4.5"}})
    assert (info.has_video, info.has_audio) == (True, True)
    assert (info.width, info.height, info.rotation) == (1920, 1080, 0)
    assert info.fps == Fraction(30) and info.fps_float == 30.0
    assert (info.is_ntsc, info.is_vfr) == (False, False)
    assert info.duration == 4.5
    assert (info.codec, info.pix_fmt, info.bit_depth) == ("h264", "yuv420p", 8)
    assert (info.audio_channels, info.audio_rate, info.audio_layout) == (2, 48000, "stereo")
    assert info.start_timecode is None and not info.is_hdr and not info.is_log


def test_rotation_swaps_display_size(tmp_path):
    info = _probe(tmp_path, {"streams": [_video(side_data_list=[{"rotation": -90}])], "format": {}})
    assert info.rotation == 270 and (info.width, info.height) == (1080, 1920)
    info = _probe(tmp_path, {"streams": [_video(tags={"rotate": "180"})], "format": {}})
    assert info.rotation == 180 and (info.width, info.height) == (1920, 1080)


def test_ntsc_and_vfr_detection(tmp_path):
    ntsc = _probe(tmp_path, {"streams": [_video(r_frame_rate="30000/1001", avg_frame_rate="30000/1001")]})
    assert ntsc.is_ntsc and not ntsc.is_vfr and ntsc.fps == Fraction(30000, 1001)
    vfr = _probe(tmp_path, {"streams": [_video(r_frame_rate="30/1", avg_frame_rate="27.42/1")]})
    assert vfr.is_vfr and vfr.fps == Fraction(30) and vfr.avg_fps == Fraction("27.42")
    fallback = _probe(tmp_path, {"streams": [_video(r_frame_rate="0/0", avg_frame_rate="25/1")]})
    assert fallback.fps == Fraction(25) and not fallback.is_vfr


def test_hdr_log_and_color_fields(tmp_path):
    hdr = _probe(tmp_path, {"streams": [_video(
        pix_fmt="yuv420p10le", color_transfer="smpte2084", color_primaries="bt2020",
        color_space="bt2020nc", color_range="tv")]})
    assert hdr.is_hdr and not hdr.is_log and hdr.bit_depth == 10
    assert (hdr.color_transfer, hdr.color_primaries, hdr.color_space, hdr.color_range) == (
        "smpte2084", "bt2020", "bt2020nc", "tv")
    hlg = _probe(tmp_path, {"streams": [_video(color_transfer="arib-std-b67")]})
    assert hlg.is_hdr
    sdr = _probe(tmp_path, {"streams": [_video(color_transfer="bt709", color_space="unknown")]})
    assert not sdr.is_hdr and not sdr.is_log and sdr.color_space is None
    log_tag = _probe(tmp_path, {"streams": [_video(color_transfer="unknown")],
                                "format": {"tags": {"com.sony.profile": "S-Log3"}}})
    assert log_tag.is_log and log_tag.color_transfer is None
    log100 = _probe(tmp_path, {"streams": [_video(color_transfer="log100")]})
    assert log100.is_log
    tagged_but_standard = _probe(tmp_path, {"streams": [_video(color_transfer="bt709")],
                                            "format": {"tags": {"x": "S-Log3"}}})
    assert not tagged_but_standard.is_log


def test_start_timecode_sources(tmp_path):
    fmt = _probe(tmp_path, {"streams": [_video()], "format": {"tags": {"timecode": "01:00:00:00"}}})
    assert fmt.start_timecode == "01:00:00:00"
    stream = _probe(tmp_path, {"streams": [_video(tags={"timecode": "10:00:00;00"})], "format": {}})
    assert stream.start_timecode == "10:00:00;00"
    tmcd = _probe(tmp_path, {"streams": [_video(), {"codec_type": "data", "tags": {"TIMECODE": "00:59:58:12"}}]})
    assert tmcd.start_timecode == "00:59:58:12"


def test_audio_only_and_attached_picture(tmp_path):
    audio_only = _probe(tmp_path, {"streams": [_audio(channels=1, sample_rate="44100", channel_layout="mono")],
                                   "format": {"duration": "12.0"}})
    assert not audio_only.has_video and audio_only.has_audio
    assert (audio_only.audio_channels, audio_only.audio_rate, audio_only.duration) == (1, 44100, 12.0)
    cover = _probe(tmp_path, {"streams": [_video(disposition={"attached_pic": 1}), _audio()]})
    assert not cover.has_video and cover.has_audio


def test_bit_depth_from_raw_sample_and_pix_fmt(tmp_path):
    raw = _probe(tmp_path, {"streams": [_video(bits_per_raw_sample="12", pix_fmt="yuv422p12le")]})
    assert raw.bit_depth == 12
    assert _probe(tmp_path, {"streams": [_video(pix_fmt="p010le")]}).bit_depth == 10
    assert _probe(tmp_path, {"streams": [_video(pix_fmt=None)]}).bit_depth is None


def test_missing_corrupt_and_empty_never_raise(tmp_path):
    clear_media_cache()
    missing = probe_media(tmp_path / "nope.mp4")
    assert not missing.has_video and not missing.has_audio and missing.fps is None and missing.width == 0
    assert not probe_media(None).has_video
    assert not probe_media(tmp_path).has_video  # a directory
    assert not _probe(tmp_path, None).has_video  # ffprobe failed
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"\x00not a video")
    clear_media_cache()
    assert not probe_media(garbage).has_video  # real ffprobe (or none installed): still no raise
    weird = _probe(tmp_path, {"streams": [{"codec_type": "video", "width": "abc", "r_frame_rate": 5}]})
    assert weird.has_video and weird.width == 0


def test_cache_keyed_by_size_and_mtime(tmp_path):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"1")
    clear_media_cache()
    payload = {"streams": [_video()], "format": {"duration": "1"}}
    with patch.object(media_info, "ffprobe_json", return_value=payload) as probe:
        probe_media(f)
        probe_media(str(f))
        assert probe.call_count == 1
        f.write_bytes(b"12345")  # size changed -> new probe
        probe_media(f)
        assert probe.call_count == 2


def test_has_audio_stream_wrapper_uses_probe_media(tmp_path):
    with_audio = _probe(tmp_path, {"streams": [_video(), _audio()]})
    assert with_audio.has_audio
    f = tmp_path / "x.mp4"
    clear_media_cache()
    with patch.object(media_info, "ffprobe_json", return_value={"streams": [_video()]}):
        assert ffmpeg_utils.has_audio_stream(f) is False
    clear_media_cache()
    with patch.object(media_info, "ffprobe_json", return_value={"streams": [_video(), _audio()]}):
        assert ffmpeg_utils.has_audio_stream(f) is True
    clear_media_cache()
    assert ffmpeg_utils.has_audio_stream(tmp_path / "missing.mp4") is False
