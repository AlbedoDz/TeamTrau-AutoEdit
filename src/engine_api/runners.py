"""Default job runners for TeamTrau Engine API (proxies, render_cache, curate, classify, soundtrack, preflight, assemble, deliver, agent)."""

from __future__ import annotations

import dataclasses
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from src.core import curation
from src.core.config import PipelineConfig
from src.core.deliver_options import build_deliver_settings
from src.core.nle_preflight import preflight_timeline
from src.core.project_io import audio_state_to_dict
from src.core.service import (
    JobCallbacks,
    StudioBackendAdapter,
    collect_video_files,
    find_soundtrack,
)
from src.core.types import ALL_DELIVER_FORMATS, DeliverSettings
from src.engine_api.ai_settings import AiSettingsStore
from src.engine_api.jobs import JobRunner
from src.engine_api.sessions import SessionStore, session_to_dict
from src.integrations.freecut_reader import read_freecut_project
from src.integrations.freecut_writer import write_freecut_project
from src.modules.agent_controller import AgentCallbacks
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
        config_overrides = params.get("config_overrides")

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
            config_overrides=config_overrides,
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
            shots=session.shots,
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
        config_overrides = params.get("config_overrides")

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
            config_overrides=config_overrides,
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
            shots=session.shots,
        )

        return {
            "project_id": project_id,
            "timeline_id": timeline_id,
            "total_cuts": assemble_res["total_cuts"],
            "duration_sec": assemble_res["duration_sec"],
        }

    return runner


