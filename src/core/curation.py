"""Auto-curation core and selects stringout pipeline orchestration."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.core.config import PipelineConfig
from src.core.service import JobCallbacks, find_soundtrack
from src.core.stage_options import apply_staged_options
from src.core.types import EditTimeline, PipelineExecutionResult, ScoredScene

logger = logging.getLogger(__name__)

TOP_N_DEFAULT = 12
NLE_TARGETS: frozenset[str] = frozenset({"fcpxml", "premiere", "otio", "capcut"})


@dataclass
class CurationSession:
    """In-memory session holding rough-cut results, shot mappings, and optional sequence."""

    session_id: str
    input_dir: Path
    config: PipelineConfig
    result: PipelineExecutionResult  # rough-cut stage result
    shots: dict[str, ScoredScene]  # "A01" / "B03" -> scene
    sequence: PipelineExecutionResult | None = None
    output_dir: Path | None = None


def _scene_duration(sc: ScoredScene) -> float:
    """Extract duration in seconds from a ScoredScene's underlying SceneCut."""
    return float(sc.scene.duration)


def summarize_curation(
    result: PipelineExecutionResult,
    top_n: int = TOP_N_DEFAULT,
) -> tuple[dict[str, Any], dict[str, ScoredScene]]:
    """Summarize curated scenes from a rough-cut pipeline result.

    Returns:
        A tuple of (summary_dict, shots_mapping), where summary_dict stays within the
        token budget (<= 2000 chars) with keys: takes, curated_duration_sec, by_role, top,
        and truncated.
    """
    scenes = result.scored_scenes or []
    shots: dict[str, ScoredScene] = {}
    by_role: dict[str, int] = {}

    a_count = 0
    b_count = 0
    total_dur = 0.0

    for sc in scenes:
        role = (sc.metadata or {}).get("content_role")
        if role == "a_roll":
            a_count += 1
            sid = f"A{a_count:02d}"
        else:
            b_count += 1
            sid = f"B{b_count:02d}"
        shots[sid] = sc

        role_key = role or "unknown"
        by_role[role_key] = by_role.get(role_key, 0) + 1
        total_dur += _scene_duration(sc)

    # Sort shots by composite_score desc, with natural ID order tie-breaker
    sorted_shots = sorted(
        shots.items(),
        key=lambda item: (
            -item[1].composite_score,
            item[0][0],
            int(item[0][1:]) if item[0][1:].isdigit() else 0,
        ),
    )
    top_items = sorted_shots[:top_n]
    top = [
        f"{sid}|{(sc.metadata or {}).get('content_role') or 'unknown'}|"
        f"{_scene_duration(sc):.1f}s|q{sc.composite_score:.2f}"
        for sid, sc in top_items
    ]

    summary = {
        "takes": len(scenes),
        "curated_duration_sec": round(total_dur, 1),
        "by_role": by_role,
        "top": top,
        "truncated": max(0, len(scenes) - len(top)),
    }
    return summary, shots


