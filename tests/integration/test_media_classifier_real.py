"""Classifier on real ffmpeg-generated clips."""

import shutil
import subprocess
import time
from pathlib import Path

from src.core.ontology import ContentRole
from src.modules.media_classifier import classify_batch, classify_clip

FFMPEG = shutil.which("ffmpeg")


def _clip(out: Path, seconds: float, audio: str | None) -> Path:
    """``audio``: None (no stream), "silence" or "gated" (0.5 s tone every second)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds}"]
    if audio == "silence":
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={seconds}"]
    elif audio == "gated":
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=48000:duration={seconds}",
                "-af", "volume='if(lt(mod(t,1),0.5),1,0)':eval=frame"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(out))
    subprocess.run(cmd, check=True)
    return out


def test_no_audio_and_silent_clips_are_b_roll(tmp_path: Path):
    for name, audio in (("mute.mp4", None), ("silent.mp4", "silence")):
        r = classify_clip(_clip(tmp_path / name, 4.0, audio))
        assert r.error is None, r.error
        assert r.role == ContentRole.B_ROLL
        assert not r.has_speech


def test_gated_voice_like_audio_is_a_roll(tmp_path: Path):
    r = classify_clip(_clip(tmp_path / "talk.mp4", 4.0, "gated"))
    assert r.error is None, r.error
    assert r.role == ContentRole.A_ROLL
    assert r.signals is not None and 0.35 <= r.signals.voice_ratio <= 0.65


def test_unicode_path_and_missing_file(tmp_path: Path):
    ok = _clip(tmp_path / "Lễ Cưới" / "cảnh 01.mp4", 3.0, None)
    res = classify_batch([ok, tmp_path / "missing.mp4"])
    assert res[0].error is None and res[0].role == ContentRole.B_ROLL
    assert res[1].error is not None and res[1].confidence == 0.0


def test_classification_is_fast(tmp_path: Path):
    clip = _clip(tmp_path / "speed.mp4", 4.0, "gated")
    t0 = time.perf_counter()
    classify_clip(clip)
    assert time.perf_counter() - t0 < 2.0
