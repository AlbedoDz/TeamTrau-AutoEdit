"""Pipeline concurrency: soundtrack analysis overlaps ingest, whisper overlaps scoring."""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.config import PipelineConfig
from src.core.service import JobCallbacks, StudioBackendAdapter
from src.core.types import SceneCut, ScoredScene
from src.modules.audio_analyzer import AudioAnalysisResult


def _setup(tmp_path: Path):
    input_dir = tmp_path / "footage"
    input_dir.mkdir()
    v1 = input_dir / "vid1.mp4"
    v1.touch()
    soundtrack = tmp_path / "audio.wav"
    soundtrack.touch()
    cut = SceneCut(v1, 0.0, 3.0, 0, 90, 3.0)
    scene = ScoredScene(cut, 100.0, 0.9, 0.95)
    audio = AudioAnalysisResult(
        tempo_bpm=120.0, duration=3.0, beats=[], cut_timestamps=[0.0, 1.5, 3.0],
        sample_rate=22050, sections=[],
    )
    return input_dir, soundtrack, cut, scene, audio


def test_audio_analysis_runs_while_ingest_is_running(tmp_path):
    input_dir, soundtrack, cut, scene, audio = _setup(tmp_path)
    ingest_running = threading.Event()
    overlap: dict[str, bool] = {}

    def slow_ingest(self, paths, **kwargs):
        ingest_running.set()
        # Audio analysis must start while we are still "ingesting".
        overlap["audio_started_during_ingest"] = audio_started.wait(timeout=5.0)
        return [cut]

    audio_started = threading.Event()

    def fake_analyze(self, path, *args, **kwargs):
        audio_started.set()
        return audio

    cfg = PipelineConfig(cache_dir=tmp_path / "c", enable_audio_fix=False, enable_vlm_tagging=False)
    with (
        patch("src.modules.ingest_detector.IngestDetector.batch_process_videos", slow_ingest),
        patch("src.modules.audio_analyzer.AudioAnalyzer.analyze", fake_analyze),
        patch("src.modules.visual_scorer.VisualScorer.score_and_rank_scenes", return_value=[scene]),
    ):
        handle = StudioBackendAdapter().submit_job(input_dir, soundtrack, tmp_path / "out", cfg)
    assert handle.result.success, handle.result.error_message
    assert overlap["audio_started_during_ingest"] is True


def test_whisper_starts_before_scoring_and_is_joined(tmp_path):
    input_dir, soundtrack, cut, scene, audio = _setup(tmp_path)
    events: list[str] = []
    whisper_started = threading.Event()

    class FakeTranscriber:
        def __init__(self, config):
            pass

        def transcribe_file(self, path):
            events.append("whisper_start")
            whisper_started.set()
            time.sleep(0.2)
            events.append("whisper_end")
            return []

        def unload_model(self):
            pass

    def scoring(*args, **kwargs):
        # Whisper began before scoring ran (concurrent, not sequential after stage 4).
        assert whisper_started.wait(timeout=5.0)
        events.append("scoring")
        return [scene]

    cfg = PipelineConfig(
        cache_dir=tmp_path / "c", enable_audio_fix=False, enable_transcription=True,
        enable_vlm_tagging=False,
    )
    with (
        patch("src.modules.ingest_detector.IngestDetector.batch_process_videos", return_value=[cut]),
        patch("src.modules.audio_analyzer.AudioAnalyzer.analyze", return_value=audio),
        patch("src.modules.visual_scorer.VisualScorer.score_and_rank_scenes", side_effect=scoring),
        patch("src.core.service.TranscriptionEngine", FakeTranscriber),
    ):
        handle = StudioBackendAdapter().submit_job(input_dir, soundtrack, tmp_path / "out", cfg)
    assert handle.result.success, handle.result.error_message
    assert events.index("whisper_start") < events.index("scoring")
    assert "whisper_end" in events  # joined before the job finished


def test_audio_error_is_reported_and_cancel_still_works(tmp_path):
    input_dir, soundtrack, cut, scene, audio = _setup(tmp_path)
    cfg = PipelineConfig(cache_dir=tmp_path / "c", enable_audio_fix=False)
    boom = MagicMock(side_effect=RuntimeError("bad soundtrack"))
    with (
        patch("src.modules.ingest_detector.IngestDetector.batch_process_videos", return_value=[cut]),
        patch("src.modules.audio_analyzer.AudioAnalyzer.analyze", boom),
    ):
        handle = StudioBackendAdapter().submit_job(input_dir, soundtrack, tmp_path / "out", cfg)
    assert handle.result.success is False
    assert "bad soundtrack" in handle.result.error_message

    # Cancelling during ingest still aborts the whole job promptly.
    adapter = StudioBackendAdapter()

    def slow_ingest(self, paths, progress_callback=None, cancel_event=None, **kw):
        for _ in range(100):
            if cancel_event.is_set():
                raise InterruptedError("cancelled")
            time.sleep(0.02)
        return [cut]

    with (
        patch("src.modules.ingest_detector.IngestDetector.batch_process_videos", slow_ingest),
        patch("src.modules.audio_analyzer.AudioAnalyzer.analyze", return_value=audio),
    ):
        handle = adapter.submit_job(input_dir, soundtrack, tmp_path / "out2", cfg, run_async=True)
        time.sleep(0.1)
        handle.cancel()
        assert handle.wait(timeout=10.0)
    assert handle.result.success is False


def test_progress_callbacks_arrive_on_job_thread(tmp_path):
    """Scoring/reframing workers must not invoke UI callbacks from pool threads."""
    input_dir, soundtrack, cut, scene, audio = _setup(tmp_path)
    cfg = PipelineConfig(cache_dir=tmp_path / "c", enable_audio_fix=False, enable_vlm_tagging=False)
    threads: set[str] = set()
    cb = JobCallbacks(on_progress=lambda *_a: threads.add(threading.current_thread().name))
    with (
        patch("src.modules.ingest_detector.IngestDetector.batch_process_videos", return_value=[cut]),
        patch("src.modules.audio_analyzer.AudioAnalyzer.analyze", return_value=audio),
    ):
        StudioBackendAdapter().submit_job(input_dir, soundtrack, tmp_path / "out", cfg, callbacks=cb)
    assert not any(n.startswith(("AutoEditAudio", "AutoEditWhisper")) for n in threads)
