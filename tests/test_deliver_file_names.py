"""Deliver names its files after the timeline; characters Windows forbids must not reach the disk."""

import threading
from pathlib import Path

from src.core.config import PipelineConfig
from src.core.service import StudioBackendAdapter
from tests.curation_fakes import timeline


def test_deliver_sanitizes_names_with_colons_and_slashes(tmp_path: Path):
    tl = timeline("Wedding – Sequence – 2026-10-25 23:58 a/b")
    res = StudioBackendAdapter()._execute_deliver(
        tl, tmp_path, PipelineConfig(), {"otio"}, None, threading.Event()
    )
    assert res.success
    assert res.otio_path is not None and res.otio_path.is_file()
    assert res.otio_path.parent == tmp_path
    assert res.otio_path.name == "Wedding – Sequence – 2026-10-25 23_58 a_b.otio"
    assert [p.name for p in tmp_path.iterdir() if ":" in p.name] == []
