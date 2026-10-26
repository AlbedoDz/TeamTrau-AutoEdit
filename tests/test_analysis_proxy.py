"""Analysis proxies: frame-accurate 1920-wide copies used only by analysis reads."""

import shutil
import subprocess
import threading
from pathlib import Path

import cv2
import pytest

from src.core.config import PipelineConfig
from src.modules import analysis_proxy as ap
from src.modules.ingest_detector import IngestDetector
from src.modules.keyframe_cache import extract_keyframes

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available on PATH")


@pytest.fixture(autouse=True)
def _clean_registry():
    ap.clear()
    yield
    ap.clear()


def _clip(path: Path, size: str, seconds: float = 2.0, fps: int = 30, audio: bool = False) -> Path:
    cmd = [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:d={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:d={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    subprocess.run([*cmd, str(path)], check=True)
    return path


def _frames(path: Path) -> int:
    cap = cv2.VideoCapture(str(path))
    n = 0
    while cap.grab():
        n += 1
    cap.release()
    return n


@needs_ffmpeg
def test_proxy_is_1920_wide_frame_accurate_and_cached(tmp_path: Path) -> None:
    src = _clip(tmp_path / "uhd.mp4", "3840x2160")
    made = ap.prepare_analysis_proxies([src], tmp_path / "cache")
    assert len(made) == 1
    entry = made[0]
    assert (entry.width, entry.height) == (3840, 2160)
    cap = cv2.VideoCapture(str(entry.proxy))
    assert int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) == 1920
    assert int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) == 1080
    cap.release()
    assert _frames(entry.proxy) == _frames(src) == 60

    assert ap.analysis_path(src) == entry.proxy
    assert ap.source_size(src) == (3840, 2160)
    again = ap.build_analysis_proxy(src, tmp_path / "cache")
    assert again is not None and again.encoder == "cached" and again.proxy == entry.proxy


@needs_ffmpeg
def test_small_footage_keeps_the_original(tmp_path: Path) -> None:
    src = _clip(tmp_path / "hd.mp4", "1920x1080")
    assert ap.prepare_analysis_proxies([src], tmp_path / "cache") == []
    assert ap.analysis_path(src) == src
    assert ap.source_size(src) is None


def test_unreadable_source_never_fails(tmp_path: Path) -> None:
    bad = tmp_path / "not_a_video.mp4"
    bad.write_bytes(b"nope")
    assert ap.prepare_analysis_proxies([bad], tmp_path / "cache") == []
    assert ap.analysis_path(bad) == bad


@needs_ffmpeg
def test_keyframes_read_the_proxy_but_report_the_original_size(tmp_path: Path) -> None:
    src = _clip(tmp_path / "uhd.mp4", "3840x2160")
    ap.prepare_analysis_proxies([src], tmp_path / "cache")
    store = extract_keyframes({src: {0.5: 0, 1.0: 0}}, max_width=640, workers=1)
    assert store.source_size(src) == (3840, 2160)
    frame = store.frame(src, 0.5)
    assert frame is not None and frame.shape[1] == 640


@needs_ffmpeg
def test_scene_and_dead_air_detection_through_the_proxy(tmp_path: Path) -> None:
    src = _clip(tmp_path / "uhd.mp4", "3840x2160", audio=True)
    det = IngestDetector(PipelineConfig())
    before_scenes = det.detect_scenes(src)
    before_dead = det.detect_dead_air_ranges(src)

    ap.prepare_analysis_proxies([src], tmp_path / "cache")
    after_scenes = det.detect_scenes(src)
    assert [s.clip_path for s in after_scenes] == [src]  # cuts still point at the original
    assert [(s.start_frame, s.end_frame) for s in after_scenes] == [
        (s.start_frame, s.end_frame) for s in before_scenes
    ]
    assert det.detect_dead_air_ranges(src) == before_dead  # audio still read from the original


def test_registry_is_thread_safe(tmp_path: Path) -> None:
    entry = ap.AnalysisProxy(tmp_path / "a.mp4", tmp_path / "a_proxy.mp4", 3840, 2160, "test")
    with ap._LOCK:
        ap._REGISTRY[entry.original.resolve()] = entry
    seen: list[Path] = []
    threads = [threading.Thread(target=lambda: seen.append(ap.analysis_path(entry.original))) for _ in range(16)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert set(seen) == {entry.proxy}
