"""Tests for FreeCut Project Writer index.json error handling."""

import json
from pathlib import Path

import pytest

from src.core.types import EditClip, EditTimeline
from src.integrations.freecut_writer import write_freecut_project


def test_corrupt_index_raises_json_decode_error(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    index_file = ws / "index.json"
    corrupt_content = "{not json"
    index_file.write_text(corrupt_content, encoding="utf-8")

    clip = EditClip(source_path=tmp_path / "dummy.mp4", source_in=0.0, frames=30, available=1.0)
    timeline = EditTimeline(name="t", fps=30.0, canvas_width=1920, canvas_height=1080, clips=[clip])

    with pytest.raises(json.JSONDecodeError):
        write_freecut_project(timeline, ws, "Test")

    assert index_file.read_text(encoding="utf-8") == corrupt_content
