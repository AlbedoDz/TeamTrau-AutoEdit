"""FCPXML 1.8 exporter: structure, exact timing, retime, music, unicode paths."""

import shutil
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from src.core.config import PipelineConfig
from src.core.media_info import MediaInfo
from src.core.types import CropWindow, EditClip, EditTimeline
from src.modules import fcpxml_exporter as fx
from src.modules import timeline_builder as tb
from src.modules.fcpxml_exporter import fmt_time, frame_rate, write_fcpxml
from src.modules.timeline_builder import TimelineBuilder


def _t(s: str) -> Fraction:
    s = s.rstrip("s")
    return Fraction(s)


def _build(tmp_path: Path, clips: list[EditClip], music: Path | None = None, fps: float = 30.0):
    for c in clips:
        Path(c.source_path).parent.mkdir(parents=True, exist_ok=True)
        Path(c.source_path).write_bytes(b"x")
    if music:
        music.write_bytes(b"x")
    tl = EditTimeline("T", fps, 1920, 1080, clips, soundtrack_path=music)
    cfg = PipelineConfig(fps=fps, enable_capcut_export=False)
    otio_tl = TimelineBuilder(cfg).build_from_edit_timeline(tl)
    out = write_fcpxml(otio_tl, tmp_path / "t.fcpxml", fps, 1920, 1080, project_name="T")
    return ET.parse(out).getroot(), out


def test_time_helpers():
    assert fmt_time(Fraction(0)) == "0s"
    assert fmt_time(Fraction(10)) == "10s"
    assert fmt_time(Fraction(1001, 30000)) == "1001/30000s"
    assert frame_rate(29.97) == Fraction(30000, 1001)
    assert frame_rate(23.976) == Fraction(24000, 1001)
    assert frame_rate(30.0) == Fraction(30)


def test_structure_and_spine_is_contiguous(tmp_path):
    src = tmp_path / "media" / "Đà Nẵng clip.mp4"
    clips = [
        EditClip(src, 1.0, 30, 5.0, label="a"),
        EditClip(src, 2.0, 60, 5.0, label="b"),
    ]
    root, out = _build(tmp_path, clips)
    text = out.read_text(encoding="utf-8")
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>')
    assert root.tag == "fcpxml" and root.get("version") == "1.8"
    fmt = root.find("resources/format")
    assert fmt.get("width") == "1920" and _t(fmt.get("frameDuration")) == Fraction(1, 30)
    assert not any(c.tag == "asset-clip" for c in root)  # nothing stray at the root

    spine = root.find("library/event/project/sequence/spine")
    items = [e for e in spine if e.tag == "asset-clip"]
    assert [e.get("name") for e in items] == ["a", "b"]
    assert _t(items[0].get("offset")) == 0 and _t(items[0].get("duration")) == 1
    assert _t(items[1].get("offset")) == 1 and _t(items[1].get("duration")) == 2
    assert _t(items[1].get("start")) == 2
    seq = root.find("library/event/project/sequence")
    assert _t(seq.get("duration")) == 3

    # one asset per file, percent-encoded unicode URL, ids resolve
    assets = root.findall("resources/asset")
    assert len(assets) == 1
    assert "%C4%90" in assets[0].get("src")
    assert items[0].get("ref") == assets[0].get("id")


def test_speed_ramp_uses_local_timeline_verified_on_resolve(tmp_path):
    """Retimed clips: media = local * speed, start = in-point / speed (pixel-verified in Resolve 21)."""
    src = tmp_path / "media" / "a.mp4"
    clips = [EditClip(src, 2.0, 30, 10.0, speed=2.0, label="fast")]
    root, _ = _build(tmp_path, clips)
    el = root.find("library/event/project/sequence/spine/asset-clip")
    assert _t(el.get("duration")) == 1
    assert _t(el.get("start")) == 1  # in-point 2 s / speed 2
    pts = el.findall("timeMap/timept")
    assert (_t(pts[0].get("time")), _t(pts[0].get("value"))) == (0, 0)
    local_end = _t(pts[1].get("time"))
    assert local_end == 2  # local start 1 s + 1 s of output
    assert _t(pts[1].get("value")) == local_end * 2  # slope == speed
    # media time at the clip's first frame equals the requested in-point
    assert _t(el.get("start")) * 2 == 2


