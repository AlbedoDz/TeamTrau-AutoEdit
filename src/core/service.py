"""Unified service layer and adapter interface for AutoEdit pipeline execution.

Decouples UI and API consumers from internal algorithmic modules, providing
thread-safe job management, incremental progress callbacks, and cancellation.
"""

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.core.config import PipelineConfig
from src.core.types import (
    ALL_DELIVER_FORMATS,
    CleanedSegment,
    CropWindow,
    DeliverFormat,
    DeliverResult,
    EditTimeline,
    PipelineExecutionResult,
    SceneCut,
    ScoredScene,
    VideoMetadata,
)
from src.dispatchers.nvenc_renderer import NvencRenderer
from src.dispatchers.resolve_dispatcher import ResolveDispatcher
from src.modules.audio_analyzer import AudioAnalysisResult, AudioAnalyzer
from src.modules.duration_packer import DurationPacker
from src.modules.edit_timeline import build_edit_timeline, build_rough_cut_edit_timeline
from src.modules.ingest_detector import IngestDetector
from src.modules.keyframe_cache import (
    KeyframeStore,
    attach_boundary_frames,
    build_requests,
    extract_keyframes,
    vlm_keyframes,
)
from src.modules.smart_reframer import SmartReframer
from src.modules.timeline_builder import TimelineBuilder
from src.modules.transcription_engine import TranscriptionEngine
from src.modules.visual_scorer import VisualScorer
from src.modules.vlm_assistant import VLMAssistant

logger = logging.getLogger(__name__)


@dataclass
class JobCallbacks:
    """Optional callbacks for observing pipeline progress and intermediate artifacts."""

    on_stage_changed: Callable[[str, int, int], None] | None = None
    on_progress: Callable[[float, float, str], None] | None = None
    on_scene_discovered: Callable[[ScoredScene], None] | None = None
    on_scenes_tagged: Callable[[list[ScoredScene]], None] | None = None
    on_log: Callable[[str, str], None] | None = None


