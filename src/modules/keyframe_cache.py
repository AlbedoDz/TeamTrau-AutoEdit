"""Shared keyframe extraction: open every clip ONCE and read all needed frames in one sweep.

The visual scorer, smart reframer, VLM tagger and the cut-continuity (exposure guard) check
all need frames from the same clips. Seeking separately for each consumer re-opens and
re-decodes the same footage many times; this module gathers every requested timestamp per
clip, reads them in one sorted sequential pass (``grab()`` across short gaps, a seek only
across long ones), downsizes them to at most ``max_width`` pixels and hands back one
``{(path, timestamp): frame}`` mapping.
"""

import logging
import threading
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from src.core.config import PipelineConfig
from src.core.types import SceneCut, ScoredScene

logger = logging.getLogger(__name__)

FrameKey = tuple[Path, float]

MAX_FRAME_WIDTH = 960
# Across gaps larger than this many frames, seeking beats decoding through.
SEEK_GAP_FRAMES = 150
# Soft cap for frames held in RAM; lowest-priority timestamps are dropped beyond it.
DEFAULT_BUDGET_BYTES = 1_500_000_000

# Request priorities (lower value = kept first when the memory budget is tight).
PRIO_SCORER, PRIO_BOUNDARY, PRIO_VLM, PRIO_REFRAME = 0, 1, 2, 3


def frame_key(path: Path | str, ts: float) -> FrameKey:
    """Canonical dictionary key for a frame: resolved-free path + timestamp in ms."""
    return Path(path), round(float(ts), 3)


class KeyframeStore(dict[FrameKey, np.ndarray]):
    """Mapping of ``(clip_path, timestamp) -> BGR frame`` plus per-clip source geometry."""

    def __init__(self) -> None:
        super().__init__()
        # Full-resolution source size per clip: frames are downscaled, crop maths is not.
        self.sizes: dict[Path, tuple[int, int]] = {}

    def frame(self, path: Path | str, ts: float) -> np.ndarray | None:
        return self.get(frame_key(path, ts))

    def source_size(self, path: Path | str) -> tuple[int, int] | None:
        return self.sizes.get(Path(path))


class FrameRequests:
    """Accumulates the timestamps every consumer needs, per clip."""

    def __init__(self) -> None:
        self._by_clip: dict[Path, dict[float, int]] = {}

    def add(self, path: Path | str, ts: float, priority: int = PRIO_SCORER) -> None:
        clip = self._by_clip.setdefault(Path(path), {})
        t = round(max(0.0, float(ts)), 3)
        clip[t] = min(priority, clip.get(t, priority))

    def add_many(self, path: Path | str, stamps: Iterable[float], priority: int = PRIO_SCORER) -> None:
        for t in stamps:
            self.add(path, t, priority)

    def as_mapping(self) -> Mapping[Path, Mapping[float, int]]:
        return self._by_clip

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_clip.values())


def scorer_timestamps(scene: SceneCut, count: int) -> list[float]:
    """Timestamps ``VisualScorer._extract_keyframes`` samples (kept in sync with it)."""
    duration = max(0.1, scene.end_time - scene.start_time)
    return [scene.start_time + duration * (i + 1) / (count + 1) for i in range(count)]


def reframer_timestamps(scene: SceneCut, num_samples: int) -> list[float]:
    """Timestamps ``SmartReframer.reframe_scene`` samples."""
    n = max(2, num_samples)
    return [float(t) for t in np.linspace(scene.start_time, scene.end_time, n)]


def vlm_timestamp(scene: SceneCut) -> float:
    return (scene.start_time + scene.end_time) / 2.0


def boundary_timestamps(scene: SceneCut) -> tuple[float, float]:
    """(first, last) boundary frame timestamps as used by ``evaluate_cut_continuity``."""
    return scene.start_time, max(scene.start_time, scene.end_time - 0.04)


def build_requests(
    scenes: Iterable[SceneCut],
    config: PipelineConfig,
    *,
    scorer: bool = True,
    reframer: bool = False,
    vlm: bool = False,
    boundaries: bool = True,
    skip_scorer_for: set[tuple[Path, str]] | None = None,
) -> FrameRequests:
    """Collect every timestamp the enabled consumers will ask for.

    ``skip_scorer_for`` holds ``(clip_path, "start-end")`` scenes whose scores are already
    cached on disk, so their scorer frames need not be decoded at all.
    """
    from src.core.analysis_cache import scene_key

    req = FrameRequests()
    for sc in scenes:
        path = sc.clip_path
        if scorer and not (skip_scorer_for and (path, scene_key(sc.start_time, sc.end_time)) in skip_scorer_for):
            req.add_many(path, scorer_timestamps(sc, config.keyframes_per_scene), PRIO_SCORER)
        if boundaries:
            first, last = boundary_timestamps(sc)
            req.add(path, first, PRIO_BOUNDARY)
            req.add(path, last, PRIO_BOUNDARY)
        if vlm:
            req.add(path, vlm_timestamp(sc), PRIO_VLM)
        if reframer:
            req.add_many(path, reframer_timestamps(sc, config.reframer_sample_frames), PRIO_REFRAME)
    return req


