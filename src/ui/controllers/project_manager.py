"""Project Manager and Workspace Persistence Controller for TeamTrau AutoEdit.

Handles .tteproj JSON project serialization, deserialization, dirty state tracking,
and Open Recent project history management.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.config import PipelineConfig

logger = logging.getLogger(__name__)

DEFAULT_RECENT_PROJECTS_PATH = Path.home() / ".teamtrau" / "recent_projects.json"
PROJECT_FILE_EXTENSION = ".tteproj"
PROJECT_FILE_FILTER = "TeamTrau Project (*.tteproj);;All Files (*.*)"


@dataclass
class ProjectData:
    """Encapsulates serializable workspace state for a video editing project."""

    project_name: str = "Untitled Project"
    version: str = "1.0.0"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    input_dir: str | None = None
    soundtrack_path: str | None = None
    output_dir: str | None = None
    preset_name: str = "1080p30"
    config: dict[str, Any] = field(default_factory=dict)
    in_point: float | None = None
    out_point: float | None = None
    timeline_clips: list[dict[str, Any]] = field(default_factory=list)
    included_scene_indices: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert project data to JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectData:
        """Construct ProjectData instance from dictionary with backward compatibility."""
        return cls(
            project_name=data.get("project_name", "Untitled Project"),
            version=data.get("version", "1.0.0"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
            input_dir=data.get("input_dir"),
            soundtrack_path=data.get("soundtrack_path"),
            output_dir=data.get("output_dir"),
            preset_name=data.get("preset_name", "1080p30"),
            config=data.get("config", {}),
            in_point=data.get("in_point"),
            out_point=data.get("out_point"),
            timeline_clips=data.get("timeline_clips", []),
            included_scene_indices=data.get("included_scene_indices", []),
        )


class ProjectManager:
    """Manages active project file persistence, recent project history, and dirty state."""

    def __init__(
        self,
        recent_path: Path | None = None,
        max_recent: int = 10,
    ) -> None:
        self.recent_path = recent_path or DEFAULT_RECENT_PROJECTS_PATH
        self.max_recent = max_recent
        self.current_project_path: Path | None = None
        self._is_dirty: bool = False

    @property
    def is_dirty(self) -> bool:
        """Return True if project has unsaved modifications."""
        return self._is_dirty

    def set_dirty(self, dirty: bool = True) -> None:
        """Update dirty modification flag."""
        self._is_dirty = dirty

    def mark_clean(self) -> None:
        """Reset dirty modification flag to clean."""
        self._is_dirty = False

    # -------------------------------------------------------------------------
    # Recent Projects History
    # -------------------------------------------------------------------------

    def get_recent_projects(self) -> list[Path]:
        """Load list of recent project file paths from disk, filtering nonexistent files."""
        if not self.recent_path.exists():
            return []
        try:
            raw = json.loads(self.recent_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []
            valid_paths: list[Path] = []
            for item in raw:
                p = Path(item)
                if p.exists() and p.is_file():
                    valid_paths.append(p)
            return valid_paths[: self.max_recent]
        except Exception as exc:
            logger.warning("Failed to load recent projects from %s: %s", self.recent_path, exc)
            return []

    def add_recent_project(self, path: Path) -> None:
        """Add a project path to the front of recent history and persist to disk."""
        resolved = path.resolve()
        existing = [p for p in self.get_recent_projects() if p != resolved]
        updated = [resolved] + existing
        updated = updated[: self.max_recent]

        try:
            self.recent_path.parent.mkdir(parents=True, exist_ok=True)
            serializable = [str(p) for p in updated]
            self.recent_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to persist recent projects to %s: %s", self.recent_path, exc)

    def clear_recent_projects(self) -> None:
        """Clear recent projects history file."""
        try:
            if self.recent_path.exists():
                self.recent_path.write_text("[]", encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to clear recent projects: %s", exc)

    # -------------------------------------------------------------------------
    # Save & Load Operations
    # -------------------------------------------------------------------------

    def save_project(self, path: Path, data: ProjectData) -> bool:
        """Serialize ProjectData into .tteproj JSON file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data.updated_at = datetime.now(timezone.utc).isoformat()
            payload = data.to_dict()
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            self.current_project_path = path.resolve()
            self.mark_clean()
            self.add_recent_project(self.current_project_path)
            logger.info("Saved project successfully to %s", path)
            return True
        except Exception as exc:
            logger.error("Failed to save project to %s: %s", path, exc, exc_info=True)
            return False

    def load_project(self, path: Path) -> ProjectData | None:
        """Deserialize ProjectData from a .tteproj JSON file."""
        if not path.exists():
            logger.error("Project file does not exist: %s", path)
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            data = ProjectData.from_dict(raw)
            self.current_project_path = path.resolve()
            self.mark_clean()
            self.add_recent_project(self.current_project_path)
            logger.info("Loaded project successfully from %s", path)
            return data
        except Exception as exc:
            logger.error("Failed to parse project file %s: %s", path, exc, exc_info=True)
            return None

    def serialize_pipeline_config(self, config: PipelineConfig) -> dict[str, Any]:
        """Convert PipelineConfig into a JSON-serializable dictionary."""
        d = config.model_dump()
        # Convert Path objects to string representations
        for k, v in d.items():
            if isinstance(v, Path):
                d[k] = str(v)
            elif v is None:
                d[k] = None
        return d

    def deserialize_pipeline_config(self, raw: dict[str, Any]) -> PipelineConfig:
        """Construct PipelineConfig from raw dictionary, restoring Path instances."""
        cfg = PipelineConfig()
        for k, v in raw.items():
            if hasattr(cfg, k):
                if k in ("watermark_path", "capcut_draft_dir") and v:
                    setattr(cfg, k, Path(v))
                else:
                    setattr(cfg, k, v)
        return cfg
