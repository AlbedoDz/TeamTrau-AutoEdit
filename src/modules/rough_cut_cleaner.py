"""Universal Defect-Pruning & Assembly Rough-Cut Engine.

Non-destructively inspects raw source footage for operator & environmental defects:
- Head/Tail start-stop wobbles (DIS Optical Flow variance)
- Mid-clip wild camera shake and whip-pans (>80 px/frame)
- Ground/idle carrying takes (downward optical flow + low texture)
- Out-of-focus hunting intervals (Tenengrad sharpness drop > 65%)
- Lens occlusions and caps (uniform darkness / blur)
- Extreme highlight & shadow clipping (>30% clamped pixels)
- LED / AC periodic flicker detection
- 80Hz microphone rumble / wind thump conditioning

Assembles surviving golden segments in strict chronological order.
"""

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from src.core.analysis_cache import AnalysisCache, scene_key
from src.core.config import PipelineConfig
from src.core.ontology import natural_sort_key
from src.core.types import CleanedSegment, DefectSpan, DefectType, SceneCut
from src.modules.audio_normalizer import AudioNormalizer

logger = logging.getLogger(__name__)


# Bump when the detection algorithm changes so stale cache entries are ignored.
_ALGO_VERSION = 1
# Frame series are stored downsampled so the cache stays small; the long-shot window planner
# (``shot_windows``) can reuse flow/sharpness without decoding the clip again.
SERIES_HZ = 4.0

_CACHE_PARAM_NAMES = (
    "enable_head_tail_trim", "head_prune_sec", "tail_prune_sec", "head_tail_motion_ratio",
    "enable_shake_pruning", "max_shake_threshold", "max_shake_min_duration",
    "enable_ground_carrying_pruning", "enable_focus_guard", "focus_drop_ratio",
    "focus_drop_min_duration", "enable_occlusion_guard", "occlusion_area_ratio",
    "occlusion_luma_threshold", "occlusion_min_duration", "enable_exposure_clipping_guard",
    "exposure_clip_ratio", "enable_flicker_repair", "min_take_duration_sec",
)


