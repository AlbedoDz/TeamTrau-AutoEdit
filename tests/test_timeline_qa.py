"""Unit tests for Timeline Integrity & Quality Assurance (TimelineQA)."""

import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange

from src.core.config import PipelineConfig
from src.modules.timeline_qa import TimelineQA


def make_test_timeline() -> otio.schema.Timeline:
    """Helper to assemble a minimal OTIO sequence with video and audio tracks."""
    timeline = otio.schema.Timeline(name="QA_Test_Sequence")
    v_track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    a_track = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)
    timeline.tracks.append(v_track)
    timeline.tracks.append(a_track)
    return timeline


def test_flash_frame_detection_and_clamping():
    """Verify clips with duration < 6 frames are clamped to the safe minimum threshold."""
    timeline = make_test_timeline()
    v_track = timeline.tracks[0]

    # Normal clip: 30 frames (1.0s at 30fps)
    clip_normal = otio.schema.Clip(
        name="NormalClip",
        source_range=TimeRange(RationalTime(0, 30), RationalTime(30, 30)),
    )
    # Flash frame: 2 frames (approx 0.067s, jarring visual artifact)
    clip_flash = otio.schema.Clip(
        name="FlashClip",
        source_range=TimeRange(RationalTime(100, 30), RationalTime(2, 30)),
    )

    v_track.append(clip_normal)
    v_track.append(clip_flash)

    qa = TimelineQA(PipelineConfig(fps=30.0))
    reports = qa.validate_and_sanitize(timeline, min_clip_frames=6)

    # Flash clip must be clamped to 6 frames
    assert clip_flash.duration().to_frames() == 6
    assert clip_normal.duration().to_frames() == 30

    # Report verification
    assert any("[FLASH-FRAME FIXED]" in r and "FlashClip" in r for r in reports)


def test_audio_boundary_validation():
    """Verify audio cut boundaries are audited and protected with 20ms parametric crossfade."""
    timeline = make_test_timeline()
    a_track = timeline.tracks[1]

    audio_clip = otio.schema.Clip(
        name="BackgroundMusic",
        source_range=TimeRange(RationalTime(0, 30), RationalTime(300, 30)),
    )
    a_track.append(audio_clip)

    qa = TimelineQA(PipelineConfig(fps=30.0))
    reports = qa.validate_and_sanitize(timeline)

    assert any("[AUDIO BOUNDARY OK]" in r for r in reports)
    assert audio_clip.metadata.get("AutoEdit_QA", {}).get("audio_crossfade_ms") == 20
    assert audio_clip.metadata.get("AutoEdit_QA", {}).get("click_prevention") is True
