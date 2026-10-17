"""Unit tests for OpenTimelineIO timeline builder and multi-NLE serialization."""

from pathlib import Path

import opentimelineio as otio
import pytest

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene
from src.modules.timeline_builder import TimelineBuilder


@pytest.fixture
def sample_scored_scenes():
    """Create sample scored scenes for timeline assembly."""
    return [
        ScoredScene(
            scene=SceneCut(Path("e:/footage/clipA.mp4"), 0.0, 5.0, 0, 150, 5.0),
            sharpness_score=100.0,
            aesthetic_score=0.9,
            composite_score=0.95,
        ),
        ScoredScene(
            scene=SceneCut(Path("e:/footage/clipB.mp4"), 2.0, 6.0, 60, 180, 4.0),
            sharpness_score=80.0,
            aesthetic_score=0.8,
            composite_score=0.85,
        ),
    ]


def test_build_timeline_structure(sample_scored_scenes):
    """Verify OTIO tracks, clip count, and non-destructive source ranges."""
    cfg = PipelineConfig(fps=30.0)
    builder = TimelineBuilder(cfg)

    cut_timestamps = [0.0, 1.0, 2.5, 4.0]  # 3 visual cut intervals
    soundtrack = Path("e:/music/bgm.wav")

    tl = builder.build_timeline(
        scored_scenes=sample_scored_scenes,
        cut_timestamps=cut_timestamps,
        soundtrack_path=soundtrack,
        timeline_name="TestSequence",
    )

    assert tl.name == "TestSequence"
    assert len(tl.tracks) == 2

    video_track = tl.tracks[0]
    audio_track = tl.tracks[1]

    assert video_track.kind == otio.schema.TrackKind.Video
    assert audio_track.kind == otio.schema.TrackKind.Audio

    # 3 cut intervals -> 3 clips in video track
    assert len(video_track) == 3
    assert len(audio_track) == 1

    # Verify first clip duration corresponds to cut interval (1.0 sec = 30 frames)
    first_clip = video_track[0]
    assert first_clip.source_range.duration.value == 30


def test_export_all_creates_fcpxml_and_premiere_xml(tmp_path, sample_scored_scenes):
    """Verify that export_all serializes both DaVinci Resolve and Premiere XML formats."""
    cfg = PipelineConfig(fps=30.0)
    builder = TimelineBuilder(cfg)

    cut_timestamps = [0.0, 2.0, 4.0]
    soundtrack = Path("e:/music/bgm.wav")

    tl = builder.build_timeline(
        scored_scenes=sample_scored_scenes,
        cut_timestamps=cut_timestamps,
        soundtrack_path=soundtrack,
    )

    res = builder.export_all(tl, tmp_path, base_name="test_export")

    assert res.clip_count == 2
    assert res.total_duration == pytest.approx(4.0, abs=0.1)

    assert res.fcpxml_path is not None and res.fcpxml_path.is_file()
    assert res.premiere_xml_path is not None and res.premiere_xml_path.is_file()

    # Check that XML content contains sequence XML elements
    premiere_xml = res.premiere_xml_path.read_text(encoding="utf-8")
    assert "<xmeml" in premiere_xml or "<sequence" in premiere_xml or "<project" in premiere_xml


def test_build_timeline_with_teaser(sample_scored_scenes):
    """Verify teaser hook clips are prepended when include_teaser is enabled."""
    cfg = PipelineConfig(fps=30.0, enable_teaser=True, teaser_duration=4.0)
    builder = TimelineBuilder(cfg)

    # 15s timeline, long enough for teaser
    cut_timestamps = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 15.0]
    soundtrack = Path("e:/music/bgm.wav")

    tl = builder.build_timeline(
        scored_scenes=sample_scored_scenes,
        cut_timestamps=cut_timestamps,
        soundtrack_path=soundtrack,
        timeline_name="TeaserSequence",
        include_teaser=True,
    )

    video_track = tl.tracks[0]
    clip_names = [c.name for c in video_track]

    # Check that teaser clips exist
    teaser_clips = [name for name in clip_names if name.startswith("TEASER_")]
    assert len(teaser_clips) >= 3

    # Check that video duration matches expected 15.0 seconds
    dur_rational = tl.duration()
    assert dur_rational.to_seconds() == pytest.approx(15.0, abs=0.1)


