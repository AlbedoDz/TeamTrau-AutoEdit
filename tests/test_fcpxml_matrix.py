"""FCPXML matrix: fps x canvas x source. Structure, id resolution, per-asset formats, exact time grid.

Sources are described by in-memory ``MediaInfo`` objects (probe is patched) so the 216-case
table runs without encoding any media; real-ffmpeg coverage lives in test_fcpxml_exporter.py.
"""

import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from src.core.config import PipelineConfig
from src.core.media_info import MediaInfo
from src.core.timebase import frame_rate_fraction
from src.core.types import CropWindow, EditClip, EditTimeline
from src.modules import fcpxml_exporter as fx
from src.modules import timeline_builder as tb

FPS_VALUES = [23.976, 25.0, 29.97, 30.0, 59.94, 60.0]
CANVASES = [(1920, 1080), (1080, 1920), (1080, 1080), (1080, 1350), (2560, 1080), (3840, 2160)]
TIME_RE = re.compile(r"^-?\d+(/\d+)?s$")


def _info(name: str, w: int, h: int, fps: Fraction, **kw) -> MediaInfo:
    fields = {"has_video": True, "duration": 30.0, "width": w, "height": h, "fps": fps, "avg_fps": fps}
    fields.update(kw)
    return MediaInfo(path=Path(name), **fields)


SOURCES = {
    "uhd_16x9": _info("uhd.mp4", 3840, 2160, Fraction(30000, 1001), has_audio=True, audio_channels=2, audio_rate=48000),
    "hd": _info("hd.mp4", 1920, 1080, Fraction(25), has_audio=True, audio_channels=2, audio_rate=48000),
    "rotated_portrait": _info(
        "rot.mp4", 1080, 1920, Fraction(30), rotation=90, has_audio=True, audio_channels=2, audio_rate=48000
    ),
    "mono_44k": _info("mono.mp4", 1920, 1080, Fraction(24000, 1001), has_audio=True, audio_channels=1, audio_rate=44100),
    "no_audio": _info("mute.mp4", 1920, 1080, Fraction(60000, 1001)),
    "timecode": _info(
        "tc.mp4", 1920, 1080, Fraction(30), has_audio=True, audio_channels=2, audio_rate=48000,
        start_timecode="01:00:00:00",
    ),
}
MUSIC = MediaInfo(path=Path("song.wav"), has_audio=True, audio_channels=2, audio_rate=44100, duration=60.0)


@pytest.fixture
def fake_probe(monkeypatch):
    table = {s.path.name: s for s in [*SOURCES.values(), MUSIC]}

    def probe(path):
        return table.get(Path(path).name if path else "", MediaInfo(path=Path(path or "")))

    monkeypatch.setattr(fx, "probe_media", probe)
    monkeypatch.setattr(tb, "probe_media", probe)


def _on_grid(t: Fraction, rate: Fraction) -> bool:
    return (t * rate).denominator == 1


def _t(text: str) -> Fraction:
    assert TIME_RE.match(text), f"not an exact rational time: {text!r}"
    return Fraction(text[:-1])


