"""Premiere XML must describe the real audio channels so Link Media accepts the files."""

import xml.etree.ElementTree as ET
from pathlib import Path

from src.core.config import PipelineConfig
from src.core.types import EditClip, EditTimeline
from src.modules.timeline_builder import TimelineBuilder


def test_premiere_xml_declares_stereo_mono_and_drops_silent(tmp_path, make_clip):
    stereo = make_clip("Đà Nẵng stereo.mp4", duration=3.0, channels=2)
    mono = make_clip("mono.mp4", duration=3.0, channels=1, sample_rate=44100)
    silent = make_clip("silent.mp4", duration=3.0, audio=False)
    clips = [EditClip(p, 0.0, 30, 3.0, label=p.stem) for p in (stereo, mono, silent)]
    tl = EditTimeline("T", 30.0, 1920, 1080, clips)  # no soundtrack -> camera audio on A1
    cfg = PipelineConfig(enable_capcut_export=False)
    builder = TimelineBuilder(cfg)
    res = builder.export_all(builder.build_from_edit_timeline(tl), tmp_path, "t", formats={"premiere"})
    root = ET.parse(res.premiere_xml_path).getroot()

    chans = {}
    for f in root.iter("file"):
        a = f.find("media/audio/channelcount")
        if a is not None:
            chans[f.findtext("name")] = (int(a.text), int(f.findtext("media/audio/samplecharacteristics/samplerate")))
    assert chans["Đà Nẵng stereo.mp4"] == (2, 48000)
    assert chans["mono.mp4"] == (1, 44100)
    assert "silent.mp4" not in chans  # no audio stream declared

    seq_audio = next(a for a in root.iter("audio") if a.find("track") is not None)
    types = {t.get("premiereTrackType"): len(t.findall("clipitem")) for t in seq_audio.findall("track")}
    assert types == {"Stereo": 1, "Mono": 1}  # silent clip's audio clipitem was dropped
    assert seq_audio.findtext("numOutputChannels") == "2"
    for item in seq_audio.iter("clipitem"):
        assert item.findtext("sourcetrack/mediatype") == "audio"
    vf = next(f for f in root.iter("file") if f.find("media/video/samplecharacteristics/width") is not None)
    assert vf.findtext("media/video/samplecharacteristics/width") == "160"
    assert Path(res.premiere_xml_path).read_text(encoding="utf-8").count("%C4%90") == 1  # only in pathurl
