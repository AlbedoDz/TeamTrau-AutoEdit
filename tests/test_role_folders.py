"""Mode B ingestion: A-roll / B-roll / Music folders."""

from pathlib import Path

from src.core.ontology import CameraRole, infer_camera_role, role_folder_of
from src.core.service import collect_video_files, find_soundtrack


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_role_folder_of_matches_directory_components_only():
    assert role_folder_of("D:/W/A-roll/x.mp4") == "a_roll"
    assert role_folder_of(Path("D:/W/b_roll/sub/x.mp4")) == "b_roll"
    assert role_folder_of("D:/W/B Roll/x.mp4") == "b_roll"
    assert role_folder_of("D:/W/BROLL/x.mp4") == "b_roll"
    assert role_folder_of("D:/W/Barollo/x.mp4") is None
    assert role_folder_of("D:/W/broll.mp4") is None
    assert role_folder_of("D:/W/x.mp4") is None


def test_hyphenated_role_folders_drive_camera_role():
    assert infer_camera_role("D:/Wedding/B-roll/detail_rings.mp4") == CameraRole.B_ROLL
    assert infer_camera_role("D:/Wedding/A-roll/vows_bride.mov") == CameraRole.CAM_A
    # An explicit folder beats file-name keywords.
    assert infer_camera_role("D:/Shoot/A-roll/DJI_0001.MP4") == CameraRole.CAM_A
    assert infer_camera_role("D:/Shoot/B-roll/GOPRO_0001.MP4") == CameraRole.B_ROLL


def test_existing_keyword_rules_still_apply():
    assert infer_camera_role("C:/Footage/DJI_Drone_Aerials/clip1.mp4") == CameraRole.DRONE
    assert infer_camera_role("D:/Shoot/B_Roll_Inserts/coffee.mp4") == CameraRole.B_ROLL
    assert infer_camera_role("D:/Footage/Main_Host/interview.mp4") == CameraRole.CAM_A


def test_collect_video_files_descends_into_role_folders_only(tmp_path: Path):
    a = _touch(tmp_path / "A-roll" / "interview_01.mp4")
    b = _touch(tmp_path / "B-roll" / "walking.mov")
    top = _touch(tmp_path / "loose.mp4")
    _touch(tmp_path / "Music" / "theme.mp3")
    _touch(tmp_path / ".cache" / "proxy.mp4")
    _touch(tmp_path / "random_sub" / "ignored.mp4")
    found, skipped = collect_video_files(tmp_path)
    assert sorted(found) == sorted([a, b, top])
    assert skipped == []


def test_find_soundtrack_single_file_in_music_folder(tmp_path: Path):
    track = _touch(tmp_path / "Music" / "theme.mp3")
    _touch(tmp_path / "Music" / "notes.txt")
    assert find_soundtrack(tmp_path) == track


def test_find_soundtrack_none_or_ambiguous(tmp_path: Path):
    assert find_soundtrack(tmp_path) is None
    _touch(tmp_path / "music" / "a.mp3")
    _touch(tmp_path / "music" / "b.wav")
    assert find_soundtrack(tmp_path) is None
