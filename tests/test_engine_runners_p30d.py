"""P3.0d: deliver and agent job runners for the Studio panels."""

import threading
import time
from pathlib import Path

import pytest

from src.core.types import DeliverResult
from src.engine_api.runners import make_agent_runner, make_deliver_runner
from src.integrations.freecut_writer import write_freecut_project
from src.modules.agent_controller import AgentResult, AgentStep
from tests.curation_fakes import timeline

RESULT_KEYS = {"success", "output_dir", "render_path", "encoder_used", "fcpxml_path",
               "premiere_xml_path", "otio_path", "capcut_draft_path", "manifest_path",
               "preflight_path", "preflight_issues"}


class _Handle:
    def __init__(self, result: DeliverResult, slow: bool = False, cb=None):
        self.cancelled = False
        self._r = result
        self._done = threading.Event()
        self._cancel = threading.Event()

        def work() -> None:
            for i in range(50 if slow else 3):
                if self._cancel.is_set():
                    break
                if cb is not None and cb.on_progress is not None:
                    cb.on_progress(i * 2.0, i * 2.0, "render")
                time.sleep(0.02 if slow else 0)
            self._done.set()

        threading.Thread(target=work, daemon=True).start()

    def wait(self, timeout=None):
        return self._done.wait(timeout)

    def cancel(self) -> bool:
        self.cancelled = True
        self._cancel.set()
        return True

    @property
    def result(self):
        return self._r


class _Adapter:
    def __init__(self, result: DeliverResult, slow: bool = False):
        self.result = result
        self.slow = slow
        self.kw: dict = {}
        self.handle: _Handle | None = None

    def submit_deliver_job(self, **kw):
        self.kw = kw
        self.handle = _Handle(self.result, self.slow, kw.get("callbacks"))
        return self.handle


def _project(tmp_path: Path) -> tuple[Path, str]:
    ws = tmp_path / "ws"
    return ws, write_freecut_project(timeline(), ws, "Deliver")


def test_deliver_runner_reads_project_and_builds_settings(tmp_path: Path):
    ws, pid = _project(tmp_path)
    out_dir = tmp_path / "out"
    res = DeliverResult(success=True, output_dir=out_dir, render_path=out_dir / "m.mp4",
                        encoder_used="h264_nvenc", fcpxml_path=out_dir / "m.fcpxml",
                        preflight_issues=["warning:AUDIO_MONO:c.mp4"])
    adapter = _Adapter(res)
    events: list[dict] = []
    out = make_deliver_runner(adapter_factory=lambda: adapter)(
        {"workspace": str(ws), "project_id": pid, "output_dir": str(out_dir),
         "formats": ["mp4", "fcpxml"], "settings": {"master": "h264", "fit": "fill"}},
        events.append, threading.Event())
    kw = adapter.kw
    assert len(kw["edit_timeline"].clips) == 1
    assert Path(kw["output_dir"]) == out_dir
    assert kw["formats"] == {"mp4", "fcpxml"}
    assert kw["settings"].master == "h264" and kw["settings"].fit_mode == "fill"
    assert kw["run_async"] is True and kw["preview"] is False
    assert set(out) == RESULT_KEYS
    assert out["success"] is True and out["encoder_used"] == "h264_nvenc"
    assert out["render_path"] == str(out_dir / "m.mp4") and out["premiere_xml_path"] is None
    assert out["preflight_issues"] == ["warning:AUDIO_MONO:c.mp4"]
    assert [e for e in events if e.get("type") == "progress"]


def test_deliver_runner_validates_before_submitting(tmp_path: Path):
    ws, pid = _project(tmp_path)
    adapter = _Adapter(DeliverResult(success=True))
    run = make_deliver_runner(adapter_factory=lambda: adapter)
    base = {"workspace": str(ws), "project_id": pid, "output_dir": str(tmp_path / "o")}
    with pytest.raises(ValueError):
        run({**base, "formats": ["mp4", "gif"]}, lambda e: None, threading.Event())
    with pytest.raises(ValueError):
        run({**base, "settings": {"master": "xyz"}}, lambda e: None, threading.Event())
    with pytest.raises(ValueError):
        run({**base, "settings": {"bitrate": 5}}, lambda e: None, threading.Event())
    assert adapter.kw == {}


def test_deliver_runner_raises_on_failed_result(tmp_path: Path):
    ws, pid = _project(tmp_path)
    adapter = _Adapter(DeliverResult(success=False, error_message="NVENC exploded"))
    with pytest.raises(RuntimeError, match="NVENC exploded"):
        make_deliver_runner(adapter_factory=lambda: adapter)(
            {"workspace": str(ws), "project_id": pid, "output_dir": str(tmp_path / "o")},
            lambda e: None, threading.Event())


def test_deliver_runner_cancels(tmp_path: Path):
    ws, pid = _project(tmp_path)
    adapter = _Adapter(DeliverResult(success=True), slow=True)
    ev = threading.Event()
    box: dict = {}

    def go() -> None:
        try:
            make_deliver_runner(adapter_factory=lambda: adapter)(
                {"workspace": str(ws), "project_id": pid, "output_dir": str(tmp_path / "o")},
                lambda e: None, ev)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            box["exc"] = exc

    th = threading.Thread(target=go)
    t0 = time.monotonic()
    th.start()
    time.sleep(0.1)
    ev.set()
    th.join(5)
    assert isinstance(box.get("exc"), InterruptedError)
    assert adapter.handle is not None and adapter.handle.cancelled
    assert time.monotonic() - t0 < 2.0


class _Controller:
    def __init__(self):
        self.seen: dict = {}

    def run(self, prompt, callbacks=None, cancel_event=None, system_prompt=None):
        self.seen = {"prompt": prompt, "cancel_event": cancel_event, "system_prompt": system_prompt}
        callbacks.on_thought("plan the edit")
        callbacks.on_tool_call("curate_footage", {"input_dir": "D:/f"})
        callbacks.on_tool_result("curate_footage", {"takes": 3, "blob": "x" * 5000})
        callbacks.on_agent_response("done")
        return AgentResult(success=True, response="done", steps=[AgentStep(step_number=1)],
                           provider_used="heuristic", model_used="heuristic")


def test_agent_runner_streams_events_and_returns_summary():
    ctl = _Controller()
    ev = threading.Event()
    events: list[dict] = []
    out = make_agent_runner(controller_factory=lambda: ctl)(
        {"prompt": "Dựng video dọc 9:16", "system_prompt": "be brief"}, events.append, ev)
    assert ctl.seen == {"prompt": "Dựng video dọc 9:16", "cancel_event": ev, "system_prompt": "be brief"}
    assert [e["type"] for e in events] == ["thought", "tool_call", "tool_result", "response"]
    assert events[0] == {"type": "thought", "text": "plan the edit"}
    assert events[1] == {"type": "tool_call", "name": "curate_footage", "args": {"input_dir": "D:/f"}}
    assert events[2]["name"] == "curate_footage" and isinstance(events[2]["result"], str)
    assert len(events[2]["result"]) <= 2000
    assert events[3] == {"type": "response", "text": "done"}
    assert out == {"success": True, "response": "done", "provider": "heuristic",
                   "model": "heuristic", "steps": 1, "error": None}


def test_agent_runner_rejects_empty_prompt():
    with pytest.raises(ValueError):
        make_agent_runner(controller_factory=_Controller)({"prompt": "  "}, lambda e: None,
                                                          threading.Event())
