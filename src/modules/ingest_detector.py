"""Non-destructive video ingestion, scene detection, and pre-trimming module."""

import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import cv2
from scenedetect import AdaptiveDetector, SceneManager, open_video

from src.core.config import PipelineConfig
from src.core.ffmpeg_utils import ffprobe_json, run_tool, stream_rotation
from src.core.types import ActiveSegment, SceneCut, VideoMetadata

logger = logging.getLogger(__name__)


def _parse_rate(s: str) -> float:
    """Parse an ffprobe rational like '30000/1001' into frames per second."""
    try:
        if "/" in s:
            n, d = s.split("/", 1)
            den = float(d)
            return float(n) / den if den != 0.0 else 0.0
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _to_float(v: object) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


class IngestDetector:
    """Orchestrates non-destructive shot detection and motion pre-trimming."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.failed_files: list[tuple[Path, str]] = []

    def probe_video(
        self,
        video_path: Path,
        camera_role: str | None = None,
        source_folder: Path | None = None,
    ) -> VideoMetadata:
        """Extract display dimensions, framerate, duration, audio presence and camera role.

        ffprobe is authoritative (display rotation, container duration, VFR flag, audio
        stream); OpenCV is only a fallback when ffprobe is unavailable.
        """
        if not video_path.is_file():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        width = height = total_frames = 0
        fps = duration = 0.0
        is_vfr = False
        rotation = 0
        has_audio = True
        codec = "unknown"

        data = ffprobe_json(video_path)
        if data is not None:
            streams = data.get("streams", []) or []
            video_streams = [s for s in streams if s.get("codec_type", "video") == "video"]
            if video_streams:
                vs = video_streams[0]
                r_fps = _parse_rate(vs.get("r_frame_rate", ""))
                avg_fps = _parse_rate(vs.get("avg_frame_rate", ""))
                if r_fps > 0 and avg_fps > 0 and abs(r_fps - avg_fps) > 0.05:
                    is_vfr = True
                fps = avg_fps or r_fps
                width = int(vs.get("width") or 0)
                height = int(vs.get("height") or 0)
                rotation = stream_rotation(vs)
                if rotation in (90, 270):
                    width, height = height, width
                codec = str(vs.get("codec_name") or codec)
                duration = _to_float(vs.get("duration")) or _to_float(
                    (data.get("format") or {}).get("duration")
                )
                total_frames = int(_to_float(vs.get("nb_frames")))
            if any("codec_type" in s for s in streams):
                has_audio = any(s.get("codec_type") == "audio" for s in streams)

        if width <= 0 or height <= 0 or fps <= 0 or duration <= 0:
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                if width > 0 and fps > 0:
                    cap = None
                else:
                    raise ValueError(f"Unable to open video stream: {video_path}")
            if cap is not None:
                width = width or int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = height or int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = fps if fps > 0 else float(cap.get(cv2.CAP_PROP_FPS))
                total_frames = total_frames or int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                cap.release()

        if fps <= 0:
            fps = self.config.fps
        if duration <= 0:
            duration = total_frames / fps if fps > 0 else 0.0
        if total_frames <= 0:
            total_frames = int(round(duration * fps))

        from src.core.ontology import infer_camera_role

        inferred_role = camera_role or infer_camera_role(source_folder or video_path).value

        return VideoMetadata(
            path=video_path,
            width=width,
            height=height,
            fps=fps,
            duration=duration,
            total_frames=total_frames,
            codec=codec,
            camera_role=inferred_role,
            source_folder=source_folder or video_path.parent,
            is_vfr=is_vfr,
            rotation=rotation,
            has_audio=has_audio,
        )

    def _cfr_cache_path(self, video_path: Path, target_fps: float) -> Path:
        st = video_path.stat()
        key = f"{video_path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{target_fps}"
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        return Path(self.config.cache_dir) / "cfr" / f"{video_path.stem}_{h}_cfr{int(round(target_fps))}.mkv"

    def normalize_to_cfr(
        self,
        video_path: Path,
        target_fps: float = 30.0,
        output_path: Path | None = None,
    ) -> Path:
        """Execute fast FFmpeg pass to convert VFR footage to Constant Frame Rate (CFR).

        Uses fast encoding: -c:v libx264 -preset ultrafast -crf 17 -vsync cfr -r {target_fps}.
        Output is cached under ``config.cache_dir/cfr`` keyed by path+size+mtime and written
        atomically (temp file then rename) so interrupted runs never poison the cache.
        """
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            logger.warning(
                "FFmpeg binary not found. Skipping CFR normalization for %s",
                video_path.name,
            )
            return video_path

        if output_path is None:
            output_path = self._cfr_cache_path(video_path, target_fps)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists() and output_path.stat().st_size > 0:
            logger.info("Reusing existing CFR normalized video: %s", output_path)
            return output_path

        tmp_path = output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")
        logger.info(
            "Normalizing VFR video %s to CFR (target %s fps) -> %s",
            video_path.name,
            target_fps,
            output_path.name,
        )
        cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-nostdin",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "17",
            "-vsync",
            "cfr",
            "-r",
            str(target_fps),
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            str(tmp_path),
        ]
        try:
            res = run_tool(cmd)
            if res.returncode != 0:
                logger.error("FFmpeg CFR normalization failed for %s: %s", video_path, res.stderr)
                raise RuntimeError(f"CFR normalization failed: {(res.stderr or '')[-800:]}")
            if tmp_path.exists():
                os.replace(tmp_path, output_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return output_path

    def detect_scenes(
        self, video_path: Path, metadata: VideoMetadata | None = None
    ) -> list[SceneCut]:
        """Detect camera shot boundaries non-destructively using PySceneDetect.

        ``metadata`` (already probed) avoids a redundant ffprobe call.
        """
        metadata = metadata or self.probe_video(video_path)
        min_scene_len_frames = max(1, int(self.config.min_scene_len_sec * metadata.fps))

        video = open_video(str(video_path))
        scene_manager = SceneManager()
        scene_manager.add_detector(
            AdaptiveDetector(
                adaptive_threshold=self.config.adaptive_threshold,
                min_scene_len=min_scene_len_frames,
            )
        )

        scene_manager.detect_scenes(video)
        scene_list = scene_manager.get_scene_list()

        cuts: list[SceneCut] = []
        if not scene_list:
            # Fallback: treat the entire clip as a single continuous scene
            cuts.append(
                SceneCut(
                    clip_path=video_path,
                    start_time=0.0,
                    end_time=metadata.duration,
                    start_frame=0,
                    end_frame=metadata.total_frames,
                    duration=metadata.duration,
                )
            )
            return cuts

        for start_tc, end_tc in scene_list:
            start_sec = float(start_tc.seconds if hasattr(start_tc, "seconds") else start_tc.get_seconds())
            end_sec = float(end_tc.seconds if hasattr(end_tc, "seconds") else end_tc.get_seconds())
            start_frm = int(start_tc.frame_num if hasattr(start_tc, "frame_num") else start_tc.get_frames())
            end_frm = int(end_tc.frame_num if hasattr(end_tc, "frame_num") else end_tc.get_frames())
            dur = max(0.0, end_sec - start_sec)

            cuts.append(
                SceneCut(
                    clip_path=video_path,
                    start_time=start_sec,
                    end_time=end_sec,
                    start_frame=start_frm,
                    end_frame=end_frm,
                    duration=dur,
                )
            )

        logger.info("Detected %d scenes in %s", len(cuts), video_path.name)
        return cuts

    @staticmethod
    def _parse_ranges(stderr: str, kind: str) -> list[tuple[float, float]]:
        """Parse ``{kind}_start`` / ``{kind}_end`` markers from ffmpeg filter stderr."""
        start_tag, end_tag = f"{kind}_start:", f"{kind}_end:"
        ranges: list[tuple[float, float]] = []
        current_start: float | None = None
        for line in stderr.splitlines():
            if start_tag in line:
                try:
                    current_start = float(line.split(start_tag)[1].strip().split()[0])
                except (ValueError, IndexError):
                    current_start = None
            elif end_tag in line and current_start is not None:
                try:
                    end_val = float(line.split(end_tag)[1].strip().split()[0])
                    ranges.append((current_start, end_val))
                except (ValueError, IndexError):
                    pass
                current_start = None
        if current_start is not None and kind == "freeze":
            # freezedetect emits no freeze_end when the clip ends while frozen: close the
            # interval at the last decoded timestamp from ffmpeg's progress output.
            matches = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
            if matches:
                h, m, sec = matches[-1]
                end_val = int(h) * 3600 + int(m) * 60 + float(sec)
                if end_val > current_start:
                    ranges.append((current_start, end_val))
        return ranges

    def _silence_filter(self) -> str:
        return f"silencedetect=noise={self.config.silence_threshold_db}dB:d={self.config.dead_air_min_duration}"

    def _freeze_filter(self) -> str:
        return f"scale=320:-2,freezedetect=n={self.config.freeze_threshold}:d={self.config.dead_air_min_duration}"

    def detect_dead_air_ranges(
        self,
        video_path: Path,
        has_audio: bool | None = None,
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """Detect silence AND freeze intervals in ONE ffmpeg decode pass.

        Returns ``(silence_ranges, freeze_ranges)``. When the file has no audio stream the
        audio filter is omitted (silence is then empty).
        """
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return [], []
        if has_audio is None:
            from src.core.ffmpeg_utils import has_audio_stream

            has_audio = has_audio_stream(video_path)

        cmd = [ffmpeg_bin, "-hide_banner", "-nostdin", "-i", str(video_path)]
        if has_audio:
            cmd += ["-af", self._silence_filter()]
        else:
            cmd += ["-an"]
        cmd += ["-vf", self._freeze_filter(), "-f", "null", "-"]
        try:
            res = run_tool(cmd)
        except (subprocess.SubprocessError, OSError):
            return [], []
        stderr = res.stderr or ""
        return self._parse_ranges(stderr, "silence"), self._parse_ranges(stderr, "freeze")

    def detect_silence_ranges(self, video_path: Path) -> list[tuple[float, float]]:
        """Detect audio silence intervals using FFmpeg silencedetect filter."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return []
        cmd = [
            ffmpeg_bin, "-hide_banner", "-nostdin", "-i", str(video_path),
            "-vn", "-af", self._silence_filter(), "-f", "null", "-",
        ]  # fmt: skip
        try:
            res = run_tool(cmd)
        except (subprocess.SubprocessError, OSError):
            return []
        return self._parse_ranges(res.stderr or "", "silence")

    def detect_freeze_ranges(self, video_path: Path) -> list[tuple[float, float]]:
        """Detect visually static/frozen intervals using FFmpeg freezedetect filter."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return []
        cmd = [
            ffmpeg_bin, "-hide_banner", "-nostdin", "-i", str(video_path),
            "-an", "-vf", self._freeze_filter(), "-f", "null", "-",
        ]  # fmt: skip
        try:
            res = run_tool(cmd)
        except (subprocess.SubprocessError, OSError):
            return []
        return self._parse_ranges(res.stderr or "", "freeze")

    def find_dead_air_intervals(
        self,
        silence_ranges: list[tuple[float, float]],
        freeze_ranges: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        """Identify intervals where silence AND video freeze overlap (true dead air)."""
        if not silence_ranges or not freeze_ranges:
            return []

        dead_air: list[tuple[float, float]] = []
        sil = sorted(silence_ranges)
        frz = sorted(freeze_ranges)
        i = j = 0
        while i < len(sil) and j < len(frz):
            overlap_start = max(sil[i][0], frz[j][0])
            overlap_end = min(sil[i][1], frz[j][1])
            if overlap_end > overlap_start and overlap_end - overlap_start >= self.config.dead_air_min_duration:
                dead_air.append((overlap_start, overlap_end))
            # Advance whichever interval ends first.
            if sil[i][1] <= frz[j][1]:
                i += 1
            else:
                j += 1
        return dead_air

    def analyze_activity(
        self, video_path: Path, metadata: VideoMetadata | None = None
    ) -> list[ActiveSegment]:
        """Evaluate motion activity to prune static sections.

        Attempts Auto-Editor CLI first; falls back to native OpenCV frame-difference analysis.
        """
        auto_editor_bin = shutil.which("auto-editor")
        if auto_editor_bin:
            try:
                return self._analyze_activity_auto_editor(video_path, auto_editor_bin)
            except Exception as exc:
                logger.warning(
                    "Auto-Editor CLI execution failed (%s). Falling back to OpenCV motion.",
                    exc,
                )

        return self._analyze_activity_opencv(video_path, metadata=metadata)

    def _analyze_activity_auto_editor(
        self, video_path: Path, executable: str
    ) -> list[ActiveSegment]:
        """Execute Auto-Editor in inspection mode to extract active motion intervals."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_json = Path(tmp.name)

        try:
            cmd = [
                executable,
                str(video_path),
                "--export",
                f"json:{tmp_json}",
                "--edit",
                f"motion:{self.config.auto_editor_motion_threshold}",
                "--margin",
                self.config.auto_editor_margin,
                "--no-color",
            ]
            res = run_tool(cmd, timeout=600)

            if res.returncode == 0 and tmp_json.is_file() and tmp_json.stat().st_size > 0:
                with tmp_json.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                segments: list[ActiveSegment] = []
                timeline_data = data.get("timeline", {})
                chunks = timeline_data.get("chunks", [])
                timebase = float(timeline_data.get("timebase", self.config.fps))

                for chunk in chunks:
                    # In Auto-Editor json: [start_frame, end_frame, speed]
                    start_frm, end_frm, speed = chunk[0], chunk[1], chunk[2]
                    # speed == 1 means keep (active), speed == 99999 or 0 means cut
                    is_active = float(speed) <= 1.5
                    start_t = start_frm / timebase
                    end_t = end_frm / timebase
                    segments.append(
                        ActiveSegment(
                            start_time=start_t,
                            end_time=end_t,
                            motion_score=1.0 if is_active else 0.0,
                            is_active=is_active,
                        )
                    )
                if segments:
                    return segments
        finally:
            if tmp_json.exists():
                tmp_json.unlink(missing_ok=True)

        return self._analyze_activity_opencv(video_path)

    def _analyze_activity_opencv(
        self,
        video_path: Path,
        sample_interval_sec: float = 0.5,
        metadata: VideoMetadata | None = None,
    ) -> list[ActiveSegment]:
        """Compute inter-frame motion scores via frame-difference luminance variance.

        Reads the stream sequentially with ``grab()`` (cheap, no colour conversion) and only
        ``retrieve()``s the sampled frames - random ``CAP_PROP_POS_FRAMES`` seeks re-decode
        from the previous keyframe every time and were ~5x slower on long-GOP footage.
        """
        metadata = metadata or self.probe_video(video_path)
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return [
                ActiveSegment(
                    start_time=0.0,
                    end_time=metadata.duration,
                    motion_score=1.0,
                    is_active=True,
                )
            ]

        step_frames = max(1, int(metadata.fps * sample_interval_sec))
        prev_gray = None
        segments: list[ActiveSegment] = []
        frame_idx = 0

        while frame_idx < metadata.total_frames:
            if not cap.grab():
                break
            if frame_idx % step_frames == 0:
                ret, frame = cap.retrieve()
                if ret and frame is not None:
                    # Downsample for ultra-fast motion estimation
                    small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
                    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

                    start_t = frame_idx / metadata.fps
                    end_t = min(metadata.duration, (frame_idx + step_frames) / metadata.fps)

                    if prev_gray is not None:
                        mean_diff = float(cv2.absdiff(gray, prev_gray).mean()) / 255.0
                        segments.append(
                            ActiveSegment(
                                start_time=start_t,
                                end_time=end_t,
                                motion_score=mean_diff,
                                is_active=mean_diff >= self.config.auto_editor_motion_threshold,
                            )
                        )
                    else:
                        segments.append(
                            ActiveSegment(
                                start_time=start_t,
                                end_time=end_t,
                                motion_score=1.0,
                                is_active=True,
                            )
                        )
                    prev_gray = gray
            frame_idx += 1

        cap.release()

        if not segments:
            segments.append(
                ActiveSegment(
                    start_time=0.0,
                    end_time=metadata.duration,
                    motion_score=1.0,
                    is_active=True,
                )
            )

        return segments

    def filter_scenes_by_activity(
        self,
        scenes: list[SceneCut],
        activity_segments: list[ActiveSegment],
        dead_air_intervals: list[tuple[float, float]] | None = None,
    ) -> list[SceneCut]:
        """Prune or refine scenes that coincide with dead air or inactive/static segments."""
        if not scenes:
            return []

        dead_sorted = sorted(dead_air_intervals or [])
        active_sorted = sorted(
            (seg.start_time, seg.end_time) for seg in activity_segments if seg.is_active
        )

        def _overlap_total(intervals: list[tuple[float, float]], start: float, end: float) -> float:
            """Total overlap of sorted intervals with [start, end] (bisect to the first candidate)."""
            total = 0.0
            lo = bisect_left(intervals, (start, float("-inf")))
            # The interval just before ``lo`` may still straddle ``start``.
            if lo > 0:
                lo -= 1
            for k in range(lo, len(intervals)):
                i_start, i_end = intervals[k]
                if i_start >= end:
                    break
                ov = min(end, i_end) - max(start, i_start)
                if ov > 0:
                    total += ov
            return total

        filtered: list[SceneCut] = []
        for scene in scenes:
            # Check dead-air overlap (simultaneous audio silence + video freeze)
            if dead_sorted and scene.duration > 0:
                dead_overlap = _overlap_total(dead_sorted, scene.start_time, scene.end_time)
                if (dead_overlap / scene.duration) >= 0.5:
                    logger.debug(
                        "Pruning dead-air scene: %s [%.2f - %.2f] (dead-air ratio: %.2f)",
                        scene.clip_path.name,
                        scene.start_time,
                        scene.end_time,
                        dead_overlap / scene.duration,
                    )
                    continue

            # Check overlap with active segments
            if activity_segments:
                active_duration = _overlap_total(active_sorted, scene.start_time, scene.end_time)

                # If at least 25% of the scene has active motion, keep it
                activity_ratio = active_duration / scene.duration if scene.duration > 0 else 1.0
                if activity_ratio >= 0.25:
                    filtered.append(scene)
                else:
                    logger.debug(
                        "Pruning static scene: %s [%.2f - %.2f] (activity ratio: %.2f)",
                        scene.clip_path.name,
                        scene.start_time,
                        scene.end_time,
                        activity_ratio,
                    )
            else:
                filtered.append(scene)

        # Safety fallback: if all scenes got pruned, retain originals
        return filtered if filtered else scenes

    def export_metadata_csv(self, scenes: list[SceneCut], output_csv: Path) -> None:
        """Export scene timecodes to non-destructive CSV."""
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        with output_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Clip Name",
                    "Start Time (s)",
                    "End Time (s)",
                    "Start Frame",
                    "End Frame",
                    "Duration (s)",
                ]
            )
            for sc in scenes:
                writer.writerow(
                    [
                        sc.clip_path.name,
                        f"{sc.start_time:.3f}",
                        f"{sc.end_time:.3f}",
                        sc.start_frame,
                        sc.end_frame,
                        f"{sc.duration:.3f}",
                    ]
                )

    def export_metadata_edl(self, scenes: list[SceneCut], output_edl: Path, fps: float) -> None:
        """Export scene timecodes as a standard CMX 3600 EDL file."""
        output_edl.parent.mkdir(parents=True, exist_ok=True)

        def to_tc(seconds: float, frame_rate: float) -> str:
            total_f = int(round(seconds * frame_rate))
            ff = total_f % int(round(frame_rate))
            total_s = total_f // int(round(frame_rate))
            ss = total_s % 60
            mm = (total_s // 60) % 60
            hh = (total_s // 3600) % 24
            return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

        lines = ["TITLE: AUTOEDIT_NON_DESTRUCTIVE_SCENES", "FCM: NON-DROP FRAME", ""]

        timeline_pos = 0.0
        for idx, sc in enumerate(scenes, start=1):
            src_in = to_tc(sc.start_time, fps)
            src_out = to_tc(sc.end_time, fps)
            rec_in = to_tc(timeline_pos, fps)
            rec_out = to_tc(timeline_pos + sc.duration, fps)
            timeline_pos += sc.duration

            lines.append(
                f"{idx:03d}  AX       V     C        {src_in} {src_out} {rec_in} {rec_out}"
            )
            lines.append(f"* FROM CLIP NAME: {sc.clip_path.name}")
            lines.append("")

        with output_edl.open("w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    # --- Kaizen Optimization: Scene Metadata Cache & Parallel Processing ---

    def _get_cache_path(self, video_path: Path) -> Path:
        """Compute deterministic hash cache path based on video identity and detection params."""
        stat = video_path.stat()
        key_str = (
            f"{video_path.resolve()}_{stat.st_size}_{stat.st_mtime}_"
            f"{self.config.adaptive_threshold}_{self.config.min_scene_len_sec}_"
            f"{self.config.auto_editor_motion_threshold}_"
            f"{self.config.enable_dead_air_detection}_{self.config.silence_threshold_db}_"
            f"{self.config.freeze_threshold}_{self.config.dead_air_min_duration}"
        )
        h = hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]
        return self.config.cache_dir / f"scene_{video_path.stem}_{h}.json"

    def _load_cached_scenes(self, cache_file: Path, video_path: Path) -> list[SceneCut] | None:
        """Load parsed scenes from disk cache if valid."""
        if not self.config.enable_cache or not cache_file.is_file():
            return None
        try:
            with cache_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            cuts: list[SceneCut] = []
            for item in data:
                cuts.append(
                    SceneCut(
                        clip_path=video_path,
                        start_time=float(item["start_time"]),
                        end_time=float(item["end_time"]),
                        start_frame=int(item["start_frame"]),
                        end_frame=int(item["end_frame"]),
                        duration=float(item["duration"]),
                    )
                )
            logger.info("Cache hit for %s (%d scenes)", video_path.name, len(cuts))
            return cuts
        except Exception as exc:
            logger.warning("Failed to read cache for %s (%s). Recomputing...", video_path.name, exc)
            return None

    def _save_cached_scenes(self, cache_file: Path, scenes: list[SceneCut]) -> None:
        """Serialize scenes to disk cache."""
        if not self.config.enable_cache:
            return
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "start_time": sc.start_time,
                    "end_time": sc.end_time,
                    "start_frame": sc.start_frame,
                    "end_frame": sc.end_frame,
                    "duration": sc.duration,
                }
                for sc in scenes
            ]
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.debug("Saved %d scenes to cache: %s", len(scenes), cache_file.name)
        except Exception as exc:
            logger.warning("Failed to save scene cache (%s)", exc)

    def process_single_video(
        self,
        video_path: Path,
        cancel_event: threading.Event | None = None,
        metadata: VideoMetadata | None = None,
    ) -> list[SceneCut]:
        """Detect scenes and prune activity for a single video with caching.

        ``metadata`` is the already-probed ``VideoMetadata`` for ``video_path`` (e.g. from the
        service's VFR pre-pass); it spares a repeated ffprobe per processing step.
        """
        if cancel_event and cancel_event.is_set():
            return []

        if getattr(self.config, "auto_normalize_vfr", True):
            try:
                metadata = metadata or self.probe_video(video_path)
                if metadata.is_vfr:
                    video_path = self.normalize_to_cfr(video_path, target_fps=self.config.fps)
                    metadata = None  # the normalised file differs from the probed one
            except Exception as e:
                logger.warning("Auto CFR normalization check failed for %s: %s", video_path.name, e)
                metadata = None

        cache_file = self._get_cache_path(video_path)
        cached = self._load_cached_scenes(cache_file, video_path)
        if cached is not None:
            return cached

        metadata = metadata or self.probe_video(video_path)

        # Full pipeline for single video
        raw_scenes = self.detect_scenes(video_path, metadata=metadata)
        if cancel_event and cancel_event.is_set():
            return []

        activity = self.analyze_activity(video_path, metadata=metadata)
        if cancel_event and cancel_event.is_set():
            return []

        dead_air: list[tuple[float, float]] = []
        if self.config.enable_dead_air_detection and metadata.has_audio:
            # Dead air needs silence AND freeze: skip the pass for audio-less clips.
            silence_ranges, freeze_ranges = self.detect_dead_air_ranges(video_path, has_audio=True)
            dead_air = self.find_dead_air_intervals(silence_ranges, freeze_ranges)

        final_scenes = self.filter_scenes_by_activity(raw_scenes, activity, dead_air)

        self._save_cached_scenes(cache_file, final_scenes)
        return final_scenes

    def _process_with_metadata(
        self,
        vp: Path,
        cancel_event: threading.Event | None,
        metadata_map: dict[Path, VideoMetadata] | None,
    ) -> list[SceneCut]:
        meta = (metadata_map or {}).get(vp)
        if meta is None:
            return self.process_single_video(vp, cancel_event)
        return self.process_single_video(vp, cancel_event, metadata=meta)

    def batch_process_videos(
        self,
        video_paths: list[Path],
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        metadata_map: dict[Path, VideoMetadata] | None = None,
    ) -> list[SceneCut]:
        """Process multiple videos concurrently using thread pool workers.

        ``metadata_map`` optionally supplies already-probed metadata keyed by path.
        """
        if not video_paths:
            return []

        workers = max(1, min(self.config.max_workers, len(video_paths)))
        logger.info(
            "Batch processing %d video files with %d concurrent workers (cache: %s)...",
            len(video_paths),
            workers,
            "ENABLED" if self.config.enable_cache else "DISABLED",
        )

        all_scenes: list[SceneCut] = []
        video_results: dict[Path, list[SceneCut]] = {}
        completed_count = 0
        total_videos = len(video_paths)

        self.failed_files = []
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            future_to_path = {
                executor.submit(self._process_with_metadata, vp, cancel_event, metadata_map): vp
                for vp in video_paths
            }
            for future in as_completed(future_to_path):
                vp = future_to_path[future]
                completed_count += 1
                try:
                    cuts = future.result()
                    video_results[vp] = cuts
                    logger.info("Completed %s: %d scenes", vp.name, len(cuts))
                except InterruptedError:
                    raise
                except Exception as exc:
                    logger.error("Processing failed for %s: %s", vp.name, exc)
                    self.failed_files.append((vp, str(exc)))
                    video_results[vp] = []

                if cancel_event and cancel_event.is_set():
                    raise InterruptedError("Batch ingestion cancelled by user.")

                if progress_callback:
                    pct = (completed_count / total_videos) * 100.0
                    progress_callback(pct, f"Processed {vp.name} ({completed_count}/{total_videos})")
        except BaseException:
            # Don't wait for queued PySceneDetect jobs on cancel/failure.
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        executor.shutdown(wait=True)

        # Assemble in original file order
        for vp in video_paths:
            all_scenes.extend(video_results.get(vp, []))

        return all_scenes

    def clear_cache(self) -> int:
        """Remove all stored scene cache files from cache directory."""
        if not self.config.cache_dir.exists():
            return 0
        count = 0
        for f in self.config.cache_dir.glob("scene_*.json"):
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
        logger.info("Cleared %d cached scene files from %s", count, self.config.cache_dir)
        return count

