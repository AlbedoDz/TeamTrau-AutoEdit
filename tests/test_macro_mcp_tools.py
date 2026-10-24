"""MCP macro-tools delegate to src.core.curation and keep sessions."""

import json
from pathlib import Path

import pytest

from src.core.types import DeliverResult
from src.modules import mcp_server
from tests.curation_fakes import FakeAdapter, pipeline_result, scene, timeline


def test_three_tools_end_to_end_with_fake_adapter(tmp_path: Path, monkeypatch):
    fake = FakeAdapter(
        pipeline_result([scene(0, "b_roll", q=0.8)], {"rough_cut": timeline("r", "rough_cut")}),
        pipeline_result([], {"master": timeline()}),
    )
    monkeypatch.setattr(mcp_server, "adapter", fake)
    cur = mcp_server.curate_footage(input_dir=str(tmp_path))
    assert len(json.dumps(cur)) <= 2000
    sid = cur["session_id"]
    music = tmp_path / "song.mp3"
    music.write_bytes(b"x")
    asm = mcp_server.assemble_cadence_timeline(session_id=sid, selected_shot_ids=["B01"],
                                               soundtrack_path=str(music))
    assert asm["status"] == "TIMELINE_ASSEMBLED_SUCCESS"
    fake.results.append(DeliverResult(success=True, fcpxml_path=tmp_path / "t.fcpxml"))
    exp = mcp_server.export_nle_project(session_id=sid, output_dir=str(tmp_path / "out"),
                                        targets=["fcpxml"])
    assert exp["status"] == "EXPORT_COMPLETE"
    assert fake.calls[-1][1]["formats"] == {"fcpxml"}


def test_unknown_session(monkeypatch):
    with pytest.raises(ValueError, match="Unknown session"):
        mcp_server.assemble_cadence_timeline(session_id="nope")


def test_session_store_is_bounded(tmp_path: Path, monkeypatch):
    results = [pipeline_result([scene(0, "b_roll")], {"rough_cut": timeline("r", "rough_cut")})
               for _ in range(mcp_server.MAX_SESSIONS + 1)]
    monkeypatch.setattr(mcp_server, "adapter", FakeAdapter(*results))
    ids = [mcp_server.curate_footage(input_dir=str(tmp_path))["session_id"]
           for _ in range(mcp_server.MAX_SESSIONS + 1)]
    with pytest.raises(ValueError, match="Unknown session"):
        mcp_server.export_nle_project(session_id=ids[0], output_dir=str(tmp_path / "o"))
    assert ids[-1] in mcp_server._SESSIONS