def test_select_scenes_for_cuts_structural_alignment():
    """Verify that DROP gets high-action footage, INTRO/OUTRO gets scenic, and BUILDUP gets dynamic."""
    from src.core.types import SongSection, SongSectionType
    from src.modules.timeline_builder import select_scenes_for_cuts

    scene_action = ScoredScene(
        scene=SceneCut(Path("e:/footage/action.mp4"), 0.0, 4.0, 0, 120, 4.0),
        sharpness_score=95.0,
        aesthetic_score=0.9,
        composite_score=0.92,
        motion_energy=0.85,
    )
    scene_scenic = ScoredScene(
        scene=SceneCut(Path("e:/footage/scenic.mp4"), 0.0, 4.0, 0, 120, 4.0),
        sharpness_score=90.0,
        aesthetic_score=0.85,
        composite_score=0.88,
        motion_energy=0.20,
    )
    scene_dynamic = ScoredScene(
        scene=SceneCut(Path("e:/footage/dynamic.mp4"), 0.0, 4.0, 0, 120, 4.0),
        sharpness_score=85.0,
        aesthetic_score=0.80,
        composite_score=0.82,
        motion_energy=0.50,
    )
    scenes = [scene_action, scene_scenic, scene_dynamic]

    sections = [
        SongSection(SongSectionType.INTRO, 0.0, 4.0, 4.0, 0.2, is_climax=False),
        SongSection(SongSectionType.BUILDUP, 4.0, 8.0, 4.0, 0.5, is_climax=False),
        SongSection(SongSectionType.DROP, 8.0, 12.0, 4.0, 0.9, is_climax=True),
        SongSection(SongSectionType.OUTRO, 12.0, 16.0, 4.0, 0.2, is_climax=False),
    ]

    cut_timestamps = [0.0, 2.0, 6.0, 10.0, 14.0]
    # Slots:
    # 0: [0.0, 2.0] mid=1.0 -> INTRO -> scene_scenic
    # 1: [2.0, 6.0] mid=4.0 -> BUILDUP -> scene_dynamic
    # 2: [6.0, 10.0] mid=8.0 -> DROP -> scene_action
    # 3: [10.0, 14.0] mid=12.0 -> OUTRO -> scene_scenic

    selected = select_scenes_for_cuts(
        scored_scenes=scenes,
        cut_timestamps=cut_timestamps,
        sections=sections,
        action_drop_threshold=0.70,
    )

    assert len(selected) == 4
    assert selected[0].scene.clip_path.name == "scenic.mp4"
    assert selected[1].scene.clip_path.name == "dynamic.mp4"
    assert selected[2].scene.clip_path.name == "action.mp4"
    assert selected[3].scene.clip_path.name == "scenic.mp4"


def test_build_timeline_with_sections(sample_scored_scenes):
    """Verify TimelineBuilder.build_timeline integrates song sections without regression."""
    from src.core.types import SongSection, SongSectionType

    cfg = PipelineConfig(fps=30.0)
    builder = TimelineBuilder(cfg)

    cut_timestamps = [0.0, 2.0, 4.0, 6.0]
    sections = [
        SongSection(SongSectionType.INTRO, 0.0, 3.0, 3.0, 0.3, is_climax=False),
        SongSection(SongSectionType.DROP, 3.0, 6.0, 3.0, 0.85, is_climax=True),
    ]
    soundtrack = Path("e:/music/bgm.wav")

    tl = builder.build_timeline(
        scored_scenes=sample_scored_scenes,
        cut_timestamps=cut_timestamps,
        soundtrack_path=soundtrack,
        sections=sections,
    )

    assert len(tl.tracks[0]) == 3
    assert tl.duration().to_seconds() == pytest.approx(6.0, abs=0.1)


def test_build_timeline_exposure_guard_and_ken_burns(sample_scored_scenes):
    """Verify exposure guard inserts cross-dissolve on gaps and ken burns effect is attached."""
    import numpy as np

    # Configure scenes: scene 0 is dark, scene 1 is bright (exposure gap)
    s0 = sample_scored_scenes[0]
    s1 = sample_scored_scenes[1]
    s0.metadata["_boundary_frame_end"] = np.full((50, 50, 3), 10, dtype=np.uint8)
    s1.metadata["_boundary_frame_start"] = np.full((50, 50, 3), 245, dtype=np.uint8)
    s0.motion_energy = 0.05  # Trigger Ken Burns

    cfg = PipelineConfig(
        fps=30.0,
        enable_exposure_guard=True,
        exposure_guard_threshold=0.45,
        enable_ken_burns=True,
        ken_burns_motion_threshold=0.20,
    )
    builder = TimelineBuilder(cfg)

    cut_timestamps = [0.0, 2.0, 4.0]
    soundtrack = Path("e:/music/bgm.wav")

    tl = builder.build_timeline(
        scored_scenes=[s0, s1],
        cut_timestamps=cut_timestamps,
        soundtrack_path=soundtrack,
        include_teaser=False,
    )

    v_track = tl.tracks[0]
    # Track items should contain: Clip(s0), Transition(Cross Dissolve), Clip(s1)
    has_transition = any(isinstance(item, otio.schema.Transition) for item in v_track)
    assert has_transition is True

    # Clip 0 should have Ken Burns effect
    first_clip = [item for item in v_track if isinstance(item, otio.schema.Clip)][0]
    has_kb_effect = any(getattr(e, "name", "") == "KenBurns" or getattr(e, "effect_name", "") == "KenBurns" for e in first_clip.effects)
    assert has_kb_effect is True
    assert first_clip.metadata.get("AutoEdit", {}).get("ken_burns") in ("zoom_in", "zoom_out")