def _downscale(frame: np.ndarray, max_width: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w <= max_width:
        return frame
    scale = max_width / float(w)
    return cv2.resize(frame, (max_width, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)


def _read_clip(
    path: Path,
    stamps: Mapping[float, int],
    default_fps: float,
    max_width: int,
    cancel_event: threading.Event | None,
    budget_frames_left: list[int],
    lock: threading.Lock,
) -> tuple[dict[float, np.ndarray], tuple[int, int] | None]:
    """Sweep one clip once: sorted frame indices, sequential grab() over short gaps."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return {}, None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or default_fps
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        # Apply the memory budget: keep the highest-priority (then earliest) timestamps.
        items = sorted(stamps.items(), key=lambda kv: (kv[1], kv[0]))
        scale_w = min(1.0, max_width / src_w) if src_w > 0 else 1.0
        frame_bytes = int(src_w * scale_w) * int(src_h * scale_w) * 3
        with lock:
            allowed = max(0, budget_frames_left[0]) if frame_bytes else len(items)
            keep = items[: max(0, min(len(items), allowed))]
            budget_frames_left[0] -= len(keep)
        if len(keep) < len(items):
            logger.info("Keyframe budget: dropped %d low-priority frames for %s", len(items) - len(keep), path.name)

        # timestamp -> frame index (identical rounding to the per-consumer seeks it replaces)
        wanted: dict[int, list[float]] = {}
        for ts, _prio in keep:
            idx = max(0, int(round(ts * fps)))
            if total > 0:
                idx = min(idx, total - 1)
            wanted.setdefault(idx, []).append(ts)

        out: dict[float, np.ndarray] = {}
        pos = 0  # index of the NEXT frame cap will produce
        for idx in sorted(wanted):
            if cancel_event is not None and cancel_event.is_set():
                break
            gap = idx - pos
            if gap < 0 or gap > SEEK_GAP_FRAMES:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                pos = idx
            else:
                ok = True
                for _ in range(gap):
                    if not cap.grab():
                        ok = False
                        break
                pos = idx
                if not ok:
                    break
            ret, frame = cap.read()
            pos += 1
            if not ret or frame is None:
                continue
            small = _downscale(frame, max_width)
            for ts in wanted[idx]:
                out[ts] = small
        return out, (src_w, src_h)
    finally:
        cap.release()


def extract_keyframes(
    requests: FrameRequests | Mapping[Path, Mapping[float, int]],
    config: PipelineConfig | None = None,
    *,
    max_width: int = MAX_FRAME_WIDTH,
    workers: int | None = None,
    cancel_event: threading.Event | None = None,
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
) -> KeyframeStore:
    """Read every requested frame with one VideoCapture per clip (clips in parallel)."""
    by_clip = requests.as_mapping() if isinstance(requests, FrameRequests) else requests
    store = KeyframeStore()
    if not by_clip:
        return store
    default_fps = config.fps if config is not None else 30.0
    n_workers = max(1, min(workers or (config.max_workers if config else 4), len(by_clip)))
    # Rough frame budget assuming 16:9 at max_width.
    per_frame = max(1, max_width * (max_width * 9 // 16) * 3)
    frames_left = [max(1, budget_bytes // per_frame)]
    lock = threading.Lock()

    def work(item: tuple[Path, Mapping[float, int]]):
        path, stamps = item
        return path, _read_clip(path, stamps, default_fps, max_width, cancel_event, frames_left, lock)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for path, (frames, size) in pool.map(work, by_clip.items()):
            if size is not None:
                store.sizes[path] = size
            for ts, frame in frames.items():
                store[(path, ts)] = frame
    logger.info("Keyframe cache: %d frames from %d clips", len(store), len(by_clip))
    return store


def attach_boundary_frames(scored_scenes: Iterable[ScoredScene], store: KeyframeStore) -> int:
    """Store first/last scene frames in ``metadata['_boundary_frame_start'/'_end']``.

    ``evaluate_cut_continuity`` reads these keys, so cut checks during timeline synthesis
    (master + variants) never seek into the clip again. Returns the number of scenes filled.
    """
    filled = 0
    for sc in scored_scenes:
        first, last = boundary_timestamps(sc.scene)
        a = store.frame(sc.scene.clip_path, first)
        b = store.frame(sc.scene.clip_path, last)
        if a is not None:
            sc.metadata["_boundary_frame_start"] = a
        if b is not None:
            sc.metadata["_boundary_frame_end"] = b
        filled += int(a is not None and b is not None)
    return filled


def vlm_keyframes(scored_scenes: list[ScoredScene], store: KeyframeStore) -> dict[int, np.ndarray]:
    """Index-keyed middle frames in the shape ``VLMAssistant.tag_scored_scenes`` expects."""
    out: dict[int, np.ndarray] = {}
    for i, sc in enumerate(scored_scenes):
        f = store.frame(sc.scene.clip_path, vlm_timestamp(sc.scene))
        if f is not None:
            out[i] = f
    return out
