"""AI Smart 9:16 Re-framing module with YOLO subject tracking and EMA jitter smoothing."""

import dataclasses
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from src.core.analysis_cache import AnalysisCache, scene_key
from src.core.config import PipelineConfig, check_cuda_available
from src.core.types import CropWindow, LayoutMode, SceneCut, ScoredScene
from src.modules.keyframe_cache import KeyframeStore, reframer_timestamps

YOLO_IMGSZ = 640

logger = logging.getLogger(__name__)


def generate_backdrop_filter(
    target_w: int = 1080,
    target_h: int = 1920,
    input_label: str = "in",
    out_label: str = "out",
) -> str:
    """Generate dynamic dual-layer FFmpeg filter graph for blurred vertical backdrop.

    - Background layer: scales to fill canvas, boxblur=20:5, luminance reduced by 15%.
    - Foreground layer: centers original media maintaining native aspect ratio.
    """
    return (
        f"[{input_label}]split=2[bg_{out_label}][fg_{out_label}];"
        f"[bg_{out_label}]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{target_h},boxblur=20:5,eq=brightness=-0.15[bg_blur_{out_label}];"
        f"[fg_{out_label}]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg_sc_{out_label}];"
        f"[bg_blur_{out_label}][fg_sc_{out_label}]overlay=(W-w)/2:(H-h)/2[{out_label}]"
    )