def test_music_is_connected_lane_and_camera_audio_ducked(tmp_path):
    src = tmp_path / "media" / "a.mp4"
    music = tmp_path / "media" / "song.wav"
    clips = [EditClip(src, 2.0, 30, 5.0), EditClip(src, 4.0, 30, 5.0)]
    root, _ = _build(tmp_path, clips, music=music)
    spine = root.find("library/event/project/sequence/spine")
    first = spine.find("asset-clip")
    assert first.find("adjust-volume").get("amount") == "-12dB"
    m = first.find("asset-clip")
    assert m is not None and m.get("lane") == "-1" and m.get("audioRole") == "music"
    # connected offsets use the parent's source timebase: parent start 2s at timeline 0
    assert _t(m.get("offset")) == 2
    assert _t(m.get("duration")) == 2
    audio_asset = [a for a in root.findall("resources/asset") if a.get("hasVideo") == "0"]
    assert len(audio_asset) == 1 and audio_asset[0].get("hasAudio") == "1"
    # schema child order: adjust-volume precedes the connected clip
    tags = [c.tag for c in first]
    assert tags.index("adjust-volume") < tags.index("asset-clip")


def test_gap_when_scene_shorter_than_slot_and_crop_note(tmp_path):
    src = tmp_path / "media" / "a.mp4"
    cw = CropWindow(10, 0, 608, 1080, 0.9, "person")
    clips = [EditClip(src, 0.0, 90, 1.0, crop_window=cw)]  # 3 s slot, only 1 s of footage
    root, _ = _build(tmp_path, clips)
    spine = root.find("library/event/project/sequence/spine")
    kinds = [e.tag for e in spine]
    assert kinds == ["asset-clip", "gap"]
    assert _t(spine.find("gap").get("offset")) == 1 and _t(spine.find("gap").get("duration")) == 2
    assert "crop window x=10" in spine.find("asset-clip/note").text
    assert _t(root.find("library/event/project/sequence").get("duration")) == 3


def test_ntsc_rate_gives_exact_fractions(tmp_path):
    src = tmp_path / "media" / "a.mp4"
    clips = [EditClip(src, 0.0, 30, 5.0)]
    root, _ = _build(tmp_path, clips, fps=29.97)
    el = root.find("library/event/project/sequence/spine/asset-clip")
    assert _t(el.get("duration")) == Fraction(30 * 1001, 30000)


def test_transition_and_markers(tmp_path):
    src = tmp_path / "media" / "a.mp4"
    c2 = EditClip(src, 3.0, 30, 5.0, transition_in_frames=10,
                  markers=[{"name": "Pruned: camera_shake", "color": "RED", "comment": "shake"}])
    root, _ = _build(tmp_path, [EditClip(src, 0.0, 30, 5.0), c2])
    spine = root.find("library/event/project/sequence/spine")
    tr = spine.find("transition")
    assert tr is not None and tr.find("filter-video").get("ref") == root.find("resources/effect").get("id")
    assert _t(tr.get("duration")) == Fraction(10, 30)
    assert _t(tr.get("offset")) == 1 - Fraction(5, 30)
    mk = spine.findall("asset-clip")[1].find("marker")
    # FCPXML 1.8 markers carry no colour: it is encoded as a value prefix
    assert mk.get("value") == "[RED] Pruned: camera_shake" and mk.get("note") == "shake"


def test_deliver_writes_real_fcpxml_not_fcp7(tmp_path):
    """export_all must never put FCP7 xmeml into a .fcpxml file."""
    src = tmp_path / "media" / "a.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    tl = EditTimeline("T", 30.0, 1920, 1080, [EditClip(src, 0.0, 30, 5.0)])
    cfg = PipelineConfig(enable_capcut_export=False)
    builder = TimelineBuilder(cfg)
    res = builder.export_all(builder.build_from_edit_timeline(tl), tmp_path / "out", "x", formats={"fcpxml"})
    assert res.fcpxml_path is not None
    head = res.fcpxml_path.read_text(encoding="utf-8")[:200]
    assert "<fcpxml" in head or "<!DOCTYPE fcpxml>" in head
    assert "xmeml" not in res.fcpxml_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("db,expected", [(-12.0, "-12dB"), (-96.0, "-96dB")])
