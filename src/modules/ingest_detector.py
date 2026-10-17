"""Non-destructive video ingestion, scene detection, and pre-trimming module."""

import csv
import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import cv2
from scenedetect import AdaptiveDetector, SceneManager, open_video

from src.core.config import PipelineConfig
from src.core.types import ActiveSegment, SceneCut, VideoMetadata

logger = logging.getLogger(__name__)


class IngestDetector:
    """Orchestrates non-destructive shot detection and motion pre-trimming."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def probe_video(self, video_path: Path) -> VideoMetadata:
        """Extract frame dimensions, framerate, and duration from video file."""
        if not video_path.is_file():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise ValueError(f"Unable to open video stream via OpenCV: {video_path}")

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()

        if fps <= 0:
            fps = self.config.fps

        duration = total_frames / fps if fps > 0 else 0.0

        return VideoMetadata(
            path=video_path,
            width=width,
            height=height,
            fps=fps,
            duration=duration,
            total_frames=total_frames,
            codec="raw",
        )

    def detect_scenes(self, video_path: Path) -> list[SceneCut]:
        """Detect camera shot boundaries non-destructively using PySceneDetect."""
        metadata = self.probe_video(video_path)
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

    def detect_silence_ranges(self, video_path: Path) -> list[tuple[float, float]]:
        """Detect audio silence intervals using FFmpeg silencedetect filter."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return []

        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-i",
            str(video_path),
            "-af",
            f"silencedetect=noise={self.config.silence_threshold_db}dB:d={self.config.dead_air_min_duration}",
            "-f",
            "null",
            "-",
        ]
        try:
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=60
            )
        except (subprocess.SubprocessError, OSError):
            return []

        ranges: list[tuple[float, float]] = []
        current_start: float | None = None
        for line in res.stderr.splitlines():
            if "silence_start:" in line:
                try:
                    current_start = float(line.split("silence_start:")[1].strip().split()[0])
                except (ValueError, IndexError):
                    current_start = None
            elif "silence_end:" in line and current_start is not None:
                try:
                    end_val = float(line.split("silence_end:")[1].strip().split()[0])
                    ranges.append((current_start, end_val))
                except (ValueError, IndexError):
                    pass
                current_start = None
        return ranges

    def detect_freeze_ranges(self, video_path: Path) -> list[tuple[float, float]]:
        """Detect visually static/frozen intervals using FFmpeg freezedetect filter."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return []

        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-i",
            str(video_path),
            "-vf",
            f"freezedetect=n={self.config.freeze_threshold}:d={self.config.dead_air_min_duration}",
            "-f",
            "null",
            "-",
        ]
        try:
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=60
            )
        except (subprocess.SubprocessError, OSError):
            return []

        ranges: list[tuple[float, float]] = []
        current_start: float | None = None
        for line in res.stderr.splitlines():
            if "freeze_start:" in line:
                try:
                    current_start = float(line.split("freeze_start:")[1].strip().split()[0])
                except (ValueError, IndexError):
                    current_start = None
            elif "freeze_end:" in line and current_start is not None:
                try:
                    end_val = float(line.split("freeze_end:")[1].strip().split()[0])
                    ranges.append((current_start, end_val))
                except (ValueError, IndexError):
                    pass
                current_start = None
        return ranges

    def find_dead_air_intervals(
        self,
        silence_ranges: list[tuple[float, float]],
        freeze_ranges: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        """Identify intervals where silence AND video freeze overlap (true dead air)."""
        if not silence_ranges or not freeze_ranges:
            return []

        dead_air: list[tuple[float, float]] = []
        for s_start, s_end in silence_ranges:
            for f_start, f_end in freeze_ranges:
                overlap_start = max(s_start, f_start)
                overlap_end = min(s_end, f_end)
                if overlap_end > overlap_start:
                    dur = overlap_end - overlap_start
                    if dur >= self.config.dead_air_min_duration:
                        dead_air.append((overlap_start, overlap_end))
        return dead_air

    def analyze_activity(self, video_path: Path) -> list[ActiveSegment]:
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

        return self._analyze_activity_opencv(video_path)

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
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=60,
            )

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
        self, video_path: Path, sample_interval_sec: float = 0.5
    ) -> list[ActiveSegment]:
        """Compute inter-frame motion scores via frame-difference luminance variance."""
        metadata = self.probe_video(video_path)
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
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                break

            # Downsample for ultra-fast motion estimation
            small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

            start_t = frame_idx / metadata.fps
            end_t = min(metadata.duration, (frame_idx + step_frames) / metadata.fps)

            if prev_gray is not None:
                diff = cv2.absdiff(gray, prev_gray)
                mean_diff = float(diff.mean()) / 255.0
                is_active = mean_diff >= self.config.auto_editor_motion_threshold
                segments.append(
                    ActiveSegment(
                        start_time=start_t,
                        end_time=end_t,
                        motion_score=mean_diff,
                        is_active=is_active,
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
            frame_idx += step_frames

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

        filtered: list[SceneCut] = []
        for scene in scenes:
            # Check dead-air overlap (simultaneous audio silence + video freeze)
            if dead_air_intervals and scene.duration > 0:
                dead_overlap = 0.0
                for d_start, d_end in dead_air_intervals:
                    overlap_start = max(scene.start_time, d_start)
                    overlap_end = min(scene.end_time, d_end)
                    if overlap_end > overlap_start:
                        dead_overlap += overlap_end - overlap_start
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
                active_duration = 0.0
                for seg in activity_segments:
                    if not seg.is_active:
                        continue
                    overlap_start = max(scene.start_time, seg.start_time)
                    overlap_end = min(scene.end_time, seg.end_time)
                    if overlap_end > overlap_start:
                        active_duration += overlap_end - overlap_start

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
    ) -> list[SceneCut]:
        """Detect scenes and prune activity for a single video with caching."""
        if cancel_event and cancel_event.is_set():
            return []

        cache_file = self._get_cache_path(video_path)
        cached = self._load_cached_scenes(cache_file, video_path)
        if cached is not None:
            return cached

        # Full pipeline for single video
        raw_scenes = self.detect_scenes(video_path)
        if cancel_event and cancel_event.is_set():
            return []

        activity = self.analyze_activity(video_path)
        if cancel_event and cancel_event.is_set():
            return []

        dead_air: list[tuple[float, float]] = []
        if self.config.enable_dead_air_detection:
            silence_ranges = self.detect_silence_ranges(video_path)
            freeze_ranges = self.detect_freeze_ranges(video_path)
            dead_air = self.find_dead_air_intervals(silence_ranges, freeze_ranges)

        final_scenes = self.filter_scenes_by_activity(raw_scenes, activity, dead_air)

        self._save_cached_scenes(cache_file, final_scenes)
        return final_scenes

    def batch_process_videos(
        self,
        video_paths: list[Path],
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[SceneCut]:
        """Process multiple videos concurrently using thread pool workers."""
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

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_path = {
                executor.submit(self.process_single_video, vp, cancel_event): vp
                for vp in video_paths
            }
            for future in as_completed(future_to_path):
                vp = future_to_path[future]
                completed_count += 1
                try:
                    cuts = future.result()
                    video_results[vp] = cuts
                    logger.info("Completed %s: %d scenes", vp.name, len(cuts))
                except Exception as exc:
                    logger.error("Processing failed for %s: %s", vp.name, exc)
                    video_results[vp] = []

                if progress_callback:
                    pct = (completed_count / total_videos) * 100.0
                    progress_callback(pct, f"Processed {vp.name} ({completed_count}/{total_videos})")

                if cancel_event and cancel_event.is_set():
                    logger.info("Batch ingestion cancelled by user.")
                    break

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

