"""Standalone headless video rendering engine utilizing FFmpeg NVENC acceleration."""

import logging
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from src.core.config import PipelineConfig, check_nvenc_available
from src.core.types import CropWindow, RenderJobResult, SceneCut, ScoredScene, SongSection
from src.dispatchers.watermark_helper import build_watermark_filter_lines
from src.modules.timeline_builder import select_scenes_for_cuts

logger = logging.getLogger(__name__)


class NvencRenderer:
    """Builds dynamic filter graphs and renders headless video via FFmpeg NVENC."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.nvenc_available = check_nvenc_available()

    def render_sequence(
        self,
        scored_scenes: list[ScoredScene],
        cut_timestamps: list[float],
        soundtrack_path: Path | None,
        output_file: Path,
        force_cpu: bool = False,
        include_teaser: bool | None = None,
        sections: list[SongSection] | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        job_handle: Any | None = None,
    ) -> RenderJobResult:
        """Render multi-clip sequence with background music to master video file."""
        if not scored_scenes:
            raise ValueError("No scenes provided for rendering.")
        if len(cut_timestamps) < 2:
            raise ValueError("cut_timestamps must have at least 2 boundary timestamps.")

        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise FileNotFoundError("FFmpeg executable not found on system PATH.")

        output_file.parent.mkdir(parents=True, exist_ok=True)
        total_duration = cut_timestamps[-1] - cut_timestamps[0]

        # Determine unique source video files to optimize input handles
        unique_sources: list[Path] = []
        source_to_idx: dict[Path, int] = {}
        for s in scored_scenes:
            resolved_p = s.scene.clip_path.resolve()
            if resolved_p not in source_to_idx:
                source_to_idx[resolved_p] = len(unique_sources)
                unique_sources.append(resolved_p)

        audio_input_idx = len(unique_sources)

        # Build dynamic filter_complex graph
        filter_lines: list[str] = []
        concat_v_labels: list[str] = []

        target_w = self.config.canvas_width
        target_h = self.config.canvas_height
        target_fps = self.config.fps

        should_teaser = (
            include_teaser if include_teaser is not None else self.config.enable_teaser
        )
        has_teaser = should_teaser and (total_duration >= self.config.teaser_duration + 4.0)

        clip_specs: list[tuple[SceneCut, float, CropWindow | None, float, str | None]] = []
        if has_teaser:
            teaser_dur = self.config.teaser_duration
            num_teaser = min(5, max(3, len(scored_scenes)))
            sub_dur = teaser_dur / num_teaser
            for j in range(num_teaser):
                candidate = scored_scenes[j % len(scored_scenes)]
                clip_dur = min(sub_dur, max(0.1, candidate.scene.duration))
                clip_specs.append(
                    (candidate.scene, clip_dur, candidate.crop_window, candidate.speed, candidate.metadata.get("ken_burns"))
                )
            main_cuts = [teaser_dur] + [t for t in cut_timestamps if t > teaser_dur]
        else:
            main_cuts = cut_timestamps

        main_scenes = select_scenes_for_cuts(
            scored_scenes=scored_scenes,
            cut_timestamps=main_cuts,
            sections=sections,
            action_drop_threshold=self.config.action_drop_threshold,
        )
        for i in range(len(main_cuts) - 1):
            slot_start = main_cuts[i]
            slot_end = main_cuts[i + 1]
            slot_dur = max(0.1, slot_end - slot_start)

            candidate = main_scenes[i]
            sc = candidate.scene
            clip_dur = min(slot_dur, max(0.1, sc.duration))
            clip_specs.append((sc, clip_dur, candidate.crop_window, candidate.speed, candidate.metadata.get("ken_burns")))

        for i, (sc, clip_dur, crop_win, speed, kb_motion) in enumerate(clip_specs):
            in_idx = source_to_idx[sc.clip_path.resolve()]
            v_label = f"v{i}"
            concat_v_labels.append(f"[{v_label}]")

            # Determine source footage duration and pts scaling
            eff_speed = max(0.1, float(speed))
            if eff_speed != 1.0:
                src_duration = min(sc.duration, clip_dur * eff_speed)
                pts_filter = f"setpts=(PTS-STARTPTS)/{eff_speed:.4f}"
            else:
                src_duration = clip_dur
                pts_filter = "setpts=PTS-STARTPTS"

            is_vertical = target_w < target_h
            is_blur_backdrop = is_vertical and (
                (crop_win is not None and crop_win.tracking_subject == "blur_backdrop")
                or getattr(self.config, "reframing_mode", "auto_hybrid") == "blur_backdrop"
            )

            if is_blur_backdrop:
                # Dynamic dual-layer blurred backdrop
                t_raw = f"t_{i}"
                trim_filter = (
                    f"[{in_idx}:v]trim=start={sc.start_time:.3f}:duration={src_duration:.3f},"
                    f"{pts_filter},setsar=1,fps={target_fps}[{t_raw}];"
                    f"[{t_raw}]split=2[bg_{i}][fg_{i}];"
                    f"[bg_{i}]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                    f"crop={target_w}:{target_h},boxblur=20:5,eq=brightness=-0.15[bg_b_{i}];"
                    f"[fg_{i}]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg_s_{i}];"
                    f"[bg_b_{i}][fg_s_{i}]overlay=(W-w)/2:(H-h)/2[{v_label}];"
                )
            elif crop_win is not None and is_vertical:
                trim_filter = (
                    f"[{in_idx}:v]trim=start={sc.start_time:.3f}:duration={src_duration:.3f},"
                    f"{pts_filter},"
                    f"crop={crop_win.width}:{crop_win.height}:{crop_win.x}:{crop_win.y},"
                    f"scale={target_w}:{target_h},"
                    f"setsar=1,fps={target_fps}[{v_label}];"
                )
            else:
                # Standard scale and pad to canvas preset maintaining aspect ratio
                trim_filter = (
                    f"[{in_idx}:v]trim=start={sc.start_time:.3f}:duration={src_duration:.3f},"
                    f"{pts_filter},"
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
                    f"setsar=1,fps={target_fps}[{v_label}];"
                )
            filter_lines.append(trim_filter)

        has_watermark = bool(
            self.config.watermark_enabled
            and self.config.watermark_path
            and Path(self.config.watermark_path).exists()
        )

        # Concat video segments with optional watermark overlay
        has_soundtrack = soundtrack_path is not None and Path(soundtrack_path).exists()
        audio_input_idx = len(unique_sources) if has_soundtrack else -1

        if has_watermark:
            concat_str = (
                f"{''.join(concat_v_labels)}concat=n={len(concat_v_labels)}:v=1:a=0[vconcat];"
            )
            filter_lines.append(concat_str)
            wm_input_idx = len(unique_sources) + (1 if has_soundtrack else 0)
            wm_lines = build_watermark_filter_lines(
                wm_input_idx=wm_input_idx,
                base_v_label="vconcat",
                out_v_label="vout",
                position=self.config.watermark_position,
                opacity=self.config.watermark_opacity,
                margin=self.config.watermark_margin,
            )
            filter_lines.extend(wm_lines)
        else:
            concat_str = (
                f"{''.join(concat_v_labels)}concat=n={len(concat_v_labels)}:v=1:a=0[vout];"
            )
            filter_lines.append(concat_str)

        # Audio track processing
        if has_soundtrack:
            audio_trim = (
                f"[{audio_input_idx}:a]atrim=start=0:duration={total_duration:.3f},"
                f"asetpts=PTS-STARTPTS[aout]"
            )
            filter_lines.append(audio_trim)
        else:
            # Concatenate synchronous camera audio from clips
            concat_a_labels: list[str] = []
            for i, (sc, clip_dur, _cw, speed, _kb) in enumerate(clip_specs):
                in_idx = source_to_idx[sc.clip_path.resolve()]
                a_label = f"a{i}"
                concat_a_labels.append(f"[{a_label}]")
                eff_speed = max(0.1, float(speed))
                src_duration = min(sc.duration, clip_dur * eff_speed) if eff_speed != 1.0 else clip_dur
                filter_lines.append(
                    f"[{in_idx}:a]atrim=start={sc.start_time:.3f}:duration={src_duration:.3f},"
                    f"asetpts=PTS-STARTPTS[{a_label}];"
                )
            filter_lines.append(
                f"{''.join(concat_a_labels)}concat=n={len(concat_a_labels)}:v=0:a=1[aout]"
            )

        filter_complex_content = "\n".join(filter_lines)

        # Use temporary file for filter_complex_script to bypass Windows CLI length limits
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".filter", delete=False, encoding="utf-8"
        ) as f_script:
            f_script.write(filter_complex_content)
            script_path = Path(f_script.name)

        use_nvenc = self.nvenc_available and not force_cpu
        encoder = "hevc_nvenc" if use_nvenc else "libx265"

        cmd: list[str] = [ffmpeg_bin, "-y", "-hide_banner"]

        # Add video inputs
        for src in unique_sources:
            cmd.extend(["-i", str(src)])

        # Add audio input if soundtrack present
        if has_soundtrack and soundtrack_path is not None:
            cmd.extend(["-i", str(soundtrack_path.resolve())])

        # Add watermark input if active
        if has_watermark and self.config.watermark_path is not None:
            cmd.extend(["-i", str(Path(self.config.watermark_path).resolve())])

        # Filter script and mapping
        cmd.extend(["-filter_complex_script", str(script_path)])
        cmd.extend(["-map", "[vout]", "-map", "[aout]"])

        # Encoder options
        if use_nvenc:
            cmd.extend(
                [
                    "-c:v",
                    "hevc_nvenc",
                    "-preset",
                    self.config.nvenc_preset,
                    "-cq",
                    str(self.config.nvenc_cq),
                    "-b:v",
                    self.config.nvenc_bitrate,
                    "-maxrate",
                    self.config.nvenc_maxrate,
                    "-bufsize",
                    self.config.nvenc_bufsize,
                    "-pix_fmt",
                    self.config.nvenc_pix_fmt,
                ]
            )
        else:
            cmd.extend(
                [
                    "-c:v",
                    "libx265",
                    "-preset",
                    "medium",
                    "-crf",
                    str(self.config.nvenc_cq),
                    "-pix_fmt",
                    self.config.nvenc_pix_fmt,
                ]
            )

        cmd.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                "320k",
                "-progress",
                "pipe:1",
                "-nostats",
                str(output_file.resolve()),
            ]
        )

        logger.info(
            "Executing FFmpeg render (%s, canvas: %dx%d @ %.1ffps)...",
            encoder,
            target_w,
            target_h,
            target_fps,
        )

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            if job_handle is not None and hasattr(job_handle, "_child_process"):
                job_handle._child_process = proc

            stderr_lines: list[str] = []

            def _drain_stderr() -> None:
                if proc.stderr:
                    try:
                        for err_line in proc.stderr:
                            stderr_lines.append(err_line)
                    except Exception:
                        pass

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_thread.start()

            total_frames = int(total_duration * target_fps)
            if proc.stdout:
                try:
                    for raw_line in proc.stdout:
                        if cancel_event is not None and cancel_event.is_set():
                            proc.terminate()
                            try:
                                proc.wait(timeout=2.0)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                            raise InterruptedError("Rendering cancelled by user.")

                        line = str(raw_line).strip()
                        if line.startswith("frame="):
                            val = line.split("=", 1)[1].strip()
                            if val.isdigit():
                                curr_frame = int(val)
                                if total_frames > 0 and progress_callback is not None:
                                    pct = min(99.0, (curr_frame / total_frames) * 100.0)
                                    progress_callback(
                                        pct,
                                        f"Rendering frame {curr_frame}/{total_frames} ({pct:.1f}%)",
                                    )
                        elif line == "progress=end":
                            if progress_callback is not None:
                                progress_callback(100.0, "Finalizing output container...")
                except Exception as exc:
                    if not isinstance(exc, InterruptedError):
                        logger.debug("Stdout streaming ended or encountered error: %s", exc)
                    else:
                        raise

            proc.wait()
            stderr_thread.join(timeout=3.0)

            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Rendering cancelled by user.")

            # If NVENC failed at runtime, try fallback to CPU libx265
            if proc.returncode != 0 and use_nvenc:
                logger.warning(
                    "NVENC render failed (code %d). Retrying with libx265 CPU fallback...",
                    proc.returncode,
                )
                return self.render_sequence(
                    scored_scenes=scored_scenes,
                    cut_timestamps=cut_timestamps,
                    soundtrack_path=soundtrack_path,
                    output_file=output_file,
                    force_cpu=True,
                    include_teaser=include_teaser,
                    sections=sections,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                    job_handle=job_handle,
                )

            if proc.returncode != 0:
                stderr_text = "".join(stderr_lines)
                raise RuntimeError(
                    f"FFmpeg render failed with exit code {proc.returncode}:\n{stderr_text[-1000:]}"
                )

            file_size = output_file.stat().st_size if output_file.is_file() else 0
            logger.info(
                "Render complete: %s (%.2f MB, encoder=%s)",
                output_file.name,
                file_size / (1024 * 1024),
                encoder,
            )

            return RenderJobResult(
                output_path=output_file,
                encoder_used=encoder,
                duration=total_duration,
                file_size_bytes=file_size,
                return_code=proc.returncode,
            )
        finally:
            if job_handle is not None and hasattr(job_handle, "_child_process"):
                job_handle._child_process = None
            if script_path.exists():
                script_path.unlink(missing_ok=True)