def make_deliver_runner(
    adapter_factory: Callable[[], Any] = StudioBackendAdapter,
) -> JobRunner:
    """Create a deliver job runner that exports/renders a FreeCut timeline."""

    def runner(
        params: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        formats_raw = params.get("formats")
        if formats_raw is not None:
            bad_formats = [f for f in formats_raw if f not in ALL_DELIVER_FORMATS]
            if bad_formats:
                raise ValueError(
                    f"Invalid deliver format(s): {bad_formats}; allowed: {ALL_DELIVER_FORMATS}"
                )
            formats: set[Any] | None = set(formats_raw)
        else:
            formats = None

        allowed_settings_keys = {
            "preset",
            "canvas",
            "fps",
            "fit",
            "color",
            "master",
            "subtitles",
            "audio_rate",
            "audio_channels",
            "variants",
            "relink",
            "watermark_in_nle",
        }
        settings_raw = params.get("settings")
        if settings_raw is not None:
            if not isinstance(settings_raw, dict):
                raise ValueError("settings must be a dictionary")
            bad_settings = set(settings_raw.keys()) - allowed_settings_keys
            if bad_settings:
                raise ValueError(
                    f"Invalid setting(s): {bad_settings}; allowed: {allowed_settings_keys}"
                )
            settings_dict = settings_raw
        else:
            settings_dict = {}

        workspace = Path(params["workspace"])
        project_id = str(params["project_id"])
        output_dir = Path(params["output_dir"])
        preview = bool(params.get("preview", False))
        force_cpu = bool(params.get("force_cpu", False))

        freecut_tl = read_freecut_project(workspace, project_id)
        base_settings = DeliverSettings.from_timeline(freecut_tl.timeline)
        deliver_settings = build_deliver_settings(base_settings, **settings_dict)

        def _on_progress(stage_pct: float, total_pct: float, msg: str) -> None:
            emit({"type": "progress", "pct": total_pct, "msg": msg})

        callbacks = JobCallbacks(on_progress=_on_progress)
        adapter = adapter_factory()

        handle = adapter.submit_deliver_job(
            edit_timeline=freecut_tl.timeline,
            output_dir=output_dir,
            config=PipelineConfig(),
            formats=formats,
            callbacks=callbacks,
            run_async=True,
            preview=preview,
            force_cpu=force_cpu,
            settings=deliver_settings,
        )

        while True:
            if cancel_event.is_set():
                handle.cancel()
                raise InterruptedError("Deliver cancelled")
            if handle.wait(0.05):
                break
            if cancel_event.is_set():
                handle.cancel()
                raise InterruptedError("Deliver cancelled")

        result = handle.result
        if getattr(result, "cancelled", False):
            raise InterruptedError("Deliver cancelled")
        if not getattr(result, "success", False):
            err_msg = getattr(result, "error_message", None) or "Deliver failed"
            raise RuntimeError(err_msg)

        return {
            "success": bool(result.success),
            "output_dir": str(result.output_dir) if result.output_dir is not None else None,
            "render_path": str(result.render_path) if result.render_path is not None else None,
            "encoder_used": result.encoder_used,
            "fcpxml_path": str(result.fcpxml_path) if result.fcpxml_path is not None else None,
            "premiere_xml_path": (
                str(result.premiere_xml_path) if result.premiere_xml_path is not None else None
            ),
            "otio_path": str(result.otio_path) if result.otio_path is not None else None,
            "capcut_draft_path": (
                str(result.capcut_draft_path) if result.capcut_draft_path is not None else None
            ),
            "manifest_path": (
                str(result.manifest_path) if result.manifest_path is not None else None
            ),
            "preflight_path": (
                str(result.preflight_path) if result.preflight_path is not None else None
            ),
            "preflight_issues": list(result.preflight_issues or []),
        }

    return runner


def make_agent_runner(
    controller_factory: Callable[[], Any] | None = None,
    ai_settings: AiSettingsStore | None = None,
) -> JobRunner:
    """Create an agent job runner that executes an autonomous reasoning/tool-calling loop."""

    def runner(
        params: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        prompt = params.get("prompt")
        if prompt is None or not str(prompt).strip():
            raise ValueError("prompt must be a non-empty string")
        prompt = str(prompt)
        system_prompt = params.get("system_prompt")

        def _on_thought(text: str) -> None:
            emit({"type": "thought", "text": text})

        def _on_tool_call(name: str, args: dict[str, Any]) -> None:
            emit({"type": "tool_call", "name": name, "args": args})

        def _on_tool_result(name: str, result: Any) -> None:
            emit({"type": "tool_result", "name": name, "result": str(result)[:2000]})

        def _on_agent_response(text: str) -> None:
            emit({"type": "response", "text": text})

        def _on_error(message: str) -> None:
            emit({"type": "error", "message": message})

        callbacks = AgentCallbacks(
            on_thought=_on_thought,
            on_tool_call=_on_tool_call,
            on_tool_result=_on_tool_result,
            on_agent_response=_on_agent_response,
            on_error=_on_error,
        )

        if controller_factory is not None:
            controller = controller_factory()
        else:
            from src.engine_api.ai_settings import agent_config_from_settings
            from src.modules.agent_controller import AutoEditAgentController

            cfg = agent_config_from_settings(ai_settings) if ai_settings is not None else None
            controller = AutoEditAgentController(config=cfg)

        result = controller.run(
            prompt,
            callbacks=callbacks,
            cancel_event=cancel_event,
            system_prompt=system_prompt,
        )

        steps_count = (
            len(result.steps)
            if hasattr(result, "steps") and isinstance(result.steps, list)
            else int(getattr(result, "steps", 0))
        )

        error = getattr(result, "error_message", None) or getattr(result, "error", None)

        return {
            "success": bool(getattr(result, "success", False)),
            "response": getattr(result, "response", ""),
            "provider": getattr(result, "provider_used", None),
            "model": getattr(result, "model_used", None),
            "steps": steps_count,
            "error": error,
        }

    return runner


def default_runners(
    sessions: SessionStore | None = None,
    ai_settings: AiSettingsStore | None = None,
) -> dict[str, JobRunner]:
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
        "deliver": make_deliver_runner(),
        "agent": make_agent_runner(ai_settings=ai_settings),
    }
