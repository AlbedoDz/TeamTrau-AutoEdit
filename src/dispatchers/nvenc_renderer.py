"""Standalone headless video rendering engine utilizing FFmpeg NVENC acceleration.

Rendering is two-stage:

1. Every timeline clip is rendered to its own normalized segment (canvas, fps,
   exact frame count, 48 kHz stereo PCM camera audio). Each segment input is
   fast-seeked with ``-ss/-t`` so ffmpeg never decodes footage before the in-point,
   and segments can be encoded in parallel.
2. Segments are joined with the concat demuxer (video stream-copied) and the
   soundtrack / camera audio mix is encoded once into the final container.

The master is written to ``<name>.part`` and only renamed on success, so a failed
or cancelled render never leaves a corrupt deliverable behind.
"""

import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from src.core.config import PipelineConfig, check_nvenc_available
from src.core.ffmpeg_utils import atempo_chain, has_audio_stream, is_hw_encoder_error, popen_tool
from src.core.types import EditClip, EditTimeline, RenderJobResult, ScoredScene, SongSection
from src.dispatchers.watermark_helper import build_watermark_filter_lines
from src.modules.edit_timeline import build_edit_timeline

logger = logging.getLogger(__name__)

AUDIO_RATE = 48000
PREVIEW_HEIGHT = 540


# A render segment is exactly one clip of the edit timeline.
RenderSegment = EditClip


class RenderError(RuntimeError):
    """FFmpeg render failure carrying the tail of ffmpeg's stderr."""

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(f"{message}\n{stderr[-1500:]}" if stderr else message)
        self.stderr = stderr


def _even(v: float) -> int:
    return max(2, int(round(v / 2.0)) * 2)