class RoughCutCleaner:
    """Non-destructive defect pruning engine for chronological assembly rough-cuts."""

    def __init__(self, config: PipelineConfig | None = None) -> None:
        """Initialize RoughCutCleaner with pipeline configuration."""
        self.config = config or PipelineConfig()
        self.audio_normalizer = AudioNormalizer(cache_dir=self.config.cache_dir / "audio_clean")
        self._cache = AnalysisCache(self.config)

    def _cache_params(self) -> dict[str, Any]:
        params = {n: getattr(self.config, n, None) for n in _CACHE_PARAM_NAMES}
        params["algo"] = _ALGO_VERSION
        return params

    def inspect_scene(
        self, scene: SceneCut, cancel_event: threading.Event | None = None
    ) -> list[CleanedSegment]:
        """Inspect a single SceneCut for all defects and return pruned golden segments.

        Non-destructively bisects and trims defective portions while preserving
        chronological coordinates. Surviving segments shorter than min_take_duration_sec
        are discarded. The per-frame analysis (the expensive part) is cached per file + scene
        range + detection parameters under the ``rough_cut`` namespace; ``cancel_event`` is
        checked every few frames and raises ``InterruptedError``.
        """
        clip_path = Path(scene.clip_path).resolve()
        if not clip_path.exists():
            logger.warning("Clip path does not exist: %s", clip_path)
            return []
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Rough cut cleaning cancelled by user.")

        ckey = self._cache.make_key(clip_path, **self._cache_params())
        skey = scene_key(scene.start_time, scene.end_time)
        entry: dict[str, Any] = self._cache.load("rough_cut", clip_path, ckey) or {}
        analysis = entry.get(skey)
        if not isinstance(analysis, dict):
            analysis = self._scan_scene(scene, clip_path, cancel_event)
            if analysis is None:  # unreadable: not cached
                return []
            entry[skey] = analysis
            self._cache.save("rough_cut", clip_path, ckey, entry)
        return self._segments_from_analysis(scene, clip_path, analysis)

    def _scan_scene(
        self, scene: SceneCut, clip_path: Path, cancel_event: threading.Event | None
    ) -> dict[str, Any] | None:
        """Decode the scene frame by frame and detect defects (the heavy, cacheable pass)."""
        # Open video capture
        cap = cv2.VideoCapture(str(clip_path))
        if not cap.isOpened():
            logger.warning("Failed to open video capture for: %s", clip_path)
            return None

        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0 or np.isnan(fps):
            fps = self.config.fps or 30.0

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        scene_start_frame = int(round(scene.start_time * fps))
        scene_end_frame = min(total_frames, int(round(scene.end_time * fps)))
        if scene_end_frame <= scene_start_frame:
            cap.release()
            return None

        # Frame-accurate sampling (step=1 for continuous sequential optical flow)
        step = 1
        cap.set(cv2.CAP_PROP_POS_FRAMES, scene_start_frame)

        timestamps: list[float] = []
        flow_mags: list[float] = []
        flow_dy: list[float] = []
        sharpnesses: list[float] = []
        occlusions: list[float] = []
        clippings: list[float] = []
        luma_means: list[float] = []

        dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
        prev_gray: np.ndarray | None = None

        frame_idx = scene_start_frame
        while frame_idx < scene_end_frame:
            if cancel_event is not None and (frame_idx & 7) == 0 and cancel_event.is_set():
                cap.release()
                raise InterruptedError("Rough cut cleaning cancelled by user.")
            ret, frame = cap.read()
            if not ret or frame is None:
                break

            # Current relative timestamp in scene (seconds)
            rel_time = (frame_idx - scene_start_frame) / fps

            # Downsample frame for fast analysis (width ~320px)
            h, w = frame.shape[:2]
            scale = 320.0 / max(1, w)
            target_w = 320
            target_h = max(1, int(round(h * scale)))
            small_frame = cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small_frame, cv2.COLOR_BGR2GRAY)

            # 1. Optical Flow & Global Motion Tracking (DIS + 2D Phase Correlation)
            if prev_gray is not None:
                flow = dis.calc(prev_gray, gray, None)
                mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
                scale_factor = max(0.0001, scale)
                norm_factor = scale_factor * max(1, step)
                orig_mag = mag / norm_factor
                mean_mag = float(np.mean(orig_mag))
                p90_mag = float(np.percentile(orig_mag, 90))
                mean_dy = float(np.mean(flow[..., 1])) / norm_factor

                # 2D Phase Correlation for macro camera translations (whip-pans & violent shakes)
                try:
                    shift, resp = cv2.phaseCorrelate(
                        prev_gray.astype(np.float32), gray.astype(np.float32)
                    )
                    # Only accept high-confidence correlation peaks
                    if resp > 0.3:
                        global_mag = float(np.hypot(shift[0], shift[1])) / norm_factor
                        global_dy = float(shift[1]) / norm_factor
                    else:
                        global_mag = 0.0
                        global_dy = mean_dy
                except Exception:
                    global_mag = 0.0
                    global_dy = mean_dy

                effective_mag = max(mean_mag, p90_mag * 0.8, global_mag)
                if abs(global_dy) > abs(mean_dy):
                    mean_dy = global_dy
            else:
                mean_mag = 0.0
                effective_mag = 0.0
                mean_dy = 0.0

            # 2. Tenengrad Sharpness
            sobelx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
            sobely = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
            tenengrad = float(np.mean(sobelx**2 + sobely**2))

            # 3. Occlusion & Cap Check (Y < occlusion_luma_threshold)
            dark_ratio = float(np.mean(gray < self.config.occlusion_luma_threshold))

            # 4. Exposure Clipping Check
            clip_ratio = float(np.mean((gray <= 2) | (gray >= 253)))

            # 5. Luminance mean for flicker
            luma_mean = float(np.mean(gray))

            timestamps.append(rel_time)
            flow_mags.append(effective_mag)
            flow_dy.append(mean_dy)
            sharpnesses.append(tenengrad)
            occlusions.append(dark_ratio)
            clippings.append(clip_ratio)
            luma_means.append(luma_mean)

            prev_gray = gray
            frame_idx += step

            if step > 1:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)

        cap.release()

        scene_dur = scene.end_time - scene.start_time
        if len(timestamps) < 2 or scene_dur < 0.2:
            return {"valid": False}

        defect_spans, requires_deflicker = self._detect_defects(
            scene_dur, timestamps, flow_mags, flow_dy, sharpnesses, occlusions, clippings, luma_means
        )
        return {
            "valid": True,
            "scene_dur": scene_dur,
            "spans": [
                {
                    "start": d.start_time,
                    "end": d.end_time,
                    "type": d.defect_type.value,
                    "severity": d.severity,
                    "description": d.description,
                }
                for d in defect_spans
            ],
            "requires_deflicker": bool(requires_deflicker),
            "mean_sharpness": float(np.mean(sharpnesses)) if sharpnesses else 1.0,
            "series": self._downsample_series(timestamps, flow_mags, sharpnesses, occlusions, clippings),
        }

    @staticmethod
    def _downsample_series(
        timestamps: list[float],
        flow_mags: list[float],
        sharpnesses: list[float],
        occlusions: list[float],
        clippings: list[float],
    ) -> dict[str, Any]:
        """Per-frame series reduced to ``SERIES_HZ``: mean flow / sharpness, worst exposure flags."""
        n_bins = int(timestamps[-1] * SERIES_HZ) + 1
        flow = [0.0] * n_bins
        sharp = [0.0] * n_bins
        dark = [0.0] * n_bins
        clip = [0.0] * n_bins
        count = [0] * n_bins
        for t, f, s, o, c in zip(timestamps, flow_mags, sharpnesses, occlusions, clippings, strict=False):
            b = min(n_bins - 1, int(t * SERIES_HZ))
            flow[b] += f
            sharp[b] += s
            dark[b] = max(dark[b], o)
            clip[b] = max(clip[b], c)
            count[b] += 1
        for b in range(n_bins):
            if count[b]:
                flow[b] /= count[b]
                sharp[b] /= count[b]
        return {
            "hz": SERIES_HZ,
            "flow": [round(v, 3) for v in flow],
            "sharpness": [round(v, 3) for v in sharp],
            "dark_ratio": [round(v, 3) for v in dark],
            "clip_ratio": [round(v, 3) for v in clip],
        }

    def _detect_defects(
        self,
        scene_dur: float,
        timestamps: list[float],
        flow_mags: list[float],
        flow_dy: list[float],
        sharpnesses: list[float],
        occlusions: list[float],
        clippings: list[float],
        luma_means: list[float],
    ) -> tuple[list[DefectSpan], bool]:
        """Defect spans (scene-relative seconds) and the flicker flag from the per-frame series."""
        defect_spans: list[DefectSpan] = []

        # -----------------------------------------------------------------
        # A. Motion & Operator Defect Detection
        # -----------------------------------------------------------------
        # 1. Head & Tail Wobble (Start/Stop Jerk)
        if self.config.enable_head_tail_trim and scene_dur >= 3.0:
            head_mask = [t <= 1.5 for t in timestamps]
            tail_mask = [t >= (scene_dur - 1.5) for t in timestamps]
            mid_mask = [1.5 < t < (scene_dur - 1.5) for t in timestamps]

            mid_mags = [flow_mags[i] for i, m in enumerate(mid_mask) if m]
            baseline_mag = float(np.mean(mid_mags)) if mid_mags else 1.0

            head_mags = [flow_mags[i] for i, m in enumerate(head_mask) if m]
            if head_mags and baseline_mag > 0.01:
                head_var = float(np.var(head_mags))
                if head_var > self.config.head_tail_motion_ratio * (baseline_mag**2):
                    prune_dur = min(self.config.head_prune_sec, scene_dur * 0.4)
                    defect_spans.append(
                        DefectSpan(
                            start_time=0.0,
                            end_time=prune_dur,
                            defect_type=DefectType.HEAD_TAIL_WOBBLE,
                            severity=float(head_var / max(0.01, baseline_mag)),
                            description="Unstable start jerk / head wobble",
                        )
                    )

            tail_mags = [flow_mags[i] for i, m in enumerate(tail_mask) if m]
            if tail_mags and baseline_mag > 0.01:
                tail_var = float(np.var(tail_mags))
                if tail_var > self.config.head_tail_motion_ratio * (baseline_mag**2):
                    prune_dur = min(self.config.tail_prune_sec, scene_dur * 0.4)
                    defect_spans.append(
                        DefectSpan(
                            start_time=max(0.0, scene_dur - prune_dur),
                            end_time=scene_dur,
                            defect_type=DefectType.HEAD_TAIL_WOBBLE,
                            severity=float(tail_var / max(0.01, baseline_mag)),
                            description="Unstable stop jerk / tail wobble",
                        )
                    )

        # 2. Mid-Clip Wild Shake & Whip-Pan (>80 px/frame)
        if self.config.enable_shake_pruning:
            shake_start: float | None = None
            for t, mag in zip(timestamps, flow_mags, strict=False):
                if mag >= self.config.max_shake_threshold:
                    if shake_start is None:
                        shake_start = t
                else:
                    if shake_start is not None:
                        dur = t - shake_start
                        if dur >= self.config.max_shake_min_duration:
                            defect_spans.append(
                                DefectSpan(
                                    start_time=shake_start,
                                    end_time=t,
                                    defect_type=DefectType.CAMERA_SHAKE,
                                    severity=dur,
                                    description=f"Severe shake / whip-pan ({dur:.2f}s)",
                                )
                            )
                        shake_start = None
            if shake_start is not None and (scene_dur - shake_start) >= self.config.max_shake_min_duration:
                defect_spans.append(
                    DefectSpan(
                        start_time=shake_start,
                        end_time=scene_dur,
                        defect_type=DefectType.CAMERA_SHAKE,
                        severity=scene_dur - shake_start,
                        description=f"Severe shake / whip-pan ({scene_dur - shake_start:.2f}s)",
                    )
                )

        # 3. Ground / Idle Carrying Detection (Downward optical flow + low texture)
        if self.config.enable_ground_carrying_pruning:
            ground_start: float | None = None
            for t, dy, sharp in zip(timestamps, flow_dy, sharpnesses, strict=False):
                # Strong downward velocity + homogeneous pavement/ground texture (low sharpness)
                is_ground = dy > 15.0 and sharp < 20.0
                if is_ground:
                    if ground_start is None:
                        ground_start = t
                else:
                    if ground_start is not None:
                        dur = t - ground_start
                        if dur >= 0.8:
                            defect_spans.append(
                                DefectSpan(
                                    start_time=ground_start,
                                    end_time=t,
                                    defect_type=DefectType.GROUND_CARRYING,
                                    severity=dur,
                                    description="Downward ground/idle carrying take",
                                )
                            )
                        ground_start = None
            if ground_start is not None and (scene_dur - ground_start) >= 0.8:
                defect_spans.append(
                    DefectSpan(
                        start_time=ground_start,
                        end_time=scene_dur,
                        defect_type=DefectType.GROUND_CARRYING,
                        severity=scene_dur - ground_start,
                        description="Downward ground/idle carrying take",
                    )
                )

        # -----------------------------------------------------------------
        # B. Optical & Sensor Defect Detection
        # -----------------------------------------------------------------
        # 4. Focus Hunting & Blurring (Tenengrad drop > 65% of rolling median)
        if self.config.enable_focus_guard and len(sharpnesses) >= 5:
            median_sharp = float(np.median(sharpnesses))
            if median_sharp > 5.0:
                focus_start: float | None = None
                for t, sharp in zip(timestamps, sharpnesses, strict=False):
                    drop_ratio = (median_sharp - sharp) / median_sharp
                    if drop_ratio >= self.config.focus_drop_ratio:
                        if focus_start is None:
                            focus_start = t
                    else:
                        if focus_start is not None:
                            dur = t - focus_start
                            if dur >= self.config.focus_drop_min_duration:
                                defect_spans.append(
                                    DefectSpan(
                                        start_time=focus_start,
                                        end_time=t,
                                        defect_type=DefectType.DEFOCUS_BLUR,
                                        severity=drop_ratio,
                                        description=f"Focus hunting / severe blur ({dur:.2f}s)",
                                    )
                                )
                            focus_start = None
                if focus_start is not None and (scene_dur - focus_start) >= self.config.focus_drop_min_duration:
                    defect_spans.append(
                        DefectSpan(
                            start_time=focus_start,
                            end_time=scene_dur,
                            defect_type=DefectType.DEFOCUS_BLUR,
                            severity=1.0,
                            description=f"Focus hunting / severe blur ({scene_dur - focus_start:.2f}s)",
                        )
                    )

        # 5. Lens Occlusion / Cap Check
        if self.config.enable_occlusion_guard:
            occ_start: float | None = None
            for t, occ in zip(timestamps, occlusions, strict=False):
                if occ >= self.config.occlusion_area_ratio:
                    if occ_start is None:
                        occ_start = t
                else:
                    if occ_start is not None:
                        dur = t - occ_start
                        if dur >= self.config.occlusion_min_duration:
                            defect_spans.append(
                                DefectSpan(
                                    start_time=occ_start,
                                    end_time=t,
                                    defect_type=DefectType.LENS_OCCLUSION,
                                    severity=dur,
                                    description="Lens occlusion / cap blockage",
                                )
                            )
                        occ_start = None
            if occ_start is not None and (scene_dur - occ_start) >= self.config.occlusion_min_duration:
                defect_spans.append(
                    DefectSpan(
                        start_time=occ_start,
                        end_time=scene_dur,
                        defect_type=DefectType.LENS_OCCLUSION,
                        severity=scene_dur - occ_start,
                        description="Lens occlusion / cap blockage",
                    )
                )

        # 6. Extreme Exposure Clipping (>30% clamped)
        if self.config.enable_exposure_clipping_guard:
            clip_start: float | None = None
            for t, cr in zip(timestamps, clippings, strict=False):
                if cr >= self.config.exposure_clip_ratio:
                    if clip_start is None:
                        clip_start = t
                else:
                    if clip_start is not None:
                        dur = t - clip_start
                        if dur >= 0.5:
                            defect_spans.append(
                                DefectSpan(
                                    start_time=clip_start,
                                    end_time=t,
                                    defect_type=DefectType.EXPOSURE_CLIPPING,
                                    severity=dur,
                                    description="Severe exposure clipping (highlights/shadows)",
                                )
                            )
                        clip_start = None
            if clip_start is not None and (scene_dur - clip_start) >= 0.5:
                defect_spans.append(
                    DefectSpan(
                        start_time=clip_start,
                        end_time=scene_dur,
                        defect_type=DefectType.EXPOSURE_CLIPPING,
                        severity=scene_dur - clip_start,
                        description="Severe exposure clipping (highlights/shadows)",
                    )
                )

        # 7. LED / AC Flicker Detection
        requires_deflicker = False
        if self.config.enable_flicker_repair and len(luma_means) >= 16:
            luma_arr = np.array(luma_means)
            luma_diff = np.diff(luma_arr)
            # Periodic oscillation check via zero-crossings and variance
            zero_crossings = np.sum(np.diff(np.signbit(luma_diff - np.mean(luma_diff))))
            if zero_crossings > len(luma_diff) * 0.4 and np.std(luma_arr) > 4.0:
                requires_deflicker = True

        return defect_spans, requires_deflicker

    def _segments_from_analysis(
        self, scene: SceneCut, clip_path: Path, analysis: dict[str, Any]
    ) -> list[CleanedSegment]:
        """Golden takes of a scene from its (possibly cached) analysis."""
        if not analysis.get("valid"):
            return []
        scene_dur = float(analysis["scene_dur"])
        requires_deflicker = bool(analysis.get("requires_deflicker", False))
        mean_sharpness = float(analysis.get("mean_sharpness", 1.0))
        defect_spans = [
            DefectSpan(
                start_time=float(s["start"]),
                end_time=float(s["end"]),
                defect_type=DefectType(s["type"]),
                severity=float(s.get("severity", 1.0)),
                description=str(s.get("description", "")),
            )
            for s in analysis.get("spans", [])
        ]
        # -----------------------------------------------------------------
        # C. Non-Destructive Interval Bisecting & Micro-Take Clamping
        # -----------------------------------------------------------------
        surviving_intervals = self._bisect_intervals(scene_dur, defect_spans)

        cleaned_segments: list[CleanedSegment] = []
        min_take = self.config.min_take_duration_sec

        for g_start, g_end in surviving_intervals:
            take_dur = g_end - g_start
            if take_dur < min_take:
                continue

            abs_start = scene.start_time + g_start
            abs_end = scene.start_time + g_end

            # Associated defects inside or adjacent to this take
            take_defects = [
                d
                for d in defect_spans
                if (d.start_time <= g_end and d.end_time >= g_start)
                or abs(d.end_time - g_start) < 0.1
                or abs(d.start_time - g_end) < 0.1
            ]

            seg = CleanedSegment(
                scene=scene,
                clip_path=clip_path,
                start_time=abs_start,
                end_time=abs_end,
                duration=take_dur,
                source_start_time=abs_start,
                source_end_time=abs_end,
                defects_removed=take_defects,
                requires_deflicker=requires_deflicker,
                sharpness_score=mean_sharpness,
                metadata={
                    "defects_count": len(take_defects),
                    "original_scene_duration": scene_dur,
                },
            )
            cleaned_segments.append(seg)

        return cleaned_segments

    def _bisect_intervals(
        self,
        scene_dur: float,
        defect_spans: list[DefectSpan],
    ) -> list[tuple[float, float]]:
        """Subtract defect spans from [0, scene_dur] and merge surviving segments."""
        if not defect_spans:
            return [(0.0, scene_dur)]

        # Merge overlapping defect spans
        raw_intervals = sorted([(max(0.0, d.start_time), min(scene_dur, d.end_time)) for d in defect_spans])
        merged_defects: list[tuple[float, float]] = []
        for start, end in raw_intervals:
            if not merged_defects:
                merged_defects.append((start, end))
            else:
                prev_start, prev_end = merged_defects[-1]
                if start <= prev_end + 0.05:
                    merged_defects[-1] = (prev_start, max(prev_end, end))
                else:
                    merged_defects.append((start, end))

        # Invert defect spans to obtain surviving golden takes
        surviving: list[tuple[float, float]] = []
        cursor = 0.0
        for d_start, d_end in merged_defects:
            if d_start > cursor + 0.05:
                surviving.append((cursor, d_start))
            cursor = max(cursor, d_end)

        if cursor < scene_dur - 0.05:
            surviving.append((cursor, scene_dur))

        return surviving

    def clean_batch(
        self,
        scenes: list[SceneCut],
        progress_callback: Callable[[float, float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[CleanedSegment]:
        """Process batch of SceneCuts across all footage, strictly ordered chronologically."""
        if not scenes:
            return []

        all_cleaned: list[CleanedSegment] = []
        total = len(scenes)

        for i, sc in enumerate(scenes):
            if progress_callback:
                progress_callback(i / total, 1.0, f"Defect pruning {i+1}/{total}: {sc.clip_path.name}")

            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Rough cut cleaning cancelled by user.")
            cleaned = self.inspect_scene(sc, cancel_event=cancel_event)
            all_cleaned.extend(cleaned)

        # Enforce strict chronological ordering based on clip_path and start_time
        all_cleaned.sort(key=lambda s: (natural_sort_key(s.clip_path), s.start_time))

        logger.info(
            "Rough cut defect pruning complete: %d raw scenes -> %d chronological golden takes.",
            len(scenes),
            len(all_cleaned),
        )
        return all_cleaned
