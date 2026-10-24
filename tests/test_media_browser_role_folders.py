"""Media Browser uses the same Mode B folder rules as the pipeline."""

import os
from pathlib import Path

import pytest

from src.core.ontology import role_folder_of
from src.ui.app import create_app
from src.ui.widgets.media_browser import MediaBrowserWidget

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * 1024)
    return p


@pytest.fixture
def project(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "Wedding"
    return {
        "root": root,
        "talk": _touch(root / "A-roll" / "vows.mp4"),
        "scenic": _touch(root / "B-roll" / "rings.mov"),
        "music": _touch(root / "Music" / "theme.mp3"),
        "ignored": _touch(root / "random" / "ignored.mp4"),
    }


def test_nearest_role_folder_wins():
    assert role_folder_of("D:/B-roll/Wedding/A-roll/x.mp4") == "a_roll"
    assert role_folder_of("D:/A-roll/Wedding/B-roll/x.mp4") == "b_roll"


def test_set_input_dir_lists_role_folder_clips_with_roles(qapp, project):
    w = MediaBrowserWidget()
    w.set_input_dir(project["root"])
    assert sorted(w.get_video_files()) == sorted([project["talk"], project["scenic"]])
    roles = w.get_clip_roles()
    assert roles[project["talk"]] == "cam_a"
    assert roles[project["scenic"]] == "b_roll"


def test_set_input_dir_autofills_music_soundtrack(qapp, project):
    w = MediaBrowserWidget()
    w.set_input_dir(project["root"])
    assert w.get_soundtrack_path() == project["music"].resolve()
    valid, msg = w.is_valid_setup()
    assert valid, msg


def test_user_soundtrack_is_not_overwritten(qapp, project, tmp_path: Path):
    chosen = _touch(tmp_path / "chosen.mp3")
    w = MediaBrowserWidget()
    w.set_soundtrack_path(chosen)
    w.set_input_dir(project["root"])
    assert w.get_soundtrack_path() == chosen.resolve()


def test_add_footage_folder_uses_per_file_roles(qapp, project):
    w = MediaBrowserWidget()
    w.add_footage_folder(project["root"] / "B-roll")
    assert w.get_clip_roles()[project["scenic"]] == "b_roll"
    w2 = MediaBrowserWidget()
    w2.add_footage_folder(project["root"])
    assert sorted(w2.get_video_files()) == sorted([project["talk"], project["scenic"]])
    assert w2.get_clip_roles()[project["talk"]] == "cam_a"
    assert w2.get_clip_roles()[project["scenic"]] == "b_roll"
