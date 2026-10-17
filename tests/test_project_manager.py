"""Unit tests for ProjectManager, ProjectData, and Open Recent history."""

from __future__ import annotations

import os
from pathlib import Path

from src.core.config import PipelineConfig
from src.ui.controllers.project_manager import (
    ProjectData,
    ProjectManager,
)

os.environ["QT_QPA_PLATFORM"] = "offscreen"


def test_project_data_serialization():
    """Verify ProjectData to_dict and from_dict bidirectional conversion."""
    data = ProjectData(
        project_name="Wedding Highlights 4K",
        input_dir="/footage/raw",
        soundtrack_path="/audio/bgm.mp3",
        output_dir="/export",
        preset_name="social_vertical",
        config={"target_duration": 60.0, "enable_speed_ramping": True},
        in_point=5.0,
        out_point=55.0,
        timeline_clips=[{"clip_id": "CLIP_1", "duration": 4.0}],
    )

    d = data.to_dict()
    assert d["project_name"] == "Wedding Highlights 4K"
    assert d["preset_name"] == "social_vertical"
    assert d["in_point"] == 5.0
    assert len(d["timeline_clips"]) == 1

    restored = ProjectData.from_dict(d)
    assert restored.project_name == data.project_name
    assert restored.input_dir == data.input_dir
    assert restored.config == data.config
    assert restored.in_point == 5.0
    assert restored.out_point == 55.0
    assert len(restored.timeline_clips) == 1


def test_project_manager_save_and_load(tmp_path: Path):
    """Verify saving to .tteproj file and reading back identical state."""
    recent_path = tmp_path / "recent.json"
    mgr = ProjectManager(recent_path=recent_path)

    project_file = tmp_path / "projects" / "wedding.tteproj"
    data = ProjectData(
        project_name="Wedding Film",
        preset_name="4k30",
        config={"vlm_provider": "openrouter"},
    )

    assert mgr.is_dirty is False
    mgr.set_dirty(True)
    assert mgr.is_dirty is True

    ok = mgr.save_project(project_file, data)
    assert ok is True
    assert project_file.exists()
    assert mgr.is_dirty is False
    assert mgr.current_project_path == project_file.resolve()

    # Read back
    mgr2 = ProjectManager(recent_path=recent_path)
    loaded = mgr2.load_project(project_file)
    assert loaded is not None
    assert loaded.project_name == "Wedding Film"
    assert loaded.preset_name == "4k30"
    assert loaded.config["vlm_provider"] == "openrouter"
    assert mgr2.current_project_path == project_file.resolve()
    assert mgr2.is_dirty is False


def test_project_manager_recent_projects_history(tmp_path: Path):
    """Verify add, cap limit, clear, and missing file filtering in Open Recent."""
    recent_path = tmp_path / "recent.json"
    mgr = ProjectManager(recent_path=recent_path, max_recent=3)

    # Initially empty
    assert mgr.get_recent_projects() == []

    # Create dummy files
    f1 = tmp_path / "p1.tteproj"
    f2 = tmp_path / "p2.tteproj"
    f3 = tmp_path / "p3.tteproj"
    f4 = tmp_path / "p4.tteproj"
    for f in (f1, f2, f3, f4):
        f.write_text("{}", encoding="utf-8")

    mgr.add_recent_project(f1)
    mgr.add_recent_project(f2)
    mgr.add_recent_project(f3)
    recents = mgr.get_recent_projects()
    assert len(recents) == 3
    assert recents[0] == f3.resolve()
    assert recents[1] == f2.resolve()
    assert recents[2] == f1.resolve()

    # Adding 4th should push oldest out
    mgr.add_recent_project(f4)
    recents = mgr.get_recent_projects()
    assert len(recents) == 3
    assert recents[0] == f4.resolve()
    assert f1.resolve() not in recents

    # If a file is deleted from disk, get_recent_projects filters it out
    f2.unlink()
    recents = mgr.get_recent_projects()
    assert len(recents) == 2
    assert f2.resolve() not in recents

    # Clear recent
    mgr.clear_recent_projects()
    assert mgr.get_recent_projects() == []


def test_project_manager_pipeline_config_serde():
    """Verify PipelineConfig serialization and deserialization with Path attributes."""
    mgr = ProjectManager()
    cfg = PipelineConfig()
    cfg.watermark_enabled = True
    cfg.watermark_path = Path("logos/brand.png")
    cfg.target_duration = 120.0

    raw = mgr.serialize_pipeline_config(cfg)
    assert raw["watermark_path"] == str(Path("logos/brand.png"))
    assert raw["target_duration"] == 120.0

    restored = mgr.deserialize_pipeline_config(raw)
    assert restored.watermark_enabled is True
    assert restored.watermark_path == Path("logos/brand.png")
    assert restored.target_duration == 120.0


