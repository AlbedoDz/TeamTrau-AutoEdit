"""Curation core with a fake adapter (no media, no ffmpeg)."""

import json
from pathlib import Path

import pytest

from src.core import curation
from src.core.types import DeliverResult
from tests.curation_fakes import FakeAdapter, pipeline_result, scene, timeline


def test_ids_roles_and_top_format():
    res = pipeline_result([scene(0, "a_roll", q=0.3), scene(1, "b_roll", q=0.9),
                           scene(2, "ambient_b_roll", q=0.6), scene(3, None, q=0.1)])
    summary, shots = curation.summarize_curation(res)
    assert list(shots) == ["A01", "B01", "B02", "B03"]
    assert summary["by_role"] == {"a_roll": 1, "b_roll": 1, "ambient_b_roll": 1, "unknown": 1}
    assert summary["top"][0] == "B01|b_roll|4.0s|q0.90"
    assert summary["takes"] == 4 and summary["truncated"] == 0
    assert summary["curated_duration_sec"] == 16.0
    assert set(summary) == {"takes", "curated_duration_sec", "by_role", "top", "truncated"}


def test_summary_stays_within_token_budget():
    scenes = [scene(i, "b_roll" if i % 3 else "a_roll", q=(i % 10) / 10) for i in range(60)]
    summary, shots = curation.summarize_curation(pipeline_result(scenes))
    assert len(json.dumps(summary)) <= 2000
    assert len(summary["top"]) == 12 and summary["truncated"] == 48
    assert len(shots) == 60
    assert curation.summarize_curation(pipeline_result(scenes))[0] == summary


def test_run_curation_submits_rough_cut(tmp_path: Path):
    rough = timeline("r", "rough_cut")
    fake = FakeAdapter(pipeline_result([scene(0, "b_roll")], {"rough_cut": rough}))
    session, summary = curation.run_curation(fake, tmp_path, "travel_daily_vlog", tmp_path / "o")
    kind, kw = fake.calls[0]
    assert kind == "job" and kw["stage"] == "rough_cut" and kw["soundtrack"] is None
    assert summary["session_id"] == session.session_id
    assert list(session.shots) == ["B01"]
    assert session.config.golden_window_max_sec == 4.5


def test_run_curation_validates_before_submitting(tmp_path: Path):
    fake = FakeAdapter()
    with pytest.raises(FileNotFoundError):
        curation.run_curation(fake, tmp_path / "nope", None, tmp_path / "o")
    with pytest.raises(ValueError):
        curation.run_curation(fake, tmp_path, "not_a_genre", tmp_path / "o")
    assert fake.calls == []


def test_run_curation_surfaces_pipeline_failure(tmp_path: Path):
    fake = FakeAdapter(pipeline_result([], success=False, error="every scene was pruned"))
    with pytest.raises(RuntimeError, match="pruned"):
        curation.run_curation(fake, tmp_path, None, tmp_path / "o")


def _session(tmp_path: Path, fake: FakeAdapter):
    rough = timeline("r", "rough_cut")
    fake.results.insert(0, pipeline_result([scene(0, "a_roll"), scene(1, "b_roll")],
                                           {"rough_cut": rough}))
    session, _ = curation.run_curation(fake, tmp_path, None, tmp_path / "o")
    return session, rough


def test_assemble_selected_shots(tmp_path: Path):
    music = tmp_path / "Music" / "theme.mp3"
    music.parent.mkdir()
    music.write_bytes(b"x")
    fake = FakeAdapter(pipeline_result([], {"master": timeline()}))
    session, rough = _session(tmp_path, fake)
    out = curation.assemble_sequence(fake, session, selected_ids=["B01"])
    kind, kw = fake.calls[-1]
    assert kind == "job" and kw["stage"] == "sequence"
    assert kw["scenes"] == [session.shots["B01"]]
    assert kw["soundtrack"] == music
    assert kw["existing_timelines"] == {"rough_cut": rough}
    assert out == {"timeline_id": f"{session.session_id}:master", "total_cuts": 1,
                   "duration_sec": 2.0, "status": "TIMELINE_ASSEMBLED_SUCCESS"}


def test_assemble_rejects_unknown_ids_and_missing_soundtrack(tmp_path: Path):
    fake = FakeAdapter()
    session, _ = _session(tmp_path, fake)
    with pytest.raises(ValueError, match="Z99"):
        curation.assemble_sequence(fake, session, soundtrack=tmp_path / "m.mp3",
                                   selected_ids=["A01", "Z99"])
    with pytest.raises(ValueError, match="soundtrack"):
        curation.assemble_sequence(fake, session)


def test_export_rough_cut_selects_and_preview(tmp_path: Path):
    fake = FakeAdapter()
    session, rough = _session(tmp_path, fake)
    fake.results += [
        DeliverResult(success=True, fcpxml_path=tmp_path / "t.fcpxml",
                      preflight_issues=["warning:AUDIO_MONO:a.mp4"]),
        DeliverResult(success=True, render_path=tmp_path / "p.mp4", is_preview=True),
    ]
    out = curation.export_session(fake, session, tmp_path / "exp", render_preview=True)
    (k1, kw1), (k2, kw2) = fake.calls[-2:]
    assert k1 == "deliver" and kw1["edit_timeline"] is rough
    assert kw1["formats"] == {"fcpxml", "premiere", "capcut"}
    assert kw2["preview"] is True
    assert out["status"] == "EXPORT_COMPLETE"
    assert out["fcpxml_path"] == str(tmp_path / "t.fcpxml")
    assert out["preview_mp4"] == str(tmp_path / "p.mp4")
    assert (out["preflight_errors"], out["preflight_warnings"]) == (0, 1)


def test_export_rejects_unknown_targets(tmp_path: Path):
    fake = FakeAdapter()
    session, _ = _session(tmp_path, fake)
    with pytest.raises(ValueError):
        curation.export_session(fake, session, tmp_path / "exp", targets=["avid"])
