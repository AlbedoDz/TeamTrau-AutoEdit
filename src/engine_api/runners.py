"""Default job runners for TeamTrau Engine API (proxies, render_cache, curate)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from src.core import curation
from src.core.service import StudioBackendAdapter
from src.engine_api.jobs import JobRunner
from src.integrations.freecut_reader import read_freecut_project
from src.integrations.freecut_writer import write_freecut_project
from src.modules.proxy_generator import ProxySpec, make_proxy
from src.modules.render_cache import render_missing, write_manifest

logger = logging.getLogger(__name__)


def run_proxies(
    params: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Generate production proxies for source media files.

    Params:
        paths: List of file path strings.
        out_dir: Output directory string or Path.
        height: Optional proxy height (default: 1080).
        gop: Optional GOP size (default: 1 for all-intra).

    Returns:
        {"proxies": {src: proxy_path}}
    """
    paths = params.get("paths", [])
    out_dir = Path(params["out_dir"])
    height = int(params.get("height", 1080))
    gop = int(params.get("gop", 1))
    spec = ProxySpec(height=height, gop=gop)

    proxies: dict[str, str] = {}
    total = len(paths)
    for i, p_str in enumerate(paths):
        if cancel_event.is_set():
            raise InterruptedError("Proxy generation cancelled.")
        p = Path(p_str)
        pct = round((i / max(total, 1)) * 100.0, 1)
        emit({"type": "progress", "pct": pct, "src": str(p), "current": i, "total": total})
        proxy_path = make_proxy(p, out_dir, spec=spec)
        proxies[str(p)] = str(proxy_path)

    emit({"type": "progress", "pct": 100.0, "current": total, "total": total})
    return {"proxies": proxies}


def run_render_cache(
    params: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Render missing timeline cache segments and write cache manifest.

    Params:
        workspace: Path to FreeCut workspace directory.
        project_id: FreeCut project identifier.
        cache_dir: Optional directory to store cache segment files and manifest
            (defaults to workspace/.teamtrau/render-cache/project_id).
        max_segment_sec: Optional max segment length in seconds (default: 10.0).

    Returns:
        {"manifest": manifest_path, "cache_dir": cache_dir_path, "rendered": count}
    """
    workspace = Path(params["workspace"])
    project_id = str(params["project_id"])
    if params.get("cache_dir"):
        cache_dir = Path(params["cache_dir"])
    else:
        cache_dir = workspace / ".teamtrau" / "render-cache" / project_id
    max_segment_sec = float(params.get("max_segment_sec", 10.0))

    freecut_tl = read_freecut_project(workspace, project_id)

    def on_progress(pct: float, msg: str) -> None:
        emit({"type": "progress", "pct": pct, "msg": msg})

    rendered = render_missing(
        freecut_tl.timeline,
        cache_dir,
        starts=freecut_tl.starts,
        max_segment_sec=max_segment_sec,
        progress=on_progress,
        cancel_event=cancel_event,
    )

    manifest_path = write_manifest(
        freecut_tl.timeline,
        cache_dir,
        starts=freecut_tl.starts,
        max_segment_sec=max_segment_sec,
        item_sigs=freecut_tl.items,
    )

    return {
        "manifest": str(manifest_path),
        "cache_dir": str(cache_dir),
        "rendered": len(rendered),
    }


def run_curate(
    params: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Execute rough-cut curation and write FreeCut workspace project.

    Params:
        input_dir: Footage directory.
        genre: Optional music/cadence genre profile.
        workspace: FreeCut workspace directory.
        top_n: Optional number of top selects (default: 12).
        project_name: Optional FreeCut project name.
        output_dir: Optional intermediate output directory.

    Returns:
        {"project_id": project_id, "summary": summary}
    """
    input_dir = Path(params["input_dir"])
    genre = params.get("genre")
    workspace = Path(params["workspace"])
    top_n = int(params.get("top_n", 12))
    project_name = str(params.get("project_name", "Curated Rough Cut"))
    out_dir = Path(params.get("output_dir", workspace / ".curation_tmp"))

    adapter = StudioBackendAdapter()
    session, summary = curation.run_curation(
        adapter=adapter,
        input_dir=input_dir,
        genre=genre,
        output_dir=out_dir,
        top_n=top_n,
    )
    if cancel_event.is_set():
        raise InterruptedError("Curation cancelled.")

    rough_tl = None
    if session.result and session.result.edit_timelines:
        rough_tl = (
            session.result.edit_timelines.get("rough_cut")
            or session.result.edit_timelines.get("master")
        )
    if rough_tl is None:
        raise RuntimeError("No rough-cut timeline produced by curation.")

    project_id = write_freecut_project(
        timeline=rough_tl,
        workspace=workspace,
        project_name=project_name,
    )
    return {
        "project_id": project_id,
        "summary": summary,
    }


def default_runners() -> dict[str, JobRunner]:
    """Return dictionary of default engine job runners."""
    return {
        "proxies": run_proxies,
        "render_cache": run_render_cache,
        "curate": run_curate,
    }
