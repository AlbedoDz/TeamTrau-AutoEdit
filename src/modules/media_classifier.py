"""Content role classifier: speech and face-based classification of media clips.

Distinguishes A-roll (dialogue/interview/narration anchor), B-roll (visual cutaways),
and Ambient B-roll (mixed or low-SNR sound) for automated curation and selects stringout.
"""

import logging
import math
import shutil
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from src.core.ffmpeg_utils import has_audio_stream, run_tool_bytes
from src.core.ontology import ContentRole, role_folder_of

logger = logging.getLogger(__name__)

_FACE_CASCADE: Any = None
_CASCADE_LOADED: bool = False


def _get_face_cascade() -> Any:
    global _FACE_CASCADE, _CASCADE_LOADED
    if not _CASCADE_LOADED:
        _CASCADE_LOADED = True
        cascade_cls = getattr(cv2, "CascadeClassifier", None)
        if cascade_cls is None:
            logger.warning(
                "Face detector unavailable: cv2 has no CascadeClassifier in this OpenCV build"
            )
            _FACE_CASCADE = None
            return None
        data_dir = getattr(cv2.data, "haarcascades", "")
        xml_path = data_dir + "haarcascade_frontalface_default.xml"
        if not Path(xml_path).is_file():
            logger.warning("Face detector unavailable: cascade file missing (%s)", xml_path)
            _FACE_CASCADE = None
            return None
        try:
            clf = cascade_cls(xml_path)
            if clf.empty():
                logger.warning("Face detector unavailable: empty cascade loaded from %s", xml_path)
                _FACE_CASCADE = None
            else:
                _FACE_CASCADE = clf
        except Exception as exc:
            logger.warning("Face detector unavailable: failed to load cascade: %s", exc)
            _FACE_CASCADE = None
    return _FACE_CASCADE


def face_detector_available() -> bool:
    """Return True if Haar cascade face detector is loaded and available."""
    return _get_face_cascade() is not None


@dataclass(frozen=True)
class ClipSignals:
    has_audio: bool
    voice_ratio: float  # 0..1 share of active (speech-like) audio frames
    snr_db: float  # median active level minus noise floor
    face_ratio: float  # largest face area / frame area over the sampled keyframes
    motion: float  # mean abs grey difference between sampled frames, 0..255


@dataclass(frozen=True)
class RoleDecision:
    role: ContentRole
    confidence: float  # 0..1
    has_speech: bool
    reasons: list[str]  # never empty


@dataclass(frozen=True)
class ClassificationResult:
    path: Path
    role: ContentRole
    confidence: float
    has_speech: bool
    reasons: list[str]
    signals: ClipSignals | None = None
    error: str | None = None


def decide_role(signals: ClipSignals) -> RoleDecision:
    """Classify clip role from extracted audio and visual signals."""
    if not signals.has_audio:
        return RoleDecision(
            role=ContentRole.B_ROLL,
            confidence=0.95,
            has_speech=False,
            reasons=["no_audio"],
        )

    if signals.voice_ratio < 0.15:
        conf = 0.70 + 0.25 * (1.0 - signals.voice_ratio / 0.15)
        return RoleDecision(
            role=ContentRole.B_ROLL,
            confidence=max(0.0, min(1.0, float(conf))),
            has_speech=False,
            reasons=[f"voice_ratio={signals.voice_ratio:.4f}<0.15"],
        )

    if signals.voice_ratio >= 0.30 and signals.snr_db >= 10.0:
        face_bonus = 0.15 if signals.face_ratio >= 0.08 else 0.0
        voice_scaled = min(1.0, (signals.voice_ratio - 0.30) / 0.40)
        conf = 0.70 + 0.15 * voice_scaled + face_bonus
        reasons = [
            f"voice_ratio={signals.voice_ratio:.2f}>=0.30",
            f"snr_db={signals.snr_db:.1f}>=10",
        ]
        if signals.face_ratio >= 0.08:
            reasons.append(f"face_ratio={signals.face_ratio:.2f}>=0.08")
        else:
            reasons.append("offscreen_speech")
        return RoleDecision(
            role=ContentRole.A_ROLL,
            confidence=max(0.0, min(1.0, float(conf))),
            has_speech=True,
            reasons=reasons,
        )

    reasons = [
        f"voice_ratio={signals.voice_ratio:.2f}",
        f"snr_db={signals.snr_db:.1f}",
    ]
    return RoleDecision(
        role=ContentRole.AMBIENT_B_ROLL,
        confidence=0.60,
        has_speech=True,
        reasons=reasons,
    )


def voice_activity(samples: np.ndarray, sample_rate: int) -> tuple[float, float]:
    """Calculate voice activity ratio and SNR (dB) using energy-based frame gating."""
    if samples.size == 0 or sample_rate <= 0:
        return 0.0, 0.0

    frame_len = int(round(0.030 * sample_rate))
    if frame_len <= 0 or samples.size < frame_len:
        return 0.0, 0.0

    n_frames = samples.size // frame_len
    if n_frames == 0:
        return 0.0, 0.0

    truncated = samples[: n_frames * frame_len].reshape(n_frames, frame_len)
    frame_power = np.mean(truncated.astype(np.float64) ** 2, axis=1)
    db = 10.0 * np.log10(frame_power + 1e-12)

    floor = float(np.percentile(db, 10))
    active = (db > (floor + 10.0)) & (db > -50.0)
    voice_ratio = float(np.mean(active))

    if np.any(active):
        snr_db = float(np.median(db[active]) - floor)
    else:
        snr_db = 0.0

    return voice_ratio, snr_db