class JobHandle:
    """Handle for tracking, controlling, and awaiting an asynchronous pipeline execution."""

    def __init__(self, job_id: str, cancel_event: threading.Event) -> None:
        self.job_id = job_id
        self.cancel_event = cancel_event
        self._thread: threading.Thread | None = None
        self._result: PipelineExecutionResult | DeliverResult | None = None
        self._completed = threading.Event()
        self._child_process: Any = None

    def cancel(self) -> bool:
        """Signal cancellation to the job and terminate any active child processes."""
        self.cancel_event.set()
        if self._child_process is not None:
            try:
                self._child_process.terminate()
            except Exception:
                pass
        return True

    def is_cancelled(self) -> bool:
        """Return True if cancellation has been requested."""
        return self.cancel_event.is_set()

    def is_running(self) -> bool:
        """Return True if the job is still actively executing."""
        return not self._completed.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until job completes or timeout expires."""
        return self._completed.wait(timeout=timeout)

    @property
    def result(self) -> Any:
        """Return final execution result if completed, else None."""
        return self._result


class _Background:
    """Run ``fn`` on a daemon thread; ``result()`` joins and re-raises its exception."""

    def __init__(self, fn: Callable[[], Any], name: str) -> None:
        self._value: Any = None
        self._exc: BaseException | None = None

        def _run() -> None:
            try:
                self._value = fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised in result()
                self._exc = exc

        self._thread = threading.Thread(target=_run, name=name, daemon=True)
        self._thread.start()

    def result(self) -> Any:
        self._thread.join()
        if self._exc is not None:
            raise self._exc
        return self._value


def _enforce_vram_headroom(max_allowed_gb: float = 2.0) -> None:
    """Ensure resident VRAM allocation is low before hardware encoding."""
    import gc

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            allocated_bytes = torch.cuda.memory_allocated()
            allocated_gb = allocated_bytes / (1024**3)
            if allocated_gb > max_allowed_gb:
                logger.warning(
                    "High VRAM allocation before NVENC render: %.2f GB (limit: %.2f GB). Triggering aggressive GC.",
                    allocated_gb,
                    max_allowed_gb,
                )
                gc.collect()
                torch.cuda.empty_cache()
            else:
                logger.info(
                    "VRAM headroom verified before NVENC render: %.2f GB allocated.",
                    allocated_gb,
                )
    except ImportError:
        pass


class StudioBackendAdapter:
    """Primary facade coordinating media analysis, timeline synthesis, and export."""

    def __init__(self) -> None:
        self._active_jobs: dict[str, JobHandle] = {}
        self._jobs_lock = threading.Lock()

    def probe_media(
        self, file_path: Path, config: PipelineConfig | None = None
    ) -> VideoMetadata:
        """Extract frame dimensions, framerate, and duration from video file."""
        cfg = config or PipelineConfig()
        detector = IngestDetector(cfg)
        return detector.probe_video(file_path)

    def quick_analyze_audio(
        self, audio_path: Path, config: PipelineConfig | None = None
    ) -> AudioAnalysisResult:
        """Perform rhythmic, tempo, and structural analysis on an audio soundtrack."""
        cfg = config or PipelineConfig()
        analyzer = AudioAnalyzer(cfg)
        return analyzer.analyze(audio_path)

    def detect_scenes(
        self,
        video_path: Path,
        config: PipelineConfig | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[SceneCut]:
        """Detect non-destructive scene boundaries for a single video file."""
        cfg = config or PipelineConfig()
        detector = IngestDetector(cfg)
        return detector.detect_scenes(video_path)

    def reframe_video(
        self,
        video_path: Path,
        target_preset: str = "social_vertical",
        config: PipelineConfig | None = None,
    ) -> CropWindow:
        """Calculate AI smart 9:16 pan-and-scan crop coordinates for a video."""
        cfg = config or PipelineConfig.from_preset(target_preset)
        detector = IngestDetector(cfg)
        meta = detector.probe_video(video_path)
        scene = SceneCut(
            clip_path=video_path,
            start_time=0.0,
            end_time=meta.duration,
            start_frame=0,
            end_frame=meta.total_frames,
            duration=meta.duration,
        )
        reframer = SmartReframer(cfg)
        return reframer.reframe_scene(
            scene,
            target_aspect_ratio=float(cfg.canvas_width) / float(cfg.canvas_height),
        )

    def ping_ai_endpoint(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_sec: float = 3.0,
    ) -> tuple[bool, float, str]:
        """Test round-trip latency to an OpenAI-compatible LLM/VLM endpoint."""
        import time
        import urllib.error
        import urllib.request

        target = (base_url or "http://localhost:11434/v1").rstrip("/")
        probe_url = f"{target}/models"
        req = urllib.request.Request(probe_url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                return True, latency_ms, f"Connected ({resp.status})"
        except Exception:
            # Fallback probe to root URL
            try:
                t0 = time.perf_counter()
                with urllib.request.urlopen(target, timeout=timeout_sec) as resp:
                    latency_ms = (time.perf_counter() - t0) * 1000.0
                    return True, latency_ms, f"Connected ({resp.status})"
            except Exception as exc:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                return False, latency_ms, str(exc)

    def get_job(self, job_id: str) -> JobHandle | None:
        """Look up an existing job handle by identifier."""
        with self._jobs_lock:
            return self._active_jobs.get(job_id)

    def cancel_job(self, job_id: str) -> bool:
        """Cancel an ongoing job by identifier."""
        handle = self.get_job(job_id)
        if handle:
            return handle.cancel()
        return False

    def submit_job(
        self,
        input_dir: Path | list[Path],
        soundtrack: Path | None,
        output_dir: Path,
        config: PipelineConfig,
        callbacks: JobCallbacks | None = None,
        run_async: bool = False,
        no_render: bool = True,
        force_cpu: bool = False,
        resolve: bool = False,
        dry_run_resolve: bool = False,
        export_nle: bool = False,
    ) -> JobHandle:
        """Submit an analysis + auto-edit job. It stops at "Timeline ready".

        Like DaVinci Resolve / Premiere / CapCut, nothing is exported or rendered unless
        explicitly requested: pass ``no_render=False`` / ``export_nle=True`` for a one-shot
        CLI run, otherwise call :meth:`submit_deliver_job` when the user clicks Export.
        """
        handle = self._new_handle()
        cancel_event = handle.cancel_event
        job_id = handle.job_id

        def _worker() -> None:
            try:
                res = self._execute_pipeline(
                    input_dir=input_dir,
                    soundtrack=soundtrack,
                    output_dir=output_dir,
                    config=config,
                    callbacks=callbacks,
                    cancel_event=cancel_event,
                    no_render=no_render,
                    force_cpu=force_cpu,
                    resolve=resolve,
                    dry_run_resolve=dry_run_resolve,
                    job_handle=handle,
                    export_nle=export_nle,
                )
                handle._result = res
            except Exception as exc:
                logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
                handle._result = PipelineExecutionResult(
                    success=False,
                    total_duration=0.0,
                    clip_count=0,
                    scenes_detected=0,
                    audio_bpm=0.0,
                    sections_detected=0,
                    fcpxml_path=None,
                    premiere_xml_path=None,
                    otio_path=None,
                    capcut_draft_path=None,
                    render_path=None,
                    encoder_used=None,
                    resolve_status=None,
                    dialogue_sentences=0,
                    error_message=str(exc),
                )
            finally:
                handle._completed.set()

        return self._launch(handle, _worker, run_async, "AutoEditWorker")

    def _new_handle(self) -> JobHandle:
        handle = JobHandle(f"job_{uuid.uuid4().hex[:8]}", threading.Event())
        with self._jobs_lock:
            self._active_jobs[handle.job_id] = handle
        return handle

    @staticmethod
    def _launch(
        handle: JobHandle, worker: Callable[[], None], run_async: bool, thread_prefix: str
    ) -> JobHandle:
        if run_async:
            thread = threading.Thread(
                target=worker, name=f"{thread_prefix}-{handle.job_id}", daemon=True
            )
            handle._thread = thread
            thread.start()
        else:
            worker()
        return handle

    # ------------------------------------------------------------------
    # Deliver (explicit export / render)
    # ------------------------------------------------------------------
    def submit_deliver_job(
        self,
        edit_timeline: EditTimeline,
        output_dir: Path,
        config: PipelineConfig,
        formats: set[DeliverFormat] | None = None,
        callbacks: JobCallbacks | None = None,
        run_async: bool = False,
        force_cpu: bool = False,
        preview: bool = False,
        resolve: bool = False,
        dry_run_resolve: bool = False,
        base_name: str | None = None,
    ) -> JobHandle:
        """Export / render an existing edit timeline. Never re-runs analysis.

        ``preview=True`` renders a fast 540p proxy into ``cache_dir/preview`` instead of
        a master deliverable.
        """
        handle = self._new_handle()

        def _worker() -> None:
            try:
                handle._result = self._execute_deliver(
                    edit_timeline,
                    output_dir,
                    config,
                    formats=set(formats) if formats is not None else {"mp4"},
                    callbacks=callbacks,
                    cancel_event=handle.cancel_event,
                    force_cpu=force_cpu,
                    preview=preview,
                    resolve=resolve,
                    dry_run_resolve=dry_run_resolve,
                    base_name=base_name,
                    job_handle=handle,
                )
            except Exception as exc:
                logger.error("Deliver job %s failed: %s", handle.job_id, exc, exc_info=True)
                handle._result = DeliverResult(
                    success=False, output_dir=output_dir, is_preview=preview, error_message=str(exc)
                )
            finally:
                handle._completed.set()

        return self._launch(handle, _worker, run_async, "AutoEditDeliver")

    def _execute_deliver(
        self,
        edit_tl: EditTimeline,
        output_dir: Path,
        config: PipelineConfig,
        formats: set[DeliverFormat],
        callbacks: JobCallbacks | None,
        cancel_event: threading.Event,
        force_cpu: bool = False,
        preview: bool = False,
        resolve: bool = False,
        dry_run_resolve: bool = False,
        base_name: str | None = None,
        job_handle: JobHandle | None = None,
        render_file_name: str | None = None,
    ) -> DeliverResult:
        unknown = set(formats) - set(ALL_DELIVER_FORMATS)
        if unknown:
            raise ValueError(f"Unknown deliver format(s): {sorted(unknown)}")
        if not edit_tl.clips:
            raise ValueError("Timeline is empty - nothing to deliver.")

        def progress(pct: float, msg: str) -> None:
            if cancel_event.is_set():
                raise InterruptedError("Deliver cancelled by user.")
            if callbacks and callbacks.on_progress:
                callbacks.on_progress(pct, pct, msg)

        def stage(name: str, idx: int, total: int) -> None:
            if callbacks and callbacks.on_stage_changed:
                callbacks.on_stage_changed(name, idx, total)

        result = DeliverResult(success=False, output_dir=output_dir, is_preview=preview)
        if preview:
            out_dir = Path(config.cache_dir).resolve() / "preview"
            out_dir.mkdir(parents=True, exist_ok=True)
            stage("Rendering 540p preview", 1, 1)
            renderer = NvencRenderer(config)
            res = renderer.render_edit_timeline(
                edit_tl,
                out_dir / f"preview_{edit_tl.variant_key}.mp4",
                force_cpu=force_cpu,
                preview=True,
                progress_callback=progress,
                cancel_event=cancel_event,
                job_handle=job_handle,
            )
            result.render_path, result.encoder_used = res.output_path, res.encoder_used
            result.duration, result.success = res.duration, True
            return result

        output_dir.mkdir(parents=True, exist_ok=True)
        name = base_name or f"{edit_tl.name}"
        nle_formats = {f for f in formats if f != "mp4"}
        if resolve or dry_run_resolve:
            nle_formats.add("fcpxml")
        total = (1 if nle_formats else 0) + (1 if "mp4" in formats else 0)
        step = 0

        if nle_formats:
            step += 1
            stage("Exporting NLE interchange files", step, total)
            progress(10.0, "Building OTIO timeline...")
            builder = TimelineBuilder(config)
            otio_tl = builder.build_from_edit_timeline(edit_tl)
            try:
                from src.modules.timeline_qa import TimelineQA

                for r in TimelineQA(config).validate_and_sanitize(
                    otio_tl, min_clip_frames=config.min_clip_frames_threshold
                ):
                    logger.info("[QA Audit] %s", r)
            except Exception as qa_err:
                logger.warning("Timeline QA sanitization warning: %s", qa_err)
            exp_cfg = config.model_copy(
                update={"canvas_width": edit_tl.canvas_width, "canvas_height": edit_tl.canvas_height, "fps": edit_tl.fps}
            )
            exp = TimelineBuilder(exp_cfg).export_all(otio_tl, output_dir, base_name=name, formats=nle_formats)
            result.fcpxml_path = exp.fcpxml_path
            result.premiere_xml_path = exp.premiere_xml_path
            result.otio_path = exp.otio_path
            result.capcut_draft_path = exp.capcut_draft_path
            progress(100.0, "NLE interchange files exported")

            if resolve or dry_run_resolve:
                dispatcher = ResolveDispatcher(config)
                result.resolve_status = dispatcher.dispatch_project(
                    fcpxml_path=exp.fcpxml_path or (output_dir / f"{name}.fcpxml"),
                    source_media_paths=sorted({Path(c.source_path) for c in edit_tl.clips}),
                    project_name=name,
                    render_output_dir=output_dir / "resolve_renders",
                    dry_run=dry_run_resolve,
                )

        if "mp4" in formats:
            step += 1
            stage("Master Video Rendering (FFmpeg NVENC)", step, total)
            _enforce_vram_headroom(max_allowed_gb=2.0)
            renderer = NvencRenderer(config)
            res = renderer.render_edit_timeline(
                edit_tl,
                output_dir / (render_file_name or f"{name}.mp4"),
                force_cpu=force_cpu,
                progress_callback=progress,
                cancel_event=cancel_event,
                job_handle=job_handle,
            )
            result.render_path, result.encoder_used = res.output_path, res.encoder_used
            result.duration = res.duration
        else:
            result.duration = edit_tl.duration

        result.success = True
        return result

    def _execute_pipeline(
        self,
        input_dir: Path | list[Path],
        soundtrack: Path | None,
        output_dir: Path,
        config: PipelineConfig,
        callbacks: JobCallbacks | None,
        cancel_event: threading.Event,
        no_render: bool,
        force_cpu: bool,
        resolve: bool,
        dry_run_resolve: bool,
        job_handle: JobHandle | None,
        export_nle: bool = False,
    ) -> PipelineExecutionResult:
        """Synchronously execute analysis + auto-edit, ending at "Timeline ready"."""
        total_stages = 7 if not no_render else 6
        output_dir.mkdir(parents=True, exist_ok=True)
        # Analysis side-products live next to the project, never among deliverables.
        analysis_dir = output_dir / ".analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)

        def _check_cancel() -> None:
            if cancel_event.is_set():
                raise InterruptedError("Pipeline execution cancelled by user.")

        def _notify_stage(name: str, idx: int) -> None:
            _check_cancel()
            if callbacks and callbacks.on_stage_changed:
                callbacks.on_stage_changed(name, idx, total_stages)
            logger.info("[Stage %d/%d] %s", idx, total_stages, name)

        def _notify_progress(stage_pct: float, stage_idx: int, message: str) -> None:
            _check_cancel()
            if callbacks and callbacks.on_progress:
                base_pct = ((stage_idx - 1) / total_stages) * 100.0
                total_pct = base_pct + (stage_pct / total_stages)
                callbacks.on_progress(stage_pct, total_pct, message)

        # -------------------------------------------------------------
        # Stage 1: Ingestion & Shot Detection (Multi-Source Capable)
        # -------------------------------------------------------------
        _notify_stage("Ingesting & Detecting Scenes", 1)
        video_exts = {".mp4", ".mov", ".mkv", ".m4v"}
        collected_files: list[Path] = []
        if isinstance(input_dir, list):
            for item in input_dir:
                p = Path(item)
                if p.is_dir():
                    collected_files.extend(
                        [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in video_exts]
                    )
                elif p.is_file() and p.suffix.lower() in video_exts:
                    collected_files.append(p)
        else:
            p = Path(input_dir)
            if p.is_dir():
                collected_files = [
                    f for f in p.iterdir() if f.is_file() and f.suffix.lower() in video_exts
                ]
            elif p.is_file() and p.suffix.lower() in video_exts:
                collected_files = [p]

        # Deduplicate while preserving order
        seen_paths = set()
        raw_files: list[Path] = []
        for f in collected_files:
            rf = f.resolve()
            if rf not in seen_paths:
                seen_paths.add(rf)
                raw_files.append(f)

        if not raw_files:
            raise FileNotFoundError(f"No video files found in {input_dir}")

        detector = IngestDetector(config)

        # Pre-ingest VFR detection and normalization pass
        probed_meta: dict[Path, VideoMetadata] = {}
        if getattr(config, "auto_normalize_vfr", True):
            normalized_raw = []
            for rf in raw_files:
                _check_cancel()
                try:
                    meta = detector.probe_video(rf)
                    if meta.is_vfr:
                        _notify_progress(5.0, 1, f"Normalizing VFR footage ({rf.name})...")
                        rf = detector.normalize_to_cfr(rf, target_fps=config.fps)
                    else:
                        probed_meta[rf] = meta  # reused by ingest: no second ffprobe
                except Exception as vfr_err:
                    logger.warning("VFR check/normalization warning for %s: %s", rf.name, vfr_err)
                normalized_raw.append(rf)
            raw_files = normalized_raw

        def _ingest_prog(pct: float, msg: str) -> None:
            _notify_progress(pct, 1, msg)

        # Soundtrack analysis does not depend on the footage (except footage_driven duration
        # solving), so it overlaps with ingest. footage_driven only pre-warms normalisation +
        # decode; the cheap duration solve then happens after ingest.
        audio_bg: _Background | None = None
        skip_audio = config.workflow_mode == "rough_cut" and (
            soundtrack is None or not Path(soundtrack).exists()
        )
        if not skip_audio:

            def _audio_job() -> tuple[Path | None, AudioAnalysisResult | None]:
                active_in: Path | None = soundtrack
                if config.enable_audio_fix and soundtrack is not None:
                    try:
                        from src.modules.audio_normalizer import AudioNormalizer

                        audio_norm = AudioNormalizer(cache_dir=Path(config.cache_dir) / "audio_clean")
                        active_in = audio_norm.clean_audio(soundtrack)
                    except Exception as norm_err:
                        logger.warning("Audio normalization skipped: %s", norm_err)
                if active_in is None:
                    raise ValueError("Soundtrack path required for music-driven editing modes.")
                engine = AudioAnalyzer(config)
                if config.duration_mode == "footage_driven":
                    if Path(active_in).is_file():
                        engine._decode_to_analysis_wav(Path(active_in))
                    return active_in, None
                res = engine.analyze(
                    active_in,
                    target_duration=config.target_duration,
                    available_footage_duration=None,
                    duration_mode=config.duration_mode,
                )
                return active_in, res

            audio_bg = _Background(_audio_job, "AutoEditAudio")

        all_scenes = detector.batch_process_videos(
            raw_files,
            progress_callback=_ingest_prog,
            cancel_event=cancel_event,
            **({"metadata_map": probed_meta} if probed_meta else {}),
        )
        for bad_path, reason in detector.failed_files:
            if callbacks and callbacks.on_log:
                callbacks.on_log("WARNING", f"Skipped unreadable clip {bad_path.name}: {reason}")
        if not all_scenes:
            raise ValueError("No usable scene intervals detected from input footage.")

        detector.export_metadata_csv(all_scenes, analysis_dir / "scenes_metadata.csv")
        detector.export_metadata_edl(
            all_scenes, analysis_dir / "scenes_metadata.edl", config.fps
        )
        total_footage_duration = sum(s.duration for s in all_scenes)
        _notify_progress(100.0, 1, f"Discovered {len(all_scenes)} scenes across footage")

        # Speech-to-text only needs the source files: start it now so it overlaps with
        # soundtrack analysis, scoring and reframing; it is joined in stage 5.
        whisper_bg: _Background | None = None
        if config.enable_transcription:

            def _transcribe_job() -> list[Any]:
                transcriber = TranscriptionEngine(config)
                sentences: list[Any] = []
                try:
                    for rf in raw_files:
                        if cancel_event.is_set():
                            break
                        sentences.extend(transcriber.transcribe_file(rf))
                finally:
                    if hasattr(transcriber, "unload_model"):
                        transcriber.unload_model()
                return sentences

            whisper_bg = _Background(_transcribe_job, "AutoEditWhisper")

        # -------------------------------------------------------------
        # Stage 2: Audio Rhythm & Structure Analysis
        # -------------------------------------------------------------
        if audio_bg is None:
            _notify_stage("Audio Rhythm & Structure (Bypassed for Rough Cut)", 2)
            _notify_progress(100.0, 2, "Camera audio will be preserved on Track A1 (No soundtrack)")
            audio_res = AudioAnalysisResult(
                tempo_bpm=0.0,
                duration=total_footage_duration,
                beats=[],
                cut_timestamps=[0.0, total_footage_duration],
                sample_rate=config.audio_sample_rate,
                sections=[],
                extracted_audio_path=None,
            )
            active_soundtrack = None
        else:
            _notify_stage("Analyzing Soundtrack & Musical Structure", 2)
            _notify_progress(10.0, 2, "Computing continuous RMS and spectral novelty...")
            active_input_soundtrack, audio_res_opt = audio_bg.result()
            if audio_res_opt is None:  # footage_driven: needs the total footage duration
                audio_res_opt = AudioAnalyzer(config).analyze(
                    active_input_soundtrack,
                    target_duration=config.target_duration,
                    available_footage_duration=total_footage_duration,
                    duration_mode=config.duration_mode,
                )
            audio_res = audio_res_opt
            active_soundtrack = audio_res.extracted_audio_path or active_input_soundtrack
            _notify_progress(
                100.0,
                2,
                f"BPM: {audio_res.tempo_bpm:.1f}, Sections: {len(audio_res.sections)}",
            )

        # -------------------------------------------------------------
        # Stage 3: Visual Quality Scoring & Deduplication / Defect Pruning
        # -------------------------------------------------------------
        cleaned_segments: list[CleanedSegment] = []
        keyframe_store: KeyframeStore | None = None
        if config.workflow_mode == "rough_cut":
            _notify_stage("Rough Cut Defect Pruning (Wobble, Shake, Focus)", 3)
            from src.modules.rough_cut_cleaner import RoughCutCleaner

            cleaner = RoughCutCleaner(config)

            def _clean_prog(sp: float, tp: float, msg: str) -> None:
                _notify_progress(sp * 100.0, 3, msg)

            cleaned_segments = cleaner.clean_batch(all_scenes, progress_callback=_clean_prog)

            ranked_scenes = []
            for seg in cleaned_segments:
                sc_cut = SceneCut(
                    clip_path=seg.clip_path,
                    start_time=seg.start_time,
                    end_time=seg.end_time,
                    start_frame=int(round(seg.start_time * config.fps)),
                    end_frame=int(round(seg.end_time * config.fps)),
                    duration=seg.duration,
                )
                sc_scene = ScoredScene(
                    scene=sc_cut,
                    sharpness_score=seg.sharpness_score,
                    aesthetic_score=seg.sharpness_score,
                    composite_score=seg.sharpness_score,
                    metadata=seg.metadata,
                )
                ranked_scenes.append(sc_scene)

            if callbacks and callbacks.on_scene_discovered:
                for sc in ranked_scenes:
                    callbacks.on_scene_discovered(sc)

            _notify_progress(
                100.0,
                3,
                f"Cleaned {len(cleaned_segments)} golden takes in chronological order",
            )
        else:
            _notify_stage("Evaluating Visual Quality & Deduplication", 3)
            scorer = VisualScorer(config)

            def _score_prog(pct: float, msg: str) -> None:
                _notify_progress(pct, 3, msg)

            # One sweep per clip feeds scorer, reframer, VLM and cut-continuity checks.
            reframe_active = config.enable_smart_reframing and (
                config.canvas_width < config.canvas_height
            )
            _notify_progress(0.0, 3, "Reading keyframes (one pass per clip)...")
            keyframe_store = extract_keyframes(
                build_requests(
                    all_scenes,
                    config,
                    scorer=True,
                    reframer=reframe_active,
                    vlm=config.enable_vlm_tagging,
                    boundaries=config.enable_exposure_guard,
                    skip_scorer_for=scorer.cached_scene_keys(all_scenes, from_store=True),
                ),
                config,
                cancel_event=cancel_event,
            )
            _check_cancel()

            ranked_scenes = scorer.score_and_rank_scenes(
                all_scenes,
                progress_callback=_score_prog,
                cancel_event=cancel_event,
                keyframes=keyframe_store,
            )
            if config.enable_exposure_guard:
                attach_boundary_frames(ranked_scenes, keyframe_store)

            if callbacks and callbacks.on_scene_discovered:
                for sc in ranked_scenes:
                    callbacks.on_scene_discovered(sc)

            _notify_progress(100.0, 3, f"Ranked {len(ranked_scenes)} unique scene candidates")
            if hasattr(scorer, "unload_model"):
                scorer.unload_model()

        # -------------------------------------------------------------
        # Stage 4: AI Smart 9:16 Reframing (if vertical)
        # -------------------------------------------------------------
        _notify_stage("AI Smart 9:16 Re-framing (YOLO & EMA)", 4)
        if config.enable_smart_reframing and (
            config.canvas_width < config.canvas_height
        ):

            def _reframe_prog(pct: float, msg: str) -> None:
                _notify_progress(pct, 4, msg)

            reframer = SmartReframer(config)
            ranked_scenes = reframer.reframe_all_scenes(
                ranked_scenes,
                progress_callback=_reframe_prog,
                cancel_event=cancel_event,
                **({"keyframes": keyframe_store} if keyframe_store is not None else {}),
            )
            _notify_progress(100.0, 4, "9:16 pan-and-scan crop windows calculated")
            if hasattr(reframer, "unload_model"):
                reframer.unload_model()
        else:
            _notify_progress(100.0, 4, "Skipped (Canvas is horizontal or reframing disabled)")

        # -------------------------------------------------------------
        # Stage 5: Speech-to-Text & VLM Semantic Tagging
        # -------------------------------------------------------------
        _notify_stage("Speech Transcription & Semantic Understanding", 5)
        transcribed_sentences = []
        if whisper_bg is not None:
            _notify_progress(20.0, 5, "Extracting speech dialogue via faster-whisper...")
            transcribed_sentences.extend(whisper_bg.result())
            _check_cancel()

            if transcribed_sentences:
                is_vert = config.canvas_width < config.canvas_height
                if getattr(config, "subtitle_style", "kinetic_ass") == "kinetic_ass":
                    try:
                        from src.modules.kinetic_subtitle import export_kinetic_ass

                        ass_path = analysis_dir / "subtitles_kinetic.ass"
                        export_kinetic_ass(
                            transcribed_sentences,
                            ass_path,
                            is_vertical=is_vert,
                            margin_v=config.subtitle_margin_v_vertical if is_vert else config.subtitle_margin_v_horizontal,
                        )
                        logger.info("Exported kinetic subtitles: %s", ass_path.name)
                    except Exception as sub_err:
                        logger.warning("Failed to export kinetic subtitles: %s", sub_err)

                if config.target_duration:
                    packer = DurationPacker(config)
                    packed_res = packer.pack_timeline(
                        sentences=transcribed_sentences,
                        scored_scenes=ranked_scenes,
                        target_duration=config.target_duration,
                    )
                    ranked_scenes = packed_res.selected_scenes


        if config.enable_vlm_tagging:
            _notify_progress(60.0, 5, f"Tagging scenes with VLM ({config.vlm_provider})...")
            vlm_engine = VLMAssistant(config)
            ranked_scenes = vlm_engine.tag_scored_scenes(
                ranked_scenes,
                keyframes=vlm_keyframes(ranked_scenes, keyframe_store) if keyframe_store is not None else None,
            )
            if callbacks and callbacks.on_scenes_tagged:
                callbacks.on_scenes_tagged(ranked_scenes)

        _notify_progress(100.0, 5, "Speech dialogue and semantic analysis completed")

        # -------------------------------------------------------------
        # Stage 6: Edit Timeline Synthesis (no files are exported here)
        # -------------------------------------------------------------
        _notify_stage("Edit Timeline Synthesis & Variants", 6)
        _notify_progress(25.0, 6, "Assembling edit timeline...")
        if config.workflow_mode == "rough_cut" and cleaned_segments:
            master_tl = build_rough_cut_edit_timeline(
                config, cleaned_segments, name=f"RoughCut_{config.preset_name}"
            )
        else:
            master_tl = build_edit_timeline(
                config,
                ranked_scenes,
                audio_res.cut_timestamps,
                soundtrack_path=active_soundtrack,
                sections=audio_res.sections,
                name=f"AutoEdit_{config.preset_name}",
                variant_key="master",
            )
        edit_timelines: dict[str, EditTimeline] = {"master": master_tl}

        variants = {}
        if config.workflow_mode != "rough_cut" and active_soundtrack is not None:
            _notify_progress(60.0, 6, "Synthesizing creative variants...")
            try:
                from src.modules.variant_synthesizer import MultiVariantTimelineSynthesizer

                variant_synth = MultiVariantTimelineSynthesizer(config)
                variants = variant_synth.synthesize_variants(
                    scored_scenes=ranked_scenes,
                    beats=audio_res.beats,
                    soundtrack_path=active_soundtrack,
                    song_sections=audio_res.sections,
                    variant_count=getattr(config, "generate_variants", 3),
                )
                for key, var in variants.items():
                    if var.edit_timeline is not None:
                        edit_timelines[key] = var.edit_timeline
                logger.info("Synthesized %d creative timeline variants.", len(variants))
            except Exception as var_err:
                logger.warning("Multi-variant timeline synthesis warning: %s", var_err)

        # Boundary frames were only needed for cut-continuity checks; free the memory.
        for sc in ranked_scenes:
            sc.metadata.pop("_boundary_frame_start", None)
            sc.metadata.pop("_boundary_frame_end", None)
        keyframe_store = None

        _notify_progress(100.0, 6, f"Timeline ready ({len(master_tl.clips)} clips, {len(variants)} variants)")

        # -------------------------------------------------------------
        # Optional one-shot Deliver (CLI --render / --export-nle only)
        # -------------------------------------------------------------
        deliver_res: DeliverResult | None = None
        formats: set[DeliverFormat] = set()
        if export_nle:
            formats |= {"fcpxml", "premiere", "otio"}
            if config.enable_capcut_export:
                formats.add("capcut")
        if not no_render:
            formats.add("mp4")
        if formats or resolve or dry_run_resolve:
            _notify_stage("Deliver: Export & Render", 7)
            deliver_res = self._execute_deliver(
                master_tl,
                output_dir,
                config,
                formats=formats,
                callbacks=JobCallbacks(
                    on_progress=lambda sp, _tp, msg: _notify_progress(sp, 7, msg)
                ),
                cancel_event=cancel_event,
                force_cpu=force_cpu,
                resolve=resolve,
                dry_run_resolve=dry_run_resolve,
                base_name="timeline",
                render_file_name=f"master_{config.preset_name}.mp4",
                job_handle=job_handle,
            )

        resolve_info = deliver_res.resolve_status if deliver_res else None

        # Write pipeline summary JSON
        summary = {
            "status": "SUCCESS",
            "preset": config.preset_name,
            "canvas": f"{config.canvas_width}x{config.canvas_height}@{config.fps}fps",
            "width": config.canvas_width,
            "height": config.canvas_height,
            "fps": config.fps,
            "duration_mode": config.duration_mode,
            "action_drop_threshold": config.action_drop_threshold,
            "smart_reframing": config.enable_smart_reframing
            and (config.canvas_width < config.canvas_height),
            "source_files": len(raw_files),
            "total_footage_duration": total_footage_duration,
            "scenes_detected": len(all_scenes),
            "scenes_selected": len(ranked_scenes),
            "transcription_enabled": config.enable_transcription,
            "dialogue_sentences": len(transcribed_sentences),
            "sections_detected": len(audio_res.sections),
            "audio_bpm": audio_res.tempo_bpm,
            "cut_count": len(audio_res.cut_timestamps) - 1,
            "total_duration": master_tl.duration,
            "timeline_clips": len(master_tl.clips),
            "variants": sorted(variants.keys()),
            "fcpxml": str(deliver_res.fcpxml_path) if deliver_res and deliver_res.fcpxml_path else None,
            "premiere_xml": (
                str(deliver_res.premiere_xml_path)
                if deliver_res and deliver_res.premiere_xml_path
                else None
            ),
            "capcut_draft": (
                str(deliver_res.capcut_draft_path)
                if deliver_res and deliver_res.capcut_draft_path
                else None
            ),
            "master_render": str(deliver_res.render_path) if deliver_res and deliver_res.render_path else None,
            "failed_files": [[str(p), r] for p, r in detector.failed_files],
            "resolve_status": resolve_info,
        }
        summary_file = analysis_dir / "pipeline_summary.json"
        with summary_file.open("w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        return PipelineExecutionResult(
            success=True,
            total_duration=master_tl.duration,
            clip_count=len(master_tl.clips),
            scenes_detected=len(all_scenes),
            audio_bpm=audio_res.tempo_bpm,
            sections_detected=len(audio_res.sections),
            fcpxml_path=deliver_res.fcpxml_path if deliver_res else None,
            premiere_xml_path=deliver_res.premiere_xml_path if deliver_res else None,
            otio_path=deliver_res.otio_path if deliver_res else None,
            capcut_draft_path=deliver_res.capcut_draft_path if deliver_res else None,
            render_path=deliver_res.render_path if deliver_res else None,
            encoder_used=deliver_res.encoder_used if deliver_res else None,
            resolve_status=resolve_info,
            dialogue_sentences=len(transcribed_sentences),
            error_message=None,
            scored_scenes=ranked_scenes,
            timeline_variants=variants,
            active_variant_name="beat_driven" if "beat_driven" in variants else None,
            failed_files=list(detector.failed_files),
            edit_timelines=edit_timelines,
            active_soundtrack=active_soundtrack,
            cut_timestamps=list(audio_res.cut_timestamps),
            sections=list(audio_res.sections),
            source_files=list(raw_files),
        )