def test_camera_volume_parameter(tmp_path, db, expected):
    src = tmp_path / "media" / "a.mp4"
    music = tmp_path / "media" / "s.wav"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"x")
    music.write_bytes(b"x")
    tl = EditTimeline("T", 30.0, 1920, 1080, [EditClip(src, 0.0, 30, 5.0)], soundtrack_path=music)
    otio_tl = TimelineBuilder(PipelineConfig()).build_from_edit_timeline(tl)
    out = write_fcpxml(otio_tl, tmp_path / "v.fcpxml", 30.0, 1920, 1080, camera_audio_db=db)
    assert ET.parse(out).getroot().find(".//asset-clip/adjust-volume").get("amount") == expected


# --------------------------------------------------------------------------------------
# Phase 22 step 4: per-asset formats, colour, audio, timecode, music anchor, reframe
# --------------------------------------------------------------------------------------



@pytest.fixture
def fake_probe(monkeypatch):
    """Route probe_media (exporter + builder) to an in-memory table keyed by file name."""
    table: dict[str, MediaInfo] = {}

    def probe(path):
        return table.get(Path(path).name if path else "", MediaInfo(path=Path(path or "")))

    monkeypatch.setattr(fx, "probe_media", probe)
    monkeypatch.setattr(tb, "probe_media", probe)
    return table


def _video(name: str, w=1920, h=1080, fps=Fraction(30), ch=2, rate=48000, tc=None, **kw) -> MediaInfo:
    return MediaInfo(
        path=Path(name), has_video=True, has_audio=ch > 0, duration=20.0, width=w, height=h,
        fps=fps, avg_fps=fps, audio_channels=ch, audio_rate=rate if ch else 0, start_timecode=tc, **kw,
    )


def _export(tmp_path, clips, fps=30.0, w=1920, h=1080, music=None, **kw):
    for c in clips:
        Path(c.source_path).parent.mkdir(parents=True, exist_ok=True)
        Path(c.source_path).write_bytes(b"x")
    if music:
        music.write_bytes(b"x")
    tl = EditTimeline("T", fps, w, h, clips, soundtrack_path=music)
    otio_tl = TimelineBuilder(PipelineConfig(fps=fps, enable_capcut_export=False)).build_from_edit_timeline(tl)
    out = write_fcpxml(otio_tl, tmp_path / "t.fcpxml", fps, w, h, **kw)
    return ET.parse(out).getroot()


def _fmt_of(root, asset):
    return root.find(f"resources/format[@id='{asset.get('format')}']")


def test_each_source_gets_its_own_format_and_sequence_keeps_canvas(tmp_path, fake_probe):
    fake_probe["uhd.mp4"] = _video("uhd.mp4", 3840, 2160, Fraction(30000, 1001), ch=1, rate=44100)
    fake_probe["hd.mp4"] = _video("hd.mp4", 1920, 1080, Fraction(25), ch=0)
    m = tmp_path / "m"
    clips = [EditClip(m / "uhd.mp4", 1.0, 30, 5.0), EditClip(m / "hd.mp4", 1.0, 30, 5.0)]
    root = _export(tmp_path, clips)
    seq_fmt = root.find("resources/format[@id='r1']")
    assert (seq_fmt.get("width"), seq_fmt.get("height")) == ("1920", "1080")
    assert _t(seq_fmt.get("frameDuration")) == Fraction(1, 30)
    seq = root.find("library/event/project/sequence")
    assert seq.get("format") == "r1"
    a = {x.get("name"): x for x in root.findall("resources/asset")}
    uhd, hd = _fmt_of(root, a["uhd"]), _fmt_of(root, a["hd"])
    assert (uhd.get("width"), uhd.get("height")) == ("3840", "2160")
    assert _t(uhd.get("frameDuration")) == Fraction(1001, 30000)
    assert (hd.get("width"), hd.get("height")) == ("1920", "1080")
    assert _t(hd.get("frameDuration")) == Fraction(1, 25)
    # mono 44.1k / no audio come from the media, not hard-coded 2ch/48k
    assert (a["uhd"].get("audioChannels"), a["uhd"].get("audioRate")) == ("1", "44100")
    assert a["hd"].get("hasAudio") == "0" and a["hd"].get("audioChannels") is None


def test_sources_matching_the_canvas_share_the_sequence_format(tmp_path, fake_probe):
    fake_probe["a.mp4"] = _video("a.mp4")
    root = _export(tmp_path, [EditClip(tmp_path / "m" / "a.mp4", 0.0, 30, 5.0)])
    assert len(root.findall("resources/format")) == 1
    assert root.find("resources/asset").get("format") == "r1"


