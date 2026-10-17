"""Unit tests for the subtitle sidecar writers and ffmpeg path escaping."""

from pathlib import Path

import pytest

from src.core.types import SentenceSegment
from src.modules.subtitle_export import (
    burn_filter,
    escape_filter_path,
    format_srt_time,
    prepare_filter_path,
    write_srt,
    write_subtitle_sidecars,
)


def _s(text: str, start: float, end: float) -> SentenceSegment:
    return SentenceSegment(text=text, start_time=start, end_time=end, duration=end - start)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.0, "00:00:00,000"), (1.5, "00:00:01,500"), (61.0015, "00:01:01,002"), (3725.25, "01:02:05,250"), (-3, "00:00:00,000")],
)
def test_format_srt_time(seconds, expected):
    assert format_srt_time(seconds) == expected


def test_write_srt_is_utf8_numbered_and_skips_empty(tmp_path):
    out = write_srt(
        [_s("Xin chào", 0.0, 1.0), _s("   ", 1.0, 2.0), _s("Tiếng Việt\n\nhai dòng", 2.0, 3.0)],
        tmp_path / "sub" / "a.srt",
    )
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert text.startswith("1\n00:00:00,000 --> 00:00:01,000\nXin chào\n")
    assert "2\n00:00:02,000 --> 00:00:03,000\nTiếng Việt\nhai dòng\n" in text
    assert "3\n" not in text.replace("00:00:03", "")


def test_write_srt_enforces_minimum_cue_length(tmp_path):
    text = write_srt([_s("x", 1.0, 1.0)], tmp_path / "a.srt").read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:01,040" in text


def test_sidecars_by_mode(tmp_path):
    master = tmp_path / "m.mp4"
    sents = [_s("Chào", 0.0, 1.0)]
    assert write_subtitle_sidecars(sents, master, "none", (1920, 1080)) == []
    assert write_subtitle_sidecars(None, master, "srt", (1920, 1080)) == []
    assert [p.suffix for p in write_subtitle_sidecars(sents, master, "srt", (1920, 1080))] == [".srt"]
    assert [p.suffix for p in write_subtitle_sidecars(sents, master, "burn", (1920, 1080))] == [".srt"]
    assert [p.suffix for p in write_subtitle_sidecars(sents, master, "ass", (1080, 1920))] == [".srt", ".ass"]
    assert "PlayResX: 1080" in master.with_suffix(".ass").read_text(encoding="utf-8")  # vertical canvas


def test_escape_filter_path_windows_drive_spaces_apostrophe():
    assert escape_filter_path(r"C:\Users\a b\it's.srt") == r"C\\:/Users/a b/it\\\'s.srt"
    assert escape_filter_path("/plain/x.srt") == "/plain/x.srt"
    assert escape_filter_path("a[1],b;c.srt") == r"a\[1\]\,b\;c.srt"


def test_burn_filter_picks_filter_by_extension():
    assert burn_filter(Path("x.srt"), (1920, 1080)).startswith("subtitles=filename=x.srt:force_style=")
    assert "FontSize=20" in burn_filter(Path("x.srt"), (1920, 1080))
    assert "FontSize=16" in burn_filter(Path("x.srt"), (1080, 1920))
    assert burn_filter(Path("x.ass"), (1920, 1080)) == "ass=filename=x.ass"
    assert r"FontName=Tahoma\,FontSize=30" in burn_filter(Path("x.srt"), (1920, 1080), "Tahoma", 30)


def test_prepare_filter_path_copies_only_awkward_paths(tmp_path):
    plain = tmp_path / "plain.srt"
    plain.write_text("1\n", encoding="utf-8")
    assert prepare_filter_path(plain, tmp_path / "scratch") == plain
    odd = tmp_path / "thư mục's" / "phụ đề.srt"
    odd.parent.mkdir()
    odd.write_text("1\n", encoding="utf-8")
    copy = prepare_filter_path(odd, tmp_path / "scratch")
    assert copy != odd and copy.read_text(encoding="utf-8") == "1\n" and copy.name == "subs.srt"
