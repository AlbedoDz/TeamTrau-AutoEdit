"""P5.1: engine CI workflow contract (triggers, OS matrix, CPU torch, ffmpeg, ruff + pytest)."""

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "engine-ci.yml"


def _load() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def _run_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def test_triggers_linux_on_push_and_all_os_on_pr_or_manual():
    wf = _load()
    on = wf["on"]
    assert set(on["push"]["branches"]) == {"main", "feature/studio-engine"}
    assert on["pull_request"]["branches"] == ["main"]
    assert "workflow_dispatch" in on
    job = wf["jobs"]["test"]
    expr = job["strategy"]["matrix"]["os"]
    m = re.fullmatch(r"\$\{\{\s*fromJSON\((.*)\)\s*\}\}", expr.strip())
    assert m, expr
    assert "github.event_name == 'push'" in m.group(1)
    assert "'[\"ubuntu-latest\"]'" in m.group(1)
    for os_name in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert os_name in m.group(1)
    assert job["strategy"]["fail-fast"] is False
    assert job["timeout-minutes"] <= 60
    assert wf["concurrency"]["cancel-in-progress"] is True


def test_installs_cpu_torch_at_the_locked_version():
    job = _load()["jobs"]["test"]
    text = _run_text(job)
    assert "uv sync --locked --no-install-package torch --no-install-package torchvision" in text
    assert "https://download.pytorch.org/whl/cpu" in text
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions = {
        p["name"]: p["version"].split("+")[0]
        for p in lock["package"]
        if p["name"] in ("torch", "torchvision")
    }
    assert f'"torch=={versions["torch"]}"' in text
    assert f'"torchvision=={versions["torchvision"]}"' in text


def test_ffmpeg_on_every_os_and_headless_qt():
    job = _load()["jobs"]["test"]
    text = _run_text(job)
    assert "apt-get install" in text and "ffmpeg" in text and "libegl1" in text
    assert "brew install ffmpeg" in text
    assert "choco install ffmpeg" in text
    assert job["env"]["QT_QPA_PLATFORM"] == "offscreen"


def test_runs_ruff_and_full_pytest_without_resyncing():
    text = _run_text(_load()["jobs"]["test"])
    assert "uv run --no-sync ruff check src tests main.py" in text
    assert re.search(r"uv run --no-sync pytest tests\b", text)
