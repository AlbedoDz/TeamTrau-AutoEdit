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
