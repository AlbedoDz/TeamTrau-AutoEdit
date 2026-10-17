"""Visual Continuity Evaluation and Ken Burns Ambient Motion Generator.

Evaluates adjacent scene cut boundaries to eliminate jarring exposure jumps via
Bhattacharyya distance histogram analysis, and synthesizes subtle ambient motion
keyframes on static, low-energy shots.
"""

import logging
from pathlib import Path

import cv2
import numpy as np

from src.core.config import PipelineConfig
from src.core.types import ScoredScene

logger = logging.getLogger(__name__)


def extract_frame_at_timestamp(
    video_path: Path, timestamp: float
) -> np.ndarray | None:
    """Extract a single BGR video frame at the specified timestamp in seconds."""
    if not video_path.is_file():
        return None

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frame_idx = max(0, int(round(timestamp * fps)))
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        return frame if ret and frame is not None else None
    except Exception as exc:
        logger.debug("Failed to extract frame at %.3fs from %s: %s", timestamp, video_path.name, exc)
        return None
    finally:
        cap.release()


def compute_luminance_histogram(frame: np.ndarray, bins: int = 64) -> np.ndarray:
    """Convert frame to YUV space and compute normalized 64-bin luminance histogram."""
    if frame is None or frame.size == 0:
        raise ValueError("Cannot compute histogram on empty or None frame.")

    yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV)
    y_channel = yuv[:, :, 0]

    hist = cv2.calcHist([y_channel], [0], None, [bins], [0, 256])
    cv2.normalize(hist, hist, alpha=1.0, norm_type=cv2.NORM_L1)
    return hist.reshape(bins, 1)


def evaluate_cut_continuity(
    scene_a: ScoredScene,
    scene_b: ScoredScene,
    threshold: float = 0.45,
    frame_a_override: np.ndarray | None = None,
    frame_b_override: np.ndarray | None = None,
) -> tuple[float, bool]:
    """Evaluate luminance continuity across cut boundary between scene_a and scene_b.

    Extracts the last frame of scene_a and the first frame of scene_b, converts to YUV,
    computes 64-bin normalized histograms on the Y (luminance) channel, and measures
    the Bhattacharyya distance.

    Returns:
        tuple[float, bool]: (bhattacharyya_distance, is_exposure_gap_flag)
    """
    # 1. Acquire boundary frames
    frame_a = frame_a_override if frame_a_override is not None else scene_a.metadata.get("_boundary_frame_end")
    if frame_a is None:
        t_a = max(scene_a.scene.start_time, scene_a.scene.end_time - 0.04)
        frame_a = extract_frame_at_timestamp(scene_a.scene.clip_path, t_a)

    frame_b = frame_b_override if frame_b_override is not None else scene_b.metadata.get("_boundary_frame_start")
    if frame_b is None:
        t_b = scene_b.scene.start_time
        frame_b = extract_frame_at_timestamp(scene_b.scene.clip_path, t_b)

    if frame_a is None or frame_b is None:
        # If frames are unreadable (e.g. synthetic test), assume neutral distance
        return 0.0, False

    try:
        hist_a = compute_luminance_histogram(frame_a, bins=64)
        hist_b = compute_luminance_histogram(frame_b, bins=64)

        dist = float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_BHATTACHARYYA))
        is_gap = dist > threshold
        return dist, is_gap
    except Exception as exc:
        logger.warning("Failed to evaluate cut continuity between %s and %s: %s",
                       scene_a.scene.clip_path.name, scene_b.scene.clip_path.name, exc)
        return 0.0, False


def assign_ambient_motion(
    scene: ScoredScene,
    clip_index: int = 0,
    motion_threshold: float = 0.20,
) -> str | None:
    """Assign Ken Burns subtle motion (zoom-in / zoom-out) if scene motion is below threshold.

    Alternates direction (zoom-in on even index, zoom-out on odd index) to avoid visual monotony.

    Returns:
        "zoom_in" | "zoom_out" | None
    """
    # Check scene motion score
    motion = scene.metadata.get("motion_score", scene.motion_energy)
    if motion < motion_threshold:
        return "zoom_in" if (clip_index % 2 == 0) else "zoom_out"
    return None


def build_zoompan_filter(
    motion: str,
    duration: float,
    fps: float = 30.0,
    width: int = 1080,
    height: int = 1920,
    scale_factor: float = 1.08,
) -> str:
    """Generate dynamic FFmpeg zoompan filter expression for Ken Burns motion."""
    total_frames = max(1, int(round(duration * fps)))
    # Step per frame for smooth continuous zoom
    step = (scale_factor - 1.0) / total_frames

    if motion == "zoom_in":
        z_expr = f"min(zoom+{step:.5f},{scale_factor:.2f})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif motion == "zoom_out":
        z_expr = f"max(zoom-{step:.5f},1.0)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    else:
        return f"scale={width}:{height}"

    return (
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':"
        f"d={total_frames}:s={width}x{height}:fps={fps}"
    )


class VisualFlowAnalyzer:
    """Stateful orchestrator for continuity analysis and ambient motion assignment."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def analyze_sequence_continuity(
        self, scenes: list[ScoredScene]
    ) -> list[tuple[int, float, bool]]:
        """Evaluate cut continuity across entire sequential list of scenes.

        Returns:
            list[tuple[cut_idx, distance, is_exposure_gap]]
        """
        results: list[tuple[int, float, bool]] = []
        if len(scenes) < 2:
            return results

        for i in range(len(scenes) - 1):
            dist, is_gap = evaluate_cut_continuity(
                scenes[i],
                scenes[i + 1],
                threshold=self.config.exposure_guard_threshold,
            )
            results.append((i, dist, is_gap))

        return results

    def assign_sequence_motion(self, scenes: list[ScoredScene]) -> dict[int, str]:
        """Assign ambient Ken Burns motion to eligible low-energy scenes in sequence."""
        assignments: dict[int, str] = {}
        if not self.config.enable_ken_burns:
            return assignments

        for idx, scene in enumerate(scenes):
            motion = assign_ambient_motion(
                scene,
                clip_index=idx,
                motion_threshold=self.config.ken_burns_motion_threshold,
            )
            if motion:
                assignments[idx] = motion
                scene.metadata["ken_burns"] = motion

        return assignments