@pytest.mark.parametrize(
    "color,expected",
    [
        ("rec709", "1-1-1 (Rec. 709)"),
        ("rec2020_hlg", "9-18-9 (Rec. 2020 HLG)"),
        ("rec2020_pq", "9-16-9 (Rec. 2020 PQ)"),
    ],
)
def test_explicit_color_sets_sequence_colorspace(tmp_path, fake_probe, color, expected):
    fake_probe["a.mp4"] = _video("a.mp4")
    root = _export(tmp_path, [EditClip(tmp_path / "m" / "a.mp4", 0.0, 30, 5.0)], color=color)
    assert root.find("resources/format").get("colorSpace") == expected


def test_passthrough_color_derives_from_source_tags(tmp_path, fake_probe):
    fake_probe["hlg.mp4"] = _video(
        "hlg.mp4", color_transfer="arib-std-b67", color_primaries="bt2020", color_space="bt2020nc"
    )
    fake_probe["pq.mp4"] = _video("pq.mp4", 1280, 720, color_transfer="smpte2084", color_primaries="bt2020")
    clips = [EditClip(tmp_path / "m" / "hlg.mp4", 0.0, 30, 5.0), EditClip(tmp_path / "m" / "pq.mp4", 0.0, 30, 5.0)]
    for color in ("passthrough", None):
        root = _export(tmp_path, clips, color=color)
        formats = {f.get("width"): f.get("colorSpace") for f in root.findall("resources/format")}
        assert formats["1920"] == "9-18-9 (Rec. 2020 HLG)"  # sequence follows the first source
        assert formats["1280"] == "9-16-9 (Rec. 2020 PQ)"
    with pytest.raises(ValueError):
        _export(tmp_path, clips, color="bogus")


def test_asset_start_is_source_timecode_and_clips_stay_consistent(tmp_path, fake_probe):
    fake_probe["tc.mp4"] = _video("tc.mp4", tc="01:00:00:00")
    src = tmp_path / "m" / "tc.mp4"
    clips = [EditClip(src, 2.0, 30, 9.0), EditClip(src, 4.0, 30, 9.0, speed=2.0)]
    root = _export(tmp_path, clips)
    asset = root.find("resources/asset")
    assert _t(asset.get("start")) == 3600
    items = root.findall("library/event/project/sequence/spine/asset-clip")
    assert _t(items[0].get("start")) == 3602  # asset start + in-point
    assert _t(items[1].get("start")) * 2 == 3604  # retime: (asset start + in-point) / speed


def test_ntsc_timecode_counts_nominal_frames_and_drop_frame_skips():
    ntsc = Fraction(30000, 1001)
    secs, df = fx.timecode_to_seconds("01:00:00:00", ntsc)
    assert (secs, df) == (Fraction(108000) / ntsc, False)
    secs, df = fx.timecode_to_seconds("01:00:00;00", ntsc)  # DF 1h = 107892 frames
    assert df is True and secs == Fraction(107892) / ntsc
    assert fx.timecode_to_seconds("00:10:00;00", ntsc)[0] == Fraction(17982) / ntsc
    assert fx.timecode_to_seconds("00:00:01:00", Fraction(25))[0] == 1
    assert fx.timecode_to_seconds(None, ntsc) is None
    assert fx.timecode_to_seconds("garbage", ntsc) is None


def test_music_anchors_to_first_clip_even_when_all_clips_are_retimed(tmp_path, fake_probe):
    fake_probe["a.mp4"] = _video("a.mp4", tc="01:00:00:00")
    src = tmp_path / "m" / "a.mp4"
    music = tmp_path / "m" / "song.wav"
    clips = [EditClip(src, 2.0, 30, 9.0, speed=2.0), EditClip(src, 6.0, 30, 9.0, speed=3.0)]
    root = _export(tmp_path, clips, music=music)
    first = root.find("library/event/project/sequence/spine/asset-clip")
    m = first.find("asset-clip")
    assert m is not None and m.get("lane") == "-1", "soundtrack must not be dropped"
    # local-time anchor: timeline frame 0 == first clip's local start (offset 0)
    assert _t(m.get("offset")) == _t(first.get("start")) - _t(first.get("offset"))
    assert _t(m.get("duration")) == 2  # full timeline length
    assert [c.tag for c in first].index("timeMap") < [c.tag for c in first].index("asset-clip")


