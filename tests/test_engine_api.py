"""Engine API: auth, capabilities, jobs, events (SSE + JSON), cancel, limits."""

import threading
import time

from starlette.testclient import TestClient

from src.core.platform_caps import PlatformCaps
from src.engine_api.app import build_server_config, create_app

TOKEN = "t0k3n-for-tests"
H = {"Authorization": f"Bearer {TOKEN}"}


def fake_caps() -> PlatformCaps:
    return PlatformCaps(os="windows", ffmpeg="ffmpeg", h264_encoders=("libx264",),
                        hevc_encoders=(), hwaccels=("d3d11va",), torch_device="cpu")


def make_client(runners=None, **kw) -> TestClient:
    return TestClient(create_app(token=TOKEN, runners=runners or {}, caps_fn=fake_caps, **kw))


def wait_done(c: TestClient, job_id: str) -> dict:
    for _ in range(500):
        st = c.get(f"/v1/jobs/{job_id}", headers=H).json()
        if st["status"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_health_needs_no_token():
    r = make_client().get("/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_other_routes_need_token():
    c = make_client()
    assert c.get("/v1/capabilities").status_code == 401
    assert c.get("/v1/capabilities", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.get("/v1/capabilities", headers=H)
    assert r.status_code == 200
    assert r.json()["h264_encoders"] == ["libx264"] and r.json()["os"] == "windows"


def test_job_lifecycle_and_events():
    def runner(params, emit, cancel):
        emit({"type": "progress", "pct": 50.0, "msg": "half"})
        return {"echo": params["x"]}

    c = make_client({"echo": runner})
    r = c.post("/v1/jobs/echo", json={"x": 7}, headers=H)
    assert r.status_code == 202
    jid = r.json()["job_id"]
    st = wait_done(c, jid)
    assert st["status"] == "done" and st["result"] == {"echo": 7} and "events" not in st
    events = c.get(f"/v1/jobs/{jid}/events?format=json", headers=H).json()["events"]
    assert [e["type"] for e in events] == ["progress", "done"]
    assert events[-1]["result"] == {"echo": 7}


def test_sse_stream_ends_after_done():
    def runner(params, emit, cancel):
        emit({"type": "progress", "pct": 100.0, "msg": "all"})
        return {}

    c = make_client({"quick": runner})
    jid = c.post("/v1/jobs/quick", json={}, headers=H).json()["job_id"]
    wait_done(c, jid)
    with c.stream("GET", f"/v1/jobs/{jid}/events", headers=H) as s:
        body = "".join(s.iter_text())
    assert "event: progress" in body and "event: done" in body


def test_failed_job_reports_error():
    def runner(params, emit, cancel):
        raise ValueError("bad input")

    c = make_client({"boom": runner})
    jid = c.post("/v1/jobs/boom", json={}, headers=H).json()["job_id"]
    st = wait_done(c, jid)
    assert st["status"] == "failed" and st["error"] == "bad input"
    events = c.get(f"/v1/jobs/{jid}/events?format=json", headers=H).json()["events"]
    assert events[-1]["type"] == "error"


def test_cancel_running_job():
    started = threading.Event()

    def runner(params, emit, cancel):
        started.set()
        while not cancel.is_set():
            time.sleep(0.01)
        raise InterruptedError("stopped")

    c = make_client({"slow": runner})
    jid = c.post("/v1/jobs/slow", json={}, headers=H).json()["job_id"]
    assert started.wait(5)
    assert c.post(f"/v1/jobs/{jid}/cancel", headers=H).json() == {"cancelled": True}
    st = wait_done(c, jid)
    assert st["status"] == "cancelled"
    events = c.get(f"/v1/jobs/{jid}/events?format=json", headers=H).json()["events"]
    assert events[-1]["type"] == "cancelled"


def test_unknown_kind_and_job():
    c = make_client()
    assert c.post("/v1/jobs/nope", json={}, headers=H).status_code == 404
    assert c.get("/v1/jobs/does-not-exist", headers=H).status_code == 404


def test_body_must_be_a_small_json_object():
    def runner(params, emit, cancel):
        return {}

    c = make_client({"echo": runner}, max_body_bytes=100)
    json_headers = {**H, "Content-Type": "application/json"}
    assert c.post("/v1/jobs/echo", content=b"[1, 2]", headers=json_headers).status_code == 400
    big = {"x": "a" * 1000}
    assert c.post("/v1/jobs/echo", json=big, headers=H).status_code == 413


def test_server_binds_loopback_only():
    cfg = build_server_config(create_app(token=TOKEN, caps_fn=fake_caps), port=8765)
    assert cfg.host == "127.0.0.1" and cfg.port == 8765
