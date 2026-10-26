"""Curation sessions on disk: ``{workspace}/.teamtrau/sessions/{session_id}.json``.

A session holds the curated takes and the user's choices on them, which cost a full analysis to
rebuild, so it is written whenever it changes and read back when an engine that restarted is
asked for it again.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from src.core.config import PipelineConfig
from src.core.curation import CurationSession
from src.core.project_io import (
    edit_timeline_from_dict,
    edit_timeline_to_dict,
    scored_scene_from_dict,
    scored_scene_to_dict,
)
from src.core.types import PipelineExecutionResult, ScoredScene

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def session_file(workspace: Path, session_id: str) -> Path:
    return Path(workspace) / ".teamtrau" / "sessions" / f"{session_id}.json"


def _json_default(value: Any) -> Any:
    """Metadata may carry paths, enums, numpy scalars or dataclasses; store them as plain JSON."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    item = getattr(value, "item", None)  # numpy scalar
    if callable(item):
        return item()
    return str(value)


def _result_to_dict(res: PipelineExecutionResult | None) -> dict[str, Any] | None:
    if res is None:
        return None
    return {
        "stage": res.stage,
        "active_variant_name": res.active_variant_name,
        "edit_timelines": {k: edit_timeline_to_dict(tl) for k, tl in res.edit_timelines.items()},
    }


def _result_from_dict(d: dict[str, Any] | None, scenes: list[ScoredScene]) -> PipelineExecutionResult | None:
    if d is None:
        return None
    timelines = {k: edit_timeline_from_dict(v) for k, v in (d.get("edit_timelines") or {}).items()}
    return PipelineExecutionResult(
        success=True,
        total_duration=0.0,
        clip_count=len(scenes),
        scenes_detected=len(scenes),
        audio_bpm=0.0,
        sections_detected=0,
        fcpxml_path=None,
        premiere_xml_path=None,
        otio_path=None,
        capcut_draft_path=None,
        render_path=None,
        encoder_used=None,
        resolve_status=None,
        scored_scenes=scenes,
        edit_timelines=timelines,
        active_variant_name=d.get("active_variant_name"),
        stage=d.get("stage", "all"),
    )


def save_session(session: CurationSession) -> Path | None:
    """Write the session next to its workspace; a session without one stays in memory only."""
    if session.workspace is None:
        return None
    path = session_file(session.workspace, session.session_id)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session.session_id,
        "project_id": session.project_id,
        "input_dir": str(session.input_dir),
        "output_dir": str(session.output_dir) if session.output_dir else None,
        "config": session.config.model_dump(mode="json"),
        "shots": {sid: scored_scene_to_dict(sc) for sid, sc in session.shots.items()},
        "result": _result_to_dict(session.result),
        "sequence": _result_to_dict(session.sequence),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def load_session(workspace: Path, session_id: str) -> CurationSession | None:
    """Read a saved session, or None when the id is unsafe, the file is missing or unreadable."""
    if not _SAFE_ID.match(session_id):
        return None
    path = session_file(workspace, session_id)
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        shots = {sid: scored_scene_from_dict(v) for sid, v in d["shots"].items()}
        scenes = list(shots.values())
        result = _result_from_dict(d.get("result"), scenes)
        return CurationSession(
            session_id=str(d["session_id"]),
            input_dir=Path(d["input_dir"]),
            config=PipelineConfig.model_validate(d["config"]),
            result=result if result is not None else _result_from_dict({}, scenes),
            shots=shots,
            sequence=_result_from_dict(d.get("sequence"), []),
            output_dir=Path(d["output_dir"]) if d.get("output_dir") else None,
            workspace=Path(workspace),
            project_id=d.get("project_id"),
        )
    except Exception as exc:  # a damaged file must not take the engine down
        logger.warning("Cannot read curation session %s: %s", path, exc)
        return None