def test_music_prefers_normal_speed_anchor_when_available(tmp_path, fake_probe):
    src = tmp_path / "m" / "a.mp4"
    clips = [EditClip(src, 2.0, 30, 9.0, speed=2.0), EditClip(src, 5.0, 30, 9.0)]
    root = _export(tmp_path, clips, music=tmp_path / "m" / "s.wav")
    spine = root.find("library/event/project/sequence/spine")
    first, second = spine.findall("asset-clip")
    assert first.find("asset-clip") is None
    m = second.find("asset-clip")
    assert _t(m.get("offset")) == 5 - 1  # parent start 5 s at timeline 1 s


def test_audio_only_music_asset_reports_probed_channels(tmp_path, fake_probe):
    fake_probe["a.mp4"] = _video("a.mp4")
    fake_probe["s.wav"] = MediaInfo(path=Path("s.wav"), has_audio=True, audio_channels=1, audio_rate=44100)
    root = _export(
        tmp_path, [EditClip(tmp_path / "m" / "a.mp4", 0.0, 30, 5.0)], music=tmp_path / "m" / "s.wav"
    )
    wav = next(a for a in root.findall("resources/asset") if a.get("hasVideo") == "0")
    assert (wav.get("audioChannels"), wav.get("audioRate")) == ("1", "44100")


def test_marker_start_follows_clip_local_time(tmp_path, fake_probe):
    fake_probe["a.mp4"] = _video("a.mp4", tc="01:00:00:00")
    src = tmp_path / "m" / "a.mp4"
    clip = EditClip(src, 4.0, 30, 9.0, speed=2.0, markers=[{"name": "x", "color": "BLUE"}])
    root = _export(tmp_path, [clip])
    el = root.find("library/event/project/sequence/spine/asset-clip")
    assert _t(el.find("marker").get("start")) == _t(el.get("start"))
    assert el.find("marker").get("value") == "[BLUE] x"


# ---- reframe -------------------------------------------------------------------------------


def test_compute_reframe_crop_window_9x16_from_16x9():
    r = fx.compute_reframe(1920, 1080, 1080, 1920, crop=(656, 0, 608, 1080))
    assert r is not None and r.conform == "fit"
    # window height 1080 -> canvas height 1920: absolute 1.7778, relative to fit (0.5625) = 3.1605
    assert r.scale[0] == pytest.approx(1920 / 1080 / 0.5625)
    assert r.position == pytest.approx((0.0, 0.0), abs=1e-9)  # window centred in the source
    left = fx.compute_reframe(1920, 1080, 1080, 1920, crop=(0, 0, 608, 1080))
    # crop centre is left of source centre -> image moves right (+x), no vertical shift
    assert left.position[0] > 0 and left.position[1] == pytest.approx(0.0, abs=1e-9)
    # 656 px offset * 1.7778 / 1920 * 100 == 60.74% of frame height
    assert left.position[0] == pytest.approx((960 - 304) * 1920 / 1080 / 1920 * 100)
    top = fx.compute_reframe(1920, 1080, 1080, 1920, crop=(656, 0, 304, 540))
    bottom = fx.compute_reframe(1920, 1080, 1080, 1920, crop=(656, 540, 304, 540))
    assert top.position[1] < 0 < bottom.position[1]  # crop centre above the middle -> image moves down (-y)


def test_compute_reframe_modes():
    assert fx.compute_reframe(1920, 1080, 1920, 1080) is None  # identical: nothing to say
    assert fx.compute_reframe(3840, 2160, 1920, 1080) == fx.Reframe("fit")  # same aspect, 4K->HD
    assert fx.compute_reframe(1920, 1080, 1080, 1080, fit_mode="fill") == fx.Reframe("fill")
    assert fx.compute_reframe(1920, 1080, 1080, 1080, fit_mode="auto") == fx.Reframe("fit")
    st = fx.compute_reframe(1920, 1080, 1080, 1080, fit_mode="stretch")
    assert st.conform == "fit" and st.scale == pytest.approx((1.0, 1920 / 1080))
    # crop is ignored for explicit fit / stretch
    assert fx.compute_reframe(1920, 1080, 1080, 1920, crop=(0, 0, 608, 1080), fit_mode="fit") == fx.Reframe("fit")
    with pytest.raises(ValueError):
        fx.compute_reframe(1, 1, 1, 1, fit_mode="zoom")


