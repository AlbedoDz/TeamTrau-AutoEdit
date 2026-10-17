"""Real-ffprobe checks for probe_media: rotation, NTSC, audio layout, start timecode."""

import subprocess
from fractions import Fraction
from pathlib import Path

from src.core.ffmpeg_utils import has_audio_stream
from src.core.media_info import probe_media
from src.modules import premiere_xml_fix
from src.modules.capcut_exporter import _source_size
from src.modules.timeline_builder import media_duration_and_rate
from tests.integration.conftest import FFMPEG


def _encode(out: Path, rate: str, extra: list[str] | None = None, size: str = "160x90", audio: bool = False) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", f"testsrc2=size={size}:rate={rate}:duration=1"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=1", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", *(extra or []), str(out)]
    subprocess.run(cmd, check=True)
    return out


def test_rotation_is_applied_to_display_size(tmp_path, make_clip):
    rotated = make_clip("iphone.mov", duration=1.0, rotation=90, size="160x90")
    info = probe_media(rotated)
    assert info.has_video and info.rotation in (90, 270)
    assert (info.width, info.height) == (90, 160)
    plain = probe_media(make_clip("plain.mp4", duration=1.0, size="160x90"))
    assert (plain.width, plain.height, plain.rotation) == (160, 90, 0)
    assert _source_size(rotated) == (90, 160)


def test_premiere_probe_honours_rotation(tmp_path, make_clip):
    rotated = make_clip("iphone2.mov", duration=1.0, rotation=90, size="160x90", channels=1)
    probe = premiere_xml_fix._probe(rotated)
    assert (probe["width"], probe["height"]) == (90, 160)
    assert probe["has_audio"] and probe["channels"] == 1 and probe["rate"] == 48000
    silent = premiere_xml_fix._probe(make_clip("silent.mp4", duration=1.0, audio=False))
    assert not silent["has_audio"] and (silent["width"], silent["height"]) == (160, 90)
    assert premiere_xml_fix._probe(tmp_path / "missing.mp4")["width"] == 0


def test_ntsc_frame_rate_is_exact(tmp_path):
    clip = _encode(tmp_path / "ntsc.mp4", "30000/1001")
    info = probe_media(clip)
    assert info.fps == Fraction(30000, 1001)
    assert info.is_ntsc and not info.is_vfr
    assert abs(media_duration_and_rate(clip)[1] - 30000 / 1001) < 1e-3
    pal = probe_media(_encode(tmp_path / "pal.mp4", "25"))
    assert pal.fps == Fraction(25) and not pal.is_ntsc


def test_audio_channels_rate_and_layout(tmp_path, make_clip):
    stereo = probe_media(make_clip("st.mp4", duration=1.0, channels=2, sample_rate=48000))
    assert (stereo.audio_channels, stereo.audio_rate, stereo.audio_layout) == (2, 48000, "stereo")
    mono = probe_media(make_clip("mo.mp4", duration=1.0, channels=1, sample_rate=44100))
    assert (mono.audio_channels, mono.audio_rate, mono.audio_layout) == (1, 44100, "mono")
    silent_path = make_clip("sil.mp4", duration=1.0, audio=False)
    silent = probe_media(silent_path)
    assert not silent.has_audio and silent.audio_channels == 0 and silent.audio_rate == 0
    assert has_audio_stream(silent_path) is False and has_audio_stream(make_clip("st.mp4", duration=1.0)) is True


def test_start_timecode_is_read(tmp_path):
    with_tc = probe_media(_encode(tmp_path / "tc.mp4", "30", ["-timecode", "01:00:00:00"]))
    assert with_tc.start_timecode == "01:00:00:00"
    assert probe_media(_encode(tmp_path / "notc.mp4", "30")).start_timecode is None


def test_audio_only_file(make_tone):
    info = probe_media(make_tone("t.wav", duration=2.0, sample_rate=44100))
    assert not info.has_video and info.has_audio
    assert info.audio_rate == 44100 and abs(info.duration - 2.0) < 0.1
    assert info.fps is None and media_duration_and_rate(make_tone("t.wav", duration=2.0))[1] == 0.0


def test_corrupt_file_does_not_raise(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is not a video" * 50)
    info = probe_media(bad)
    assert not info.has_video and not info.has_audio
