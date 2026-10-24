"""Deliver writes preflight.json next to the NLE files."""

import json
from pathlib import Path

from src.core.config import PipelineConfig
from src.core.service import StudioBackendAdapter
from src.core.types import EditClip, EditTimeline


def test_deliver_writes_preflight(tmp_path: Path, make_clip):
    mono = make_clip("Lễ mono.mp4", duration=2.0, channels=1, sample_rate=44100)
    tl = EditTimeline(name="pf", fps=30.0, canvas_width=1920, canvas_height=1080,
                      clips=[EditClip(source_path=mono, source_in=0.0, frames=30, available=2.0)])
    cfg = PipelineConfig()
    cfg.cache_dir = tmp_path / "cache"
    handle = StudioBackendAdapter().submit_deliver_job(
        edit_timeline=tl, output_dir=tmp_path / "out", config=cfg, formats={"fcpxml"},
    )
    assert handle.wait(timeout=300)
    res = handle.result
    assert res.success, res.error_message
    assert res.preflight_path is not None and res.preflight_path.exists()
    data = json.loads(res.preflight_path.read_text(encoding="utf-8"))
    assert {i["code"] for i in data["issues"]} >= {"AUDIO_MONO", "AUDIO_RATE_MISMATCH"}
    assert any("AUDIO_MONO" in s for s in res.preflight_issues)
    assert res.fcpxml_path is not None and res.fcpxml_path.exists()
