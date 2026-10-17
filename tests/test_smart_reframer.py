"""Unit tests for AI Smart 9:16 Re-framing module and EMA jitter smoothing."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

from src.core.config import PipelineConfig
from src.core.types import CropWindow, SceneCut, ScoredScene
from src.dispatchers.nvenc_renderer import NvencRenderer
from src.modules.smart_reframer import SmartReframer


@pytest.fixture
def synthetic_video_clip(tmp_path: Path) -> Path:
    """Generate a 1-second 16:9 synthetic video clip (1920x1080 @ 30fps)."""
    clip_path = tmp_path / "test_reframer_clip.mp4"
    w, h, fps = 1920, 1080, 30.0
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(clip_path), fourcc, fps, (w, h))

    for i in range(30):
        frame = np.full((h, w, 3), 40, dtype=np.uint8)
        # Draw a simulated moving person around x=500
        cv2.rectangle(frame, (450 + i * 2, 200), (550 + i * 2, 800), (200, 200, 200), -1)
        out.write(frame)
    out.release()
    return clip_path


def test_calculate_crop_window_center():
    """Verify standard 16:9 to 9:16 crop calculation centered by default."""
    cfg = PipelineConfig(canvas_width=1080, canvas_height=1920)
    reframer = SmartReframer(cfg, model=MagicMock())

    # 1920x1080 source, 9:16 ratio -> height=1080, width=608 (even)
    cw = reframer.calculate_crop_window(
        source_width=1920,
        source_height=1080,
        target_aspect_ratio=9.0 / 16.0,
        subject_x=None,
    )

    assert cw.height == 1080
    assert cw.width == 606 or cw.width == 608
    assert cw.width % 2 == 0
    assert cw.height % 2 == 0
    assert cw.x == (1920 - cw.width) // 2
    assert cw.y == 0
    assert cw.tracking_subject == "center"


def test_calculate_crop_window_clamped_bounds():
    """Verify horizontal crop window clamps within frame boundaries when subject is near edges."""
    cfg = PipelineConfig(canvas_width=1080, canvas_height=1920)
    reframer = SmartReframer(cfg, model=MagicMock())

    # Subject near extreme left edge (x=50)
    cw_left = reframer.calculate_crop_window(
        source_width=1920,
        source_height=1080,
        target_aspect_ratio=9.0 / 16.0,
        subject_x=50.0,
    )
    assert cw_left.x == 0
    assert cw_left.tracking_subject == "person"

    # Subject near extreme right edge (x=1900)
    cw_right = reframer.calculate_crop_window(
        source_width=1920,
        source_height=1080,
        target_aspect_ratio=9.0 / 16.0,
        subject_x=1900.0,
    )
    assert cw_right.x == (1920 - cw_right.width) - ((1920 - cw_right.width) % 2)
    assert cw_right.tracking_subject == "person"


def test_calculate_crop_window_already_vertical():
    """Verify already vertical video (1080x1920) retains its aspect ratio without horizontal cropping."""
    cfg = PipelineConfig(canvas_width=1080, canvas_height=1920)
    reframer = SmartReframer(cfg, model=MagicMock())

    cw = reframer.calculate_crop_window(
        source_width=1080,
        source_height=1920,
        target_aspect_ratio=9.0 / 16.0,
    )

    assert cw.width == 1080
    assert cw.height == 1920
    assert cw.x == 0
    assert cw.y == 0
    assert cw.tracking_subject == "passthrough_vertical"


def test_reframe_scene_with_mock_yolo_detection(synthetic_video_clip: Path):
    """Verify scene reframing detects person subject and centers crop window."""
    cfg = PipelineConfig(canvas_width=1080, canvas_height=1920, reframer_sample_frames=3)

    mock_model = MagicMock()
    # Mock box with coordinates [400, 100, 600, 900] -> center_x = 500
    mock_box = MagicMock()
    mock_box.xyxy = [[400.0, 100.0, 600.0, 900.0]]
    mock_box.conf = [0.92]

    mock_result = MagicMock()
    mock_result.boxes = [mock_box]
    mock_model.return_value = [mock_result]

    reframer = SmartReframer(cfg, model=mock_model)
    scene = SceneCut(
        clip_path=synthetic_video_clip,
        start_time=0.0,
        end_time=1.0,
        start_frame=0,
        end_frame=30,
        duration=1.0,
    )

    cw = reframer.reframe_scene(scene)

    assert cw.tracking_subject == "person"
    assert cw.confidence == pytest.approx(0.92, abs=0.01)
    # Expected center around 500: crop_x ~ 500 - (608 / 2) = ~196
    expected_x = int(500.0 - (cw.width / 2.0))
    expected_x = expected_x - (expected_x % 2)
    assert abs(cw.x - expected_x) <= 4


def test_reframe_all_scenes_fallback_when_no_detections(synthetic_video_clip: Path):
    """Verify graceful fallback to center-crop when model finds no human subjects."""
    cfg = PipelineConfig(canvas_width=1080, canvas_height=1920, reframing_mode="smart_crop")

    mock_model = MagicMock()
    mock_result = MagicMock()
    mock_result.boxes = []
    mock_model.return_value = [mock_result]

    reframer = SmartReframer(cfg, model=mock_model)
    scene = SceneCut(synthetic_video_clip, 0.0, 1.0, 0, 30, 1.0)
    scored = [
        ScoredScene(
            scene=scene,
            sharpness_score=90.0,
            aesthetic_score=0.8,
            composite_score=0.85,
        )
    ]

    reframed = reframer.reframe_all_scenes(scored)
    assert len(reframed) == 1
    assert reframed[0].crop_window is not None
    assert reframed[0].crop_window.tracking_subject == "center"


def test_nvenc_renderer_applies_crop_window_in_filter(tmp_path: Path):
    """Verify NvencRenderer inserts dynamic crop filter when crop_window is provided on vertical canvas."""
    cfg = PipelineConfig(canvas_width=1080, canvas_height=1920, fps=30.0)
    renderer = NvencRenderer(cfg)

    crop_win = CropWindow(x=200, y=0, width=608, height=1080, confidence=0.9, tracking_subject="person")
    scene = SceneCut(Path("e:/footage/clip1.mp4"), 0.0, 2.0, 0, 60, 2.0)
    scored_scene = ScoredScene(
        scene=scene,
        sharpness_score=95.0,
        aesthetic_score=0.9,
        composite_score=0.92,
        crop_window=crop_win,
    )

    cut_timestamps = [0.0, 2.0]
    soundtrack = Path("e:/music/bgm.wav")
    output_mp4 = tmp_path / "vertical_render.mp4"

    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.stdout = []
    mock_process.stderr = []
    captured_filter: list[str] = []

    def fake_popen(cmd, **kwargs):
        script_idx = cmd.index("-filter_complex_script") + 1
        script_file = Path(cmd[script_idx])
        if script_file.is_file():
            captured_filter.append(script_file.read_text(encoding="utf-8"))
        return mock_process

    with patch("subprocess.Popen", side_effect=fake_popen):
        output_mp4.touch()
        res = renderer.render_sequence(
            scored_scenes=[scored_scene],
            cut_timestamps=cut_timestamps,
            soundtrack_path=soundtrack,
            output_file=output_mp4,
        )

        assert res.return_code == 0
        assert len(captured_filter) == 1
        filter_str = captured_filter[0]
        # Must contain dynamic crop coordinates crop=608:1080:200:0
        assert "crop=608:1080:200:0" in filter_str
        assert "scale=1080:1920" in filter_str


def test_generate_backdrop_filter():
    """Verify generate_backdrop_filter produces valid dual-layer blur backdrop filter."""
    from src.modules.smart_reframer import generate_backdrop_filter

    filter_str = generate_backdrop_filter(target_w=1080, target_h=1920, input_label="0:v", out_label="v0")
    assert "boxblur=20:5" in filter_str
    assert "eq=brightness=-0.15" in filter_str
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in filter_str
    assert "scale=1080:1920:force_original_aspect_ratio=decrease" in filter_str
    assert "overlay=(W-w)/2:(H-h)/2[v0]" in filter_str


def test_reframe_scene_blur_backdrop_and_auto_hybrid(synthetic_video_clip: Path):
    """Verify reframing behavior under BLUR_BACKDROP and AUTO_HYBRID modes."""
    from src.core.types import LayoutMode

    cfg = PipelineConfig(canvas_width=1080, canvas_height=1920)
    mock_model = MagicMock()
    mock_model.return_value = []  # No detections (landscape/drone b-roll)
    reframer = SmartReframer(cfg, model=mock_model)

    scene = SceneCut(synthetic_video_clip, 0.0, 1.0, 0, 30, 1.0)

    # 1. BLUR_BACKDROP: directly returns blur_backdrop tracking subject
    cw_blur = reframer.reframe_scene(scene, layout_mode=LayoutMode.BLUR_BACKDROP)
    assert cw_blur.tracking_subject == "blur_backdrop"

    # 2. AUTO_HYBRID with no subjects: falls back to blur_backdrop
    cw_hybrid = reframer.reframe_scene(scene, layout_mode=LayoutMode.AUTO_HYBRID)
    assert cw_hybrid.tracking_subject == "blur_backdrop"

    # 3. SMART_CROP with no subjects: falls back to center crop
    cw_smart = reframer.reframe_scene(scene, layout_mode=LayoutMode.SMART_CROP)
    assert cw_smart.tracking_subject == "center"

