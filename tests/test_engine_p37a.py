"""P3.7a: shot details, advanced pipeline options and AI settings for the Studio panels."""

import json
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.core import curation
from src.core.config import PipelineConfig
from src.engine_api.ai_settings import AiSettingsStore, agent_config_from_settings
from src.engine_api.app import create_app
from src.engine_api.options import ADVANCED_OPTIONS, apply_config_overrides
from src.engine_api.runners import make_assemble_runner, make_curate_runner
from src.engine_api.sessions import SessionStore, session_to_dict
from tests.curation_fakes import FakeAdapter, pipeline_result, scene, timeline

TOKEN = "q" * 32
H = {"Authorization": f"Bearer {TOKEN}"}
DETAIL_KEYS = {"is_long_shot", "defects_count", "has_dead_air", "requires_deflicker", "trimmed_sec",
               "golden_window", "scored_by", "vlm"}


def _result():
    a = scene(0, "a_roll", q=0.9)
    a.metadata.update({"is_long_shot": True, "defects_count": 2, "scored_by": "mobilenet",
                       "golden": {"source_in": 0.5, "source_out": 3.5, "trimmed_sec": 1.0,
                                  "requires_deflicker": True, "defects": []},
                       "golden_window": {"span": [0.0, 4.0], "window": [0.5, 3.5]},
                       "vlm_description": "a man talks", "vlm_tags": ["talk"], "vlm_mood": "calm"})
    b = scene(1, "b_roll", q=0.4)
    b.metadata["has_dead_air"] = True
    return pipeline_result([a, b], timelines={"rough_cut": timeline("rough_cut", "rough_cut")})


def _session(tmp_path: Path, store: SessionStore | None = None):
    sess, _ = curation.run_curation(FakeAdapter(_result()), tmp_path, None, tmp_path / "out")
    if store is not None:
        store.put(sess)
    return sess


def test_session_details_are_opt_in(tmp_path: Path):
    sess = _session(tmp_path)
    assert set(session_to_dict(sess)) == {"session_id", "input_dir", "takes", "shots"}
    d = session_to_dict(sess, detail=True)
    assert set(d) == {"session_id", "input_dir", "takes", "shots", "details"}
    assert set(d["details"]) == {"A01", "B01"}
    a, b = d["details"]["A01"], d["details"]["B01"]
    assert set(a) == DETAIL_KEYS and set(b) == DETAIL_KEYS
    assert (a["is_long_shot"], a["defects_count"], a["requires_deflicker"], a["trimmed_sec"]) == (True, 2, True, 1.0)
    assert a["golden_window"] == {"span": [0.0, 4.0], "window": [0.5, 3.5]} and a["scored_by"] == "mobilenet"
    assert a["vlm"] == {"description": "a man talks", "tags": ["talk"], "mood": "calm", "pacing": None,
                        "key_moment": None}
    assert (b["has_dead_air"], b["is_long_shot"], b["defects_count"], b["trimmed_sec"]) == (True, False, 0, 0.0)
    assert b["vlm"] is None and b["golden_window"] is None
    json.dumps(d)


def test_session_details_over_http(tmp_path: Path):
    store = SessionStore()
    sess = _session(tmp_path, store)
    c = TestClient(create_app(token=TOKEN, runners={}, sessions=store))
    plain = c.get(f"/v1/sessions/{sess.session_id}", headers=H).json()
    assert "details" not in plain
    full = c.get(f"/v1/sessions/{sess.session_id}?detail=1", headers=H).json()
    assert full["details"]["B01"]["has_dead_air"] is True


def test_apply_config_overrides_validates_and_copies():
    base = PipelineConfig()
    cfg = apply_config_overrides(base, {"enable_ken_burns": False, "teaser_duration": 4.0,
                                        "duration_mode": "full_track"}, stage="assemble")
    assert (cfg.enable_ken_burns, cfg.teaser_duration, cfg.duration_mode) == (False, 4.0, "full_track")
    assert base.enable_ken_burns is True and base.duration_mode == "fixed"
    cur = apply_config_overrides(base, {"min_take_duration_sec": 2.0, "enable_shake_pruning": False},
                                 stage="curate")
    assert (cur.min_take_duration_sec, cur.enable_shake_pruning) == (2.0, False)
    for bad in ({"no_such_option": 1}, {"enable_ken_burns": "no"}, {"teaser_duration": 100.0},
                {"duration_mode": "forever"}, {"min_take_duration_sec": 2.0}):
        with pytest.raises(ValueError):
            apply_config_overrides(base, bad, stage="assemble")
    assert apply_config_overrides(base, {}, stage="curate") == base


