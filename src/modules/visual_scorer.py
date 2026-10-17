"""Visual quality, sharpness, and aesthetic scoring module using OpenCV and CUDA/PyTorch."""

import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from src.core.analysis_cache import AnalysisCache, scene_key
from src.core.config import PipelineConfig, check_cuda_available
from src.core.types import SceneCut, ScoredScene
from src.modules.keyframe_cache import KeyframeStore, scorer_timestamps

logger = logging.getLogger(__name__)


def compute_dhash(image: np.ndarray, hash_size: int = 8) -> str:
    """Compute perceptual difference hash (dHash) for an image.

    Resizes image to (hash_size + 1, hash_size), computes horizontal gradient bits,
    and returns a hexadecimal string representation.
    """
    if image is None or image.size == 0:
        return "0" * ((hash_size * hash_size) // 4)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    resized = cv2.resize(gray, (hash_size + 1, hash_size), interpolation=cv2.INTER_AREA)

    # Compute horizontal difference: col+1 > col
    diff = resized[:, 1:] > resized[:, :-1]

    # Convert bool array to integer bit sequence
    decimal_val = 0
    for bit in diff.flatten():
        decimal_val = (decimal_val << 1) | int(bit)

    hex_len = (hash_size * hash_size) // 4
    return f"{decimal_val:0{hex_len}x}"


def hamming_distance(hash1: str, hash2: str) -> int:
    """Calculate the Hamming distance between two hexadecimal hash strings."""
    try:
        val1 = int(hash1, 16)
        val2 = int(hash2, 16)
        xor_val = val1 ^ val2
        return bin(xor_val).count("1")
    except ValueError:
        return 64


AESTHETIC_BATCH_SIZE = 32
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _hash_to_int(h: str) -> int | None:
    try:
        return int(h, 16)
    except ValueError:
        return None


class VisualScorer:
    """Evaluates sharpness, composition, and visual appeal across scene candidates."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.cuda_available = check_cuda_available()
        self._torch_model = None
        self._model_lock = threading.Lock()
        self._init_aesthetic_model()

    def _init_aesthetic_model(self) -> None:
        """Initialize lightweight PyTorch model on CUDA if available."""
        if not self.cuda_available:
            logger.info("CUDA not active. Using OpenCV multi-metric aesthetic engine.")
            return

        try:
            import torch
            from torchvision import models

            # Use lightweight MobileNetV3-Small for feature extraction
            weights = models.MobileNet_V3_Small_Weights.DEFAULT
            model = models.mobilenet_v3_small(weights=weights)
            model.eval()
            if torch.cuda.is_available():
                model = model.to("cuda").half()
                logger.info("Loaded MobileNetV3 visual scorer on CUDA device (fp16).")
            self._torch_model = model
            self._torch_transforms = weights.transforms()
        except Exception as exc:
            logger.warning(
                "PyTorch model init failed (%s). Falling back to OpenCV aesthetic engine.",
                exc,
            )
            self._torch_model = None

    def unload_model(self) -> None:
        """Unload PyTorch models from VRAM and run garbage collection."""
        if self._torch_model is not None:
            del self._torch_model
            self._torch_model = None
        if hasattr(self, "_torch_transforms"):
            del self._torch_transforms
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logger.info("Evicted VisualScorer PyTorch model from CUDA VRAM.")
        except ImportError:
            pass

    def score_scene(
        self, scene: SceneCut, keyframes: KeyframeStore | None = None
    ) -> ScoredScene:
        """Sample keyframes and compute sharpness, aesthetic, and composite scores."""
        frames = self._scene_frames(scene, keyframes)
        metrics = self._metrics_from_frames(frames, self._compute_aesthetic_batch(frames)) if frames else None
        return self._scored_from_metrics(scene, metrics)

    def _scene_frames(self, scene: SceneCut, keyframes: KeyframeStore | None) -> list[np.ndarray]:
        """Frames for a scene: from the shared keyframe store when present, else a direct read."""
        if keyframes is not None:
            found = [
                f
                for ts in scorer_timestamps(scene, self.config.keyframes_per_scene)
                if (f := keyframes.frame(scene.clip_path, ts)) is not None
            ]
            if found:
                return found
        return self._extract_keyframes(
            scene.clip_path, scene.start_time, scene.end_time, self.config.keyframes_per_scene
        )

    def _metrics_from_frames(
        self, keyframes: list[np.ndarray], aesthetic_scores: list[float]
    ) -> dict[str, Any]:
        """Raw per-scene metrics (JSON-serialisable, so they can be cached on disk)."""
        sharpness_scores = [self._compute_sharpness(img) for img in keyframes]
        dhash_str = compute_dhash(keyframes[len(keyframes) // 2])

        # Inter-keyframe motion energy estimation
        motion_energy = 0.5
        if len(keyframes) >= 2:
            small = [cv2.cvtColor(cv2.resize(k, (160, 90)), cv2.COLOR_BGR2GRAY) for k in keyframes]
            diffs = [
                float(np.mean(cv2.absdiff(small[i], small[i + 1]))) / 255.0
                for i in range(len(small) - 1)
            ]
            raw_motion = float(np.mean(diffs))
            # Non-linear scaling: 0.05 is calm (~0.35), 0.12+ is high action (0.75-1.0)
            motion_energy = float(min(1.0, max(0.0, raw_motion * 6.5)))

        return {
            "sharpness": float(np.mean(sharpness_scores)),
            "aesthetic": float(np.mean(aesthetic_scores)),
            "motion": motion_energy,
            "dhash": dhash_str,
            "n": len(keyframes),
        }

    @staticmethod
    def _scored_from_metrics(scene: SceneCut, m: dict[str, Any] | None) -> ScoredScene:
        if not m:
            # Degraded fallback if video reading failed
            return ScoredScene(
                scene=scene, sharpness_score=0.0, aesthetic_score=0.0, composite_score=0.0
            )
        return ScoredScene(
            scene=scene,
            sharpness_score=float(m["sharpness"]),
            aesthetic_score=float(m["aesthetic"]),
            composite_score=0.0,  # Computed during batch normalization
            motion_energy=float(m["motion"]),
            metadata={
                "clip_name": scene.clip_path.name,
                "keyframes_sampled": int(m["n"]),
                "dhash": str(m["dhash"]),
                "motion_energy": float(m["motion"]),
            },
        )

    def deduplicate_scenes(
        self, scored_scenes: list[ScoredScene], threshold: int | None = None
    ) -> list[ScoredScene]:
        """Remove redundant/duplicate angle scenes based on perceptual dHash Hamming distance.

        Preserves scenes with higher composite scores. Hashes are converted to ints once and
        compared with XOR + popcount.
        """
        if not scored_scenes:
            return []

        limit = threshold if threshold is not None else self.config.duplicate_hash_threshold

        kept: list[ScoredScene] = []
        kept_hashes: list[tuple[int, ScoredScene]] = []
        for candidate in scored_scenes:
            cand_val = _hash_to_int(str(candidate.metadata.get("dhash", "")))
            if cand_val is None:
                kept.append(candidate)
                continue

            duplicate_of: ScoredScene | None = None
            dist = 64
            for acc_val, accepted in kept_hashes:
                dist = (cand_val ^ acc_val).bit_count()
                if dist <= limit:
                    duplicate_of = accepted
                    break

            if duplicate_of is not None:
                logger.debug(
                    "Deduplicating scene %s [%.2f - %.2f] (matches %s, dist=%d <= %d)",
                    candidate.scene.clip_path.name,
                    candidate.scene.start_time,
                    candidate.scene.end_time,
                    duplicate_of.scene.clip_path.name,
                    dist,
                    limit,
                )
            else:
                kept.append(candidate)
                kept_hashes.append((cand_val, candidate))

        return kept

    def score_and_rank_scenes(
        self,
        scenes: list[SceneCut],
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        keyframes: KeyframeStore | None = None,
    ) -> list[ScoredScene]:
        """Score all scenes and normalize composite scores for sequence assembly.

        Scenes are scored clip-by-clip in a thread pool (one VideoCapture per clip, or none
        when ``keyframes`` already holds the frames); results of unchanged clips come from the
        on-disk analysis cache.
        """
        if not scenes:
            return []

        total = len(scenes)
        groups: dict[Path, list[tuple[int, SceneCut]]] = defaultdict(list)
        for idx, sc in enumerate(scenes):
            groups[sc.clip_path].append((idx, sc))

        results: dict[int, ScoredScene] = {}
        done = 0
        workers = max(1, min(self.config.max_workers, len(groups)))
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                pool.submit(self._score_clip, path, items, keyframes, cancel_event): len(items)
                for path, items in groups.items()
            }
            for fut in as_completed(futures):
                results.update(fut.result())
                done += futures[fut]
                if progress_callback:
                    progress_callback(min(100.0, done / total * 100.0), f"Scored scene {done}/{total}")
                if cancel_event and cancel_event.is_set():
                    logger.info("Visual scoring cancelled by user.")
                    break
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        scored_list: list[ScoredScene] = [results[i] for i in sorted(results)]

        if not scored_list:
            return []

        # Normalization across batch to [0.0, 1.0]
        sharp_vals = [s.sharpness_score for s in scored_list]
        aesth_vals = [s.aesthetic_score for s in scored_list]

        min_s, max_s = min(sharp_vals), max(sharp_vals)
        min_a, max_a = min(aesth_vals), max(aesth_vals)

        range_s = (max_s - min_s) if (max_s - min_s) > 1e-6 else 1.0
        range_a = (max_a - min_a) if (max_a - min_a) > 1e-6 else 1.0

        for s in scored_list:
            norm_s = (s.sharpness_score - min_s) / range_s
            norm_a = (s.aesthetic_score - min_a) / range_a
            s.composite_score = (
                self.config.sharpness_weight * norm_s + self.config.aesthetic_weight * norm_a
            )
            s.speed = self.calculate_speed_for_score(
                s.composite_score,
                max_speed=self.config.max_speed_multiplier,
                enabled=self.config.enable_speed_ramping,
            )

        # Sort descending by composite score
        scored_list.sort(key=lambda x: x.composite_score, reverse=True)

        if self.config.enable_deduplication:
            scored_list = self.deduplicate_scenes(scored_list)

        return scored_list

    def _cache_params(self, from_store: bool) -> dict[str, Any]:
        return {
            "kf": self.config.keyframes_per_scene,
            "backend": "mobilenetv3_fp16" if self._torch_model is not None else "opencv",
            "src": "kf960" if from_store else "full",
        }

    def cached_scene_keys(
        self, scenes: list[SceneCut], from_store: bool = True
    ) -> set[tuple[Path, str]]:
        """Scenes whose scores are already on disk (their frames need not be decoded)."""
        cache = AnalysisCache(self.config)
        if not cache.enabled:
            return set()
        hits: set[tuple[Path, str]] = set()
        by_clip: dict[Path, list[SceneCut]] = defaultdict(list)
        for sc in scenes:
            by_clip[sc.clip_path].append(sc)
        for path, items in by_clip.items():
            entry = cache.load("visual", path, cache.make_key(path, **self._cache_params(from_store)))
            if not entry:
                continue
            for sc in items:
                if scene_key(sc.start_time, sc.end_time) in entry:
                    hits.add((path, scene_key(sc.start_time, sc.end_time)))
        return hits

    def _score_clip(
        self,
        path: Path,
        items: list[tuple[int, SceneCut]],
        keyframes: KeyframeStore | None,
        cancel_event: threading.Event | None,
    ) -> dict[int, ScoredScene]:
        """Score every scene of one clip (cache lookup, one batched aesthetic pass)."""
        cache = AnalysisCache(self.config)
        key = cache.make_key(path, **self._cache_params(keyframes is not None))
        entry: dict[str, Any] = cache.load("visual", path, key) or {}
        dirty = False

        out: dict[int, ScoredScene] = {}
        pending: list[tuple[int, SceneCut, list[np.ndarray]]] = []
        for idx, sc in items:
            if cancel_event and cancel_event.is_set():
                break
            hit = entry.get(scene_key(sc.start_time, sc.end_time))
            if hit:
                out[idx] = self._scored_from_metrics(sc, hit)
                continue
            frames = self._scene_frames(sc, keyframes)
            if not frames:
                out[idx] = self._scored_from_metrics(sc, None)
            else:
                pending.append((idx, sc, frames))

        if pending:
            flat = [f for _i, _s, frames in pending for f in frames]
            aesthetics = self._compute_aesthetic_batch(flat)
            pos = 0
            for idx, sc, frames in pending:
                metrics = self._metrics_from_frames(frames, aesthetics[pos : pos + len(frames)])
                pos += len(frames)
                out[idx] = self._scored_from_metrics(sc, metrics)
                entry[scene_key(sc.start_time, sc.end_time)] = metrics
                dirty = True
        if dirty:
            cache.save("visual", path, key, entry)
        return out

    @staticmethod
    def calculate_speed_for_score(
        composite_score: float, max_speed: float = 8.0, enabled: bool = True
    ) -> float:
        """Calculate dynamic playback speed multiplier based on aesthetic composite score."""
        if not enabled:
            return 1.0
        if composite_score >= 0.75:
            return 1.0
        if composite_score >= 0.45:
            return 2.0
        return min(max_speed, 4.0)

    def _extract_keyframes(
        self, video_path: Path, start_sec: float, end_sec: float, count: int
    ) -> list[np.ndarray]:
        """Extract evenly spaced keyframe images across the scene interval."""
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return []

        fps = cap.get(cv2.CAP_PROP_FPS) or self.config.fps
        duration = max(0.1, end_sec - start_sec)
        timestamps = [start_sec + duration * (i + 1) / (count + 1) for i in range(count)]

        frames: list[np.ndarray] = []
        for ts in timestamps:
            target_frame = int(round(ts * fps))
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            ret, frame = cap.read()
            if ret and frame is not None:
                frames.append(frame)

        cap.release()
        return frames

    def _compute_sharpness(self, bgr_img: np.ndarray) -> float:
        """Compute Tenengrad gradient and Laplacian variance sharpness."""
        # Downsample large 4K image to 1080p for efficient sharpness evaluation
        h, w = bgr_img.shape[:2]
        if w > 1920:
            scale = 1920.0 / w
            bgr_img = cv2.resize(bgr_img, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

        gray = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2GRAY)

        # Laplacian variance (high response to edges, low to blur)
        lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        # Sobel Tenengrad metric
        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        tenengrad = float(np.mean(gx**2 + gy**2))

        return float(0.6 * lap_var + 0.4 * (tenengrad / 100.0))

    def _compute_aesthetic(self, bgr_img: np.ndarray) -> float:
        """Compute composition and visual appeal using PyTorch CUDA or OpenCV heuristics."""
        return self._compute_aesthetic_batch([bgr_img])[0]

    @staticmethod
    def _preprocess_for_mobilenet(bgr_img: np.ndarray) -> np.ndarray:
        """Resize(256) + CenterCrop(224) + ImageNet normalisation -> CHW float32."""
        rgb = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        scale = 256.0 / min(h, w)
        nh, nw = max(224, int(round(h * scale))), max(224, int(round(w * scale)))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        rgb = cv2.resize(rgb, (nw, nh), interpolation=interp)
        top, left = (nh - 224) // 2, (nw - 224) // 2
        crop = rgb[top : top + 224, left : left + 224].astype(np.float32) / 255.0
        return ((crop - _IMAGENET_MEAN) / _IMAGENET_STD).transpose(2, 0, 1)

    def _compute_aesthetic_batch(self, images: list[np.ndarray]) -> list[float]:
        """Aesthetic scores for many frames: batched (32/forward, fp16) on CUDA, else OpenCV."""
        if not images:
            return []
        if self._torch_model is not None:
            try:
                import torch

                scores: list[float] = []
                for i in range(0, len(images), AESTHETIC_BATCH_SIZE):
                    chunk = images[i : i + AESTHETIC_BATCH_SIZE]
                    arr = np.stack([self._preprocess_for_mobilenet(im) for im in chunk])
                    with self._model_lock, torch.no_grad():
                        tensor = torch.from_numpy(arr).to("cuda").half()
                        features = self._torch_model(tensor).float()
                        # L2 energy of the logits as a visual-richness proxy.
                        scores.extend(torch.linalg.vector_norm(features, dim=1).cpu().tolist())
                return [float(x) for x in scores]
            except Exception as exc:
                logger.debug("PyTorch inference error (%s); using OpenCV heuristic.", exc)

        # OpenCV Heuristic: rule of thirds contrast + color saturation + dynamic range
        return [self._compute_aesthetic_opencv(im) for im in images]

    def _compute_aesthetic_opencv(self, bgr_img: np.ndarray) -> float:
        """Evaluate color dynamic range, saturation variance, and rule-of-thirds contrast."""
        h, w = bgr_img.shape[:2]
        small = cv2.resize(bgr_img, (320, 180), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)

        # 1. Color saturation distribution
        sat = hsv[:, :, 1]
        sat_std = float(np.std(sat))

        # 2. Luminance dynamic range (avoid blown out or muddy frames)
        val = hsv[:, :, 2]
        p10, p90 = np.percentile(val, [10, 90])
        dynamic_range = float(p90 - p10)

        # 3. Rule of thirds intersection edge activity
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        # Intersection boxes at 1/3 and 2/3
        h_third, w_third = 180 // 3, 320 // 3
        power_points_mask = np.zeros_like(edges)
        for y_idx in [h_third, 2 * h_third]:
            for x_idx in [w_third, 2 * w_third]:
                power_points_mask[
                    max(0, y_idx - 15) : min(180, y_idx + 15),
                    max(0, x_idx - 15) : min(320, x_idx + 15),
                ] = 1

        power_edge_density = float(np.sum(edges * power_points_mask)) / (
            float(np.sum(edges)) + 1e-5
        )

        aesthetic_score = (
            0.4 * (dynamic_range / 255.0) + 0.3 * (sat_std / 128.0) + 0.3 * power_edge_density
        )
        return float(aesthetic_score)