def test_clip_gets_conform_and_transform_when_cropped(tmp_path, fake_probe):
    fake_probe["a.mp4"] = _video("a.mp4")
    cw = CropWindow(0, 0, 608, 1080, 0.9, "person")
    clips = [EditClip(tmp_path / "m" / "a.mp4", 0.0, 30, 5.0, crop_window=cw, speed=2.0)]
    root = _export(tmp_path, clips, w=1080, h=1920, music=tmp_path / "m" / "s.wav")
    el = root.find("library/event/project/sequence/spine/asset-clip")
    tags = [c.tag for c in el]
    assert tags == ["note", "timeMap", "adjust-conform", "adjust-transform", "adjust-volume", "asset-clip"]
    assert el.find("adjust-conform").get("type") == "fit"
    sx, sy = (float(v) for v in el.find("adjust-transform").get("scale").split())
    assert sx == sy and sx == pytest.approx(3.1605, abs=1e-3)
    px, py = (float(v) for v in el.find("adjust-transform").get("position").split())
    assert px > 0 and py == 0
    root2 = _export(tmp_path, clips, w=1080, h=1920, write_transforms=False)
    el2 = root2.find("library/event/project/sequence/spine/asset-clip")
    assert el2.find("adjust-conform") is not None and el2.find("adjust-transform") is None


def test_4k_source_in_hd_canvas_and_square_canvas(tmp_path, fake_probe):
    fake_probe["uhd.mp4"] = _video("uhd.mp4", 3840, 2160)
    src = tmp_path / "m" / "uhd.mp4"
    el = _export(tmp_path, [EditClip(src, 0.0, 30, 5.0)]).find("library/event/project/sequence/spine/asset-clip")
    assert el.find("adjust-conform").get("type") == "fit" and el.find("adjust-transform") is None
    el = _export(tmp_path, [EditClip(src, 0.0, 30, 5.0)], w=1080, h=1080, fit_mode="fill").find(
        "library/event/project/sequence/spine/asset-clip"
    )
    assert el.find("adjust-conform").get("type") == "fill"


def test_unprobed_source_keeps_sequence_format_and_note_only(tmp_path):
    """Files ffprobe cannot read (placeholders) fall back to the sequence format, no reframe."""
    cw = CropWindow(10, 0, 608, 1080)
    src = tmp_path / "m" / "a.mp4"
    root = _export(tmp_path, [EditClip(src, 0.0, 30, 5.0, crop_window=cw)], w=1080, h=1920)
    el = root.find("library/event/project/sequence/spine/asset-clip")
    assert el.find("adjust-conform") is None and el.find("note") is not None
    assert root.find("resources/asset").get("format") == "r1"


# ---- real ffmpeg ---------------------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_real_media_formats_timecode_and_audio(tmp_path):
    uhd = tmp_path / "uhd.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=3840x2160:rate=30000/1001:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2", "-ac", "1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-timecode", "01:00:00:00",
         "-c:a", "aac", "-shortest", str(uhd)],
        check=True,
    )
    clips = [EditClip(uhd, 0.5, 30, 1.5)]
    otio_tl = TimelineBuilder(PipelineConfig(fps=25.0, enable_capcut_export=False)).build_from_edit_timeline(
        EditTimeline("T", 25.0, 1920, 1080, clips)
    )
    out = write_fcpxml(otio_tl, tmp_path / "r.fcpxml", 25.0, 1920, 1080)
    root = ET.parse(out).getroot()
    asset = root.find("resources/asset")
    f = _fmt_of(root, asset)
    assert (f.get("width"), f.get("height")) == ("3840", "2160")
    assert _t(f.get("frameDuration")) == Fraction(1001, 30000)
    assert (asset.get("audioChannels"), asset.get("audioRate")) == ("1", "44100")
    assert _t(asset.get("start")) == Fraction(108000 * 1001, 30000)
    el = root.find("library/event/project/sequence/spine/asset-clip")
    assert _t(el.get("start")) == _t(asset.get("start")) + Fraction(15 * 1001, 30000)
    assert el.find("adjust-conform").get("type") == "fit"