class SmartReframer:
    """Tracks primary human subjects and calculates smooth 9:16 pan-and-scan crop windows."""

    def __init__(self, config: PipelineConfig, model: Any | None = None) -> None:
        self.config = config
        self._model = model
        self._infer_lock = threading.Lock()
        if self._model is None and self.config.enable_smart_reframing:
            self._init_model()

    def _init_model(self) -> None:
        """Initialize YOLO detection model with graceful fallback on offline or load failure."""
        try:
            from ultralytics import YOLO

            logger.info("Initializing YOLO model: %s", self.config.reframer_model)
            self._model = YOLO(self.config.reframer_model)
        except Exception as e:
            logger.warning(
                "Failed to initialize YOLO model '%s' (%s). Fallback to center-crop.",
                self.config.reframer_model,
                e,
            )
            self._model = None

    def unload_model(self) -> None:
        """Unload YOLO model from VRAM and run garbage collection."""
        if self._model is not None:
            del self._model
            self._model = None
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logger.info("Evicted SmartReframer YOLO model from CUDA VRAM.")
        except ImportError:
            pass

    def is_vertical_target(self) -> bool:
        """Check if target canvas preset is vertical (9:16 or height > width)."""
        return self.config.canvas_width < self.config.canvas_height

    def detect_subject_centers(self, frame: np.ndarray) -> list[tuple[float, float, float]]:
        """Detect human subjects in frame and return sorted list of (center_x, area, confidence)."""
        if frame is None or frame.size == 0:
            return []
        out = self.detect_subject_centers_batch([frame])
        return out[0] if out else []

    def detect_subject_centers_batch(
        self, frames: list[np.ndarray]
    ) -> list[list[tuple[float, float, float]]]:
        """Run YOLO once over a list of frames (imgsz=640, fp16 on CUDA).

        Returns one ``[(center_x, area, confidence), ...]`` list per input frame, sorted by
        area x confidence (largest person first).
        """
        if self._model is None or not frames:
            return [[] for _ in frames]

        try:
            kwargs: dict[str, Any] = {
                "classes": [0],  # Person class in COCO dataset
                "conf": self.config.reframer_confidence_threshold,
                "imgsz": YOLO_IMGSZ,
                "verbose": False,
            }
            if check_cuda_available():
                kwargs["half"] = True
            with self._infer_lock:
                results = list(self._model(list(frames), **kwargs))
            per_frame: list[list[tuple[float, float, float]]] = []
            for r in results[: len(frames)]:
                candidates: list[tuple[float, float, float]] = []
                if hasattr(r, "boxes") and r.boxes is not None:
                    for box in r.boxes:
                        xy = box.xyxy[0]
                        coords = xy.tolist() if hasattr(xy, "tolist") else list(xy)
                        x1, y1, x2, y2 = float(coords[0]), float(coords[1]), float(coords[2]), float(coords[3])
                        conf_raw = box.conf[0]
                        conf = float(conf_raw.item()) if hasattr(conf_raw, "item") else float(conf_raw)
                        candidates.append(((x1 + x2) / 2.0, (x2 - x1) * (y2 - y1), conf))
                # Prioritize largest person bounding box area weighted by confidence
                candidates.sort(key=lambda c: c[1] * c[2], reverse=True)
                per_frame.append(candidates)
            while len(per_frame) < len(frames):
                per_frame.append([])
            return per_frame
        except Exception as e:
            logger.debug("YOLO inference failed on frame batch: %s", e)
            return [[] for _ in frames]

    def calculate_crop_window(
        self,
        source_width: int,
        source_height: int,
        target_aspect_ratio: float = 9.0 / 16.0,
        subject_x: float | None = None,
        confidence: float = 1.0,
    ) -> CropWindow:
        """Compute exact pixel coordinates for a 9:16 crop box centered on subject_x.

        Ensures dimensions and coordinates are even integers for video encoder compatibility.
        """
        if source_width <= 0 or source_height <= 0:
            raise ValueError(f"Invalid frame dimensions: {source_width}x{source_height}")

        src_aspect = source_width / source_height

        if src_aspect <= target_aspect_ratio:
            # Source is already narrower or equal to target aspect ratio (vertical)
            crop_w = source_width - (source_width % 2)
            raw_h = int(round(crop_w / target_aspect_ratio))
            crop_h = min(source_height, raw_h)
            crop_h = crop_h - (crop_h % 2)
            crop_x = 0
            crop_y = (source_height - crop_h) // 2
            crop_y = crop_y - (crop_y % 2)
            subject_tag = "passthrough_vertical"
        else:
            # Source is horizontal (e.g. 16:9). Crop height matches source height.
            crop_h = source_height - (source_height % 2)
            raw_w = int(round(crop_h * target_aspect_ratio))
            crop_w = min(source_width, raw_w)
            crop_w = crop_w - (crop_w % 2)

            if subject_x is not None:
                raw_x = int(round(subject_x - crop_w / 2.0))
                subject_tag = "person"
            else:
                raw_x = (source_width - crop_w) // 2
                subject_tag = "center"

            # Clamp crop within horizontal image bounds
            crop_x = max(0, min(source_width - crop_w, raw_x))
            crop_x = crop_x - (crop_x % 2)
            crop_y = 0

        return CropWindow(
            x=int(crop_x),
            y=int(crop_y),
            width=int(crop_w),
            height=int(crop_h),
            confidence=float(confidence),
            tracking_subject=subject_tag,
        )

    def reframe_scene(
        self,
        scene: SceneCut,
        target_aspect_ratio: float = 9.0 / 16.0,
        layout_mode: str | LayoutMode | None = None,
        keyframes: KeyframeStore | None = None,
    ) -> CropWindow:
        """Sample keyframes across scene duration, apply EMA smoothing to subject position, and return CropWindow.

        ``keyframes`` (shared store) supplies pre-decoded, downscaled frames; any missing
        timestamp falls back to a direct read from the clip.
        """
        clip_path = scene.clip_path
        if not clip_path.is_file():
            raise FileNotFoundError(f"Video file not found: {clip_path}")

        mode_val = layout_mode or getattr(self.config, "reframing_mode", "auto_hybrid")
        mode = mode_val.value if isinstance(mode_val, LayoutMode) else str(mode_val)

        cap: cv2.VideoCapture | None = None
        size = keyframes.source_size(clip_path) if keyframes is not None else None

        def _cap() -> cv2.VideoCapture:
            nonlocal cap
            if cap is None:
                cap = cv2.VideoCapture(str(clip_path))
                if not cap.isOpened():
                    raise RuntimeError(f"Could not open video file: {clip_path}")
            return cap

        try:
            if size is not None:
                width, height = size
                fps = 0.0
            else:
                c = _cap()
                width = int(c.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(c.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = float(c.get(cv2.CAP_PROP_FPS)) or 30.0

            if width <= 0 or height <= 0:
                width, height = self.config.canvas_width, self.config.canvas_height

            if mode in (LayoutMode.BLUR_BACKDROP.value, "blur_backdrop"):
                return CropWindow(
                    x=0,
                    y=0,
                    width=width,
                    height=height,
                    confidence=0.0,
                    tracking_subject="blur_backdrop",
                )

            sample_times = reframer_timestamps(scene, self.config.reframer_sample_frames)

            frames: list[np.ndarray] = []
            scales: list[float] = []  # detection coordinates -> source pixels
            for t in sample_times:
                frame = keyframes.frame(clip_path, t) if keyframes is not None else None
                if frame is None:
                    c = _cap()
                    if fps <= 0:
                        fps = float(c.get(cv2.CAP_PROP_FPS)) or 30.0
                    c.set(cv2.CAP_PROP_POS_FRAMES, int(round(t * fps)))
                    ret, frame = c.read()
                    if not ret or frame is None:
                        continue
                frames.append(frame)
                scales.append(width / float(frame.shape[1]) if frame.shape[1] > 0 else 1.0)

            detected_centers: list[float] = []
            confidences: list[float] = []
            for scale, detections in zip(scales, self.detect_subject_centers_batch(frames)):
                if detections:
                    primary_x, _, conf = detections[0]
                    detected_centers.append(primary_x * scale)
                    confidences.append(conf)

            if not detected_centers:
                # If auto_hybrid and no subjects detected -> fall back to blurred backdrop
                if mode in (LayoutMode.AUTO_HYBRID.value, "auto_hybrid"):
                    return CropWindow(
                        x=0,
                        y=0,
                        width=width,
                        height=height,
                        confidence=0.0,
                        tracking_subject="blur_backdrop",
                    )
                # Standard center crop fallback
                return self.calculate_crop_window(
                    source_width=width,
                    source_height=height,
                    target_aspect_ratio=target_aspect_ratio,
                    subject_x=None,
                    confidence=0.0,
                )

            # Apply Exponential Moving Average (EMA) to smooth subject center and eliminate camera jitter
            alpha = self.config.reframer_smoothing_alpha
            smoothed_x = detected_centers[0]
            for curr_x in detected_centers[1:]:
                smoothed_x = alpha * curr_x + (1.0 - alpha) * smoothed_x

            avg_conf = float(np.mean(confidences))
            return self.calculate_crop_window(
                source_width=width,
                source_height=height,
                target_aspect_ratio=target_aspect_ratio,
                subject_x=smoothed_x,
                confidence=avg_conf,
            )
        finally:
            if cap is not None:
                cap.release()

    def reframe_all_scenes(
        self,
        scored_scenes: list[ScoredScene],
        target_aspect_ratio: float = 9.0 / 16.0,
        layout_mode: str | LayoutMode | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        keyframes: KeyframeStore | None = None,
    ) -> list[ScoredScene]:
        """Compute smooth 9:16 crop coordinates for each scene if target preset is vertical.

        Scenes are processed clip-by-clip in a thread pool; crop windows of unchanged clips
        are served from the on-disk analysis cache.
        """
        if not scored_scenes:
            return []

        # Only execute crop calculation if target canvas is vertical or user enabled
        if not self.is_vertical_target() and not self.config.enable_smart_reframing:
            return scored_scenes

        logger.info(
            "Executing AI Smart 9:16 Re-framing across %d scenes (EMA alpha=%.2f)...",
            len(scored_scenes),
            self.config.reframer_smoothing_alpha,
        )

        total_scenes = len(scored_scenes)
        groups: dict[Path, list[ScoredScene]] = defaultdict(list)
        for s in scored_scenes:
            groups[s.scene.clip_path].append(s)

        def _check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                logger.info("Smart reframing cancelled by user.")
                raise InterruptedError("Smart reframing cancelled.")

        mode_val = layout_mode or getattr(self.config, "reframing_mode", "auto_hybrid")
        mode = mode_val.value if isinstance(mode_val, LayoutMode) else str(mode_val)
        cache = AnalysisCache(self.config)

        def _do_clip(path: Path, items: list[ScoredScene]) -> int:
            key = cache.make_key(
                path,
                mode=mode,
                samples=self.config.reframer_sample_frames,
                conf=self.config.reframer_confidence_threshold,
                alpha=self.config.reframer_smoothing_alpha,
                model=self.config.reframer_model if self._model is not None else "none",
                aspect=round(target_aspect_ratio, 5),
                src="kf960" if keyframes is not None else "full",
            )
            entry: dict[str, Any] = cache.load("reframe", path, key) or {}
            dirty = False
            for s in items:
                _check_cancel()
                sk = scene_key(s.scene.start_time, s.scene.end_time)
                hit = entry.get(sk)
                if hit:
                    try:
                        s.crop_window = CropWindow(**hit)
                        continue
                    except TypeError:
                        pass
                try:
                    crop_win = self.reframe_scene(
                        s.scene,
                        target_aspect_ratio=target_aspect_ratio,
                        layout_mode=layout_mode,
                        keyframes=keyframes,
                    )
                    s.crop_window = crop_win
                    entry[sk] = dataclasses.asdict(crop_win)
                    dirty = True
                except Exception as e:
                    logger.warning(
                        "Reframing failed for scene %s (%s). Using center fallback.", s.scene.clip_path.name, e
                    )
                    # Safe fallback (not cached)
                    s.crop_window = self.calculate_crop_window(
                        source_width=self.config.canvas_width,
                        source_height=self.config.canvas_height,
                        target_aspect_ratio=target_aspect_ratio,
                        subject_x=None,
                        confidence=0.0,
                    )
            if dirty:
                cache.save("reframe", path, key, entry)
            return len(items)

        done = 0
        workers = max(1, min(self.config.max_workers, len(groups)))
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = [pool.submit(_do_clip, path, items) for path, items in groups.items()]
            for fut in futures:
                done += fut.result()
                if progress_callback is not None:
                    progress_callback(min(100.0, done / total_scenes * 100.0), f"Reframed scene {done}/{total_scenes}")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        logger.info("Successfully reframed %d scenes for vertical 9:16 playback.", len(scored_scenes))
        return scored_scenes
