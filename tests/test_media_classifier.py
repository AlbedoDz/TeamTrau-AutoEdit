"""Content role classifier: decision matrix and signal extractors (no media files)."""

from pathlib import Path

import numpy as np
import pytest

from src.core.ontology import ContentRole
from src.modules.media_classifier import (
    ClipSignals,
    classify_batch,
    classify_clip,
    decide_role,
    face_area_ratio,
    motion_score,
    voice_activity,
)


def sig(**kw) -> ClipSignals:
    base = dict(has_audio=True, voice_ratio=0.0, snr_db=0.0, face_ratio=0.0, motion=0.0)
    base.update(kw)
    return ClipSignals(**base)


def test_content_role_values():
    assert ContentRole.A_ROLL.value == "a_roll"
    assert ContentRole.B_ROLL.value == "b_roll"
    assert ContentRole.AMBIENT_B_ROLL.value == "ambient_b_roll"


@pytest.mark.parametrize(
    "signals, role",
    [
        (sig(voice_ratio=0.45, snr_db=14.5, face_ratio=0.30, motion=2.0), ContentRole.A_ROLL),
        (sig(voice_ratio=0.45, snr_db=14.5, face_ratio=0.0, motion=2.0), ContentRole.A_ROLL),
        (sig(voice_ratio=0.45, snr_db=6.0, face_ratio=0.30), ContentRole.AMBIENT_B_ROLL),
        (sig(voice_ratio=0.20, snr_db=20.0, face_ratio=0.30), ContentRole.AMBIENT_B_ROLL),
        (sig(voice_ratio=0.10, snr_db=20.0, face_ratio=0.50), ContentRole.B_ROLL),
        (sig(has_audio=False, face_ratio=0.5), ContentRole.B_ROLL),
    ],
)
def test_decision_matrix(signals, role):
    assert decide_role(signals).role == role


def test_threshold_edges():
    assert decide_role(sig(voice_ratio=0.30, snr_db=10.0)).role == ContentRole.A_ROLL
    assert decide_role(sig(voice_ratio=0.15, snr_db=10.0)).role == ContentRole.AMBIENT_B_ROLL
    assert decide_role(sig(voice_ratio=0.1499, snr_db=10.0)).role == ContentRole.B_ROLL


def test_face_raises_confidence_and_speech_flags():
    with_face = decide_role(sig(voice_ratio=0.45, snr_db=14.5, face_ratio=0.30))
    no_face = decide_role(sig(voice_ratio=0.45, snr_db=14.5, face_ratio=0.0))
    assert with_face.confidence >= 0.85
    assert with_face.confidence > no_face.confidence
    assert with_face.has_speech and no_face.has_speech
    assert "offscreen_speech" in no_face.reasons
    assert not decide_role(sig(has_audio=False)).has_speech


def test_decision_is_total_and_explained():
    for v in np.linspace(0.0, 1.0, 11):
        for s in (-5.0, 0.0, 9.99, 10.0, 40.0):
            for f in (0.0, 0.08, 0.5):
                d = decide_role(sig(voice_ratio=float(v), snr_db=s, face_ratio=f))
                assert isinstance(d.role, ContentRole)
                assert 0.0 <= d.confidence <= 1.0
                assert d.reasons


def test_voice_activity_silence_and_empty():
    assert voice_activity(np.zeros(16000 * 2, dtype=np.float32), 16000)[0] == 0.0
    assert voice_activity(np.zeros(0, dtype=np.float32), 16000) == (0.0, 0.0)


def test_voice_activity_gated_bursts():
    sr = 16000
    rng = np.random.default_rng(0)
    x = 0.001 * rng.standard_normal(sr * 4).astype(np.float32)
    for k in range(4):  # 0.5 s bursts every second -> 50 % activity
        x[k * sr : k * sr + sr // 2] += 0.3 * rng.standard_normal(sr // 2).astype(np.float32)
    ratio, snr = voice_activity(x, sr)
    assert 0.40 <= ratio <= 0.60
    assert snr >= 20.0


def test_voice_activity_stationary_tone_is_not_speech():
    sr = 16000
    t = np.arange(sr * 3) / sr
    ratio, _ = voice_activity((0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), sr)
    assert ratio <= 0.05


def test_motion_score():
    still = [np.full((90, 160, 3), 128, np.uint8)] * 3
    assert motion_score(still) == 0.0
    rng = np.random.default_rng(1)
    noisy = [rng.integers(0, 256, (90, 160, 3), dtype=np.uint8) for _ in range(3)]
    assert motion_score(noisy) > 30.0
    assert motion_score(still[:1]) == 0.0


def test_face_area_ratio_without_faces():
    assert face_area_ratio([]) == 0.0
    assert face_area_ratio([np.full((180, 320, 3), 127, np.uint8)] * 3) == 0.0


def test_classify_clip_uses_role_folder_without_probing(tmp_path: Path):
    def boom(_p: Path) -> ClipSignals:
        raise AssertionError("extractor must not run for role folders")

    r = classify_clip(tmp_path / "A-roll" / "x.mp4", extractor=boom)
    assert r.role == ContentRole.A_ROLL and r.confidence == 1.0 and r.has_speech
    r = classify_clip(tmp_path / "B-roll" / "y.mp4", extractor=boom)
    assert r.role == ContentRole.B_ROLL and r.confidence == 1.0


def test_classify_clip_never_raises(tmp_path: Path):
    def broken(_p: Path) -> ClipSignals:
        raise RuntimeError("decode failed")

    r = classify_clip(tmp_path / "x.mp4", extractor=broken)
    assert r.role == ContentRole.B_ROLL
    assert r.confidence == 0.0
    assert r.error == "decode failed"
    assert r.reasons == ["probe_failed"]


def test_classify_batch_keeps_input_order(tmp_path: Path):
    paths = [tmp_path / f"c{i}.mp4" for i in range(6)]

    def fake(p: Path) -> ClipSignals:
        speech = int(p.stem[1:]) % 2 == 0
        return sig(voice_ratio=0.6 if speech else 0.0, snr_db=20.0)

    out = classify_batch(paths, max_workers=3, extractor=fake)
    assert [r.path for r in out] == paths
    assert [r.role for r in out] == [ContentRole.A_ROLL, ContentRole.B_ROLL] * 3
