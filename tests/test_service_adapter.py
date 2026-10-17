"""Unit tests for StudioBackendAdapter, JobHandle, and JobCallbacks."""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.config import PipelineConfig
from src.core.service import JobCallbacks, JobHandle, StudioBackendAdapter
from src.core.types import (
    CropWindow,
    PipelineExecutionResult,
    RenderJobResult,
    SceneCut,
    ScoredScene,
    VideoMetadata,
)
from src.modules.audio_analyzer import AudioAnalysisResult


@pytest.fixture
def mock_backend():
    return StudioBackendAdapter()


def test_job_handle_lifecycle():
    """Verify JobHandle cancellation, child process termination, and wait semantics."""
    cancel_evt = threading.Event()
    handle = JobHandle("job_test", cancel_evt)

    assert handle.job_id == "job_test"
    assert not handle.is_cancelled()
    assert handle.is_running()
    assert handle.result is None

    mock_proc = MagicMock()
    handle._child_process = mock_proc

    # Trigger cancel
    assert handle.cancel() is True
    assert handle.is_cancelled()
    mock_proc.terminate.assert_called_once()

    # Complete job
    fake_result = PipelineExecutionResult(
        success=True,
        total_duration=10.0,
        clip_count=3,
        scenes_detected=3,
        audio_bpm=120.0,
        sections_detected=2,
        fcpxml_path=Path("out.fcpxml"),
        premiere_xml_path=None,
        otio_path=None,
        capcut_draft_path=None,
        render_path=None,
        encoder_used=None,
        resolve_status=None,
        dialogue_sentences=0,
        error_message=None,
    )
    handle._result = fake_result
    handle._completed.set()

    assert not handle.is_running()
    assert handle.wait(timeout=1.0) is True
    assert handle.result == fake_result


def test_adapter_probe_media(mock_backend, tmp_path):
    """Verify probe_media delegates to IngestDetector."""
    fake_video = tmp_path / "dummy.mp4"
    fake_video.touch()

    meta = VideoMetadata(
        path=fake_video,
        duration=12.5,
        fps=30.0,
        width=1920,
        height=1080,
        total_frames=375,
    )
    with patch("src.modules.ingest_detector.IngestDetector.probe_video", return_value=meta):
        res = mock_backend.probe_media(fake_video)
        assert res.duration == 12.5
        assert res.width == 1920


def test_adapter_quick_analyze_audio(mock_backend, tmp_path):
    """Verify quick_analyze_audio delegates to AudioAnalyzer."""
    fake_audio = tmp_path / "bgm.wav"
    fake_audio.touch()

    mock_analysis = AudioAnalysisResult(
        tempo_bpm=128.0,
        duration=2.0,
        beats=[],
        cut_timestamps=[0.0, 1.0, 2.0],
        sample_rate=22050,
        sections=[],
    )
    with patch("src.modules.audio_analyzer.AudioAnalyzer.analyze", return_value=mock_analysis):
        res = mock_backend.quick_analyze_audio(fake_audio)
        assert res.tempo_bpm == 128.0
        assert len(res.cut_timestamps) == 3


def test_adapter_detect_scenes(mock_backend, tmp_path):
    """Verify detect_scenes delegates to IngestDetector."""
    fake_video = tmp_path / "clip.mp4"
    fake_video.touch()

    cuts = [SceneCut(fake_video, 0.0, 2.0, 0, 60, 2.0)]
    with patch("src.modules.ingest_detector.IngestDetector.detect_scenes", return_value=cuts):
        res = mock_backend.detect_scenes(fake_video)
        assert len(res) == 1
        assert res[0].duration == 2.0


def test_adapter_reframe_video(mock_backend, tmp_path):
    """Verify reframe_video probes video and calls SmartReframer."""
    fake_video = tmp_path / "clip.mp4"
    fake_video.touch()

    meta = VideoMetadata(fake_video, 1920, 1080, 30.0, 5.0, 150)
    crop = CropWindow(420, 0, 608, 1080, 0.85, "person")

    with patch("src.modules.ingest_detector.IngestDetector.probe_video", return_value=meta):
        with patch("src.modules.smart_reframer.SmartReframer.reframe_scene", return_value=crop):
            res = mock_backend.reframe_video(fake_video, target_preset="social_vertical")
            assert res.width == 608
            assert res.tracking_subject == "person"