def run_curation(
    adapter: Any,
    input_dir: Path,
    genre: str | None,
    output_dir: Path,
    top_n: int = TOP_N_DEFAULT,
    timeout: float | None = None,
    callbacks: JobCallbacks | None = None,
    cancel_event: threading.Event | None = None,
    config_overrides: Mapping[str, Any] | None = None,
) -> tuple[CurationSession, dict[str, Any]]:
    """Execute rough-cut curation and defect pruning on source footage.

    Validates directory and genre before making any adapter call.
    """
    input_path = Path(input_dir)
    if not input_path.is_dir():
        raise FileNotFoundError(f"Input directory does not exist or is not a directory: {input_path}")

    out_path = Path(output_dir)
    cfg = PipelineConfig()

    if genre is not None:
        apply_staged_options(cfg, genre=genre)

    if config_overrides:
        from src.engine_api.options import apply_config_overrides

        cfg = apply_config_overrides(cfg, config_overrides, stage="curate")

    submit_kwargs: dict[str, Any] = {
        "input_dir": input_path,
        "soundtrack": None,
        "output_dir": out_path,
        "config": cfg,
        "stage": "rough_cut",
    }
    if callbacks is not None:
        submit_kwargs["callbacks"] = callbacks

    if cancel_event is not None:
        submit_kwargs["run_async"] = True
        handle = adapter.submit_job(**submit_kwargs)
        t0 = time.monotonic()
        while True:
            if cancel_event.is_set():
                handle.cancel()
                raise InterruptedError("Curation cancelled")
            wait_slice = 0.2
            if timeout is not None:
                elapsed = time.monotonic() - t0
                remaining = timeout - elapsed
                if remaining <= 0:
                    handle.cancel()
                    raise TimeoutError("Rough-cut curation job timed out")
                wait_slice = min(wait_slice, remaining)
            if handle.wait(wait_slice):
                break
            if cancel_event.is_set():
                handle.cancel()
                raise InterruptedError("Curation cancelled")
            if timeout is not None and (time.monotonic() - t0) >= timeout:
                handle.cancel()
                raise TimeoutError("Rough-cut curation job timed out")
    else:
        handle = adapter.submit_job(**submit_kwargs)
        if not handle.wait(timeout):
            raise TimeoutError("Rough-cut curation job timed out")

    result = handle.result
    if not result.success:
        raise RuntimeError(result.error_message or "Rough-cut curation failed")

    session_id = uuid4().hex[:8]
    summary, shots = summarize_curation(result, top_n=top_n)

    session = CurationSession(
        session_id=session_id,
        input_dir=input_path,
        config=cfg,
        result=result,
        shots=shots,
        sequence=None,
        output_dir=out_path,
    )
    summary_with_id = dict(summary)
    summary_with_id["session_id"] = session_id
    return session, summary_with_id


