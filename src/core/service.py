"""Unified service layer and adapter interface for AutoEdit pipeline execution.

Decouples UI and API consumers from internal algorithmic modules, providing
thread-safe job management, incremental progress callbacks, and cancellation.
"""

import copy
import dataclasses
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from src.core.analysis_cache import scene_key as span_key
from src.core.config import PipelineConfig
from src.core.ontology import ContentRole, infer_camera_role, role_folder_of
from src.core.types import (
    ALL_DELIVER_FORMATS,
    UNSUPPORTED_RAW_EXTENSIONS,
    VIDEO_EXTENSIONS,
    CleanedSegment,
    CropWindow,
    DeliverFormat,
    DeliverResult,
    DeliverSettings,
    EditTimeline,
    PipelineExecutionResult,
    SceneCut,
    ScoredScene,
    SentenceSegment,
    VideoMetadata,
)
from src.dispatchers.nvenc_renderer import NvencRenderer
from src.dispatchers.resolve_dispatcher import ResolveDispatcher
from src.modules.audio_analyzer import AudioAnalysisResult, AudioAnalyzer
from src.modules.duration_packer import DurationPacker
from src.modules.edit_timeline import (
    build_edit_timeline,
    build_rough_cut_edit_timeline,
    retarget_timeline,
)
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


AUDIO_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"})


def collect_video_files(input_dir: Path | list[Path]) -> tuple[list[Path], list[Path]]:
    """Footage found in a folder / file (or a list of them) and the unsupported RAW files skipped.

    Folders are scanned at the top level, plus one level inside subfolders whose name is a
    role folder (e.g. A-roll, B-roll). Extensions come from ``VIDEO_EXTENSIONS``.
    """
    collected: list[Path] = []
    skipped: list[Path] = []
    for item in input_dir if isinstance(input_dir, list) else [input_dir]:
        p = Path(item)
        if p.is_dir():
            files = [f for f in p.iterdir() if f.is_file()]
            collected.extend(f for f in files if f.suffix.lower() in VIDEO_EXTENSIONS)
            skipped.extend(f for f in files if f.suffix.lower() in UNSUPPORTED_RAW_EXTENSIONS)

            subdirs = sorted(
                [d for d in p.iterdir() if d.is_dir() and role_folder_of(d / "x") is not None],
                key=lambda d: d.name.lower(),
            )
            for sub in subdirs:
                sub_files = [f for f in sub.iterdir() if f.is_file()]
                collected.extend(f for f in sub_files if f.suffix.lower() in VIDEO_EXTENSIONS)
                skipped.extend(f for f in sub_files if f.suffix.lower() in UNSUPPORTED_RAW_EXTENSIONS)
        elif p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
            collected.append(p)
    return collected, skipped


def find_soundtrack(input_dir: Path) -> Path | None:
    """Find a single soundtrack audio file inside a direct subfolder named 'music'.

    Case-insensitive directory name ('music', 'Music', etc.).
    Returns the audio file path if exactly one audio file (per ``AUDIO_EXTENSIONS``) exists,
    otherwise None.
    """
    p = Path(input_dir)
    if not p.is_dir():
        return None
    music_dirs = [d for d in p.iterdir() if d.is_dir() and d.name.lower() == "music"]
    if not music_dirs:
        return None
    audio_files: list[Path] = []
    for md in music_dirs:
        audio_files.extend(
            f for f in md.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
        )
    if len(audio_files) == 1:
        return audio_files[0]
    return None


def _camera_role(scene: SceneCut) -> str:
    """Camera role of the source clip (folder / file name heuristics, same as ingest)."""
    try:
        return infer_camera_role(scene.original_path or scene.clip_path).value
    except Exception:  # pragma: no cover - defensive: roles are cosmetic
        return "cam_a"


def _is_scored(scene: ScoredScene) -> bool:
    """True when the scene already carries normalised VisualScorer metrics (not raw rough-cut sharpness)."""
    meta = scene.metadata or {}
    return meta.get("scored_by") == "visual_scorer" or "dhash" in meta


