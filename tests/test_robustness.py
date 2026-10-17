"""Regression tests for Phase 17 robustness fixes (ordering, failure reporting, helpers)."""

from pathlib import Path
from unittest.mock import patch

from src.core.config import PipelineConfig
from src.core.ffmpeg_utils import atempo_chain, is_hw_encoder_error
from src.core.ontology import natural_sort_key
from src.modules.ingest_detector import IngestDetector


def test_natural_sort_orders_numbered_clips():
    names = [Path("C0010.MP4"), Path("C0002.MP4"), Path("clip10.mp4"), Path("clip2.mp4")]
    assert sorted(names, key=natural_sort_key) == [
        Path("C0002.MP4"), Path("C0010.MP4"), Path("clip2.mp4"), Path("clip10.mp4")
    ]


def test_atempo_chain_stays_within_filter_limits():
    assert atempo_chain(1.0) == ""
    for speed in (0.25, 3.0, 8.0):
        factors = [float(x.split("=")[1]) for x in atempo_chain(speed).split(",")]
        assert all(0.5 <= f <= 2.0 for f in factors)
        prod = 1.0
        for f in factors:
            prod *= f
        assert abs(prod - speed) < 1e-4


def test_hw_error_detection_only_for_encoder_failures():
    assert is_hw_encoder_error("[hevc_nvenc @ 0x] OpenEncodeSessionEx failed: out of memory")
    assert not is_hw_encoder_error("Invalid argument: crop width is larger than input")


def test_batch_ingest_reports_failed_files(tmp_path):
    det = IngestDetector(PipelineConfig(cache_dir=tmp_path))
    good, bad = tmp_path / "good.mp4", tmp_path / "bad.mp4"

    def fake(vp, cancel_event=None):
        if vp == bad:
            raise ValueError("moov atom not found")
        return []

    with patch.object(det, "process_single_video", side_effect=fake):
        det.batch_process_videos([good, bad])
    assert det.failed_files == [(bad, "moov atom not found")]
