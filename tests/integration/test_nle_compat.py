"""Premiere / CapCut exports keep speed, gaps and fit/crop (real media probed with ffprobe)."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from src.core.config import PipelineConfig
from src.core.types import CropWindow, EditClip, EditTimeline
from src.modules.capcut_exporter import _fit_transform
from src.modules.timeline_builder import TimelineBuilder


def _export(tmp_path, clips, music=None, canvas=(1080, 1920), fps=30.0):
    tl = EditTimeline("T", fps, canvas[0], canvas[1], clips, soundtrack_path=music)
    cfg = PipelineConfig(fps=fps, canvas_width=canvas[0], canvas_height=canvas[1], enable_capcut_export=True)
    b = TimelineBuilder(cfg)
    return b.export_all(b.build_from_edit_timeline(tl), tmp_path / "out", "t", formats={"premiere", "capcut"})


def test_premiere_sequence_format_speed_and_fit(tmp_path, make_clip, make_tone):
    src = make_clip("wide.mp4", duration=8.0, size="320x180")
    music = make_tone(duration=8.0)
    cw = CropWindow(100, 0, 101, 180, 0.9, "person")  # 9:16 window inside 16:9
    clips = [
        EditClip(src, 1.0, 30, 7.0, label="plain"),
        EditClip(src, 2.0, 30, 6.0, speed=2.0, label="fast"),
        EditClip(src, 0.0, 30, 7.0, label="cropped", crop_window=cw),
    ]
    res = _export(tmp_path, clips, music)
    root = ET.parse(res.premiere_xml_path).getroot()

    sc = root.find(".//sequence/media/video/format/samplecharacteristics")
    assert sc.findtext("width") == "1080" and sc.findtext("height") == "1920"

    items = root.findall(".//sequence/media/video/track/clipitem")
    assert len(items) == 3

    def effect(item, name):
        for e in item.iter("effect"):
            if e.findtext("name") == name:
                return {p.findtext("parameterid"): p for p in e.findall("parameter")}
        return None

    # plain: letterbox "set to frame size" instead of native pixels
    scale = float(effect(items[0], "Basic Motion")["scale"].findtext("value"))
    assert abs(scale - (1080 / 320) * 100) < 0.01
    # speed ramp: Time Remap 200% and in/out cover 2x the source frames
    assert float(effect(items[1], "Time Remap")["speed"].findtext("value")) == 200.0
    assert int(items[1].findtext("out")) - int(items[1].findtext("in")) == 60
    # crop window fills the canvas height, centred on the window
    p = effect(items[2], "Basic Motion")
    assert abs(float(p["scale"].findtext("value")) - (1920 / 180) * 100) < 0.01
    horiz = float(p["center"].find("value/horiz").text)
    assert horiz > 0  # window centre 150.5 < 160: image must move right to centre it


def test_capcut_speed_gap_crop_and_camera_volume(tmp_path, make_clip, make_tone):
    src = make_clip("wide.mp4", duration=8.0, size="320x180")
    music = make_tone(duration=8.0)
    cw = CropWindow(100, 0, 101, 180, 0.9, "person")
    clips = [
        EditClip(src, 1.0, 30, 7.0),
        EditClip(src, 2.0, 30, 6.0, speed=3.0),
        EditClip(src, 0.0, 90, 1.0, crop_window=cw),  # 1 s of footage in a 3 s slot -> gap
        EditClip(src, 3.0, 30, 3.0),
    ]
    res = _export(tmp_path, clips, music)
    draft = json.loads((res.capcut_draft_path / "draft_content.json").read_text(encoding="utf-8"))
    segs = draft["tracks"][0]["segments"]
    assert [s["speed"] for s in segs] == [1.0, 3.0, 1.0, 1.0]
    assert segs[1]["source_timerange"]["duration"] == 3 * segs[1]["target_timerange"]["duration"]
    assert len(segs[1]["extra_material_refs"]) == 1
    speeds = {m["id"]: m["speed"] for m in draft["materials"]["speeds"]}
    assert speeds[segs[1]["extra_material_refs"][0]] == 3.0
    # gap: the last clip starts after the (shortened) cropped clip plus the 2 s hole
    assert segs[3]["target_timerange"]["start"] == 1_000_000 + 1_000_000 + 1_000_000 + 2_000_000
    assert "clip" in segs[2] and "clip" not in segs[0]
    assert draft["materials"]["videos"][0]["width"] == 320  # real source size, not the canvas
    assert 0 < segs[0]["volume"] < 1  # camera audio ducked under the soundtrack
    assert len(draft["tracks"][1]["segments"]) == 1  # only the music, not camera audio


def test_capcut_crop_transform_math():
    # window centred in a 1920x1080 source -> no shift, scale fills canvas height
    tf = _fit_transform((656.5, 0, 607, 1080), (1920, 1080), (1080, 1920))
    assert abs(tf["transform"]["x"]) < 1e-3 and abs(tf["transform"]["y"]) < 1e-6
    fit_k = 1080 / 1920
    assert abs(tf["scale"]["x"] - (1920 / 1080) / fit_k) < 1e-6
    right = _fit_transform((1000, 0, 607, 1080), (1920, 1080), (1080, 1920))
    assert right["transform"]["x"] < 0  # window right of centre -> image moves left
    assert _fit_transform(None, (1920, 1080), (1080, 1920)) is None


def test_premiere_matches_user_rough_cut_has_stereo_tracks(tmp_path, make_clip):
    stereo = make_clip("s.mp4", duration=3.0, channels=2)
    tl = EditTimeline("RC", 30.0, 1920, 1080, [EditClip(stereo, 0.0, 30, 3.0)], kind="rough_cut")
    cfg = PipelineConfig(fps=30.0, enable_capcut_export=False)
    b = TimelineBuilder(cfg)
    res = b.export_all(b.build_from_edit_timeline(tl), Path(tmp_path) / "o", "rc", formats={"premiere"})
    root = ET.parse(res.premiere_xml_path).getroot()
    assert [t.get("premiereTrackType") for t in root.iter("track") if t.get("premiereTrackType")] == ["Stereo"]