def test_project_manager_full_session_persistence(tmp_path: Path):
    """Verify serialization and deserialization of complete session state with all analysis and scoring."""
    from src.core.types import (
        BeatMarker,
        CropWindow,
        SceneCut,
        SceneVLMAnalysis,
        ScoredScene,
        SongSection,
        SongSectionType,
        TimelineVariant,
    )
    from src.modules.audio_analyzer import AudioAnalysisResult

    mgr = ProjectManager()

    # 1. Scored scene with crop window and VLM tags
    cut = SceneCut(
        clip_path=tmp_path / "drone_01.mp4",
        start_time=0.0,
        end_time=5.0,
        start_frame=0,
        end_frame=150,
        duration=5.0,
    )
    cw = CropWindow(x=100, y=0, width=1080, height=1920, confidence=0.95, tracking_subject="vehicle")
    vlm = SceneVLMAnalysis(
        scene_index=1,
        description="Epic mountain sunset landscape aerial",
        tags=["mountain", "sunset", "drone", "scenic"],
        mood="cinematic",
        suggested_pacing="slow",
        is_key_moment=True,
        confidence=0.98,
    )
    scene = ScoredScene(
        scene=cut,
        sharpness_score=94.5,
        aesthetic_score=0.88,
        composite_score=0.91,
        motion_energy=0.35,
        speed=1.0,
        crop_window=cw,
        vlm_analysis=vlm,
        camera_role="drone",
        metadata={"included": True, "custom_tag": "hero_shot"},
    )

    # 2. Audio analysis
    beat = BeatMarker(timestamp=1.25, frame_idx=37, strength=0.92, is_downbeat=True)
    sec = SongSection(
        section_type=SongSectionType.DROP,
        start_time=15.0,
        end_time=30.0,
        duration=15.0,
        avg_energy=0.85,
        chroma_profile=[0.1] * 12,
        is_climax=True,
    )
    audio_res = AudioAnalysisResult(
        tempo_bpm=128.0,
        duration=45.0,
        sample_rate=48000,
        cut_timestamps=[0.0, 1.25, 2.5, 5.0],
        beats=[beat],
        sections=[sec],
    )

    # 3. Timeline variant
    var = TimelineVariant(
        name="cinematic",
        display_name="Variant 2: Cinematic",
        description="Slow scenic pacing",
        target_pacing="slow",
        clip_count=1,
        total_duration=5.0,
        fcpxml_path=tmp_path / "cinematic.fcpxml",
        sequence_scenes=[scene],
    )

    # Assemble complete ProjectData
    proj_data = ProjectData(
        project_name="Commercial Master 4K",
        version="2.0.0",
        input_dir=str(tmp_path / "raw_cam_a"),
        input_dirs=[str(tmp_path / "raw_cam_a"), str(tmp_path / "raw_drone")],
        clip_roles={str(tmp_path / "drone_01.mp4"): "drone"},
        soundtrack_path=str(tmp_path / "bgm.wav"),
        output_dir=str(tmp_path / "exports"),
        preset_name="4k30",
        config={"enable_vlm_tagging": True, "generate_variants": 3},
        in_point=1.0,
        out_point=40.0,
        playhead_time=15.5,
        scored_scenes=[mgr.serialize_scored_scene(scene, is_included=True)],
        audio_analysis=mgr.serialize_audio_analysis(audio_res),
        timeline_variants={"cinematic": mgr.serialize_timeline_variant(var)},
        active_variant_name="cinematic",
        deliverables={"render_path": str(tmp_path / "master.mp4"), "total_duration": 45.0},
    )

    save_file = tmp_path / "session_full.tteproj"
    ok = mgr.save_project(save_file, proj_data)
    assert ok is True
    assert save_file.exists()

    # Load back and verify complete state fidelity
    loaded = mgr.load_project(save_file)
    assert loaded is not None
    assert loaded.project_name == "Commercial Master 4K"
    assert loaded.version == "2.0.0"
    assert len(loaded.input_dirs) == 2
    assert loaded.clip_roles[str(tmp_path / "drone_01.mp4")] == "drone"
    assert loaded.playhead_time == 15.5
    assert loaded.in_point == 1.0
    assert loaded.out_point == 40.0

    # Deserialize scored scene
    assert len(loaded.scored_scenes) == 1
    restored_scene, inc = mgr.deserialize_scored_scene(loaded.scored_scenes[0])
    assert inc is True
    assert restored_scene.scene.clip_path == tmp_path / "drone_01.mp4"
    assert restored_scene.sharpness_score == 94.5
    assert restored_scene.aesthetic_score == 0.88
    assert restored_scene.composite_score == 0.91
    assert restored_scene.camera_role == "drone"
    assert restored_scene.crop_window is not None
    assert restored_scene.crop_window.width == 1080
    assert restored_scene.crop_window.tracking_subject == "vehicle"
    assert restored_scene.vlm_analysis is not None
    assert restored_scene.vlm_analysis.mood == "cinematic"
    assert restored_scene.vlm_analysis.tags == ["mountain", "sunset", "drone", "scenic"]
    assert restored_scene.vlm_analysis.is_key_moment is True

    # Deserialize audio analysis
    restored_audio = mgr.deserialize_audio_analysis(loaded.audio_analysis)
    assert restored_audio.tempo_bpm == 128.0
    assert restored_audio.sample_rate == 48000
    assert len(restored_audio.beats) == 1
    assert restored_audio.beats[0].is_downbeat is True
    assert len(restored_audio.sections) == 1
    assert restored_audio.sections[0].section_type == SongSectionType.DROP
    assert restored_audio.sections[0].is_climax is True

    # Deserialize variant
    assert "cinematic" in loaded.timeline_variants
    restored_var = mgr.deserialize_timeline_variant(loaded.timeline_variants["cinematic"])
    assert restored_var.name == "cinematic"
    assert restored_var.display_name == "Variant 2: Cinematic"
    assert restored_var.target_pacing == "slow"
    assert len(restored_var.sequence_scenes) == 1
    assert restored_var.sequence_scenes[0].scene.clip_path == tmp_path / "drone_01.mp4"