def test_adapter_submit_job_sync_with_callbacks(mock_backend, tmp_path):
    """Verify synchronous submit_job notifies callbacks across stages."""
    input_dir = tmp_path / "footage"
    input_dir.mkdir()
    v1 = input_dir / "vid1.mp4"
    v1.touch()

    soundtrack = tmp_path / "audio.wav"
    soundtrack.touch()
    out_dir = tmp_path / "out"

    stages_visited: list[tuple[str, int]] = []
    progress_reported: list[tuple[float, str]] = []
    scenes_seen: list[ScoredScene] = []

    callbacks = JobCallbacks(
        on_stage_changed=lambda name, idx, total: stages_visited.append((name, idx)),
        on_progress=lambda stage_pct, tot_pct, msg: progress_reported.append((tot_pct, msg)),
        on_scene_discovered=lambda sc: scenes_seen.append(sc),
    )

    fake_cut = SceneCut(v1, 0.0, 3.0, 0, 90, 3.0)
    fake_scene = ScoredScene(fake_cut, 100.0, 0.9, 0.95)
    mock_audio = AudioAnalysisResult(
        tempo_bpm=120.0,
        duration=3.0,
        beats=[],
        cut_timestamps=[0.0, 1.5, 3.0],
        sample_rate=22050,
        sections=[],
    )
    mock_render = RenderJobResult(out_dir / "master.mp4", "hevc_nvenc", 3.0, 2048, 0)

    cfg = PipelineConfig(preset_name="1080p30")

    with patch("src.modules.ingest_detector.IngestDetector.batch_process_videos", return_value=[fake_cut]):
        with patch("src.modules.audio_analyzer.AudioAnalyzer.analyze", return_value=mock_audio):
            with patch("src.modules.visual_scorer.VisualScorer.score_and_rank_scenes", return_value=[fake_scene]):
                with patch("src.dispatchers.nvenc_renderer.NvencRenderer.render_sequence", return_value=mock_render):
                    handle = mock_backend.submit_job(
                        input_dir=input_dir,
                        soundtrack=soundtrack,
                        output_dir=out_dir,
                        config=cfg,
                        callbacks=callbacks,
                        run_async=False,
                    )

                    assert handle.result is not None
                    assert handle.result.success is True
                    assert len(stages_visited) > 0
                    assert len(progress_reported) > 0
                    assert len(scenes_seen) == 1
                    assert (out_dir / ".analysis" / "pipeline_summary.json").is_file()
                    # Analysis never exports deliverables on its own.
                    assert handle.result.render_path is None
                    assert handle.result.fcpxml_path is None
                    assert "master" in handle.result.edit_timelines


def test_adapter_submit_job_async_and_cancel(mock_backend, tmp_path):
    """Verify asynchronous submit_job executes on background thread and can be cancelled."""
    input_dir = tmp_path / "footage"
    input_dir.mkdir()
    v1 = input_dir / "vid1.mp4"
    v1.touch()

    soundtrack = tmp_path / "audio.wav"
    soundtrack.touch()
    out_dir = tmp_path / "out_async"

    cfg = PipelineConfig()

    def slow_batch_process(*args, **kwargs):
        cancel_evt = kwargs.get("cancel_event")
        for _ in range(50):
            if cancel_evt and cancel_evt.is_set():
                raise InterruptedError("Cancelled")
            time.sleep(0.05)
        return []

    with patch("src.modules.ingest_detector.IngestDetector.batch_process_videos", side_effect=slow_batch_process):
        handle = mock_backend.submit_job(
            input_dir=input_dir,
            soundtrack=soundtrack,
            output_dir=out_dir,
            config=cfg,
            run_async=True,
        )

        assert handle.is_running()
        assert mock_backend.get_job(handle.job_id) is handle

        # Cancel job
        time.sleep(0.1)
        mock_backend.cancel_job(handle.job_id)
        handle.wait(timeout=3.0)

        assert not handle.is_running()
        assert handle.result is not None
        assert handle.result.success is False
        assert "Cancelled" in (handle.result.error_message or "")