def face_area_ratio(frames: list[np.ndarray]) -> float:
    """Return largest face bounding box area divided by frame area across sampled keyframes."""
    if not frames:
        return 0.0

    cascade = _get_face_cascade()
    if cascade is None:
        return 0.0

    max_ratio = 0.0
    for frame in frames:
        if frame is None or frame.size == 0:
            continue
        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        try:
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
        except Exception:
            faces = ()
        frame_area = float(w * h)
        for _fx, _fy, fw, fh in faces:
            ratio = float(fw * fh) / frame_area
            if ratio > max_ratio:
                max_ratio = ratio

    return float(max_ratio)


def motion_score(frames: list[np.ndarray]) -> float:
    """Calculate mean absolute gray difference between consecutive downscaled frames."""
    if len(frames) < 2:
        return 0.0

    processed: list[np.ndarray] = []
    for f in frames:
        if f is None or f.size == 0:
            continue
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if f.ndim == 3 else f
        h, w = gray.shape[:2]
        if w != 160:
            scale = 160.0 / float(max(w, 1))
            new_h = max(1, int(round(h * scale)))
            gray = cv2.resize(gray, (160, new_h))
        processed.append(gray)

    if len(processed) < 2:
        return 0.0

    diffs = [
        float(np.mean(cv2.absdiff(processed[i], processed[i + 1])))
        for i in range(len(processed) - 1)
    ]
    return float(np.mean(diffs)) if diffs else 0.0


def extract_signals(path: Path, max_audio_sec: float = 120.0) -> ClipSignals:
    """Extract audio speech metrics and sampled video frames from a clip."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Media file not found: {p}")

    cap = None
    frames: list[np.ndarray] = []
    try:
        cap = cv2.VideoCapture(str(p))
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video: {p}")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not math.isfinite(fps) or fps <= 1.0:
            fps = 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 0:
            target_indices = [
                max(0, min(total - 1, int(round(total * 0.20)))),
                max(0, min(total - 1, int(round(total * 0.50)))),
                max(0, min(total - 1, int(round(total * 0.80)))),
            ]
            for idx in target_indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                if ok and frame is not None:
                    frames.append(frame)
        else:
            for _ in range(3):
                ok, frame = cap.read()
                if ok and frame is not None:
                    frames.append(frame)
                else:
                    break
    finally:
        if cap is not None:
            cap.release()

    face_ratio = face_area_ratio(frames)
    motion = motion_score(frames)

    has_audio = has_audio_stream(p)
    voice_ratio = 0.0
    snr_db = 0.0
    if has_audio:
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [
            ffmpeg_bin,
            "-v",
            "error",
            "-i",
            str(p),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-t",
            str(max_audio_sec),
            "-f",
            "f32le",
            "-",
        ]
        proc = run_tool_bytes(cmd, timeout=60.0)
        if proc.returncode == 0 and proc.stdout:
            samples = np.frombuffer(proc.stdout, dtype=np.float32)
            voice_ratio, snr_db = voice_activity(samples, 16000)

    return ClipSignals(
        has_audio=has_audio,
        voice_ratio=voice_ratio,
        snr_db=snr_db,
        face_ratio=face_ratio,
        motion=motion,
    )


def classify_clip(
    path: Path,
    extractor: Callable[[Path], ClipSignals] = extract_signals,
) -> ClassificationResult:
    """Classify a single clip using folder heuristics or signal extraction. Never raises."""
    p = Path(path)
    folder_role = role_folder_of(p)
    if folder_role is not None:
        if folder_role == "a_roll":
            role = ContentRole.A_ROLL
            reasons = ["folder:a_roll"]
            has_speech = True
        else:
            role = ContentRole.B_ROLL
            reasons = ["folder:b_roll"]
            has_speech = False
        return ClassificationResult(
            path=p,
            role=role,
            confidence=1.0,
            has_speech=has_speech,
            reasons=reasons,
            signals=None,
            error=None,
        )

    try:
        signals = extractor(p)
        decision = decide_role(signals)
        reasons = list(decision.reasons)
        detector_fn = getattr(
            sys.modules[__name__], "face_detector_available", face_detector_available
        )
        if not detector_fn():
            reasons.append("face_detector_unavailable")
        return ClassificationResult(
            path=p,
            role=decision.role,
            confidence=decision.confidence,
            has_speech=decision.has_speech,
            reasons=reasons,
            signals=signals,
            error=None,
        )
    except Exception as exc:
        logger.debug("classify_clip failed for %s: %s", p, exc)
        return ClassificationResult(
            path=p,
            role=ContentRole.B_ROLL,
            confidence=0.0,
            has_speech=False,
            reasons=["probe_failed"],
            signals=None,
            error=str(exc),
        )


def classify_batch(
    paths: list[Path],
    max_workers: int = 4,
    extractor: Callable[[Path], ClipSignals] = extract_signals,
) -> list[ClassificationResult]:
    """Classify a batch of clips concurrently, preserving input order."""
    if not paths:
        return []
    if max_workers <= 1 or len(paths) == 1:
        return [classify_clip(p, extractor=extractor) for p in paths]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(classify_clip, p, extractor) for p in paths]
        return [f.result() for f in futures]
