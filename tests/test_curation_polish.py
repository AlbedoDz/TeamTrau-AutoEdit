"""Tests for review polish items (items 4 and 5 of T8)."""

from pathlib import Path

import pytest

from src.core import curation
from src.core.config import PipelineConfig
from src.core.curation import CurationSession
from src.modules import mcp_server
from tests.curation_fakes import FakeAdapter, pipeline_result


def test_export_session_raises_when_no_timeline(tmp_path: Path):
    """export_session must raise ValueError BEFORE calling adapter when no timeline is found."""
    fake = FakeAdapter()
    empty_result = pipeline_result([], timelines={})
    session = CurationSession(
        session_id="test_empty_tl",
        input_dir=tmp_path,
        config=PipelineConfig(),
        result=empty_result,
        shots={},
        sequence=None,
        output_dir=tmp_path / "out",
    )

    with pytest.raises(ValueError, match="^Nothing to export: run curate_footage first$"):
        curation.export_session(fake, session, tmp_path / "exp")

    assert fake.calls == []


def test_curate_footage_clamps_top_n(tmp_path: Path, monkeypatch):
    """curate_footage must clamp top_n to 1..20 range before calling run_curation."""
    recorded_top_n: list[int] = []

    def fake_run_curation(adapter, input_dir, genre, output_dir, top_n):
        recorded_top_n.append(top_n)
        fake_result = pipeline_result([])
        session = CurationSession(
            session_id=f"sess_{len(recorded_top_n)}",
            input_dir=input_dir,
            config=PipelineConfig(),
            result=fake_result,
            shots={},
            sequence=None,
            output_dir=output_dir,
        )
        return session, {"session_id": session.session_id}

    monkeypatch.setattr(mcp_server, "run_curation", fake_run_curation)
    monkeypatch.setattr(mcp_server, "adapter", FakeAdapter())

    # Test values below range (< 1)
    mcp_server.curate_footage(input_dir=str(tmp_path), top_n=0)
    assert recorded_top_n[-1] == 1

    mcp_server.curate_footage(input_dir=str(tmp_path), top_n=-5)
    assert recorded_top_n[-1] == 1

    # Test values above range (> 20)
    mcp_server.curate_footage(input_dir=str(tmp_path), top_n=25)
    assert recorded_top_n[-1] == 20

    mcp_server.curate_footage(input_dir=str(tmp_path), top_n=100)
    assert recorded_top_n[-1] == 20

    # Test value within range
    mcp_server.curate_footage(input_dir=str(tmp_path), top_n=15)
    assert recorded_top_n[-1] == 15
