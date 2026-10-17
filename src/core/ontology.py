"""Ontology definitions for multi-camera media ingestion, camera roles, and genre taxonomy."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class CameraRole(str, Enum):
    """Semantic role assigned to ingested camera footage or media assets."""

    CAM_A = "cam_a"            # Primary subject / talking head / master shot
    CAM_B = "cam_b"            # Secondary subject / reverse angle / reaction
    DRONE = "drone"            # Aerial landscape / establishing shot
    ACTION_CAM = "action_cam"  # POV / high-vibration / extreme angle
    B_ROLL = "b_roll"          # Cutaways / ambient details / inserts
    EXTERNAL_AUDIO = "audio"   # Dedicated high-fidelity audio stem


@dataclass
class MediaFolderEntry:
    """Represents a discrete source directory ingested with an assigned camera role."""

    folder_path: Path
    role: CameraRole = CameraRole.CAM_A
    clip_count: int = 0
    total_duration: float = 0.0


def infer_camera_role(path_or_name: str | Path) -> CameraRole:
    """Infer the most probable CameraRole from directory or file naming heuristics."""
    full_path_str = str(path_or_name).replace("\\", "/").lower()

    if any(k in full_path_str for k in ("cam_b", "camb", "cam-b", "angle_b", "angle2", "guest", "reaction", "close_up")):
        return CameraRole.CAM_B
    if any(k in full_path_str for k in ("drone", "aerial", "dji", "mavic", "air3", "mini4", "fpv")):
        return CameraRole.DRONE
    if any(k in full_path_str for k in ("gopro", "action_cam", "actioncam", "hero", "insta360", "osmo", "pov")) or "/action" in full_path_str or "_action" in full_path_str or "-action" in full_path_str:
        return CameraRole.ACTION_CAM
    if any(k in full_path_str for k in ("broll", "b_roll", "cutaway", "detail", "insert", "ambient", "scenic")):
        return CameraRole.B_ROLL
    if any(k in full_path_str for k in ("audio", "soundtrack", "bgm", "mic", "voice", "podcast_mic")):
        return CameraRole.EXTERNAL_AUDIO

    return CameraRole.CAM_A


def get_role_display_name(role: CameraRole | str) -> str:
    """Human-readable display name for camera roles in UI selectors."""
    val = role.value if isinstance(role, CameraRole) else str(role)
    mapping = {
        CameraRole.CAM_A.value: "Cam A (Master/Host)",
        CameraRole.CAM_B.value: "Cam B (Guest/Reaction)",
        CameraRole.DRONE.value: "Drone (Aerial/Wide)",
        CameraRole.ACTION_CAM.value: "ActionCam (POV/Motion)",
        CameraRole.B_ROLL.value: "B-Roll (Inserts/Details)",
        CameraRole.EXTERNAL_AUDIO.value: "External Audio (Stem)",
    }
    return mapping.get(val, "Cam A (Master/Host)")


def natural_sort_key(path: Path | str) -> list[object]:
    """Human ordering for file paths: 'clip2' sorts before 'clip10' (case-insensitive)."""
    import re

    parts = re.split(r"(\d+)", str(path).lower())
    return [int(p) if p.isdigit() else p for p in parts]
