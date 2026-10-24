"""HTTP service application and uvicorn configuration for TeamTrau Engine API."""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from src.core.platform_caps import PlatformCaps, get_caps
from src.engine_api.jobs import JobRegistry, JobRunner
from src.engine_api.runners import default_runners

logger = logging.getLogger(__name__)


def _tuples_to_lists(val: Any) -> Any:
    """Recursively convert tuples to lists within nested collections."""
    if isinstance(val, tuple):
        return [_tuples_to_lists(x) for x in val]
    if isinstance(val, list):
        return [_tuples_to_lists(x) for x in val]
    if isinstance(val, dict):
        return {k: _tuples_to_lists(v) for k, v in val.items()}
    return val


class _AuthMiddleware:
    """Enforce Bearer token authentication on all routes except /v1/health."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.rstrip("/") != "/v1/health":
                headers = dict(scope.get("headers", []))
                auth = headers.get(b"authorization", b"").decode("latin-1").strip()
                if not hmac.compare_digest(auth, f"Bearer {self.token}"):
                    res = JSONResponse({"error": "unauthorized"}, status_code=401)
                    await res(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def create_app(
    token: str,
    *,
    runners: Mapping[str, JobRunner] | None = None,
    caps_fn: Callable[[], PlatformCaps] = get_caps,
    registry: JobRegistry | None = None,
    max_body_bytes: int = 1_000_000,
) -> Starlette:
    """Create and configure Starlette Engine API service application.

    Args:
        token: Bearer authentication token.
        runners: Mapping from job kind to runner callable (defaults to default_runners()).
        caps_fn: Callable returning PlatformCaps (defaults to get_caps).
        registry: JobRegistry instance (created if None).
        max_body_bytes: Maximum allowed request body size in bytes (default: 1 MB).

    Returns:
        Configured Starlette application instance.
    """
    if not token or not token.strip():
        raise ValueError("token must not be empty or whitespace")

    _runners: Mapping[str, JobRunner] = default_runners() if runners is None else runners
    _registry: JobRegistry = JobRegistry() if registry is None else registry

    async def health_endpoint(request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": "1"})

    async def capabilities_endpoint(request: Request) -> Response:
        caps = caps_fn()
        d = _tuples_to_lists(dataclasses.asdict(caps))
        return JSONResponse(d)

    async def submit_job_endpoint(request: Request) -> Response:
        kind = request.path_params.get("kind", "")
        if kind not in _runners:
            return JSONResponse({"error": f"unknown job kind: {kind}"}, status_code=404)

        cl_str = request.headers.get("content-length")
        if cl_str is not None:
            try:
                if int(cl_str) > max_body_bytes:
                    return JSONResponse({"error": "request body too large"}, status_code=413)
            except ValueError:
                pass

        body = await request.body()
        if len(body) > max_body_bytes:
            return JSONResponse({"error": "request body too large"}, status_code=413)

        try:
            params = json.loads(body.decode("utf-8"))
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        if not isinstance(params, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        job_id = _registry.submit(kind, _runners[kind], params)
        return JSONResponse({"job_id": job_id}, status_code=202)

    async def get_job_endpoint(request: Request) -> Response:
        job_id = request.path_params.get("id", "")
        job = _registry.get(job_id)
        if job is None:
            return JSONResponse({"error": "job not found"}, status_code=404)
        data = {k: v for k, v in job.items() if k != "events"}
        return JSONResponse(data)

    async def get_job_events_endpoint(request: Request) -> Response:
        job_id = request.path_params.get("id", "")
        job = _registry.get(job_id)
        if job is None:
            return JSONResponse({"error": "job not found"}, status_code=404)

        if request.query_params.get("format") == "json":
            return JSONResponse({"events": job.get("events", [])})

        async def sse_stream() -> AsyncIterator[str]:
            idx = 0
            while True:
                cur = _registry.get(job_id)
                if cur is None:
                    break
                events = cur.get("events", [])
                while idx < len(events):
                    ev = events[idx]
                    ev_type = ev.get("type", "message")
                    ev_data = json.dumps(ev)
                    yield f"event: {ev_type}\ndata: {ev_data}\n\n"
                    idx += 1
                if cur.get("status") != "running" and idx >= len(events):
                    break
                await asyncio.sleep(0.01)

        return StreamingResponse(
            sse_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def cancel_job_endpoint(request: Request) -> Response:
        job_id = request.path_params.get("id", "")
        job = _registry.get(job_id)
        if job is None:
            return JSONResponse({"error": "job not found"}, status_code=404)
        cancelled = _registry.cancel(job_id)
        return JSONResponse({"cancelled": cancelled})

    routes = [
        Route("/v1/health", health_endpoint, methods=["GET"]),
        Route("/v1/capabilities", capabilities_endpoint, methods=["GET"]),
        Route("/v1/jobs/{kind}", submit_job_endpoint, methods=["POST"]),
        Route("/v1/jobs/{id}/events", get_job_events_endpoint, methods=["GET"]),
        Route("/v1/jobs/{id}/cancel", cancel_job_endpoint, methods=["POST"]),
        Route("/v1/jobs/{id}", get_job_endpoint, methods=["GET"]),
    ]

    return Starlette(
        routes=routes,
        middleware=[Middleware(_AuthMiddleware, token=token)],
    )


def build_server_config(app: Any, port: int) -> uvicorn.Config:
    """Build uvicorn server configuration bound strictly to loopback 127.0.0.1.

    Args:
        app: ASGI application (Starlette).
        port: Listening port integer.

    Returns:
        Configured uvicorn.Config.
    """
    return uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="info")