def test_presets_publish_advanced_options():
    c = TestClient(create_app(token=TOKEN, runners={}))
    adv = c.get("/v1/presets", headers=H).json()["advanced"]
    assert set(adv) == {"curate", "assemble"}
    for stage, items in adv.items():
        keys = {i["key"] for i in items}
        assert keys == {k for k, spec in ADVANCED_OPTIONS.items() if stage in spec["stages"]}
        for i in items:
            assert set(i) >= {"key", "type", "default"}
            assert i["type"] in {"bool", "float", "enum"}
            assert i["default"] == getattr(PipelineConfig(), i["key"])


def test_runners_apply_overrides_before_submitting(tmp_path: Path):
    store = SessionStore()
    curate_adapter = FakeAdapter(_result())
    make_curate_runner(store, adapter_factory=lambda: curate_adapter)(
        {"input_dir": str(tmp_path), "workspace": str(tmp_path / "ws"),
         "config_overrides": {"min_take_duration_sec": 2.0}}, lambda e: None, threading.Event())
    assert curate_adapter.calls[-1][1]["config"].min_take_duration_sec == 2.0

    sess = _session(tmp_path, store)
    song = tmp_path / "song.wav"
    song.write_bytes(b"x")
    asm = FakeAdapter(pipeline_result([], timelines={"master": timeline("master", "music")}))
    make_assemble_runner(store, adapter_factory=lambda: asm)(
        {"session_id": sess.session_id, "workspace": str(tmp_path / "ws"), "soundtrack": str(song),
         "config_overrides": {"enable_ken_burns": False}}, lambda e: None, threading.Event())
    assert asm.calls[-1][1]["config"].enable_ken_burns is False

    untouched = FakeAdapter()
    with pytest.raises(ValueError):
        make_assemble_runner(store, adapter_factory=lambda: untouched)(
            {"session_id": sess.session_id, "workspace": str(tmp_path / "ws"), "soundtrack": str(song),
             "config_overrides": {"enable_ken_burns": "nope"}}, lambda e: None, threading.Event())
    assert untouched.calls == []


def test_ai_settings_store_never_reveals_the_key(tmp_path: Path):
    path = tmp_path / "ai.json"
    store = AiSettingsStore(path)
    assert store.get() == {"provider": "auto", "base_url": None, "model": "deepseek/deepseek-chat",
                           "has_api_key": False}
    store.set({"provider": "openai", "model": "gpt-4o", "api_key": "sk-secret"})
    again = AiSettingsStore(path)
    assert again.get() == {"provider": "openai", "base_url": None, "model": "gpt-4o", "has_api_key": True}
    assert "sk-secret" not in json.dumps(again.get())
    assert again.secret() == "sk-secret"
    again.set({"api_key": ""})
    assert again.get()["has_api_key"] is False and again.get()["provider"] == "openai"
    with pytest.raises(ValueError):
        again.set({"provider": "skynet"})
    with pytest.raises(ValueError):
        again.set({"temperature": 2})


def test_ai_settings_over_http_and_agent_config(tmp_path: Path):
    store = AiSettingsStore(tmp_path / "ai.json")
    c = TestClient(create_app(token=TOKEN, runners={}, ai_settings=store))
    assert c.get("/v1/settings/ai").status_code == 401
    r = c.put("/v1/settings/ai", headers=H, json={"provider": "deepseek", "api_key": "dk-1"})
    assert r.status_code == 200 and r.json()["has_api_key"] is True and "dk-1" not in r.text
    assert c.put("/v1/settings/ai", headers=H, json={"provider": "x"}).status_code == 400
    assert c.put("/v1/settings/ai", headers=H, json=[1]).status_code == 400
    assert c.get("/v1/settings/ai", headers=H).json()["provider"] == "deepseek"

    cfg = agent_config_from_settings(store)
    assert (cfg.agent_provider, cfg.agent_api_key, cfg.agent_model) == ("deepseek", "dk-1", "deepseek/deepseek-chat")
