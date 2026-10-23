"""Rough-cut stage on real clips: roles (folder + classifier) and golden windows."""

import shutil
import subprocess
from pathlib import Path

from src.core.config import PipelineConfig
from src.core.service import StudioBackendAdapter

FFMPEG = shutil.which("ffmpeg")


def _clip(out: Path, seconds: float, gated_audio: bool) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds}"]
    if gated_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=48000:duration={seconds}",
                "-af", "volume='if(lt(mod(t,1),0.5),1,0)':eval=frame", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15", str(out)]
    subprocess.run(cmd, check=True)
    return out


def _run_rough_cut(input_dir: Path, tmp_path: Path):
    cfg = PipelineConfig()
    cfg.cache_dir = tmp_path / "cache"
    handle = StudioBackendAdapter().submit_job(
        input_dir=input_dir, soundtrack=None, output_dir=tmp_path / "out", config=cfg,
        stage="rough_cut",
    )
    assert handle.wait(timeout=600)
    res = handle.result
    assert res.success, res.error_message
    return cfg, res


def _check_windows(cfg, res) -> set[str]:
    roles = {s.metadata.get("content_role") for s in res.scored_scenes}
    for s in res.scored_scenes:
        if s.metadata.get("content_role") in ("b_roll", "ambient_b_roll"):
            assert s.scene.duration <= cfg.golden_window_max_sec + 1.0 / cfg.fps
    assert "rough_cut" in res.edit_timelines
    return roles


def test_mode_b_role_folders(tmp_path: Path):
    _clip(tmp_path / "footage" / "A-roll" / "talk.mp4", 20.0, True)
    _clip(tmp_path / "footage" / "B-roll" / "scenic.mp4", 20.0, False)
    cfg, res = _run_rough_cut(tmp_path / "footage", tmp_path)
    assert {"a_roll", "b_roll"} <= _check_windows(cfg, res)


def test_mode_a_single_folder_classifier(tmp_path: Path):
    _clip(tmp_path / "footage" / "talk.mp4", 20.0, True)
    _clip(tmp_path / "footage" / "scenic.mp4", 20.0, False)
    cfg, res = _run_rough_cut(tmp_path / "footage", tmp_path)
    by_clip = {Path(s.scene.clip_path).stem: s.metadata.get("content_role")
               for s in res.scored_scenes}
    assert by_clip.get("talk") == "a_roll"
    assert by_clip.get("scenic") == "b_roll"
    _check_windows(cfg, res)