def _build(tmp_path: Path, fps: float, canvas: tuple[int, int], src_key: str) -> ET.Element:
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    src = media / SOURCES[src_key].path.name
    music = media / "song.wav"
    src.write_bytes(b"x")
    music.write_bytes(b"x")
    n = max(1, round(fps))  # one second of timeline
    crop = CropWindow(100, 0, 608, 1080) if canvas[0] < canvas[1] and SOURCES[src_key].width > SOURCES[src_key].height else None
    clips = [
        EditClip(src, 1.0, n, 9.0, label="normal", crop_window=crop),
        EditClip(src, 3.0, n, 9.0, speed=2.0, label="fast", transition_in_frames=max(2, n // 6)),
        EditClip(src, 0.0, 3 * n, 1.0, label="short", markers=[{"name": "shake", "color": "RED"}]),  # gap
        EditClip(src, 6.0, n, 9.0, speed=0.5, label="slow"),
    ]
    tl = EditTimeline("M", fps, canvas[0], canvas[1], clips, soundtrack_path=music)
    otio_tl = tb.TimelineBuilder(PipelineConfig(fps=fps, enable_capcut_export=False)).build_from_edit_timeline(tl)
    out = fx.write_fcpxml(otio_tl, tmp_path / "m.fcpxml", fps, canvas[0], canvas[1], project_name="M")
    return ET.parse(out).getroot()


@pytest.mark.parametrize("src_key", list(SOURCES))
@pytest.mark.parametrize("canvas", CANVASES, ids=lambda c: f"{c[0]}x{c[1]}")
@pytest.mark.parametrize("fps", FPS_VALUES)
def test_matrix(tmp_path, fake_probe, fps, canvas, src_key):
    root = _build(tmp_path, fps, canvas, src_key)
    rate = frame_rate_fraction(fps)
    info = SOURCES[src_key]
    media_rate = frame_rate_fraction(info.fps)

    # well-formed, version, ids unique and every reference resolves
    assert root.tag == "fcpxml" and root.get("version") == "1.8"
    ids = [e.get("id") for e in root.find("resources")]
    assert len(ids) == len(set(ids))
    for el in root.iter():
        for attr in ("ref", "format"):
            if el.get(attr) is not None:
                assert el.get(attr) in ids, f"{el.tag}@{attr}={el.get(attr)} unresolved"

    # sequence format == canvas; asset format == probe
    seq = root.find("library/event/project/sequence")
    sfmt = root.find(f"resources/format[@id='{seq.get('format')}']")
    assert (sfmt.get("width"), sfmt.get("height")) == (str(canvas[0]), str(canvas[1]))
    assert _t(sfmt.get("frameDuration")) == 1 / rate
    video_assets = [a for a in root.findall("resources/asset") if a.get("hasVideo") == "1"]
    assert len(video_assets) == 1
    asset = video_assets[0]
    afmt = root.find(f"resources/format[@id='{asset.get('format')}']")
    assert (afmt.get("width"), afmt.get("height")) == (str(info.width), str(info.height))
    assert _t(afmt.get("frameDuration")) == 1 / media_rate
    assert afmt.get("colorSpace") == "1-1-1 (Rec. 709)"
    if info.has_audio:
        assert asset.get("hasAudio") == "1"
        assert (asset.get("audioChannels"), asset.get("audioRate")) == (
            str(info.audio_channels), str(info.audio_rate),
        )
    else:
        assert asset.get("hasAudio") == "0" and asset.get("audioChannels") is None
    asset_start = _t(asset.get("start"))
    if info.start_timecode:
        assert asset_start == 3600
    else:
        assert asset_start == 0

    # every time is an exact rational, on the right grid
    for el in root.iter():
        for attr in ("offset", "duration", "start", "tcStart", "frameDuration"):
            if el.get(attr) is not None:
                _t(el.get(attr))
    assert _on_grid(_t(asset.get("duration")), media_rate)
    assert _on_grid(asset_start, media_rate)
    spine = seq.find("spine")
    cursor = Fraction(0)
    for el in spine:
        if el.tag in ("asset-clip", "gap"):
            assert _t(el.get("offset")) == cursor
            dur = _t(el.get("duration"))
            assert _on_grid(dur, rate), f"{el.tag} duration {dur} off the {rate} grid"
            assert _on_grid(cursor, rate)
            cursor += dur
        if el.tag == "asset-clip":
            speed = Fraction(1)
            tm = el.find("timeMap")
            if tm is not None:
                p0, p1 = tm.findall("timept")
                speed = _t(p1.get("value")) / _t(p1.get("time"))
            media_t = _t(el.get("start")) * speed
            assert _on_grid(media_t, media_rate), "clip in-point must land on a source frame"
            assert media_t >= asset_start
        if el.tag == "transition":
            assert _on_grid(_t(el.get("duration")), rate)
    # spine durations sum to the sequence duration
    assert cursor == _t(seq.get("duration"))
    assert _on_grid(cursor, rate)

    # music: connected on lane -1, full timeline length, audio attrs from the probe
    first = spine.find("asset-clip")
    music_clips = [c for c in spine.iter("asset-clip") if c.get("lane") == "-1"]
    assert len(music_clips) == 1 and _t(music_clips[0].get("duration")) == cursor
    wav = next(a for a in root.findall("resources/asset") if a.get("hasVideo") == "0")
    assert (wav.get("audioChannels"), wav.get("audioRate")) == ("2", "44100")
    assert first is not None

    # reframe: conform present whenever the source size differs from the canvas
    for clip in spine.findall("asset-clip"):
        conform = clip.find("adjust-conform")
        if (info.width, info.height) != canvas:
            assert conform is not None and conform.get("type") in ("fit", "fill")
        else:
            assert conform is None
        tr = clip.find("adjust-transform")
        if tr is not None:
            assert clip.find("note") is not None  # transforms only come from a crop window
            for key in ("position", "scale"):
                assert all(re.fullmatch(r"-?\d+(\.\d+)?", v) for v in tr.get(key).split())

    # colour-prefixed marker survives
    markers = [m.get("value") for m in root.iter("marker")]
    assert markers == ["[RED] shake"]