class NvencRenderer:
    """Builds per-segment filter graphs and renders headless video via FFmpeg NVENC."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.nvenc_available = check_nvenc_available()

    # ------------------------------------------------------------------
    # Timeline -> segments
    # ------------------------------------------------------------------
    def build_segments(
        self,
        scored_scenes: list[ScoredScene],
        cut_timestamps: list[float],
        include_teaser: bool | None = None,
        sections: list[SongSection] | None = None,
    ) -> list[EditClip]:
        """Turn analysed scenes + beat cuts into frame-exact output segments."""
        if not scored_scenes:
            raise ValueError("No scenes provided for rendering.")
        if len(cut_timestamps) < 2:
            raise ValueError("cut_timestamps must have at least 2 boundary timestamps.")
        return build_edit_timeline(
            self.config,
            scored_scenes,
            cut_timestamps,
            sections=sections,
            include_teaser=include_teaser,
        ).clips

    # ------------------------------------------------------------------
    # Command builders (pure, unit-testable)
    # ------------------------------------------------------------------
    def canvas_size(self, preview: bool = False) -> tuple[int, int]:
        w, h = self.config.canvas_width, self.config.canvas_height
        if preview and h > PREVIEW_HEIGHT:
            return _even(w * PREVIEW_HEIGHT / h), PREVIEW_HEIGHT
        return w, h

    def encoder_args(self, use_nvenc: bool, preview: bool = False) -> tuple[str, list[str]]:
        cfg = self.config
        pix = ["-pix_fmt", cfg.nvenc_pix_fmt]
        if preview:
            if use_nvenc:
                return "h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "30", *pix]
            return "libx264", ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", *pix]
        if use_nvenc:
            return "hevc_nvenc", [
                "-c:v", "hevc_nvenc",
                "-preset", cfg.nvenc_preset,
                "-cq", str(cfg.nvenc_cq),
                "-b:v", cfg.nvenc_bitrate,
                "-maxrate", cfg.nvenc_maxrate,
                "-bufsize", cfg.nvenc_bufsize,
                *pix,
            ]
        return "libx265", [
            "-c:v", "libx265",
            "-preset", cfg.cpu_encoder_preset,
            "-crf", str(cfg.nvenc_cq),
            "-x265-params", "log-level=error:repeat-headers=1",
            *pix,
        ]

    def _geometry_filters(self, seg: RenderSegment, w: int, h: int, src: str, out: str) -> list[str]:
        """Scale / crop / backdrop the source label ``src`` onto a WxH canvas as ``out``."""
        cw = seg.crop_window
        is_vertical = w < h
        is_blur_backdrop = is_vertical and (
            (cw is not None and cw.tracking_subject == "blur_backdrop")
            or getattr(self.config, "reframing_mode", "auto_hybrid") == "blur_backdrop"
        )
        if is_blur_backdrop:
            return [
                f"[{src}]split=2[bg][fg]",
                f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                f"boxblur=20:5,eq=brightness=-0.15[bgb]",
                f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs]",
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[{out}]",
            ]
        if cw is not None and is_vertical:
            return [
                f"[{src}]crop={cw.width}:{cw.height}:{cw.x}:{cw.y},scale={w}:{h}[{out}]"
            ]
        return [
            f"[{src}]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black[{out}]"
        ]

    def build_segment_filter(
        self,
        seg: RenderSegment,
        canvas: tuple[int, int],
        with_audio: bool,
        source_has_audio: bool,
        watermark_input: int | None = None,
    ) -> str:
        """Filter graph for one segment: input 0 is the seeked source clip."""
        w, h = canvas
        fps = self.config.fps
        out_dur = seg.frames / fps
        lines: list[str] = [f"[0:v]setpts=(PTS-STARTPTS)/{seg.speed:.4f}[vsp]"]
        lines += self._geometry_filters(seg, w, h, "vsp", "vgeo")

        tail = "vgeo"
        if seg.ken_burns in ("zoom_in", "zoom_out"):
            n = max(1, seg.frames)
            k = max(1.0, float(self.config.ken_burns_scale_factor)) - 1.0
            z = f"1+{k:.4f}*on/{n}" if seg.ken_burns == "zoom_in" else f"{1 + k:.4f}-{k:.4f}*on/{n}"
            lines.append(
                f"[vgeo]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d=1:s={w}x{h}:fps={fps}[vkb]"
            )
            tail = "vkb"

        base_label = "vbase" if watermark_input is not None else "vseg"
        lines.append(
            f"[{tail}]setsar=1,fps={fps},"
            f"tpad=stop_mode=clone:stop_duration={out_dur + 1:.3f},"
            f"trim=end_frame={seg.frames},setpts=PTS-STARTPTS,"
            f"format={self.config.nvenc_pix_fmt}[{base_label}]"
        )
        if watermark_input is not None:
            lines.extend(
                line.rstrip(";")
                for line in build_watermark_filter_lines(
                    wm_input_idx=watermark_input,
                    base_v_label="vbase",
                    out_v_label="vseg",
                    position=self.config.watermark_position,
                    opacity=self.config.watermark_opacity,
                    margin=self.config.watermark_margin,
                )
            )

        if with_audio:
            n_samples = int(round(seg.frames * AUDIO_RATE / fps))
            norm = f"aresample={AUDIO_RATE},aformat=sample_fmts=s16:channel_layouts=stereo"
            if source_has_audio:
                tempo = atempo_chain(seg.speed)
                tempo = f"{tempo}," if tempo else ""
                lines.append(
                    f"[0:a]asetpts=PTS-STARTPTS,{tempo}{norm},apad,"
                    f"atrim=end_sample={n_samples},asetpts=PTS-STARTPTS[aseg]"
                )
            else:
                lines.append(
                    f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=end_sample={n_samples},"
                    f"aformat=sample_fmts=s16:channel_layouts=stereo[aseg]"
                )
        return ";".join(lines)

    def build_segment_command(
        self,
        ffmpeg_bin: str,
        seg: RenderSegment,
        out_path: Path,
        use_nvenc: bool,
        with_audio: bool,
        preview: bool = False,
    ) -> list[str]:
        canvas = self.canvas_size(preview)
        src_dur = min(seg.available, seg.frames / self.config.fps * seg.speed)
        src_has_audio = with_audio and has_audio_stream(seg.source_path)
        wm = self._watermark_path()

        cmd = [ffmpeg_bin, "-y", "-hide_banner", "-nostdin", "-loglevel", "error"]
        cmd += ["-ss", f"{max(0.0, seg.source_in):.3f}", "-t", f"{src_dur + 0.25:.3f}"]
        cmd += ["-i", str(Path(seg.source_path).resolve())]
        wm_idx = None
        if wm is not None:
            cmd += ["-loop", "1", "-i", str(wm)]
            wm_idx = 1

        graph = self.build_segment_filter(seg, canvas, with_audio, src_has_audio, wm_idx)
        cmd += ["-filter_complex", graph, "-map", "[vseg]"]
        _enc, enc_args = self.encoder_args(use_nvenc, preview)
        cmd += enc_args
        if with_audio:
            cmd += ["-map", "[aseg]", "-c:a", "pcm_s16le"]
        cmd += ["-f", "matroska", str(out_path)]
        return cmd

    def build_concat_command(
        self,
        ffmpeg_bin: str,
        list_file: Path,
        out_path: Path,
        total_duration: float,
        segments_have_audio: bool,
        soundtrack_path: Path | None,
        soundtrack_in: float = 0.0,
        encoder: str = "hevc_nvenc",
    ) -> list[str]:
        """Join stream-copied video segments and encode the final audio mix once."""
        cmd = [ffmpeg_bin, "-y", "-hide_banner", "-nostdin", "-loglevel", "error"]
        cmd += ["-f", "concat", "-safe", "0", "-i", str(list_file)]
        norm = f"aresample={AUDIO_RATE},aformat=channel_layouts=stereo"
        dur = f"{total_duration:.3f}"
        graph: str | None = None
        if soundtrack_path is not None:
            cmd += ["-ss", f"{max(0.0, soundtrack_in):.3f}", "-i", str(Path(soundtrack_path).resolve())]
            music = f"[1:a]{norm},apad,atrim=duration={dur},asetpts=PTS-STARTPTS"
            if segments_have_audio:
                if self.config.workflow_mode == "unboxing_narration":
                    graph = (
                        f"{music}[mus];[0:a]{norm},asplit=2[cam][sc];"
                        f"[mus][sc]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[duck];"
                        f"[duck][cam]amix=inputs=2:duration=first:normalize=0[aout]"
                    )
                else:
                    graph = (
                        f"{music}[mus];[0:a]{norm},volume={self.config.camera_audio_mix_db}dB[cam];"
                        f"[mus][cam]amix=inputs=2:duration=first:normalize=0[aout]"
                    )
            else:
                graph = f"{music}[aout]"
        elif segments_have_audio:
            graph = f"[0:a]{norm}[aout]"

        cmd += ["-map", "0:v"]
        if graph is not None:
            cmd += ["-filter_complex", graph, "-map", "[aout]", "-c:a", "aac", "-b:a", "320k"]
        cmd += ["-c:v", "copy"]
        if "hevc" in encoder or encoder == "libx265":
            cmd += ["-tag:v", "hvc1"]
        cmd += ["-t", dur, "-movflags", "+faststart", "-f", "mp4", str(out_path)]
        return cmd

    def _watermark_path(self) -> Path | None:
        cfg = self.config
        if cfg.watermark_enabled and cfg.watermark_path and Path(cfg.watermark_path).exists():
            return Path(cfg.watermark_path).resolve()
        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _run(
        self,
        cmd: list[str],
        cancel_event: threading.Event | None,
        job_handle: Any | None,
    ) -> tuple[int, str]:
        proc = popen_tool(cmd)
        if job_handle is not None and hasattr(job_handle, "_child_process"):
            job_handle._child_process = proc
        stderr = ""
        while True:
            try:
                _out, stderr = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    proc.terminate()
                    try:
                        proc.communicate(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
                    raise InterruptedError("Rendering cancelled by user.") from None
        return proc.returncode, stderr or ""

    def render_segments(
        self,
        segments: list[RenderSegment],
        soundtrack_path: Path | None,
        output_file: Path,
        force_cpu: bool = False,
        preview: bool = False,
        soundtrack_in: float = 0.0,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        job_handle: Any | None = None,
    ) -> RenderJobResult:
        """Render an explicit, ordered list of segments to ``output_file``."""
        if not segments:
            raise ValueError("No segments provided for rendering.")
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise FileNotFoundError("FFmpeg executable not found on system PATH.")

        output_file = Path(output_file)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        part_file = output_file.with_name(output_file.name + ".part")
        fps = self.config.fps
        total_frames = sum(s.frames for s in segments)
        total_duration = total_frames / fps

        has_soundtrack = soundtrack_path is not None and Path(soundtrack_path).exists()
        with_audio = (not has_soundtrack) or bool(self.config.keep_video_audio)
        use_nvenc = self.nvenc_available and not force_cpu

        work_root = Path(self.config.cache_dir).resolve() / "render"
        work_root.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="seg_", dir=work_root))

        def notify(pct: float, msg: str) -> None:
            if progress_callback is not None:
                progress_callback(pct, msg)

        try:
            while True:
                seg_paths = [work_dir / f"seg_{i:05d}.mkv" for i in range(len(segments))]
                done = 0
                lock = threading.Lock()

                def render_one(i: int, nvenc: bool = use_nvenc) -> None:
                    nonlocal done
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("Rendering cancelled by user.")
                    cmd = self.build_segment_command(
                        ffmpeg_bin, segments[i], seg_paths[i], nvenc, with_audio, preview
                    )
                    rc, err = self._run(cmd, cancel_event, job_handle)
                    if rc != 0:
                        raise RenderError(f"Segment {i + 1}/{len(segments)} failed (code {rc}).", err)
                    with lock:
                        done += 1
                        notify(95.0 * done / len(segments), f"Rendered segment {done}/{len(segments)}")

                try:
                    # First segment alone: validates encoder/driver before fanning out.
                    render_one(0)
                    workers = 1 if preview else self.config.render_workers
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = [pool.submit(render_one, i) for i in range(1, len(segments))]
                        try:
                            for f in futures:
                                f.result()
                        except BaseException:
                            pool.shutdown(wait=True, cancel_futures=True)
                            raise
                except RenderError as exc:
                    if use_nvenc and is_hw_encoder_error(exc.stderr):
                        logger.warning("NVENC failed (%s). Re-rendering on CPU encoder.", exc.stderr[-200:])
                        use_nvenc = False
                        for p in work_dir.glob("seg_*.mkv"):
                            p.unlink(missing_ok=True)
                        continue
                    raise
                break

            encoder, _ = self.encoder_args(use_nvenc, preview)
            list_file = work_dir / "concat.txt"
            list_file.write_text(
                "".join(
                    "file '{}'\n".format(p.as_posix().replace("'", "'\\''")) for p in seg_paths
                ),
                encoding="utf-8",
            )
            notify(96.0, "Muxing master and mixing audio...")
            cmd = self.build_concat_command(
                ffmpeg_bin,
                list_file,
                part_file,
                total_duration,
                with_audio,
                Path(soundtrack_path) if has_soundtrack and soundtrack_path else None,
                soundtrack_in,
                encoder,
            )
            rc, err = self._run(cmd, cancel_event, job_handle)
            if rc != 0:
                raise RenderError(f"FFmpeg concat/mux failed with exit code {rc}.", err)
            os.replace(part_file, output_file)
            notify(100.0, "Render complete.")

            file_size = output_file.stat().st_size
            logger.info(
                "Render complete: %s (%.2f MB, encoder=%s, %d segments)",
                output_file.name, file_size / (1024 * 1024), encoder, len(segments),
            )
            return RenderJobResult(
                output_path=output_file,
                encoder_used=encoder,
                duration=total_duration,
                file_size_bytes=file_size,
                return_code=0,
            )
        finally:
            if job_handle is not None and hasattr(job_handle, "_child_process"):
                job_handle._child_process = None
            part_file.unlink(missing_ok=True)
            shutil.rmtree(work_dir, ignore_errors=True)

    def render_edit_timeline(
        self,
        edit_tl: EditTimeline,
        output_file: Path,
        force_cpu: bool = False,
        preview: bool = False,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        job_handle: Any | None = None,
    ) -> RenderJobResult:
        """Render exactly what is on the edit timeline (canvas and fps come from the timeline)."""
        renderer = self
        if (edit_tl.canvas_width, edit_tl.canvas_height, edit_tl.fps) != (
            self.config.canvas_width,
            self.config.canvas_height,
            self.config.fps,
        ):
            renderer = NvencRenderer(
                self.config.model_copy(
                    update={
                        "canvas_width": edit_tl.canvas_width,
                        "canvas_height": edit_tl.canvas_height,
                        "fps": edit_tl.fps,
                    }
                )
            )
            renderer.nvenc_available = self.nvenc_available
        if edit_tl.kind == "rough_cut":
            renderer.config = renderer.config.model_copy(update={"keep_video_audio": True})
        return renderer.render_segments(
            edit_tl.clips,
            edit_tl.soundtrack_path,
            output_file,
            force_cpu=force_cpu,
            preview=preview,
            soundtrack_in=edit_tl.soundtrack_in,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            job_handle=job_handle,
        )

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
        preview: bool = False,
    ) -> RenderJobResult:
        """Render analysed scenes on beat cuts with background music to a master file."""
        segments = self.build_segments(scored_scenes, cut_timestamps, include_teaser, sections)
        return self.render_segments(
            segments,
            soundtrack_path,
            output_file,
            force_cpu=force_cpu,
            preview=preview,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            job_handle=job_handle,
        )
