"""Visual quality, sharpness, and aesthetic scoring module using OpenCV and CUDA/PyTorch."""

import logging
import threading
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from src.core.config import PipelineConfig, check_cuda_available
from src.core.types import SceneCut, ScoredScene

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


class VisualScorer:
    """Evaluates sharpness, composition, and visual appeal across scene candidates."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.cuda_available = check_cuda_available()
        self._torch_model = None
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
                model = model.to("cuda")
                logger.info("Loaded MobileNetV3 visual scorer on CUDA device.")
            self._torch_model = model
            self._torch_transforms = weights.transforms()
        except Exception as exc:
            logger.warning(
                "PyTorch model init failed (%s). Falling back to OpenCV aesthetic engine.",
                exc,
            )
            self._torch_model = None

    def score_scene(self, scene: SceneCut) -> ScoredScene:
        """Sample keyframes and compute sharpness, aesthetic, and composite scores."""
        keyframes = self._extract_keyframes(
            scene.clip_path, scene.start_time, scene.end_time, self.config.keyframes_per_scene
        )

        if not keyframes:
            # Degraded fallback if video reading failed
            return ScoredScene(
                scene=scene,
                sharpness_score=0.0,
                aesthetic_score=0.0,
                composite_score=0.0,
            )

        sharpness_scores: list[float] = [self._compute_sharpness(img) for img in keyframes]
        aesthetic_scores: list[float] = [self._compute_aesthetic(img) for img in keyframes]

        avg_sharpness = float(np.mean(sharpness_scores))
        avg_aesthetic = float(np.mean(aesthetic_scores))

        dhash_str = compute_dhash(keyframes[len(keyframes) // 2])

        # Inter-keyframe motion energy estimation
        motion_energy = 0.5
        if len(keyframes) >= 2:
            diffs = []
            for i in range(len(keyframes) - 1):
                k1 = cv2.cvtColor(cv2.resize(keyframes[i], (160, 90)), cv2.COLOR_BGR2GRAY)
                k2 = cv2.cvtColor(cv2.resize(keyframes[i + 1], (160, 90)), cv2.COLOR_BGR2GRAY)
                diffs.append(float(np.mean(cv2.absdiff(k1, k2))) / 255.0)
            raw_motion = float(np.mean(diffs))
            # Non-linear scaling: 0.05 is calm (~0.35), 0.12+ is high action (0.75-1.0)
            motion_energy = float(min(1.0, max(0.0, raw_motion * 6.5)))

        return ScoredScene(
            scene=scene,
            sharpness_score=avg_sharpness,
            aesthetic_score=avg_aesthetic,
            composite_score=0.0,  # Computed during batch normalization
            motion_energy=motion_energy,
            metadata={
                "clip_name": scene.clip_path.name,
                "keyframes_sampled": len(keyframes),
                "dhash": dhash_str,
                "motion_energy": motion_energy,
            },
        )

    def deduplicate_scenes(
        self, scored_scenes: list[ScoredScene], threshold: int | None = None
    ) -> list[ScoredScene]:
        """Remove redundant/duplicate angle scenes based on perceptual dHash Hamming distance.

        Preserves scenes with higher composite scores.
        """
        if not scored_scenes:
            return []

        limit = threshold if threshold is not None else self.config.duplicate_hash_threshold

        kept: list[ScoredScene] = []
        for candidate in scored_scenes:
            cand_hash = str(candidate.metadata.get("dhash", ""))
            if not cand_hash:
                kept.append(candidate)
                continue

            is_duplicate = False
            for accepted in kept:
                acc_hash = str(accepted.metadata.get("dhash", ""))
                if not acc_hash:
                    continue
                dist = hamming_distance(cand_hash, acc_hash)
                if dist <= limit:
                    logger.debug(
                        "Deduplicating scene %s [%.2f - %.2f] (matches %s, dist=%d <= %d)",
                        candidate.scene.clip_path.name,
                        candidate.scene.start_time,
                        candidate.scene.end_time,
                        accepted.scene.clip_path.name,
                        dist,
                        limit,
                    )
                    is_duplicate = True
                    break

            if not is_duplicate:
                kept.append(candidate)

        return kept

    def score_and_rank_scenes(
        self,
        scenes: list[SceneCut],
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[ScoredScene]:
        """Score all scenes and normalize composite scores for sequence assembly."""
        if not scenes:
            return []

        scored_list: list[ScoredScene] = []
        total = len(scenes)
        for idx, sc in enumerate(scenes):
            if cancel_event and cancel_event.is_set():
                logger.info("Visual scoring cancelled by user.")
                break
            scored = self.score_scene(sc)
            scored_list.append(scored)
            if progress_callback:
                pct = ((idx + 1) / total) * 100.0
                progress_callback(pct, f"Scored scene {idx+1}/{total}")

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
        if self._torch_model is not None:
            try:
                import torch
                from PIL import Image

                rgb = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)
                pil_img = Image.fromarray(rgb)
                tensor = self._torch_transforms(pil_img).unsqueeze(0).to("cuda")

                with torch.no_grad():
                    features = self._torch_model(tensor)
                    # Use activation energy / entropy across top features as visual richness proxy
                    score = float(torch.norm(features, p=2).item())
                    return score
            except Exception as exc:
                logger.debug("PyTorch inference error (%s); using OpenCV heuristic.", exc)

        # OpenCV Heuristic: rule of thirds contrast + color saturation + dynamic range
        return self._compute_aesthetic_opencv(bgr_img)

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
