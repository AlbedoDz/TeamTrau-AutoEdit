"""Unit tests for CapCut Desktop native draft exporter (pyCapCut logic)."""

import json
from pathlib import Path

import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene
from src.modules.capcut_exporter import CapCutExporter
from src.modules.timeline_builder import TimelineBuilder


def test_export_timeline_creates_valid_capcut_draft(tmp_path: Path):
    """Verify CapCut draft directory contains valid draft_content.json and draft_meta_info.json."""
    cfg = PipelineConfig(canvas_width=1920, canvas_height=1080, fps=30.0)
    exporter = CapCutExporter(cfg)

    # Build a simple synthetic OTIO timeline
    tl = otio.schema.Timeline(name="CapCut_Test_Project")
    v_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    a_track = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)
    tl.tracks.append(v_track)
    tl.tracks.append(a_track)

    clip1 = otio.schema.Clip(
        name="clip1",
        media_reference=otio.schema.ExternalReference(
            target_url="file:///e:/raw/clip1.mp4",
            available_range=TimeRange(RationalTime(0, 30), RationalTime(150, 30)),
        ),
        source_range=TimeRange(RationalTime(0, 30), RationalTime(60, 30)),  # 2.0s
    )
    clip2 = otio.schema.Clip(
        name="clip2",
        media_reference=otio.schema.ExternalReference(
            target_url="file:///e:/raw/clip2.mp4",
            available_range=TimeRange(RationalTime(0, 30), RationalTime(150, 30)),
        ),
        source_range=TimeRange(RationalTime(30, 30), RationalTime(90, 30)),  # 3.0s
    )
    v_track.append(clip1)
    v_track.append(clip2)

    a_clip = otio.schema.Clip(
        name="bgm",
        media_reference=otio.schema.ExternalReference(
            target_url="file:///e:/music/bgm.wav",
            available_range=TimeRange(RationalTime(0, 30), RationalTime(300, 30)),
        ),
        source_range=TimeRange(RationalTime(0, 30), RationalTime(150, 30)),  # 5.0s
    )
    a_track.append(a_clip)

    draft_dir = tmp_path / "My_CapCut_Project"
    exported_dir = exporter.export_timeline(tl, draft_dir, draft_name="My_CapCut_Project")

    assert exported_dir == draft_dir
    content_file = draft_dir / "draft_content.json"
    meta_file = draft_dir / "draft_meta_info.json"

    assert content_file.is_file()
    assert meta_file.is_file()

    # 1. Validate draft_content.json
    content_data = json.loads(content_file.read_text(encoding="utf-8"))
    assert content_data["canvas_config"]["width"] == 1920
    assert content_data["canvas_config"]["height"] == 1080
    assert content_data["canvas_config"]["ratio"] == "16:9"
    assert content_data["fps"] == 30.0

    # Total duration = 5.0 seconds = 5,000,000 microseconds
    assert content_data["duration"] == 5_000_000

    # Check tracks
    tracks = content_data["tracks"]
    assert len(tracks) == 2
    video_track = tracks[0]
    audio_track = tracks[1]
    assert video_track["type"] == "video"
    assert len(video_track["segments"]) == 2
    assert audio_track["type"] == "audio"
    assert len(audio_track["segments"]) == 1

    # Check segment timing: clip1 (0 -> 2s), clip2 (2s -> 5s)
    seg1 = video_track["segments"][0]
    seg2 = video_track["segments"][1]
    assert seg1["target_timerange"]["start"] == 0
    assert seg1["target_timerange"]["duration"] == 2_000_000
    assert seg2["target_timerange"]["start"] == 2_000_000
    assert seg2["target_timerange"]["duration"] == 3_000_000

    # Check materials
    materials = content_data["materials"]
    assert len(materials["videos"]) == 2
    assert len(materials["audios"]) == 1

    # 2. Validate draft_meta_info.json
    meta_data = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta_data["draft_name"] == "My_CapCut_Project"
    assert meta_data["draft_timeline_duration"] == 5_000_000
    assert "draft_id" in meta_data
    assert len(meta_data["draft_materials"]) == 2


def test_export_timeline_vertical_preset(tmp_path: Path):
    """Verify vertical canvas (1080x1920) outputs 9:16 ratio tag for CapCut."""
    cfg = PipelineConfig(canvas_width=1080, canvas_height=1920, fps=30.0)
    exporter = CapCutExporter(cfg)

    tl = otio.schema.Timeline(name="Vertical_CapCut")
    v_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    tl.tracks.append(v_track)

    draft_dir = tmp_path / "Vertical_Draft"
    exporter.export_timeline(tl, draft_dir)

    content_data = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    assert content_data["canvas_config"]["ratio"] == "9:16"
    assert content_data["canvas_config"]["width"] == 1080
    assert content_data["canvas_config"]["height"] == 1920


def test_timeline_builder_export_all_includes_capcut_draft(tmp_path: Path):
    """Verify TimelineBuilder.export_all generates CapCut draft alongside FCPXML and Premiere XML."""
    cfg = PipelineConfig(enable_capcut_export=True)
    builder = TimelineBuilder(cfg)

    scenes = [
        ScoredScene(
            scene=SceneCut(Path("e:/footage/clip1.mp4"), 0.0, 3.0, 0, 90, 3.0),
            sharpness_score=90.0,
            aesthetic_score=0.85,
            composite_score=0.88,
        )
    ]
    cut_timestamps = [0.0, 2.0]
    soundtrack = Path("e:/music/bgm.wav")

    tl = builder.build_timeline(
        scored_scenes=scenes,
        cut_timestamps=cut_timestamps,
        soundtrack_path=soundtrack,
        timeline_name="Multi_NLE_Sequence",
    )

    res = builder.export_all(tl, tmp_path, base_name="handoff_test")

    assert res.fcpxml_path is not None and res.fcpxml_path.is_file()
    assert res.premiere_xml_path is not None and res.premiere_xml_path.is_file()
    assert res.otio_path is not None and res.otio_path.is_file()
    assert res.capcut_draft_path is not None and res.capcut_draft_path.is_dir()

    content_json = res.capcut_draft_path / "draft_content.json"
    assert content_json.is_file()
