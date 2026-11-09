"""Face detector availability is reported, never silently ignored."""

import logging
from pathlib import Path

import cv2
import numpy as np

import src.modules.media_classifier as mc
from src.core.ontology import ContentRole


def _speech(_p: Path) -> mc.ClipSignals:
    return mc.ClipSignals(has_audio=True, voice_ratio=0.6, snr_db=20.0, face_ratio=0.0, motion=1.0)


def test_missing_face_detector_is_a_reason(monkeypatch):
    monkeypatch.setattr(mc, "face_detector_available", lambda: False)
    r = mc.classify_clip(Path("talk.mp4"), extractor=_speech)
    assert r.role == ContentRole.A_ROLL
    assert "face_detector_unavailable" in r.reasons


def test_available_face_detector_adds_no_reason(monkeypatch):
    monkeypatch.setattr(mc, "face_detector_available", lambda: True)
    r = mc.classify_clip(Path("talk.mp4"), extractor=_speech)
    assert "face_detector_unavailable" not in r.reasons


def test_folder_and_failure_results_are_unchanged(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(mc, "face_detector_available", lambda: False)
    r = mc.classify_clip(tmp_path / "A-roll" / "x.mp4", extractor=_speech)
    assert r.reasons == ["folder:a_roll"]

    def broken(_p: Path) -> mc.ClipSignals:
        raise RuntimeError("boom")

    assert mc.classify_clip(tmp_path / "y.mp4", extractor=broken).reasons == ["probe_failed"]


def test_unavailable_detector_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(mc, "_FACE_DETECTOR", None)
    monkeypatch.setattr(mc, "_DETECTOR_LOADED", False)
    monkeypatch.delattr(mc.cv2, "FaceDetectorYN", raising=False)
    with caplog.at_level(logging.WARNING, logger=mc.__name__):
        assert mc.face_detector_available() is False
        assert mc.face_detector_available() is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_missing_model_file_is_reported(monkeypatch, tmp_path: Path, caplog):
    monkeypatch.setattr(mc, "_FACE_DETECTOR", None)
    monkeypatch.setattr(mc, "_DETECTOR_LOADED", False)
    monkeypatch.setattr(mc, "YUNET_MODEL", tmp_path / "missing.onnx")
    with caplog.at_level(logging.WARNING, logger=mc.__name__):
        assert mc.face_detector_available() is False
    assert "model file missing" in caplog.text


def test_yunet_ships_and_finds_a_face(monkeypatch):
    """The bundled model loads in this OpenCV build and finds a drawn face on a 4K frame."""
    monkeypatch.setattr(mc, "_FACE_DETECTOR", None)
    monkeypatch.setattr(mc, "_DETECTOR_LOADED", False)
    assert mc.YUNET_MODEL.is_file()
    assert mc.face_detector_available() is True
    assert mc.face_area_ratio([np.zeros((2160, 3840, 3), np.uint8)]) == 0.0
    assert mc.face_area_ratio([_draw_face()]) > 0.02


def _draw_face(size: tuple[int, int] = (720, 1280)) -> np.ndarray:
    """A plain cartoon face (skin oval, eyes, brows, nose, mouth) filling about a tenth of the frame."""
    img = np.full((*size, 3), 90, np.uint8)
    cx, cy = size[1] // 2, size[0] // 2
    cv2.ellipse(img, (cx, cy), (150, 200), 0, 0, 360, (150, 180, 225), -1)
    for dx in (-60, 60):
        cv2.ellipse(img, (cx + dx, cy - 50), (28, 16), 0, 0, 360, (255, 255, 255), -1)
        cv2.circle(img, (cx + dx, cy - 50), 10, (40, 30, 20), -1)
        cv2.line(img, (cx + dx - 35, cy - 90), (cx + dx + 35, cy - 92), (40, 40, 60), 8)
    cv2.line(img, (cx, cy - 30), (cx - 12, cy + 40), (110, 130, 180), 6)
    cv2.ellipse(img, (cx, cy + 95), (60, 22), 0, 0, 180, (60, 60, 150), 8)
    return img