def _golden_scenes(
    config: PipelineConfig, cleaned: list[CleanedSegment]
) -> tuple[list[SceneCut], dict[tuple[Path, str], dict[str, Any]]]:
    """Golden takes as ``SceneCut``s plus their JSON-safe ``metadata`` (``golden`` block, long-shot flag).

    ``golden.defects`` are in source seconds (clip timeline); ``trimmed_sec`` is the footage the
    cleaner removed from the originating scene (original duration minus all its takes).
    """
    kept: dict[tuple[str, str], float] = {}
    for seg in cleaned:
        k = (str(seg.clip_path), span_key(seg.scene.start_time, seg.scene.end_time))
        kept[k] = kept.get(k, 0.0) + seg.duration
    cuts: list[SceneCut] = []
    meta: dict[tuple[Path, str], dict[str, Any]] = {}
    for seg in cleaned:
        cut = SceneCut(
            clip_path=seg.clip_path,
            start_time=seg.start_time,
            end_time=seg.end_time,
            start_frame=int(round(seg.start_time * config.fps)),
            end_frame=int(round(seg.end_time * config.fps)),
            duration=seg.duration,
            original_path=seg.scene.original_path,
        )
        cuts.append(cut)
        orig = float(seg.scene.end_time - seg.scene.start_time)
        k = (str(seg.clip_path), span_key(seg.scene.start_time, seg.scene.end_time))
        meta[(cut.clip_path, span_key(cut.start_time, cut.end_time))] = {
            "golden": {
                "source_in": round(float(seg.source_start_time), 4),
                "source_out": round(float(seg.source_end_time), 4),
                "defects": [
                    {
                        "type": d.defect_type.value,
                        "start": round(float(seg.scene.start_time + d.start_time), 4),
                        "end": round(float(seg.scene.start_time + d.end_time), 4),
                        "description": d.description,
                    }
                    for d in seg.defects_removed
                ],
                "requires_deflicker": bool(seg.requires_deflicker),
                "original_scene_duration": round(orig, 4),
                "trimmed_sec": round(max(0.0, orig - kept[k]), 4),
            },
            "is_long_shot": bool(seg.duration >= config.extended_shot_threshold_sec),
            "defects_count": len(seg.defects_removed),
            "original_scene_duration": round(orig, 4),
        }
        for field in ("content_role", "role_confidence", "golden_window"):
            if field in seg.metadata:
                meta[(cut.clip_path, span_key(cut.start_time, cut.end_time))][field] = seg.metadata[field]
    return cuts, meta


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
        *,
        stage: Literal["rough_cut", "sequence", "all"] = "all",
        scenes: list[ScoredScene] | None = None,
        existing_timelines: dict[str, EditTimeline] | None = None,
    ) -> JobHandle:
        """Submit an analysis + auto-edit job. It stops at "Timeline ready".

        Like DaVinci Resolve / Premiere / CapCut, nothing is exported or rendered unless
        explicitly requested: pass ``no_render=False`` / ``export_nle=True`` for a one-shot
        CLI run, otherwise call :meth:`submit_deliver_job` when the user clicks Export.

        ``stage`` selects the staged workflow:

        * ``"all"`` (default): the classic one-pass analysis. With ``config.run_rough_cut_in_all``
          the defect-pruning rough cut runs first and the sequence is built from the golden takes.
        * ``"rough_cut"``: ingest + defect pruning only; the result holds the golden takes
          (``scored_scenes``) and ``edit_timelines["rough_cut"]``. No soundtrack is needed.
        * ``"sequence"``: audio + scoring + reframing + cadence engine (master + variants). With
          ``scenes`` (golden / curated scenes, e.g. from a saved project) footage is NOT ingested
          again; scenes with ``metadata["included"] == False`` are left out of the edit.
          ``existing_timelines`` are merged into the result (a ``rough_cut`` timeline survives; an
          old master / variants are replaced). Needs a soundtrack.

        ``config.workflow_mode == "rough_cut"`` stays the standalone alternative mode and ignores
        ``stage``.
        """
        if stage not in ("rough_cut", "sequence", "all"):
            raise ValueError(f"Unknown stage '{stage}'. Use rough_cut, sequence or all.")
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
                    stage=stage,
                    scenes=scenes,
                    existing_timelines=existing_timelines,
                )
                handle._result = res
            except Exception as exc:
                cancelled = isinstance(exc, InterruptedError)
                if cancelled:
                    logger.info("Job %s cancelled by user.", job_id)
                else:
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
                    error_message="Cancelled by user." if cancelled else str(exc),
                    cancelled=cancelled,
                    stage=stage,
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
        settings: DeliverSettings | None = None,
        sentences: list[SentenceSegment] | None = None,
        variant_timelines: dict[str, EditTimeline] | None = None,
    ) -> JobHandle:
        """Export / render an existing edit timeline. Never re-runs analysis.

        ``preview=True`` renders a fast 540p proxy into ``cache_dir/preview`` instead of
        a master deliverable. ``settings`` (Deliver settings) retargets the timeline to the chosen
        canvas / frame rate and drives the master codec, colour, audio, subtitles and the NLE files.
        ``settings.variants`` selects which timelines are delivered (``["active"]`` = ``edit_timeline``
        only, a list of keys, or ``["all"]``); other variants are looked up in ``variant_timelines``
        and every variant but a lone active one goes to ``<output_dir>/<variant_key>/``.
        """
        handle = self._new_handle()

        def _worker() -> None:
            try:
                handle._result = self._execute_deliver_variants(
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
                    settings=settings,
                    sentences=sentences,
                    variant_timelines=variant_timelines,
                )
            except Exception as exc:
                cancelled = isinstance(exc, InterruptedError)
                if cancelled:
                    logger.info("Deliver job %s cancelled by user.", handle.job_id)
                else:
                    logger.error("Deliver job %s failed: %s", handle.job_id, exc, exc_info=True)
                handle._result = DeliverResult(
                    success=False,
                    output_dir=output_dir,
                    is_preview=preview,
                    error_message="Cancelled by user." if cancelled else str(exc),
                    cancelled=cancelled,
                )
            finally:
                handle._completed.set()

        return self._launch(handle, _worker, run_async, "AutoEditDeliver")

    @staticmethod
    def resolve_variant_keys(
        active_key: str, settings: DeliverSettings | None, available: dict[str, EditTimeline] | None
    ) -> list[str]:
        """Timeline keys a deliver job must export: ``["active"]`` -> the active one, ``["all"]``
        -> every timeline in ``available`` (active first), otherwise the picked keys.

        Raises ValueError for a key that is neither the active timeline nor in ``available``.
        """
        requested = list(settings.variants) if settings else ["active"]
        known = list(available or {})
        keys: list[str] = []
        for item in requested:
            if item == "active":
                picks = [active_key]
            elif item == "all":
                picks = [active_key] + [k for k in known if k != active_key]
            elif item == active_key or item in known:
                picks = [item]
            else:
                raise ValueError(
                    f"Unknown variant '{item}'. Available: {sorted({active_key, *known})}"
                )
            keys.extend(k for k in picks if k not in keys)
        return keys or [active_key]

    def _execute_deliver_variants(
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
        settings: DeliverSettings | None = None,
        sentences: list[SentenceSegment] | None = None,
        variant_timelines: dict[str, EditTimeline] | None = None,
    ) -> DeliverResult:
        """Deliver the active timeline, or several variants into ``<output>/<variant_key>/``."""
        common: dict[str, Any] = {
            "force_cpu": force_cpu,
            "preview": preview,
            "resolve": resolve,
            "dry_run_resolve": dry_run_resolve,
            "job_handle": job_handle,
            "settings": settings,
            "sentences": sentences,
        }
        keys = (
            [edit_tl.variant_key]
            if preview
            else self.resolve_variant_keys(edit_tl.variant_key, settings, variant_timelines)
        )
        if keys == [edit_tl.variant_key]:
            return self._execute_deliver(
                edit_tl, output_dir, config, formats, callbacks, cancel_event, base_name=base_name, **common
            )

        timelines = dict(variant_timelines or {})
        timelines[edit_tl.variant_key] = edit_tl  # the live (possibly edited) timeline wins
        combined = DeliverResult(success=True, output_dir=output_dir, settings=settings)
        errors: list[str] = []
        total = len(keys)
        for idx, key in enumerate(keys):
            sub_callbacks = callbacks
            if callbacks is not None:
                sub_callbacks = self._variant_callbacks(callbacks, key, idx, total)
            sub = self._execute_deliver(
                timelines[key],
                output_dir / key,
                config,
                formats,
                sub_callbacks,
                cancel_event,
                base_name=f"{base_name}_{key}" if base_name else None,
                **common,
            )
            combined.variant_results[key] = sub
            if not sub.success:
                errors.append(f"{key}: {sub.error_message or 'failed'}")
        first = combined.variant_results[edit_tl.variant_key if edit_tl.variant_key in keys else keys[0]]
        for attr in (
            "render_path", "encoder_used", "duration", "fcpxml_path", "premiere_xml_path",
            "otio_path", "capcut_draft_path", "resolve_status", "master_codec",
        ):
            setattr(combined, attr, getattr(first, attr))
        combined.subtitle_paths = list(first.subtitle_paths)
        combined.success = not errors
        combined.error_message = "; ".join(errors) or None
        eff = settings or DeliverSettings.from_timeline(edit_tl, config)
        variant_manifests: dict[str, str | None] = {}
        for key, sub in combined.variant_results.items():
            variant_manifests[key] = (
                (output_dir / key / "deliver_manifest.json").relative_to(output_dir).as_posix()
                if sub.manifest_path
                else None
            )
        combined.manifest_path = self._write_manifest(
            output_dir, eff, edit_tl.name, formats, combined, variants=variant_manifests
        )
        return combined

    @staticmethod
    def _variant_callbacks(callbacks: JobCallbacks, key: str, idx: int, total: int) -> JobCallbacks:
        """Scale one variant's progress into its slice of the whole job and tag messages."""

        def on_stage(name: str, i: int, n: int) -> None:
            if callbacks.on_stage_changed:
                callbacks.on_stage_changed(f"[{key}] {name}", i, n)

        def on_progress(stage_pct: float, total_pct: float, msg: str) -> None:
            if callbacks.on_progress:
                callbacks.on_progress(stage_pct, (idx * 100.0 + total_pct) / total, f"[{key}] {msg}")

        return dataclasses.replace(callbacks, on_stage_changed=on_stage, on_progress=on_progress)

    @staticmethod
    def _write_manifest(
        output_dir: Path,
        settings: DeliverSettings,
        timeline_name: str,
        formats: set[DeliverFormat],
        result: DeliverResult,
        variants: dict[str, str | None] | None = None,
        variant_key: str | None = None,
    ) -> Path | None:
        """Write ``deliver_manifest.json`` (settings, files produced, exporter versions)."""
        from src.core.project_io import deliver_settings_to_dict

        def rel(path: Path | None) -> str | None:
            if path is None:
                return None
            try:
                return Path(path).resolve().relative_to(output_dir.resolve()).as_posix()
            except ValueError:
                return str(path)

        try:
            import opentimelineio as otio

            otio_version = getattr(otio, "__version__", None) or "unknown"
        except Exception:  # pragma: no cover - otio is a hard dependency
            otio_version = "unavailable"
        manifest: dict[str, Any] = {
            "manifest_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "timeline": timeline_name,
            "variant": variant_key,
            "success": result.success,
            "settings": deliver_settings_to_dict(settings),
            "formats": sorted(formats),
            "files": {
                "master": rel(result.render_path),
                "fcpxml": rel(result.fcpxml_path),
                "premiere_xml": rel(result.premiere_xml_path),
                "otio": rel(result.otio_path),
                "capcut_draft": rel(result.capcut_draft_path),
                "subtitles": [rel(p) for p in result.subtitle_paths],
            },
            "master": {
                "codec": result.master_codec,
                "encoder": result.encoder_used,
                "duration": round(float(result.duration or 0.0), 3),
            },
            "exporters": {
                "fcpxml": "1.8",
                "premiere_xml": "xmeml (Final Cut Pro 7 XML)",
                "otio": otio_version,
                "capcut": "draft_content.json",
            },
            "watermark_in_nle": {"requested": settings.watermark_in_nle, "applied": False},
        }
        if variants is not None:
            manifest["variants"] = variants
        path = output_dir / "deliver_manifest.json"
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write deliver manifest %s: %s", path, exc)
            return None
        return path

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
        settings: DeliverSettings | None = None,
        sentences: list[SentenceSegment] | None = None,
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
            # The preview keeps the Deliver canvas (aspect ratio, fps) but ignores codec, colour
            # and subtitles: it is always a 540p H.265 proxy.
            preview_tl = retarget_timeline(edit_tl, settings) if settings is not None else edit_tl
            preview_cfg = config.model_copy(
                update={
                    "canvas_width": preview_tl.canvas_width,
                    "canvas_height": preview_tl.canvas_height,
                    "fps": preview_tl.fps,
                }
            )
            renderer = NvencRenderer(preview_cfg)
            res = renderer.render_edit_timeline(
                preview_tl,
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

        # Effective Deliver settings: the user's, or the timeline's own canvas/fps with defaults.
        eff = settings or DeliverSettings.from_timeline(edit_tl, config)
        result.settings = eff
        if settings is not None:
            edit_tl = retarget_timeline(edit_tl, settings)

        output_dir.mkdir(parents=True, exist_ok=True)
        name = base_name or f"{edit_tl.name}"
        nle_formats = {f for f in formats if f != "mp4"}
        if resolve or dry_run_resolve:
            nle_formats.add("fcpxml")
        total = (1 if nle_formats else 0) + (1 if "mp4" in formats else 0)
        step = 0
        exp_cfg = config.model_copy(
            update={"canvas_width": edit_tl.canvas_width, "canvas_height": edit_tl.canvas_height, "fps": edit_tl.fps}
        )
        if eff.watermark_in_nle:
            # TODO(phase 22): an NLE watermark overlay layer is not implemented; the master render
            # still burns the watermark when it is enabled in the config.
            logger.warning(
                "watermark_in_nle requested but NLE watermark layers are not implemented yet; "
                "the NLE files are written without a watermark."
            )

        if nle_formats:
            step += 1
            stage("Exporting NLE interchange files", step, total)
            progress(10.0, "Building OTIO timeline...")
            builder = TimelineBuilder(exp_cfg)
            otio_tl = builder.build_from_edit_timeline(edit_tl, relink_original=eff.relink_original)
            try:
                from src.modules.timeline_qa import TimelineQA

                # QA runs at the Deliver frame rate (exp_cfg), not the analysis preset's.
                for r in TimelineQA(exp_cfg).validate_and_sanitize(
                    otio_tl, min_clip_frames=config.min_clip_frames_threshold
                ):
                    logger.info("[QA Audit] %s", r)
            except Exception as qa_err:
                logger.warning("Timeline QA sanitization warning: %s", qa_err)
            exp = builder.export_all(
                otio_tl,
                output_dir,
                base_name=name,
                formats=nle_formats,
                settings=settings,
            )
            result.fcpxml_path = exp.fcpxml_path
            result.premiere_xml_path = exp.premiere_xml_path
            result.otio_path = exp.otio_path
            result.capcut_draft_path = exp.capcut_draft_path
            progress(100.0, "NLE interchange files exported")

            if resolve or dry_run_resolve:
                dispatcher = ResolveDispatcher(exp_cfg)
                result.resolve_status = dispatcher.dispatch_project(
                    fcpxml_path=exp.fcpxml_path or (output_dir / f"{name}.fcpxml"),
                    source_media_paths=sorted(
                        {c.media_path(eff.relink_original) for c in edit_tl.clips}
                    ),
                    project_name=name,
                    render_output_dir=output_dir / "resolve_renders",
                    dry_run=dry_run_resolve,
                    fit_mode=eff.fit_mode,
                    color=eff.color,
                    master=eff.master,
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
                settings=settings,
                sentences=sentences,
            )
            result.render_path, result.encoder_used = res.output_path, res.encoder_used
            result.duration = res.duration
            result.master_codec = eff.master
            if settings is not None and settings.subtitles != "none" and sentences:
                # The renderer wrote the sidecars next to the master; list them.
                result.subtitle_paths = [
                    p
                    for p in (res.output_path.with_suffix(".srt"), res.output_path.with_suffix(".ass"))
                    if p.is_file()
                ]
        else:
            result.duration = edit_tl.duration
            if settings is not None and settings.subtitles != "none" and sentences:
                from src.modules.subtitle_export import write_subtitle_sidecars

                result.subtitle_paths = write_subtitle_sidecars(
                    sentences,
                    output_dir / f"{name}.mp4",
                    settings.subtitles,
                    (edit_tl.canvas_width, edit_tl.canvas_height),
                )

        result.success = True
        result.manifest_path = self._write_manifest(
            output_dir, eff, edit_tl.name, formats, result, variant_key=edit_tl.variant_key
        )
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
        stage: Literal["rough_cut", "sequence", "all"] = "all",
        scenes: list[ScoredScene] | None = None,
        existing_timelines: dict[str, EditTimeline] | None = None,
    ) -> PipelineExecutionResult:
        """Synchronously execute analysis + auto-edit, ending at "Timeline ready".

        See :meth:`submit_job` for ``stage`` / ``scenes`` / ``existing_timelines``.
        """
        legacy_rough = config.workflow_mode == "rough_cut"  # standalone alternative mode
        if legacy_rough:
            stage = "all"
        golden = not legacy_rough and (
            stage == "rough_cut" or (stage == "all" and bool(getattr(config, "run_rough_cut_in_all", False)))
        )
        supplied: list[ScoredScene] = list(scenes or [])
        if stage == "sequence" and scenes is not None and not supplied:
            raise ValueError("No scenes supplied for the sequence stage - run the rough cut first.")
        reuse_scenes = stage == "sequence" and bool(supplied)  # no ingest: golden / curated pool
        needs_audio = stage != "rough_cut"
        if stage == "sequence" and soundtrack is None:
            raise ValueError("The sequence stage needs a soundtrack.")
        pool_path = golden or reuse_scenes  # scored through the staged helpers below

        # Stage plan: indices / total follow the stages that really run.
        plan: list[str] = []
        if not reuse_scenes:
            plan.append("ingest")
        if needs_audio:
            plan.append("audio")
        if golden or legacy_rough:
            plan.append("clean")
        if not legacy_rough:
            plan.append("score")
        if stage != "rough_cut":
            plan += ["reframe", "enrich"]
        plan.append("synth")
        stage_idx = {k: i + 1 for i, k in enumerate(plan)}

        deliver_formats: set[DeliverFormat] = set()
        if export_nle:
            deliver_formats |= {"fcpxml", "premiere", "otio"}
            if config.enable_capcut_export:
                deliver_formats.add("capcut")
        if not no_render:
            deliver_formats.add("mp4")
        deliver_wanted = bool(deliver_formats) or resolve or dry_run_resolve
        total_stages = len(plan) + (1 if deliver_wanted else 0)

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

        def _notify_progress(stage_pct: float, stage_idx_: int, message: str) -> None:
            _check_cancel()
            if callbacks and callbacks.on_progress:
                base_pct = ((stage_idx_ - 1) / total_stages) * 100.0
                total_pct = base_pct + (stage_pct / total_stages)
                callbacks.on_progress(stage_pct, total_pct, message)

        # -------------------------------------------------------------
        # Stage 1: Ingestion & Shot Detection (Multi-Source Capable)
        # -------------------------------------------------------------
        detector = IngestDetector(config)
        raw_files: list[Path] = []
        probed_meta: dict[Path, VideoMetadata] = {}
        all_scenes: list[SceneCut] = []
        if reuse_scenes:
            # Golden / curated scenes come from the caller (saved project): nothing is re-ingested.
            _check_cancel()
            all_scenes = [s.scene for s in supplied]
            for sc_in in all_scenes:
                if sc_in.clip_path not in raw_files:
                    raw_files.append(sc_in.clip_path)
        else:
            _notify_stage("Ingesting & Detecting Scenes", stage_idx["ingest"])
            collected_files, skipped_raw = collect_video_files(input_dir)
            for raw in skipped_raw:
                logger.warning("Skipping %s: camera RAW format is not supported (transcode it first).", raw.name)

            # Deduplicate while preserving order
            seen_paths = set()
            for f in collected_files:
                rf = f.resolve()
                if rf not in seen_paths:
                    seen_paths.add(rf)
                    raw_files.append(f)

            if not raw_files:
                raise FileNotFoundError(f"No video files found in {input_dir}")

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
        if needs_audio and not skip_audio:

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

        if not reuse_scenes:
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
        if not reuse_scenes:
            _notify_progress(100.0, 1, f"Discovered {len(all_scenes)} scenes across footage")

        # Speech-to-text only needs the source files: start it now so it overlaps with
        # soundtrack analysis, scoring and reframing; it is joined in stage 5.
        whisper_bg: _Background | None = None
        if config.enable_transcription and stage != "rough_cut":

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
        analysed_audio: AudioAnalysisResult | None = None
        if not needs_audio:
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
        elif audio_bg is None:
            _notify_stage("Audio Rhythm & Structure (Bypassed for Rough Cut)", stage_idx["audio"])
            _notify_progress(100.0, stage_idx["audio"], "Camera audio will be preserved on Track A1 (No soundtrack)")
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
            a_idx = stage_idx["audio"]
            _notify_stage("Analyzing Soundtrack & Musical Structure", a_idx)
            _notify_progress(10.0, a_idx, "Computing continuous RMS and spectral novelty...")
            active_input_soundtrack, audio_res_opt = audio_bg.result()
            if audio_res_opt is None:  # footage_driven: needs the total footage duration
                audio_res_opt = AudioAnalyzer(config).analyze(
                    active_input_soundtrack,
                    target_duration=config.target_duration,
                    available_footage_duration=total_footage_duration,
                    duration_mode=config.duration_mode,
                )
            audio_res = audio_res_opt
            analysed_audio = audio_res
            active_soundtrack = audio_res.extracted_audio_path or active_input_soundtrack
            _notify_progress(
                100.0,
                a_idx,
                f"BPM: {audio_res.tempo_bpm:.1f}, Sections: {len(audio_res.sections)}",
            )

        # -------------------------------------------------------------
        # Stage 3: Rough-cut defect pruning and/or visual quality scoring & deduplication
        # -------------------------------------------------------------
        cleaned_segments: list[CleanedSegment] = []
        golden_meta: dict[tuple[Path, str], dict[str, Any]] = {}
        keyframe_store: KeyframeStore | None = None
        scored_all: list[ScoredScene] = []  # pool path: every golden / supplied scene
        if golden or legacy_rough:
            c_idx = stage_idx["clean"]
            _notify_stage("Rough Cut Defect Pruning (Wobble, Shake, Focus)", c_idx)
            from src.modules.rough_cut_cleaner import RoughCutCleaner

            cleaner = RoughCutCleaner(config)

            def _clean_prog(sp: float, tp: float, msg: str) -> None:
                _notify_progress(sp * 100.0, c_idx, msg)

            cleaned_segments = cleaner.clean_batch(
                all_scenes, progress_callback=_clean_prog, cancel_event=cancel_event
            )
            if golden and not cleaned_segments:
                raise ValueError("Rough cut kept no usable takes - every scene was pruned as defective.")

            if golden and cleaned_segments:
                roles: dict[Path, ContentRole] = {}
                confidences: dict[Path, float] = {}
                if config.auto_classify_roles:
                    from src.modules.media_classifier import classify_batch

                    unique_clips: list[tuple[Path, Path]] = []
                    seen_clips: set[Path] = set()
                    for seg in cleaned_segments:
                        if seg.clip_path not in seen_clips:
                            seen_clips.add(seg.clip_path)
                            orig = (
                                Path(seg.scene.original_path)
                                if seg.scene.original_path
                                else seg.clip_path
                            )
                            unique_clips.append((seg.clip_path, orig))

                    if unique_clips:
                        _notify_progress(
                            0.0, c_idx, f"Classifying media roles ({len(unique_clips)} clips)"
                        )
                        for clip_path, orig_path in unique_clips:
                            _check_cancel()
                            res_list = classify_batch([orig_path])
                            if res_list:
                                roles[clip_path] = res_list[0].role
                                confidences[clip_path] = res_list[0].confidence
                        _check_cancel()

                if roles:
                    new_segs = []
                    for seg in cleaned_segments:
                        role = roles.get(seg.clip_path)
                        conf = confidences.get(seg.clip_path)
                        if role is not None:
                            meta = dict(seg.metadata)
                            meta["content_role"] = (
                                role.value if hasattr(role, "value") else str(role)
                            )
                            if conf is not None:
                                meta["role_confidence"] = float(conf)
                            new_segs.append(dataclasses.replace(seg, metadata=meta))
                        else:
                            new_segs.append(seg)
                    cleaned_segments = new_segs

                if config.enable_golden_windows and cleaned_segments:
                    from src.core.analysis_cache import AnalysisCache
                    from src.modules.golden_takes import window_cleaned_segments
                    from src.modules.shot_windows import QualityCurve, compute_quality_curve

                    analysis_cache = AnalysisCache(config)

                    def _curve_fn(path: Path, s: float, e: float) -> QualityCurve | None:
                        return compute_quality_curve(path, s, e, hz=4.0, cache=analysis_cache)

                    cleaned_segments = window_cleaned_segments(
                        cleaned_segments,
                        roles,
                        config.golden_window_min_sec,
                        config.golden_window_max_sec,
                        _curve_fn,
                    )

        ranked_scenes: list[ScoredScene]
        if legacy_rough:
            ranked_scenes = []
            for seg in cleaned_segments:
                sc_cut = SceneCut(
                    clip_path=seg.clip_path,
                    start_time=seg.start_time,
                    end_time=seg.end_time,
                    start_frame=int(round(seg.start_time * config.fps)),
                    end_frame=int(round(seg.end_time * config.fps)),
                    duration=seg.duration,
                    original_path=seg.scene.original_path,
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
                c_idx,
                f"Cleaned {len(cleaned_segments)} golden takes in chronological order",
            )
        elif pool_path:
            s_idx = stage_idx["score"]
            if golden:
                golden_cuts, golden_meta = _golden_scenes(config, cleaned_segments)
                _notify_progress(100.0, c_idx, f"Cleaned {len(cleaned_segments)} golden takes in chronological order")
                prescored: list[ScoredScene] = []
                todo_cuts = golden_cuts
            else:
                prescored = [s for s in supplied if _is_scored(s)]
                todo_cuts = [s.scene for s in supplied if not _is_scored(s)]
            _notify_stage("Evaluating Visual Quality & Deduplication", s_idx)
            # Golden takes are scored on the normalised VisualScorer scale (never raw Tenengrad);
            # de-duplication happens on the edit pool only, so the take list stays complete.
            scorer = VisualScorer(config.model_copy(update={"enable_deduplication": False}))

            def _pool_score_prog(pct: float, msg: str) -> None:
                _notify_progress(pct, s_idx, msg)

            reframe_active = config.enable_smart_reframing and (
                config.canvas_width < config.canvas_height
            )
            _notify_progress(0.0, s_idx, "Reading keyframes (one pass per clip)...")
            skip_keys = scorer.cached_scene_keys(todo_cuts, from_store=True) if todo_cuts else set()
            skip_keys |= {
                (s.scene.clip_path, span_key(s.scene.start_time, s.scene.end_time)) for s in prescored
            }
            keyframe_store = extract_keyframes(
                build_requests(
                    [s.scene for s in prescored] + todo_cuts,
                    config,
                    scorer=bool(todo_cuts),
                    reframer=reframe_active and stage != "rough_cut",
                    vlm=config.enable_vlm_tagging and stage != "rough_cut",
                    boundaries=config.enable_exposure_guard and stage != "rough_cut",
                    skip_scorer_for=skip_keys,
                ),
                config,
                cancel_event=cancel_event,
            )
            _check_cancel()
            scored_new = (
                scorer.score_and_rank_scenes(
                    todo_cuts,
                    progress_callback=_pool_score_prog,
                    cancel_event=cancel_event,
                    keyframes=keyframe_store,
                )
                if todo_cuts
                else []
            )
            _check_cancel()
            by_key = {(s.scene.clip_path, span_key(s.scene.start_time, s.scene.end_time)): s for s in supplied}
            for sc in scored_new:
                key = (sc.scene.clip_path, span_key(sc.scene.start_time, sc.scene.end_time))
                old = by_key.get(key)
                if old is not None:  # keep curation / golden info, take the fresh scores
                    merged = dict(old.metadata)
                    merged.update(sc.metadata)
                    sc.metadata = merged
                    sc.camera_role = old.camera_role
                    sc.metadata["scored_by"] = "visual_scorer"
                else:
                    sc.metadata.update(copy.deepcopy(golden_meta.get(key, {})))
                    sc.camera_role = _camera_role(sc.scene)
                    sc.metadata["scored_by"] = "visual_scorer"
            scored_all = sorted(prescored + scored_new, key=lambda s: s.composite_score, reverse=True)
            if callbacks and callbacks.on_scene_discovered:
                for sc in scored_all:
                    callbacks.on_scene_discovered(sc)

            if stage == "rough_cut":
                ranked_scenes = list(scored_all)
            else:
                ranked_scenes = [s for s in scored_all if s.metadata.get("included", True) is not False]
                if not ranked_scenes:
                    raise ValueError("Every scene is excluded - include at least one scene to build the sequence.")
                if config.enable_deduplication:
                    ranked_scenes = scorer.deduplicate_scenes(ranked_scenes)
                if config.enable_exposure_guard:
                    attach_boundary_frames(ranked_scenes, keyframe_store)
            _notify_progress(100.0, s_idx, f"Ranked {len(ranked_scenes)} scene candidates")
            if hasattr(scorer, "unload_model"):
                scorer.unload_model()
        else:
            s_idx = stage_idx["score"]
            _notify_stage("Evaluating Visual Quality & Deduplication", s_idx)
            scorer = VisualScorer(config)

            def _score_prog(pct: float, msg: str) -> None:
                _notify_progress(pct, s_idx, msg)

            # One sweep per clip feeds scorer, reframer, VLM and cut-continuity checks.
            reframe_active = config.enable_smart_reframing and (
                config.canvas_width < config.canvas_height
            )
            _notify_progress(0.0, s_idx, "Reading keyframes (one pass per clip)...")
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

            _notify_progress(100.0, s_idx, f"Ranked {len(ranked_scenes)} unique scene candidates")
            if hasattr(scorer, "unload_model"):
                scorer.unload_model()

        transcribed_sentences: list[Any] = []
        if stage != "rough_cut":
            # -------------------------------------------------------------
            # Stage 4: AI Smart 9:16 Reframing (if vertical)
            # -------------------------------------------------------------
            r_idx = stage_idx["reframe"]
            _notify_stage("AI Smart 9:16 Re-framing (YOLO & EMA)", r_idx)
            if config.enable_smart_reframing and (
                config.canvas_width < config.canvas_height
            ):

                def _reframe_prog(pct: float, msg: str) -> None:
                    _notify_progress(pct, r_idx, msg)

                reframer = SmartReframer(config)
                ranked_scenes = reframer.reframe_all_scenes(
                    ranked_scenes,
                    progress_callback=_reframe_prog,
                    cancel_event=cancel_event,
                    **({"keyframes": keyframe_store} if keyframe_store is not None else {}),
                )
                _notify_progress(100.0, r_idx, "9:16 pan-and-scan crop windows calculated")
                if hasattr(reframer, "unload_model"):
                    reframer.unload_model()
            else:
                _notify_progress(100.0, r_idx, "Skipped (Canvas is horizontal or reframing disabled)")

            # -------------------------------------------------------------
            # Stage 5: Speech-to-Text & VLM Semantic Tagging
            # -------------------------------------------------------------
            e_idx = stage_idx["enrich"]
            _notify_stage("Speech Transcription & Semantic Understanding", e_idx)
            if whisper_bg is not None:
                _notify_progress(20.0, e_idx, "Extracting speech dialogue via faster-whisper...")
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
                _notify_progress(60.0, e_idx, f"Tagging scenes with VLM ({config.vlm_provider})...")
                vlm_engine = VLMAssistant(config)
                ranked_scenes = vlm_engine.tag_scored_scenes(
                    ranked_scenes,
                    keyframes=vlm_keyframes(ranked_scenes, keyframe_store) if keyframe_store is not None else None,
                )
                if callbacks and callbacks.on_scenes_tagged:
                    callbacks.on_scenes_tagged(ranked_scenes)

            _notify_progress(100.0, e_idx, "Speech dialogue and semantic analysis completed")

        # -------------------------------------------------------------
        # Stage 6: Edit Timeline Synthesis (no files are exported here)
        # -------------------------------------------------------------
        y_idx = stage_idx["synth"]
        _notify_stage("Edit Timeline Synthesis & Variants", y_idx)
        _notify_progress(25.0, y_idx, "Assembling edit timeline...")
        rough_tl: EditTimeline | None = None
        if cleaned_segments and (golden or legacy_rough):
            rough_tl = build_rough_cut_edit_timeline(
                config,
                cleaned_segments,
                name=f"RoughCut_{config.preset_name}",
                variant_key="master" if legacy_rough else "rough_cut",
            )
        if legacy_rough and rough_tl is not None:
            master_tl = rough_tl
        elif stage == "rough_cut":
            assert rough_tl is not None
            master_tl = rough_tl
        else:
            master_tl = build_edit_timeline(
                config,
                ranked_scenes,
                audio_res.cut_timestamps,
                soundtrack_path=active_soundtrack,
                sections=audio_res.sections,
                name=f"AutoEdit_{config.preset_name}",
                variant_key="master",
                bar_grid=audio_res.bar_grid,
                profile=config.cadence_profile,
                cadence_seed=config.cadence_seed,
            )
        edit_timelines: dict[str, EditTimeline] = {}
        if stage == "rough_cut":
            edit_timelines["rough_cut"] = master_tl
        else:
            edit_timelines["master"] = master_tl

        variants = {}
        stage_warnings: list[str] = []
        if not legacy_rough and stage != "rough_cut" and active_soundtrack is not None:
            _notify_progress(60.0, y_idx, "Synthesizing creative variants...")
            try:
                from src.modules.variant_synthesizer import MultiVariantTimelineSynthesizer

                variant_synth = MultiVariantTimelineSynthesizer(config)
                variants = variant_synth.synthesize_variants(
                    scored_scenes=ranked_scenes,
                    beats=audio_res.beats,
                    soundtrack_path=active_soundtrack,
                    song_sections=audio_res.sections,
                    variant_count=getattr(config, "generate_variants", 3),
                    bar_grid=audio_res.bar_grid,
                    cut_timestamps=audio_res.cut_timestamps,
                    warnings=stage_warnings,
                )
                for key, var in variants.items():
                    if var.edit_timeline is not None:
                        edit_timelines[key] = var.edit_timeline
                logger.info("Synthesized %d creative timeline variants.", len(variants))
            except Exception as var_err:
                logger.warning("Multi-variant timeline synthesis warning: %s", var_err, exc_info=True)
                stage_warnings.append(f"Variant synthesis failed: {var_err}")

        if stage != "rough_cut" and not legacy_rough:
            # The rough cut survives a re-sequence; an old master / variants are replaced.
            if rough_tl is not None:
                edit_timelines["rough_cut"] = rough_tl
            elif existing_timelines:
                for key, tl in existing_timelines.items():
                    if tl.kind == "rough_cut" and key not in edit_timelines:
                        edit_timelines[key] = tl

        # Boundary frames were only needed for cut-continuity checks; free the memory.
        for sc in ranked_scenes + scored_all:
            sc.metadata.pop("_boundary_frame_start", None)
            sc.metadata.pop("_boundary_frame_end", None)
        keyframe_store = None

        # Pool path: the project keeps every golden / supplied scene (curation list), the edit
        # pool first.
        result_scenes = ranked_scenes
        if pool_path and stage != "rough_cut":
            in_pool = {(s.scene.clip_path, span_key(s.scene.start_time, s.scene.end_time)) for s in ranked_scenes}
            result_scenes = ranked_scenes + [
                s
                for s in scored_all
                if (s.scene.clip_path, span_key(s.scene.start_time, s.scene.end_time)) not in in_pool
            ]

        _notify_progress(100.0, y_idx, f"Timeline ready ({len(master_tl.clips)} clips, {len(variants)} variants)")

        # -------------------------------------------------------------
        # Optional one-shot Deliver (CLI --render / --export-nle only)
        # -------------------------------------------------------------
        deliver_res: DeliverResult | None = None
        if deliver_wanted:
            d_idx = len(plan) + 1
            _notify_stage("Deliver: Export & Render", d_idx)
            deliver_res = self._execute_deliver(
                master_tl,
                output_dir,
                config,
                formats=deliver_formats,
                callbacks=JobCallbacks(
                    on_progress=lambda sp, _tp, msg: _notify_progress(sp, d_idx, msg)
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
            "stage": stage,
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
            "golden_takes": len(cleaned_segments),
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
            scored_scenes=result_scenes,
            timeline_variants=variants,
            active_variant_name="beat_driven" if "beat_driven" in variants else None,
            failed_files=list(detector.failed_files),
            edit_timelines=edit_timelines,
            active_soundtrack=active_soundtrack,
            cut_timestamps=list(audio_res.cut_timestamps),
            sections=list(audio_res.sections),
            source_files=list(raw_files),
            warnings=stage_warnings,
            stage=stage,
            cleaned_segments=cleaned_segments,
            audio_analysis=analysed_audio,
        )