def assemble_sequence(
    adapter: Any,
    session: CurationSession,
    soundtrack: Path | None = None,
    selected_ids: list[str] | None = None,
    cadence: str | None = None,
    timeout: float | None = None,
    callbacks: JobCallbacks | None = None,
    cancel_event: threading.Event | None = None,
    config_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a cadence-driven sequence timeline from curated shots and soundtrack.

    Validates shot IDs and resolves soundtrack before making any adapter call.
    """
    if selected_ids is not None:
        unknown = [sid for sid in selected_ids if sid not in session.shots]
        if unknown:
            raise ValueError(f"Unknown shot IDs: {', '.join(unknown)}")
        selected_scenes = [session.shots[sid] for sid in selected_ids]
    else:
        selected_scenes = list(session.shots.values())

    st = soundtrack
    if st is None:
        st = find_soundtrack(session.input_dir)
    if st is None:
        raise ValueError("No soundtrack provided and none found in input_dir/Music")
    st = Path(st)

    if cadence is not None:
        apply_staged_options(session.config, cadence=cadence)

    if config_overrides:
        from src.engine_api.options import apply_config_overrides

        session.config = apply_config_overrides(session.config, config_overrides, stage="assemble")

    submit_kw: dict[str, Any] = {
        "input_dir": session.input_dir,
        "soundtrack": st,
        "output_dir": session.output_dir or (session.input_dir / "output"),
        "config": session.config,
        "stage": "sequence",
        "scenes": selected_scenes,
    }
    if session.result and session.result.edit_timelines and "rough_cut" in session.result.edit_timelines:
        submit_kw["existing_timelines"] = {"rough_cut": session.result.edit_timelines["rough_cut"]}

    if callbacks is not None:
        submit_kw["callbacks"] = callbacks

    if cancel_event is not None:
        submit_kw["run_async"] = True
        handle = adapter.submit_job(**submit_kw)
        t0 = time.monotonic()
        while True:
            if cancel_event.is_set():
                handle.cancel()
                raise InterruptedError("Sequence assembly cancelled")
            wait_slice = 0.2
            if timeout is not None:
                elapsed = time.monotonic() - t0
                remaining = timeout - elapsed
                if remaining <= 0:
                    handle.cancel()
                    raise TimeoutError("Sequence assembly job timed out")
                wait_slice = min(wait_slice, remaining)
            if handle.wait(wait_slice):
                break
            if cancel_event.is_set():
                handle.cancel()
                raise InterruptedError("Sequence assembly cancelled")
            if timeout is not None and (time.monotonic() - t0) >= timeout:
                handle.cancel()
                raise TimeoutError("Sequence assembly job timed out")
    else:
        handle = adapter.submit_job(**submit_kw)
        if not handle.wait(timeout):
            raise TimeoutError("Sequence assembly job timed out")

    res = handle.result
    if not res.success:
        raise RuntimeError(res.error_message or "Sequence assembly failed")

    session.sequence = res

    # Resolve active or primary timeline
    active_name = res.active_variant_name or "master"
    tl: EditTimeline | None = None
    tl_key = active_name

    if res.edit_timelines and active_name in res.edit_timelines:
        tl = res.edit_timelines[active_name]
        tl_key = active_name
    elif res.edit_timelines:
        for k, v in res.edit_timelines.items():
            if getattr(v, "kind", None) == "music":
                tl = v
                tl_key = k
                break
        if tl is None:
            first_k = next(iter(res.edit_timelines))
            tl = res.edit_timelines[first_k]
            tl_key = first_k

    total_cuts = len(tl.clips) if tl else 0
    duration_sec = round(tl.duration, 1) if tl else 0.0

    return {
        "timeline_id": f"{session.session_id}:{tl_key}",
        "total_cuts": total_cuts,
        "duration_sec": duration_sec,
        "status": "TIMELINE_ASSEMBLED_SUCCESS",
    }


def export_session(
    adapter: Any,
    session: CurationSession,
    output_dir: Path,
    targets: Iterable[str] = ("fcpxml", "premiere", "capcut"),
    render_preview: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Export curated rough cut or assembled sequence to NLE interchange formats."""
    target_set = set(targets)
    if not target_set.issubset(NLE_TARGETS):
        unknown = sorted(target_set - NLE_TARGETS)
        raise ValueError(f"Unknown export targets: {unknown}. Allowed: {sorted(NLE_TARGETS)}")

    out_path = Path(output_dir)

    # Pick assembled timeline if sequence exists, else rough_cut selects
    tl: EditTimeline | None = None
    if session.sequence and session.sequence.edit_timelines:
        active_name = session.sequence.active_variant_name or "master"
        if active_name in session.sequence.edit_timelines:
            tl = session.sequence.edit_timelines[active_name]
        else:
            for v in session.sequence.edit_timelines.values():
                if getattr(v, "kind", None) == "music":
                    tl = v
                    break
            if tl is None:
                tl = next(iter(session.sequence.edit_timelines.values()))
    elif (
        session.result
        and session.result.edit_timelines
        and "rough_cut" in session.result.edit_timelines
    ):
        tl = session.result.edit_timelines["rough_cut"]

    if tl is None:
        raise ValueError("Nothing to export: run curate_footage first")

    handle1 = adapter.submit_deliver_job(
        edit_timeline=tl,
        output_dir=out_path,
        config=session.config,
        formats=target_set,
    )
    if not handle1.wait(timeout):
        raise TimeoutError("NLE export deliver job timed out")
    res1 = handle1.result
    if not res1.success:
        raise RuntimeError(res1.error_message or "NLE export deliver job failed")

    res2 = None
    if render_preview:
        handle2 = adapter.submit_deliver_job(
            edit_timeline=tl,
            output_dir=out_path,
            config=session.config,
            formats={"mp4"},
            preview=True,
        )
        if not handle2.wait(timeout):
            raise TimeoutError("Preview render deliver job timed out")
        res2 = handle2.result
        if not res2.success:
            raise RuntimeError(res2.error_message or "Preview render deliver job failed")

    all_issues = list(res1.preflight_issues or [])
    if res2 and res2.preflight_issues:
        all_issues.extend(res2.preflight_issues)

    preflight_errors = sum(1 for i in all_issues if str(i).startswith("error"))
    preflight_warnings = sum(1 for i in all_issues if str(i).startswith("warning"))

    preview_path = None
    if res2 and res2.render_path:
        preview_path = str(res2.render_path)
    elif res1.render_path and res1.is_preview:
        preview_path = str(res1.render_path)

    return {
        "fcpxml_path": str(res1.fcpxml_path) if res1.fcpxml_path else None,
        "premiere_xml_path": str(res1.premiere_xml_path) if res1.premiere_xml_path else None,
        "otio_path": str(res1.otio_path) if res1.otio_path else None,
        "capcut_draft_path": str(res1.capcut_draft_path) if res1.capcut_draft_path else None,
        "preview_mp4": preview_path,
        "preflight_errors": preflight_errors,
        "preflight_warnings": preflight_warnings,
        "status": "EXPORT_COMPLETE",
    }
