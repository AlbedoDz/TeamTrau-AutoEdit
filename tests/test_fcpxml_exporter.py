"""FCPXML 1.8 exporter: structure, exact timing, retime, music, unicode paths."""

import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from src.core.config import PipelineConfig
from src.core.types import CropWindow, EditClip, EditTimeline
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


def test_speed_ramp_uses_timemap_with_source_consumed(tmp_path):
    src = tmp_path / "media" / "a.mp4"
    clips = [EditClip(src, 1.0, 30, 10.0, speed=3.0, label="fast")]
    root, _ = _build(tmp_path, clips)
    el = root.find("library/event/project/sequence/spine/asset-clip")
    pts = el.findall("timeMap/timept")
    assert _t(el.get("duration")) == 1
    assert (_t(pts[0].get("time")), _t(pts[0].get("value"))) == (0, 1)
    assert (_t(pts[1].get("time")), _t(pts[1].get("value"))) == (1, 4)  # 1 s out consumes 3 s src


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
    assert mk.get("value") == "Pruned: camera_shake" and mk.get("note") == "shake"


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
