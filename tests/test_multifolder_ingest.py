"""Unit tests for multi-folder ingestion and camera role tagging."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.config import PipelineConfig
from src.core.ontology import CameraRole, infer_camera_role
from src.core.service import StudioBackendAdapter
from src.core.types import SceneCut, ScoredScene
from src.modules.audio_analyzer import AudioAnalysisResult


def test_infer_camera_role_heuristics():
    """Verify camera role inference based on directory and file patterns."""
    assert infer_camera_role("C:/Footage/DJI_Drone_Aerials/clip1.mp4") == CameraRole.DRONE
    assert infer_camera_role("E:/Media/GoPro_Action_POV/hero12.mp4") == CameraRole.ACTION_CAM
    assert infer_camera_role("D:/Shoot/Cam_B_Guest_Reaction/angle2.mov") == CameraRole.CAM_B
    assert infer_camera_role("D:/Shoot/B_Roll_Inserts/coffee.mp4") == CameraRole.B_ROLL
    assert infer_camera_role("D:/Audio/Soundtrack_BGM.wav") == CameraRole.EXTERNAL_AUDIO
    assert infer_camera_role("D:/Footage/Main_Host/interview.mp4") == CameraRole.CAM_A


def test_multi_folder_batch_ingestion(tmp_path: Path):
    """Verify backend adapter ingests clips across multiple distinct directories."""
    dir_cam_a = tmp_path / "Cam_A_Host"
    dir_drone = tmp_path / "Drone_Aerial"
    dir_action = tmp_path / "GoPro_Action"
    for d in (dir_cam_a, dir_drone, dir_action):
        d.mkdir(parents=True, exist_ok=True)

    clip1 = dir_cam_a / "host.mp4"
    clip2 = dir_drone / "drone_flyby.mp4"
    clip3 = dir_action / "gopro_stunt.mp4"
    for c in (clip1, clip2, clip3):
        c.touch()

    soundtrack = tmp_path / "bgm.mp3"
    soundtrack.touch()
    out_dir = tmp_path / "output"

    adapter = StudioBackendAdapter()
    config = PipelineConfig(enable_cache=False, generate_variants=2)

    # Mock low-level detector, audio analyzer, and scorer to avoid needing real video frames
    mock_scenes = [
        SceneCut(clip1, 0.0, 3.0, 0, 90, 3.0),
        SceneCut(clip2, 0.0, 3.0, 0, 90, 3.0),
        SceneCut(clip3, 0.0, 3.0, 0, 90, 3.0),
    ]
    mock_scored = [
        ScoredScene(mock_scenes[0], 0.8, 0.8, 0.8, 0.4, camera_role="cam_a"),
        ScoredScene(mock_scenes[1], 0.9, 0.9, 0.9, 0.7, camera_role="drone"),
        ScoredScene(mock_scenes[2], 0.7, 0.7, 0.7, 0.9, camera_role="action_cam"),
    ]

    mock_audio = AudioAnalysisResult(
        tempo_bpm=128.0,
        duration=9.0,
        sample_rate=22050,
        beats=[],
        cut_timestamps=[0.0, 3.0, 6.0, 9.0],
        extracted_audio_path=soundtrack,
        sections=[],
    )

    with (
        patch("src.modules.ingest_detector.IngestDetector.batch_process_videos", return_value=mock_scenes),
        patch("src.modules.audio_analyzer.AudioAnalyzer.analyze", return_value=mock_audio),
        patch("src.modules.visual_scorer.VisualScorer.score_and_rank_scenes", return_value=mock_scored),
        patch("src.modules.timeline_builder.TimelineBuilder.export_all") as mock_exp,
    ):
        mock_exp.return_value = MagicMock(
            clip_count=3,
            total_duration=9.0,
            fcpxml_path=out_dir / "timeline.fcpxml",
            premiere_xml_path=out_dir / "timeline.xml",
            otio_path=out_dir / "timeline.otio",
            capcut_draft_path=None,
        )

        handle = adapter.submit_job(
            input_dir=[dir_cam_a, dir_drone, dir_action],
            soundtrack=soundtrack,
            output_dir=out_dir,
            config=config,
            run_async=False,
            no_render=True,
        )

        res = handle.result
        assert res is not None
        assert res.success is True
        assert res.clip_count == 3
        assert len(res.timeline_variants) == 2
        assert "beat_driven" in res.timeline_variants
        assert "cinematic" in res.timeline_variants
