"""Long-shot analysis: quality-over-time curves and best sub-window selection.

A single 25 s drone shot used to be placed from its first frame every time. Here a cheap curve
(sharpness, frame-to-frame motion, jerk, exposure flags) is sampled at a few Hz for long scenes
only, and :func:`extract_optimal_subwindow` slides a window over it to find the smoothest, sharpest
part that was not used yet. Curves live in ``AnalysisCache`` namespace ``curves`` (never in the
keyframe store).

Everything degrades gracefully: an unreadable file gives ``None`` and callers fall back to a
plain "next free window" choice.
"""

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from src.core.analysis_cache import AnalysisCache

logger = logging.getLogger(__name__)

CURVE_NAMESPACE = "curves"
CURVE_ALGO_VERSION = 1

# Window score weights (see score_window).
JERK_WEIGHT = 1.0
MOTION_WEIGHT = 0.8
EXPOSURE_PENALTY = 5.0  # "hard": a fully clipped/occluded window scores far below any clean one
_EPS = 1e-6


@dataclass
class QualityCurve:
    """Per-sample quality of a source interval. ``times`` are absolute source seconds."""

    times: np.ndarray
    sharpness: np.ndarray  # 0..1
    motion: np.ndarray  # mean abs adjacent-frame difference, 0..255
    jerk: np.ndarray  # |d motion| between neighbouring samples
    exposure_bad: np.ndarray  # bool: clipped or occluded frame
    hz: float = 4.0

    def __len__(self) -> int:
        return int(self.times.size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hz": self.hz,
            "times": [round(float(x), 4) for x in self.times],
            "sharpness": [round(float(x), 5) for x in self.sharpness],
            "motion": [round(float(x), 4) for x in self.motion],
            "jerk": [round(float(x), 4) for x in self.jerk],
            "exposure_bad": [bool(x) for x in self.exposure_bad],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QualityCurve":
        return cls(
            times=np.asarray(d["times"], dtype=float),
            sharpness=np.asarray(d["sharpness"], dtype=float),
            motion=np.asarray(d["motion"], dtype=float),
            jerk=np.asarray(d["jerk"], dtype=float),
            exposure_bad=np.asarray(d["exposure_bad"], dtype=bool),
            hz=float(d.get("hz", 4.0)),
        )


# --------------------------------------------------------------------------- curve


def _small_gray(frame: np.ndarray, size: int) -> np.ndarray:
    h, w = frame.shape[:2]
    scale = size / float(max(w, 1))
    if scale < 1.0:
        frame = cv2.resize(frame, (size, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame


def compute_quality_curve(
    path: Path | str,
    start: float,
    end: float,
    hz: float = 4.0,
    size: int = 320,
    cache: AnalysisCache | None = None,
    *,
    occlusion_luma: int = 15,
    occlusion_ratio: float = 0.40,
    clip_ratio: float = 0.30,
) -> QualityCurve | None:
    """Sample ``[start, end]`` of ``path`` at ``hz`` and measure quality at each sample.

    Each sample reads two consecutive frames (so shake shows as large adjacent-frame motion).
    Frames between samples are skipped with ``grab()`` when the gap is small, or by seeking when
    it is large. Never raises; returns ``None`` when the file cannot be read.
    """
    p = Path(path)
    hz = max(0.5, float(hz))
    key = None
    if cache is not None:
        key = cache.make_key(
            p,
            start=round(float(start), 3),
            end=round(float(end), 3),
            hz=hz,
            size=int(size),
            occ=(occlusion_luma, occlusion_ratio, clip_ratio),
            algo=CURVE_ALGO_VERSION,
        )
        hit = cache.load(CURVE_NAMESPACE, p, key)
        if hit:
            try:
                return QualityCurve.from_dict(hit)
            except (KeyError, ValueError, TypeError):
                pass

    curve = _compute(p, float(start), float(end), hz, int(size), occlusion_luma, occlusion_ratio, clip_ratio)
    if curve is not None and cache is not None:
        cache.save(CURVE_NAMESPACE, p, key, curve.to_dict())
    return curve


def _compute(
    p: Path,
    start: float,
    end: float,
    hz: float,
    size: int,
    occlusion_luma: int,
    occlusion_ratio: float,
    clip_ratio: float,
) -> QualityCurve | None:
    cap = None
    try:
        cap = cv2.VideoCapture(str(p))
        if not cap.isOpened():
            return None
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not math.isfinite(fps) or fps <= 1.0:
            fps = 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        last_frame = max(total - 2, 0) if total > 0 else 10**9
        step = max(1, int(round(fps / hz)))
        first = max(0, int(round(start * fps)))
        stop = min(int(math.floor(end * fps)), last_frame)
        if stop <= first:
            return None

        times: list[float] = []
        sharp: list[float] = []
        motion: list[float] = []
        bad: list[bool] = []
        pos = -1  # index of the NEXT frame cap will return, -1 = unknown
        for idx in range(first, stop + 1, step):
            if pos < 0 or idx < pos or (idx - pos) > 30:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                pos = idx
            while pos < idx:  # cheap skip over small gaps
                if not cap.grab():
                    break
                pos += 1
            ok, f0 = cap.read()
            pos += 1
            if not ok or f0 is None:
                break
            ok2, f1 = cap.read()
            pos += 1
            g0 = _small_gray(f0, size)
            m = 0.0
            if ok2 and f1 is not None:
                g1 = _small_gray(f1, size)
                m = float(np.mean(cv2.absdiff(g0, g1)))
            lap = float(cv2.Laplacian(g0, cv2.CV_64F).var())
            sharp.append(1.0 - math.exp(-lap / 300.0))
            motion.append(m)
            dark = float(np.mean(g0 < occlusion_luma))
            clipped = float(np.mean((g0 <= 2) | (g0 >= 253)))
            bad.append(dark >= occlusion_ratio or clipped >= clip_ratio)
            times.append(idx / fps)
        if len(times) < 2:
            return None
        mot = np.asarray(motion, dtype=float)
        jerk = np.abs(np.diff(mot, prepend=mot[0]))
        return QualityCurve(
            times=np.asarray(times, dtype=float),
            sharpness=np.asarray(sharp, dtype=float),
            motion=mot,
            jerk=jerk,
            exposure_bad=np.asarray(bad, dtype=bool),
            hz=hz,
        )
    except Exception as exc:  # never break an edit because a curve failed
        logger.debug("Quality curve failed for %s: %s", p, exc)
        return None
    finally:
        if cap is not None:
            cap.release()


# --------------------------------------------------------------------------- scoring


def _per_sample_goodness(curve: QualityCurve) -> np.ndarray:
    """Higher is better, one value per sample (sharpness minus unsteadiness, exposure hard-penalised)."""
    motion_n = 1.0 - np.exp(-curve.motion / 12.0)
    jerk_n = 1.0 - np.exp(-curve.jerk / 6.0)
    good = curve.sharpness - JERK_WEIGHT * jerk_n - MOTION_WEIGHT * motion_n
    return good - EXPOSURE_PENALTY * curve.exposure_bad.astype(float)


def score_window(curve: QualityCurve, t0: float, t1: float) -> float:
    """``mean(quality) - lambda * mean(jerk)`` over ``[t0, t1]`` (source seconds), exposure penalised.

    Returns ``-inf`` when the curve has no sample inside the window.
    """
    mask = (curve.times >= t0 - _EPS) & (curve.times <= t1 + _EPS)
    if not np.any(mask):
        return float("-inf")
    return float(np.mean(_per_sample_goodness(curve)[mask]))


def _scene_of(scene: Any) -> Any:
    return getattr(scene, "scene", scene)


def _overlaps(a0: float, a1: float, ranges: Sequence[tuple[float, float]], gap: float) -> bool:
    return any(a0 < r1 + gap - _EPS and a1 > r0 - gap + _EPS for r0, r1 in ranges)


def extract_optimal_subwindow(
    scene: Any,
    slot_sec: float,
    curve: QualityCurve | None,
    used_ranges: Sequence[tuple[float, float]] = (),
    min_gap: float = 0.5,
    hz: float = 4.0,
    avoid_starts: Sequence[float] = (),
) -> tuple[float, float] | None:
    """Best ``slot_sec`` long window inside ``scene`` that keeps ``min_gap`` from ``used_ranges``.

    ``scene`` is a ScoredScene or SceneCut; the result is ``(start, end)`` in source seconds.
    The window slides in steps of ``1/hz`` over the scene. Without a curve the earliest free
    window is returned. ``None`` means nothing fits (scene too short or fully used).
    """
    sc = _scene_of(scene)
    s0, s1 = float(sc.start_time), float(sc.start_time + sc.duration)
    slot = max(0.04, float(slot_sec))
    if slot > (s1 - s0) + 1e-3:
        return None
    last = max(s0, s1 - slot)
    step = 1.0 / max(hz, 0.5)
    n = int(math.floor((last - s0) / step + 1e-9)) + 1
    starts = [s0 + k * step for k in range(n)]
    if starts[-1] < last - 1e-6:
        starts.append(last)

    free = [s for s in starts if not _overlaps(s, s + slot, used_ranges, min_gap)]
    if not free:
        return None
    fresh = [s for s in free if all(abs(s - a) >= 0.5 for a in avoid_starts)]
    if fresh:
        free = fresh
    if curve is None or len(curve) < 2:
        return (free[0], free[0] + slot)

    good = _per_sample_goodness(curve)
    csum = np.concatenate([[0.0], np.cumsum(good)])
    lo = np.searchsorted(curve.times, np.asarray(free) - _EPS, side="left")
    hi = np.searchsorted(curve.times, np.asarray(free) + slot + _EPS, side="right")
    count = hi - lo
    sums = csum[hi] - csum[lo]
    with np.errstate(divide="ignore", invalid="ignore"):
        scores = np.where(count > 0, sums / np.maximum(count, 1), -np.inf)
    best = int(np.argmax(scores))  # earliest on ties
    return (free[best], free[best] + slot)


def plan_scene_segments(
    scene: Any,
    curve: QualityCurve | None,
    max_segments: int = 1,
    slot_sec: float = 8.0,
    allow_multi_segment_for_long_shots: bool = True,
    long_threshold: float = 30.0,
    hz: float = 4.0,
) -> list[tuple[float, float]]:
    """Up to two disjoint high scoring windows of a long scene, at least 20 % of it apart.

    Scenes of ``long_threshold`` seconds or less (or with multi-segment disabled) give one window.
    """
    sc = _scene_of(scene)
    dur = float(sc.duration)
    first = extract_optimal_subwindow(scene, slot_sec, curve, (), hz=hz)
    if first is None:
        return []
    want = 2 if (allow_multi_segment_for_long_shots and dur > long_threshold) else 1
    want = max(1, min(want, int(max_segments)))
    out = [first]
    if want > 1:
        sep = 0.2 * dur
        blocked = [(first[0] - sep, first[1] + sep)]
        second = extract_optimal_subwindow(scene, slot_sec, curve, blocked, min_gap=0.0, hz=hz)
        if second is not None:
            out.append(second)
    return sorted(out)


# --------------------------------------------------------------------------- planner


class SubWindowPlanner:
    """Remembers which parts of each scene a timeline build already used.

    ``room`` tells the slot allocator how much unused footage a scene still has, and ``choose``
    returns the in-point for a slot (quality curve for scenes >= ``threshold``, earliest free
    window otherwise) so one scene never gives the same in-point twice within an epoch.
    """

    def __init__(
        self,
        threshold: float = 12.0,
        cache: AnalysisCache | None = None,
        hz: float = 4.0,
        min_gap: float = 0.5,
        compute_curves: bool = True,
    ) -> None:
        self.threshold = float(threshold)
        self.cache = cache
        self.hz = hz
        self.min_gap = min_gap
        self.compute_curves = compute_curves
        self.used: dict[tuple[str, float], list[tuple[float, float]]] = {}
        self.history: dict[tuple[str, float], list[float]] = {}
        self._curves: dict[tuple[str, float], QualityCurve | None] = {}
        self.curve_seconds = 0.0

    @staticmethod
    def key(scene: Any) -> tuple[str, float]:
        sc = _scene_of(scene)
        return (str(Path(sc.clip_path).resolve()), round(float(sc.start_time), 3))

    def curve_for(self, scene: Any) -> QualityCurve | None:
        sc = _scene_of(scene)
        if not self.compute_curves or sc.duration < self.threshold:
            return None
        k = self.key(scene)
        if k not in self._curves:
            import time

            t0 = time.perf_counter()
            self._curves[k] = compute_quality_curve(
                sc.clip_path, sc.start_time, sc.start_time + sc.duration, hz=self.hz, cache=self.cache
            )
            self.curve_seconds += time.perf_counter() - t0
        return self._curves[k]

    def room(self, scene: Any) -> float:
        """Longest unused stretch (source seconds) a new window could still take."""
        sc = _scene_of(scene)
        s0, s1 = float(sc.start_time), float(sc.start_time + sc.duration)
        ranges = sorted(self.used.get(self.key(scene), []))
        if not ranges:
            return s1 - s0
        best, cursor = 0.0, s0
        for r0, r1 in ranges:
            best = max(best, (r0 - self.min_gap) - cursor)
            cursor = max(cursor, r1 + self.min_gap)
        return max(best, s1 - cursor, 0.0)

    def choose(self, scene: Any, source_sec: float) -> tuple[float, float] | None:
        """Reserve and return the best free ``source_sec`` window of ``scene`` (or ``None``)."""
        k = self.key(scene)
        sc = _scene_of(scene)
        used = self.used.get(k, [])
        window: tuple[float, float] | None
        if not used and source_sec >= sc.duration - 1e-3:
            window = (float(sc.start_time), float(sc.start_time + sc.duration))
        else:
            window = extract_optimal_subwindow(
                scene,
                source_sec,
                self.curve_for(scene),
                used,
                self.min_gap,
                self.hz,
                avoid_starts=self.history.get(k, ()),
            )
        if window is None:
            return None
        self.used.setdefault(k, []).append(window)
        self.history.setdefault(k, []).append(window[0])
        return window

    def reset_epoch(self) -> None:
        """Forget used ranges (footage was exhausted); in-point history is kept to vary repeats."""
        self.used.clear()


def select_golden_windows(
    curve: QualityCurve | None,
    start: float,
    end: float,
    min_len: float,
    max_len: float,
    max_takes: int = 1,
    tolerance: float = 0.05,
) -> list[tuple[float, float]]:
    """Select up to ``max_takes`` non-overlapping golden windows inside ``[start, end]``.

    Uses ``score_window`` on ``curve``, preferring the longest candidate length
    (from ``{max_len, (min_len + max_len) / 2, min_len}``) within ``tolerance`` of the best score.
    Returns windows sorted by start time.
    """
    if max_takes <= 0 or start >= end:
        return []

    span = end - start
    if span <= max_len:
        return [(start, end)]

    if curve is None or len(curve) == 0:
        takes: list[tuple[float, float]] = []
        cur_t0 = start
        while len(takes) < max_takes:
            cur_t1 = cur_t0 + max_len
            if cur_t1 > end + 1e-9:
                break
            takes.append((cur_t0, cur_t1))
            cur_t0 = cur_t1 + min_len
        return takes

    hz = curve.hz if curve.hz > 0 else 4.0

    # Candidate lengths in descending order: {max_len, (min_len + max_len) / 2, min_len}
    candidate_lengths: list[float] = [max_len]
    mid_len = (min_len + max_len) / 2.0
    if abs(mid_len - max_len) > 1e-6:
        candidate_lengths.append(mid_len)
    if abs(min_len - mid_len) > 1e-6 and abs(min_len - max_len) > 1e-6:
        candidate_lengths.append(min_len)

    def _get_starts(length: float) -> list[float]:
        max_start = end - length
        if max_start < start - 1e-9:
            return []
        starts: list[float] = []
        k = 0
        while True:
            t = start + k / hz
            if t > max_start + 1e-9:
                break
            starts.append(t)
            k += 1
        if not starts:
            starts.append(max_start)
        elif abs(starts[-1] - max_start) > 1e-6 and max_start > starts[-1]:
            starts.append(max_start)
        return starts

    # For each candidate length, find best start position and best score
    results_by_len: dict[float, tuple[float, float, list[float]]] = {}
    for L in candidate_lengths:
        starts = _get_starts(L)
        best_t0: float | None = None
        best_score = float("-inf")
        for t0 in starts:
            s = score_window(curve, t0, t0 + L)
            if best_t0 is None or s > best_score:
                best_score = s
                best_t0 = t0
        if best_t0 is not None:
            results_by_len[L] = (best_score, best_t0, starts)

    if not results_by_len:
        return [(start, end)]

    s_star = max(res[0] for res in results_by_len.values())

    # Choose longest L whose best score >= s* - tolerance
    chosen_L: float = candidate_lengths[0]
    chosen_t0: float = results_by_len[chosen_L][1]
    chosen_starts: list[float] = results_by_len[chosen_L][2]
    for L in candidate_lengths:
        if L in results_by_len:
            score_L, t0_L, starts_L = results_by_len[L]
            if score_L >= s_star - tolerance - 1e-9:
                chosen_L = L
                chosen_t0 = t0_L
                chosen_starts = starts_L
                break

    chosen_takes: list[tuple[float, float]] = [(chosen_t0, chosen_t0 + chosen_L)]

    # Further takes up to max_takes using the same L
    while len(chosen_takes) < max_takes:
        best_t0_next: float | None = None
        best_score_next = float("-inf")
        for t0 in chosen_starts:
            c0 = t0
            c1 = t0 + chosen_L
            # A candidate must not overlap any earlier take widened by min_len on each side.
            # Overlap is strict: a0 < b1 and b0 < a1 (touching is allowed).
            overlaps = False
            for taken_0, taken_1 in chosen_takes:
                w0 = taken_0 - min_len
                w1 = taken_1 + min_len
                if c0 < w1 - 1e-9 and c1 > w0 + 1e-9:
                    overlaps = True
                    break
            if overlaps:
                continue

            s = score_window(curve, c0, c1)
            if best_t0_next is None or s > best_score_next:
                best_score_next = s
                best_t0_next = c0

        if best_t0_next is None:
            break

        chosen_takes.append((best_t0_next, best_t0_next + chosen_L))

    return sorted(chosen_takes, key=lambda w: w[0])
