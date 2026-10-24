"""Face detector availability is reported, never silently ignored."""

import logging
from pathlib import Path

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


def test_unavailable_cascade_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(mc, "_FACE_CASCADE", None)
    monkeypatch.setattr(mc, "_CASCADE_LOADED", False)
    monkeypatch.delattr(mc.cv2, "CascadeClassifier", raising=False)
    with caplog.at_level(logging.WARNING, logger=mc.__name__):
        assert mc.face_detector_available() is False
        assert mc.face_detector_available() is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
