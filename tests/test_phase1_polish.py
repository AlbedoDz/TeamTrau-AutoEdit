"""Phase 1 review polish: failure paths of probe, writer, reader, engine API, visual scorer."""

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.core.config import PipelineConfig
from src.core.platform_caps import probe
from src.core.types import EditClip, EditTimeline
from src.engine_api.app import create_app
from src.engine_api.jobs import JobRegistry
from src.integrations.freecut_reader import read_freecut_project
from src.integrations.freecut_writer import write_freecut_project


def _tl(path: Path) -> EditTimeline:
    clip = EditClip(source_path=path, source_in=0.0, frames=30, available=1.0)
    return EditTimeline(name="t", fps=30.0, canvas_width=1920, canvas_height=1080, clips=[clip])


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired(cmd="ffmpeg", timeout=15),
                                 OSError("ffmpeg vanished")])
def test_probe_survives_ffmpeg_list_failure(exc):
    def boom(*_a, **_k):
        raise exc

    caps = probe(ffmpeg="ffmpeg", os_name="linux", run=boom, smoke_test=False,
                 torch_device="cpu")
    assert caps.h264_encoders == () and caps.hevc_encoders == () and caps.hwaccels == ()
    assert caps.best_h264() is None


def test_corrupt_index_leaves_no_orphan_project_dir(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "index.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        write_freecut_project(_tl(tmp_path / "a.mp4"), ws, "x")
    projects = ws / "projects"
    assert not projects.exists() or not any(projects.iterdir())


@pytest.mark.parametrize("name", ["source.link.json", "metadata.json"])
def test_reader_rejects_corrupt_media_json(tmp_path: Path, name: str):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"not really video")
    ws = tmp_path / "ws"
    pid = write_freecut_project(_tl(src), ws, "x")
    proj = json.loads((ws / "projects" / pid / "project.json").read_text(encoding="utf-8"))
    mid = next(i["mediaId"] for i in proj["timeline"]["items"] if i["type"] == "video")
    if name == "metadata.json":
        (ws / "media" / mid / "source.link.json").unlink()
    (ws / "media" / mid / name).write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError, match=mid):
        read_freecut_project(ws, pid)


def test_create_app_rejects_empty_token():
    with pytest.raises(ValueError):
        create_app("", runners={})
    with pytest.raises(ValueError):
        create_app("   ", runners={})


def test_serve_rejects_empty_token_file(tmp_path: Path, monkeypatch):
    import uvicorn

    from main import cli

    started: list[bool] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: started.append(True))
    tok = tmp_path / "token"
    tok.write_text("  \n", encoding="utf-8")
    res = CliRunner().invoke(cli, ["serve", "--port", "8798", "--token-file", str(tok)])
    assert res.exit_code != 0
    assert started == []


def _wait_done(reg: JobRegistry, job_id: str, timeout: float = 10.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        job = reg.get(job_id)
        if job is not None and job["status"] != "running":
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def test_registry_evicts_oldest_finished_jobs():
    reg = JobRegistry(max_finished=2)
    ok = lambda params, emit, cancel: {"ok": True}  # noqa: E731
    ids = []
    for _ in range(3):
        jid = reg.submit("k", ok, {})
        _wait_done(reg, jid)
        ids.append(jid)
    gate = threading.Event()
    running = reg.submit("k", lambda p, e, c: gate.wait(10) and {}, {})
    assert reg.get(ids[0]) is None
    assert reg.get(ids[1]) is not None and reg.get(ids[2]) is not None
    assert reg.get(running)["status"] == "running"
    gate.set()
    _wait_done(reg, running)


def test_visual_scorer_skips_torch_model_without_torch_cuda(monkeypatch):
    import torch

    import src.modules.visual_scorer as vs

    monkeypatch.setattr(vs, "check_cuda_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    scorer = vs.VisualScorer(PipelineConfig())
    assert scorer._torch_model is None
