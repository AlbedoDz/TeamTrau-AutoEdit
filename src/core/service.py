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
    CleanedSegment,
    CropWindow,
    PipelineExecutionResult,
    SceneCut,
    ScoredScene,
    VideoMetadata,
)
from src.dispatchers.nvenc_renderer import NvencRenderer
from src.dispatchers.resolve_dispatcher import ResolveDispatcher
from src.modules.audio_analyzer import AudioAnalysisResult, AudioAnalyzer
from src.modules.duration_packer import DurationPacker
from src.modules.ingest_detector import IngestDetector
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
        self._result: PipelineExecutionResult | None = None
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
    def result(self) -> PipelineExecutionResult | None:
        """Return final execution result if completed, else None."""
        return self._result


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
        no_render: bool = False,
        force_cpu: bool = False,
        resolve: bool = False,
        dry_run_resolve: bool = False,
    ) -> JobHandle:
        """Submit a full video editing pipeline job for synchronous or asynchronous execution."""
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        cancel_event = threading.Event()
        handle = JobHandle(job_id, cancel_event)

        with self._jobs_lock:
            self._active_jobs[job_id] = handle

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

        if run_async:
            thread = threading.Thread(
                target=_worker, name=f"AutoEditWorker-{job_id}", daemon=True
            )
            handle._thread = thread
            thread.start()
        else:
            _worker()

        return handle

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
    ) -> PipelineExecutionResult:
        """Synchronously execute the multi-stage pipeline, updating progress callbacks."""
        total_stages = 7 if not no_render else 6
        output_dir.mkdir(parents=True, exist_ok=True)

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
        if getattr(config, "auto_normalize_vfr", True):
            normalized_raw = []
            for rf in raw_files:
                _check_cancel()
                try:
                    meta = detector.probe_video(rf)
                    if meta.is_vfr:
                        _notify_progress(5.0, 1, f"Normalizing VFR footage ({rf.name})...")
                        rf = detector.normalize_to_cfr(rf, target_fps=config.fps)
                except Exception as vfr_err:
                    logger.warning("VFR check/normalization warning for %s: %s", rf.name, vfr_err)
                normalized_raw.append(rf)
            raw_files = normalized_raw

        def _ingest_prog(pct: float, msg: str) -> None:
            _notify_progress(pct, 1, msg)

        all_scenes = detector.batch_process_videos(
            raw_files,
            progress_callback=_ingest_prog,
            cancel_event=cancel_event,
        )
        for bad_path, reason in detector.failed_files:
            if callbacks and callbacks.on_log:
                callbacks.on_log("WARNING", f"Skipped unreadable clip {bad_path.name}: {reason}")
        if not all_scenes:
            raise ValueError("No usable scene intervals detected from input footage.")

        detector.export_metadata_csv(all_scenes, output_dir / "scenes_metadata.csv")
        detector.export_metadata_edl(
            all_scenes, output_dir / "scenes_metadata.edl", config.fps
        )
        total_footage_duration = sum(s.duration for s in all_scenes)
        _notify_progress(100.0, 1, f"Discovered {len(all_scenes)} scenes across footage")

        # -------------------------------------------------------------
        # Stage 2: Audio Rhythm & Structure Analysis
        # -------------------------------------------------------------
        if config.workflow_mode == "rough_cut" and (soundtrack is None or not Path(soundtrack).exists()):
            _notify_stage("Audio Rhythm & Structure (Bypassed for Rough Cut)", 2)
            _notify_progress(100.0, 2, "Camera audio will be preserved on Track A1 (No soundtrack)")
            audio_res = AudioAnalysisResult(
                tempo_bpm=0.0,
                beat_timestamps=[],
                beats=[],
                downbeat_timestamps=[],
                cut_timestamps=[0.0, total_footage_duration],
                sections=[],
                extracted_audio_path=None,
            )
            active_soundtrack = None
        else:
            _notify_stage("Analyzing Soundtrack & Musical Structure", 2)
            _notify_progress(10.0, 2, "Computing continuous RMS and spectral novelty...")

            active_input_soundtrack = soundtrack
            if config.enable_audio_fix and soundtrack is not None:
                try:
                    from src.modules.audio_normalizer import AudioNormalizer

                    audio_norm = AudioNormalizer(cache_dir=Path(config.cache_dir) / "audio_clean")
                    active_input_soundtrack = audio_norm.clean_audio(soundtrack)
                except Exception as norm_err:
                    logger.warning("Audio normalization skipped: %s", norm_err)

            if active_input_soundtrack is None:
                raise ValueError("Soundtrack path required for music-driven editing modes.")

            audio_engine = AudioAnalyzer(config)
            audio_res = audio_engine.analyze(
                active_input_soundtrack,
                target_duration=config.target_duration,
                available_footage_duration=total_footage_duration,
                duration_mode=config.duration_mode,
            )
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

            ranked_scenes = scorer.score_and_rank_scenes(
                all_scenes,
                progress_callback=_score_prog,
                cancel_event=cancel_event,
            )

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
        if config.enable_transcription:
            _notify_progress(20.0, 5, "Extracting speech dialogue via faster-whisper...")
            transcriber = TranscriptionEngine(config)
            for rf in raw_files:
                _check_cancel()
                transcribed_sentences.extend(transcriber.transcribe_file(rf))

            if transcribed_sentences:
                is_vert = config.canvas_width < config.canvas_height
                if getattr(config, "subtitle_style", "kinetic_ass") == "kinetic_ass":
                    try:
                        from src.modules.kinetic_subtitle import export_kinetic_ass

                        ass_path = output_dir / "subtitles_kinetic.ass"
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

            if hasattr(transcriber, "unload_model"):
                transcriber.unload_model()

        if config.enable_vlm_tagging:
            _notify_progress(60.0, 5, f"Tagging scenes with VLM ({config.vlm_provider})...")
            vlm_engine = VLMAssistant(config)
            ranked_scenes = vlm_engine.tag_scored_scenes(ranked_scenes)
            if callbacks and callbacks.on_scenes_tagged:
                callbacks.on_scenes_tagged(ranked_scenes)

        _notify_progress(100.0, 5, "Speech dialogue and semantic analysis completed")

        # -------------------------------------------------------------
        # Stage 6: Timeline Synthesis & Multi-NLE Export
        # -------------------------------------------------------------
        _notify_stage("Timeline Synthesis & Multi-NLE Serialization", 6)
        _notify_progress(25.0, 6, "Assembling OpenTimelineIO sequence tracks...")
        timeline_engine = TimelineBuilder(config)
        if config.workflow_mode == "rough_cut" and cleaned_segments:
            master_timeline = timeline_engine.build_rough_cut_timeline(
                cleaned_segments=cleaned_segments,
                timeline_name=f"RoughCut_{config.preset_name}",
                fps=config.fps,
            )
        else:
            master_timeline = timeline_engine.build_timeline(
                scored_scenes=ranked_scenes,
                cut_timestamps=audio_res.cut_timestamps,
                soundtrack_path=active_soundtrack if active_soundtrack else raw_files[0],
                timeline_name=f"AutoEdit_{config.preset_name}",
                sections=audio_res.sections,
            )

        # Pre-dispatch Timeline QA & Sanitization Hook
        _notify_progress(45.0, 6, "Auditing sequence integrity (TimelineQA)...")
        try:
            from src.modules.timeline_qa import TimelineQA

            qa_engine = TimelineQA(config)
            qa_reports = qa_engine.validate_and_sanitize(
                master_timeline,
                min_clip_frames=getattr(config, "min_clip_frames_threshold", 6),
            )
            for r in qa_reports:
                logger.info("[QA Audit] %s", r)
        except Exception as qa_err:
            logger.warning("Timeline QA sanitization warning: %s", qa_err)

        _notify_progress(60.0, 6, "Serializing FCPXML, Premiere XML, OTIO & CapCut...")
        export_res = timeline_engine.export_all(
            master_timeline, output_dir, base_name="timeline"
        )
        _notify_progress(80.0, 6, f"Exported {export_res.clip_count} clips successfully")

        # Multi-Variant Timeline Synthesis (1-3 Variations)
        variants = {}
        if config.workflow_mode != "rough_cut" and active_soundtrack is not None:
            try:
                from src.modules.variant_synthesizer import MultiVariantTimelineSynthesizer

                variant_synth = MultiVariantTimelineSynthesizer(config)
                variant_cnt = getattr(config, "generate_variants", 3)
                variants = variant_synth.synthesize_variants(
                    scored_scenes=ranked_scenes,
                    beats=audio_res.beats,
                    soundtrack_path=active_soundtrack,
                    song_sections=audio_res.sections,
                    variant_count=variant_cnt,
                    output_dir=output_dir / "variants",
                )
                logger.info("Synthesized %d creative timeline variants.", len(variants))
            except Exception as var_err:
                logger.warning("Multi-variant timeline synthesis warning: %s", var_err)

        _notify_progress(100.0, 6, f"Multi-variant synthesis ready ({len(variants)} cuts)")

        # DaVinci Resolve Dispatcher (if requested)
        resolve_info = None
        if resolve or dry_run_resolve:
            logger.info("Triggering DaVinci Resolve Studio automation...")
            dispatcher = ResolveDispatcher(config)
            resolve_info = dispatcher.dispatch_project(
                fcpxml_path=export_res.fcpxml_path or (output_dir / "timeline.fcpxml"),
                source_media_paths=raw_files,
                project_name=f"AutoEdit_{config.preset_name}",
                render_output_dir=output_dir / "resolve_renders",
                dry_run=dry_run_resolve,
            )

        # -------------------------------------------------------------
        # Stage 7: Master Video Rendering (if enabled)
        # -------------------------------------------------------------
        render_res = None
        if not no_render:
            _enforce_vram_headroom(max_allowed_gb=2.0)
            _notify_stage("Master Video Rendering (FFmpeg NVENC)", 7)
            renderer = NvencRenderer(config)
            master_video_path = output_dir / f"master_{config.preset_name}.mp4"

            def _render_prog(pct: float, msg: str) -> None:
                _notify_progress(pct, 7, msg)

            render_cuts = audio_res.cut_timestamps
            if config.workflow_mode == "rough_cut":
                cum_cuts = [0.0]
                cur = 0.0
                for s in ranked_scenes:
                    cur += s.scene.duration
                    cum_cuts.append(cur)
                render_cuts = cum_cuts

            render_res = renderer.render_sequence(
                scored_scenes=ranked_scenes,
                cut_timestamps=render_cuts,
                soundtrack_path=active_soundtrack,
                output_file=master_video_path,
                force_cpu=force_cpu,
                sections=audio_res.sections,
                progress_callback=_render_prog,
                cancel_event=cancel_event,
                job_handle=job_handle,
            )
            _notify_progress(100.0, 7, "Master video render complete")

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
            "total_duration": export_res.total_duration,
            "fcpxml": str(export_res.fcpxml_path) if export_res.fcpxml_path else None,
            "premiere_xml": (
                str(export_res.premiere_xml_path)
                if export_res.premiere_xml_path
                else None
            ),
            "capcut_draft": (
                str(export_res.capcut_draft_path)
                if export_res.capcut_draft_path
                else None
            ),
            "master_render": str(render_res.output_path) if render_res else None,
            "resolve_status": resolve_info,
        }
        summary_file = output_dir / "pipeline_summary.json"
        with summary_file.open("w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        return PipelineExecutionResult(
            success=True,
            total_duration=export_res.total_duration,
            clip_count=export_res.clip_count,
            scenes_detected=len(all_scenes),
            audio_bpm=audio_res.tempo_bpm,
            sections_detected=len(audio_res.sections),
            fcpxml_path=export_res.fcpxml_path,
            premiere_xml_path=export_res.premiere_xml_path,
            otio_path=export_res.otio_path,
            capcut_draft_path=export_res.capcut_draft_path,
            render_path=render_res.output_path if render_res else None,
            encoder_used=render_res.encoder_used if render_res else None,
            resolve_status=resolve_info,
            dialogue_sentences=len(transcribed_sentences),
            error_message=None,
            scored_scenes=ranked_scenes,
            timeline_variants=variants,
            active_variant_name="beat_driven" if "beat_driven" in variants else None,
            failed_files=list(detector.failed_files),
        )
