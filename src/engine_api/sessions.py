"""Curation session store and serialization for TeamTrau Engine API."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from src.core.curation import CurationSession
from src.core.types import ScoredScene

logger = logging.getLogger(__name__)

ALLOWED_PATCH_ROLES: frozenset[str] = frozenset({"a_roll", "b_roll", "ambient_b_roll"})


def shot_to_dict(shot_id: str, sc: ScoredScene) -> dict[str, Any]:
    """Serialize a single ScoredScene to a JSON-safe dictionary.

    Returns exactly the keys:
        shot_id, clip_path, start, end, duration, composite, sharpness,
        aesthetic, motion_energy, content_role, role_confidence, included,
        golden, defects.
    """
    meta = sc.metadata or {}
    golden_raw = meta.get("golden")
    golden: dict[str, Any] | None = None
    defects: list[Any] = []
    if isinstance(golden_raw, dict):
        golden = dict(golden_raw)
        raw_defects = golden.get("defects")
        if isinstance(raw_defects, list):
            defects = [dict(d) if isinstance(d, dict) else d for d in raw_defects]

    included = bool(meta.get("included", True))
    role_conf = meta.get("role_confidence")
    if role_conf is not None:
        role_conf = float(role_conf)

    return {
        "shot_id": shot_id,
        "clip_path": str(sc.scene.clip_path),
        "start": float(sc.scene.start_time),
        "end": float(sc.scene.end_time),
        "duration": float(sc.scene.duration),
        "composite": float(sc.composite_score),
        "sharpness": float(sc.sharpness_score),
        "aesthetic": float(sc.aesthetic_score),
        "motion_energy": float(sc.motion_energy),
        "content_role": meta.get("content_role"),
        "role_confidence": role_conf,
        "included": included,
        "golden": golden,
        "defects": defects,
    }


def session_to_dict(session: CurationSession, detail: bool = False) -> dict[str, Any]:
    """Serialize a CurationSession to a JSON-safe dictionary with shots in natural order."""
    def _natural_key(item: tuple[str, ScoredScene]) -> tuple[str, int, str]:
        sid = item[0]
        prefix = sid[0] if sid else ""
        num_str = sid[1:]
        num = int(num_str) if num_str.isdigit() else 0
        return (prefix, num, sid)

    sorted_shots = sorted(session.shots.items(), key=_natural_key)
    serialized_shots = [shot_to_dict(sid, sc) for sid, sc in sorted_shots]

    data: dict[str, Any] = {
        "session_id": session.session_id,
        "input_dir": str(session.input_dir),
        "takes": len(session.shots),
        "shots": serialized_shots,
    }

    if detail:
        details: dict[str, dict[str, Any]] = {}
        vlm_keys = ("vlm_description", "vlm_tags", "vlm_mood", "vlm_pacing", "vlm_key_moment")
        for sid, sc in sorted_shots:
            meta = sc.metadata or {}
            golden_raw = meta.get("golden") if isinstance(meta.get("golden"), dict) else {}
            gw = meta.get("golden_window")
            if isinstance(gw, dict):
                golden_window: dict[str, Any] | None = dict(gw)
            else:
                golden_window = None

            if any(k in meta for k in vlm_keys):
                raw_tags = meta.get("vlm_tags")
                vlm = {
                    "description": meta.get("vlm_description"),
                    "tags": list(raw_tags) if isinstance(raw_tags, (list, tuple)) else raw_tags,
                    "mood": meta.get("vlm_mood"),
                    "pacing": meta.get("vlm_pacing"),
                    "key_moment": meta.get("vlm_key_moment"),
                }
            else:
                vlm = None

            details[sid] = {
                "is_long_shot": bool(meta.get("is_long_shot", False)),
                "defects_count": int(meta.get("defects_count", 0)),
                "has_dead_air": bool(
                    meta.get("has_dead_air", False)
                    or getattr(sc, "is_dead_air", False)
                    or meta.get("is_dead_air", False)
                ),
                "requires_deflicker": bool(golden_raw.get("requires_deflicker", False)),
                "trimmed_sec": float(golden_raw.get("trimmed_sec", 0.0)),
                "golden_window": golden_window,
                "scored_by": meta.get("scored_by"),
                "vlm": vlm,
            }
        data["details"] = details

    return data


class SessionStore:
    """Thread-safe LRU store for in-memory CurationSessions."""

    def __init__(self, max_sessions: int = 8) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[str, CurationSession] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, session: CurationSession) -> str:
        """Store or update session, evicting the least recently used if over max_sessions."""
        with self._lock:
            sid = session.session_id
            if sid in self._sessions:
                self._sessions.move_to_end(sid)
            self._sessions[sid] = session
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
            return sid

    def get(self, session_id: str) -> CurationSession | None:
        """Retrieve session by id, refreshing its LRU recency, or None if not found."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                self._sessions.move_to_end(session_id)
            return session

    def patch(self, session_id: str, updates: Mapping[str, Mapping[str, Any]]) -> CurationSession:
        """Patch shot fields in session. Validates all updates before mutating.

        Raises:
            KeyError: if session_id or any shot_id is not found.
            ValueError: if any update is not a mapping, has invalid fields, or invalid values.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Session '{session_id}' not found")

            # Phase 1: Full validation before mutation
            for shot_id, shot_updates in updates.items():
                if shot_id not in session.shots:
                    raise KeyError(f"Shot '{shot_id}' not found in session '{session_id}'")
                if not isinstance(shot_updates, Mapping):
                    raise ValueError(f"Updates for shot '{shot_id}' must be a mapping")
                for field, val in shot_updates.items():
                    if field == "included":
                        if not isinstance(val, bool):
                            raise ValueError(
                                f"'included' must be a bool, got {type(val).__name__}: {val!r}"
                            )
                    elif field == "content_role":
                        if val not in ALLOWED_PATCH_ROLES:
                            raise ValueError(
                                f"Invalid content_role: {val!r}. Allowed: {sorted(ALLOWED_PATCH_ROLES)}"
                            )
                    else:
                        raise ValueError(f"Disallowed patch field: {field!r}")

            # Phase 2: Mutation
            for shot_id, shot_updates in updates.items():
                sc = session.shots[shot_id]
                if sc.metadata is None:
                    sc.metadata = {}
                for field, val in shot_updates.items():
                    sc.metadata[field] = val

            self._sessions.move_to_end(session_id)
            return session
