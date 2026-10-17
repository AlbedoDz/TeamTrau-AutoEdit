"""Unit tests for Model Context Protocol (MCP) server tools."""

import asyncio
from pathlib import Path

import cv2
import numpy as np
import pytest
import soundfile as sf

from src.modules.mcp_server import (
    analyze_soundtrack,
    detect_scenes,
    export_timeline,
    mcp_server,
    reframe_video,
)


@pytest.fixture
def synthetic_audio_path(tmp_path: Path) -> Path:
    """Generate a 4-second rhythmic stereo 44.1kHz synthetic WAV audio file."""
    audio_path = tmp_path / "test_mcp_audio.wav"
    sr = 44100
    duration = 4.0
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    # Generate 120 BPM beat pulses (every 0.5 seconds)
    signal = 0.3 * np.sin(2 * np.pi * 440 * t)
    for beat_time in np.arange(0, duration, 0.5):
        idx = int(beat_time * sr)
        window = min(int(0.05 * sr), len(signal) - idx)
        signal[idx : idx + window] += 0.7 * np.sin(2 * np.pi * 880 * t[:window])

    stereo = np.column_stack([signal, signal])
    sf.write(str(audio_path), stereo, sr)
    return audio_path


@pytest.fixture
def synthetic_video_path(tmp_path: Path) -> Path:
    """Generate a 2-second 16:9 synthetic MP4 video clip."""
    video_dir = tmp_path / "footage"
    video_dir.mkdir(parents=True, exist_ok=True)
    video_path = video_dir / "clip_mcp.mp4"

    w, h, fps = 1280, 720, 30.0
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(video_path), fourcc, fps, (w, h))

    for i in range(60):
        # Create a scene transition at frame 30
        color = 60 if i < 30 else 180
        frame = np.full((h, w, 3), color, dtype=np.uint8)
        # Draw a simulated subject
        cv2.circle(frame, (400 + i * 4, 360), 80, (255, 255, 255), -1)
        out.write(frame)
    out.release()
    return video_path


def test_mcp_tools_discovery():
    """Verify standard MCP server tools are registered properly."""

    async def _check():
        tools = await mcp_server.list_tools()
        names = [t.name for t in tools]
        assert "analyze_soundtrack" in names
        assert "detect_scenes" in names
        assert "reframe_video" in names
        assert "export_timeline" in names
        assert "render_master" in names

    asyncio.run(_check())


def test_mcp_analyze_soundtrack_missing():
    """Verify analyze_soundtrack raises FileNotFoundError on invalid path."""
    with pytest.raises(FileNotFoundError):
        analyze_soundtrack("non_existent_path.wav")


def test_mcp_analyze_soundtrack(synthetic_audio_path: Path):
    """Verify analyze_soundtrack tool returns structured audio metrics."""
    res = analyze_soundtrack(
        audio_path=str(synthetic_audio_path),
        target_duration=3.0,
        duration_mode="fixed",
    )

    assert "audio_path" in res
    assert res["bpm"] > 0
    assert res["duration"] > 0
    assert "sections" in res
    assert isinstance(res["sections"], list)


def test_mcp_detect_scenes(synthetic_video_path: Path):
    """Verify detect_scenes tool extracts scene cut boundaries."""
    res = detect_scenes(
        video_path=str(synthetic_video_path),
        threshold=2.0,
        min_scene_len=0.5,
    )

    assert res["video_path"] == str(synthetic_video_path.resolve())
    assert res["scene_count"] >= 1
    assert len(res["scenes"]) == res["scene_count"]
    first_scene = res["scenes"][0]
    assert "start_time" in first_scene
    assert "end_time" in first_scene
    assert "duration" in first_scene


def test_mcp_reframe_video(synthetic_video_path: Path):
    """Verify reframe_video tool computes 9:16 vertical crop window."""
    res = reframe_video(
        video_path=str(synthetic_video_path),
        target_preset="social_vertical",
    )

    assert res["target_preset"] == "social_vertical"
    assert res["canvas_width"] == 1080
    assert res["canvas_height"] == 1920
    cw = res["crop_window"]
    assert cw["width"] > 0
    assert cw["height"] > 0
    assert cw["width"] % 2 == 0
    assert cw["height"] % 2 == 0


def test_mcp_export_timeline(
    synthetic_video_path: Path,
    synthetic_audio_path: Path,
    tmp_path: Path,
):
    """Verify export_timeline tool synthesizes timeline and multi-NLE formats."""
    out_dir = tmp_path / "mcp_out"
    input_dir = synthetic_video_path.parent

    res = export_timeline(
        input_dir=str(input_dir),
        soundtrack=str(synthetic_audio_path),
        output_dir=str(out_dir),
        preset="1080p30",
        duration_mode="fixed",
    )

    assert res["clip_count"] >= 1
    assert res["total_duration"] > 0
    assert res["fcpxml_path"] is not None
    assert Path(res["fcpxml_path"]).is_file()
    assert res["premiere_xml_path"] is not None
    assert Path(res["premiere_xml_path"]).is_file()
    assert res["capcut_draft_path"] is not None
    assert Path(res["capcut_draft_path"]).is_dir()
