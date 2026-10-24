"""Comprehensive real-time playback engine verification tests for PlayerWidget and MonitorCanvas."""

import os
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from src.core.types import SceneCut, ScoredScene
from src.ui.app import create_app
from src.ui.widgets.player_widget import PlayerWidget, cv_frame_to_pixmap

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


def create_test_video(path: Path, frames: int = 60, fps: float = 30.0, size: tuple[int, int] = (640, 360), color: tuple[int, int, int] = (100, 150, 200)) -> None:
    """Helper to generate a test video file with distinct frame timestamps."""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(path), fourcc, fps, size)
    w, h = size
    for f in range(frames):
        frame = np.full((h, w, 3), color, dtype=np.uint8)
        # Add visual counter variation
        frame[:30, :60] = (f * 4 % 255, 255 - (f * 4 % 255), 128)
        out.write(frame)
    out.release()


def test_cv_frame_to_pixmap_simd_downscaling(qapp):
    """Verify that cv_frame_to_pixmap downsamples large 4K frames to max_dim without error."""
    # 4K frame (3840x2160)
    frame_4k = np.full((2160, 3840, 3), 120, dtype=np.uint8)
    pixmap = cv_frame_to_pixmap(frame_4k, max_dim=1280)
    assert pixmap is not None
    assert not pixmap.isNull()
    # Downscaled width must not exceed 1280
    assert pixmap.width() <= 1280
    assert pixmap.height() <= 720


def test_monitor_canvas_scaled_cache(qapp):
    """Verify MonitorCanvas pre-scales pixmap once and caches target rect."""
    player = PlayerWidget()
    raw = np.full((360, 640, 3), 150, dtype=np.uint8)
    pix = cv_frame_to_pixmap(raw)

    player.canvas.set_frame_pixmap(pix)
    assert player.canvas._cached_scaled is not None
    assert player.canvas._cached_target_rect is not None

    cached_ref = player.canvas._cached_scaled
    # Paint event should reuse cached pixmap directly without reallocating
    player.canvas.repaint()
    assert player.canvas._cached_scaled is cached_ref

    # Changing zoom invalidates and updates cache
    player.canvas.set_zoom("50%")
    assert player.canvas.zoom_mode == "50%"
    assert player.canvas._cached_scaled is not None

    player.close()


def test_realtime_playback_source_sequential_decoding(qapp, tmp_path):
    """Verify real-time playback sequential decoding advances without repeated POS_MSEC seeks."""
    video_path = tmp_path / "source_stream.mp4"
    create_test_video(video_path, frames=60, fps=30.0)

    player = PlayerWidget()
    ok = player.load_source_video(video_path)
    assert ok is True
    assert player.duration == 2.0

    player.play()
    assert player.is_playing is True
    assert player.canvas.is_playing is True

    # Simulate 10 playback ticks at 30fps
    for i in range(1, 11):
        # Mock advancing wall time by 1/30s
        player._play_start_wall_time = time.perf_counter() - (i * (1.0 / 30.0))
        player._on_playback_tick()
        assert player.canvas.current_pixmap is not None

    # Verify time advanced sequentially
    assert player.current_time > 0.25
    assert player.current_time <= 0.40
    assert player._last_decoded_src_sec is not None

    player.stop()
    assert player.is_playing is False
    assert player.canvas.is_playing is False
    player.close()


def test_realtime_playback_timeline_cut_preroll(qapp, tmp_path):
    """Verify timeline sequence pre-rolls upcoming clip and transitions across cut with zero freeze."""
    clip1 = tmp_path / "clip1.mp4"
    clip2 = tmp_path / "clip2.mp4"
    create_test_video(clip1, frames=30, fps=30.0, color=(255, 0, 0))
    create_test_video(clip2, frames=30, fps=30.0, color=(0, 255, 0))

    s1 = ScoredScene(
        scene=SceneCut(clip_path=clip1, start_frame=0, end_frame=30, start_time=0.0, end_time=1.0, duration=1.0),
        sharpness_score=80.0,
        aesthetic_score=0.8,
        composite_score=0.8,
    )
    s2 = ScoredScene(
        scene=SceneCut(clip_path=clip2, start_frame=0, end_frame=30, start_time=0.0, end_time=1.0, duration=1.0),
        sharpness_score=85.0,
        aesthetic_score=0.85,
        composite_score=0.85,
    )

    player = PlayerWidget()
    player.load_timeline_sequence([s1, s2], total_duration=2.0)
    assert player._mode == "timeline"
    assert player.duration == 2.0

    # Start playback from 0.0
    player.play()

    # Advance to 0.7s (approaching cut boundary < 0.5s remaining in clip 1)
    player._play_start_wall_time = time.perf_counter() - 0.70
    player._on_playback_tick()

    # Pre-warming of clip 2 (index 1) must be triggered
    assert player._prewarmed_clip_idx == 1

    # Cross cut boundary into clip 2 at 1.05s
    player._play_start_wall_time = time.perf_counter() - 1.05
    player._on_playback_tick()

    assert player._last_active_clip_idx == 1
    assert player.canvas.current_pixmap is not None

    player.stop()
    player.close()


def test_realtime_playback_shuttle_wallclock_sync(qapp):
    """Verify J-K-L shuttle rate multiplier updates wall-clock anchor seamlessly."""
    player = PlayerWidget()
    player.set_duration(60.0)

    # Start normal play
    player.play()
    assert player.is_playing is True

    t0_wall = player._play_start_wall_time
    time.sleep(0.01)

    # Change to 2.0x shuttle
    player.set_shuttle_rate(2.0)
    assert player.shuttle_rate == 2.0
    assert player._play_start_wall_time >= t0_wall

    # Change to stop
    player.set_shuttle_rate(0.0)
    assert player.is_playing is False
    assert player.shuttle_rate == 0.0

    player.close()
