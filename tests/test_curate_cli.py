"""main.py curate."""

import json
from pathlib import Path

from click.testing import CliRunner

import main
from src.core import curation


def test_curate_writes_summary(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def fake_run(adapter, input_dir, genre, output_dir, top_n=12, timeout=None):
        seen.update(input_dir=Path(input_dir), genre=genre, top_n=top_n)
        return object(), {"session_id": "s1", "takes": 2, "top": ["B01|b_roll|4.0s|q0.90"]}

    monkeypatch.setattr(curation, "run_curation", fake_run)
    (tmp_path / "in").mkdir()
    r = CliRunner().invoke(main.cli, ["curate", "--input-dir", str(tmp_path / "in"),
                                      "--genre", "travel_daily_vlog",
                                      "--output-dir", str(tmp_path / "out"), "--top", "5"])
    assert r.exit_code == 0, r.output
    data = json.loads((tmp_path / "out" / "curation_summary.json").read_text(encoding="utf-8"))
    assert data["takes"] == 2
    assert seen == {"input_dir": tmp_path / "in", "genre": "travel_daily_vlog", "top_n": 5}


def test_curate_export_flag_calls_export(tmp_path: Path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(curation, "run_curation",
                        lambda adapter, input_dir, genre, output_dir, top_n=12, timeout=None:
                        ("SESSION", {"session_id": "s1", "takes": 1}))
    monkeypatch.setattr(curation, "export_session",
                        lambda adapter, session, output_dir, **kw:
                        calls.append(session) or {"status": "EXPORT_COMPLETE"})
    (tmp_path / "in").mkdir()
    r = CliRunner().invoke(main.cli, ["curate", "--input-dir", str(tmp_path / "in"),
                                      "--output-dir", str(tmp_path / "out"), "--export"])
    assert r.exit_code == 0, r.output
    assert calls == ["SESSION"]


def test_curate_rejects_unknown_genre(tmp_path: Path):
    (tmp_path / "in").mkdir()
    r = CliRunner().invoke(main.cli, ["curate", "--input-dir", str(tmp_path / "in"),
                                      "--genre", "not_a_genre",
                                      "--output-dir", str(tmp_path / "out")])
    assert r.exit_code == 2
    assert "not_a_genre" in r.output
