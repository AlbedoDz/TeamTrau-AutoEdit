"""Default job runners for TeamTrau Engine API (proxies, render_cache, curate, classify, soundtrack, preflight, assemble)."""

from __future__ import annotations

import dataclasses
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from src.core import curation
from src.core.nle_preflight import preflight_timeline
from src.core.project_io import audio_state_to_dict
from src.core.service import (
    JobCallbacks,
    StudioBackendAdapter,
    collect_video_files,
    find_soundtrack,
)
from src.engine_api.jobs import JobRunner
from src.engine_api.sessions import SessionStore, session_to_dict
from src.integrations.freecut_reader import read_freecut_project
from src.integrations.freecut_writer import write_freecut_project
from src.modules.media_classifier import ClassificationResult, classify_batch
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


def make_curate_runner(
    sessions: SessionStore,
    adapter_factory: Callable[[], Any] = StudioBackendAdapter,
) -> JobRunner:
    """Create a curate job runner backed by a shared SessionStore and adapter factory."""

    def runner(
        params: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        input_dir = Path(params["input_dir"])
        workspace = Path(params["workspace"])
        genre = params.get("genre")
        top_n = int(params.get("top_n", 12))
        project_name = str(params.get("project_name", "Curated Rough Cut"))
        out_dir = Path(params.get("output_dir", workspace / ".curation_tmp"))

        def _on_progress(stage_pct: float, total_pct: float, msg: str) -> None:
            emit({"type": "progress", "pct": total_pct, "msg": msg})

        callbacks = JobCallbacks(on_progress=_on_progress)
        adapter = adapter_factory()

        session, summary = curation.run_curation(
            adapter=adapter,
            input_dir=input_dir,
            genre=genre,
            output_dir=out_dir,
            top_n=top_n,
            callbacks=callbacks,
            cancel_event=cancel_event,
        )

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

        sessions.put(session)
        shots = session_to_dict(session)["shots"]

        return {
            "session_id": session.session_id,
            "project_id": project_id,
            "summary": summary,
            "shots": shots,
        }

    return runner


def run_curate(
    params: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Execute rough-cut curation and write FreeCut workspace project."""
    runner = make_curate_runner(SessionStore())
    return runner(params, emit, cancel_event)


def make_classify_runner(
    classify_fn: Callable[..., list[ClassificationResult]] | None = None,
    chunk_size: int = 4,
) -> JobRunner:
    """Create a classify job runner that scans input_dir and categorizes media clips."""

    def runner(
        params: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        input_dir = Path(params["input_dir"])
        if not input_dir.is_dir():
            raise FileNotFoundError(
                f"Input directory does not exist or is not a directory: {input_dir}"
            )

        collected, skipped = collect_video_files(input_dir)
        collected = sorted(collected)

        fn = classify_fn if classify_fn is not None else classify_batch

        results: list[ClassificationResult] = []
        total = len(collected)
        done = 0

        for i in range(0, total, chunk_size):
            if cancel_event.is_set():
                raise InterruptedError("Classification cancelled.")
            chunk = collected[i : i + chunk_size]
            res = fn(chunk, max_workers=chunk_size)
            results.extend(res)
            done += len(chunk)
            pct = 100.0 if total == 0 else (100.0 * done / total)
            emit({"type": "progress", "pct": pct})

        files_list: list[dict[str, Any]] = []
        for r in results:
            files_list.append(
                {
                    "path": str(r.path),
                    "role": r.role.value if hasattr(r.role, "value") else str(r.role),
                    "confidence": float(r.confidence),
                    "has_speech": bool(r.has_speech),
                    "reasons": list(r.reasons),
                    "error": str(r.error) if r.error is not None else None,
                    "signals": dataclasses.asdict(r.signals) if r.signals is not None else None,
                }
            )

        st = find_soundtrack(input_dir)

        return {
            "files": files_list,
            "skipped": [str(p) for p in skipped],
            "soundtrack": str(st) if st is not None else None,
        }

    return runner


def make_soundtrack_runner(
    analyze_fn: Callable[[Path], Any] | None = None,
) -> JobRunner:
    """Create a soundtrack analysis job runner."""

    def runner(
        params: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        p = Path(params["path"])
        if not p.is_file():
            raise FileNotFoundError(f"Soundtrack file does not exist: {p}")
        if cancel_event.is_set():
            raise InterruptedError("Soundtrack analysis cancelled.")
        fn = analyze_fn if analyze_fn is not None else StudioBackendAdapter().quick_analyze_audio
        analysis = fn(p)
        return {"path": str(p), **audio_state_to_dict(analysis)}

    return runner


def make_preflight_runner() -> JobRunner:
    """Create a preflight job runner that checks timeline media references."""

    def runner(
        params: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        workspace = Path(params["workspace"])
        project_id = str(params["project_id"])
        if cancel_event.is_set():
            raise InterruptedError("Preflight cancelled.")
        freecut_proj = read_freecut_project(workspace, project_id)
        report = preflight_timeline(freecut_proj.timeline)
        return report.to_dict()

    return runner


def make_assemble_runner(
    sessions: SessionStore,
    adapter_factory: Callable[[], Any] = StudioBackendAdapter,
) -> JobRunner:
    """Create an assemble job runner that generates a timeline sequence from session shots."""

    def runner(
        params: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        session_id = str(params["session_id"])
        session = sessions.get(session_id)
        if session is None:
            raise KeyError(f"Curation session not found: {session_id}")

        workspace = Path(params["workspace"])
        soundtrack = Path(params["soundtrack"]) if params.get("soundtrack") else None
        cadence = params.get("cadence")
        selected_ids = params.get("selected_ids")
        project_name = str(params.get("project_name", "Assembled Sequence"))

        def _on_progress(stage_pct: float, total_pct: float, msg: str) -> None:
            emit({"type": "progress", "pct": total_pct, "msg": msg})

        callbacks = JobCallbacks(on_progress=_on_progress)
        adapter = adapter_factory()

        assemble_res = curation.assemble_sequence(
            adapter=adapter,
            session=session,
            soundtrack=soundtrack,
            selected_ids=selected_ids,
            cadence=cadence,
            callbacks=callbacks,
            cancel_event=cancel_event,
        )

        timeline_id = assemble_res["timeline_id"]
        tl_key = timeline_id.split(":", 1)[1] if ":" in timeline_id else timeline_id

        if (
            session.sequence is None
            or not session.sequence.edit_timelines
            or tl_key not in session.sequence.edit_timelines
        ):
            raise RuntimeError(f"Assembled timeline '{tl_key}' not found in sequence result.")

        tl = session.sequence.edit_timelines[tl_key]
        project_id = write_freecut_project(
            timeline=tl,
            workspace=workspace,
            project_name=project_name,
        )

        return {
            "project_id": project_id,
            "timeline_id": timeline_id,
            "total_cuts": assemble_res["total_cuts"],
            "duration_sec": assemble_res["duration_sec"],
        }

    return runner


def default_runners(sessions: SessionStore | None = None) -> dict[str, JobRunner]:
    """Return dictionary of default engine job runners."""
    store = sessions if sessions is not None else SessionStore()
    return {
        "proxies": run_proxies,
        "render_cache": run_render_cache,
        "curate": make_curate_runner(store),
        "classify": make_classify_runner(),
        "analyze_soundtrack": make_soundtrack_runner(),
        "preflight": make_preflight_runner(),
        "assemble": make_assemble_runner(store),
    }
