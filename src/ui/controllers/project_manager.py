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

from src.core.config import PRESETS, PipelineConfig
from src.core.project_io import (
    audio_state_from_dict,
    audio_state_to_dict,
    deliver_settings_from_dict,
    edit_timeline_from_dict,
    edit_timeline_to_dict,
    scored_scene_from_dict,
    scored_scene_to_dict,
)
from src.core.types import (
    BeatMarker,
    DeliverSettings,
    EditTimeline,
    PipelineExecutionResult,
    ScoredScene,
    SongSection,
    SongSectionType,
    TimelineVariant,
)
from src.modules.audio_analyzer import AudioAnalysisResult

logger = logging.getLogger(__name__)

DEFAULT_RECENT_PROJECTS_PATH = Path.home() / ".teamtrau" / "recent_projects.json"
PROJECT_FILE_EXTENSION = ".tteproj"
PROJECT_FILE_FILTER = "TeamTrau Project (*.tteproj);;All Files (*.*)"
PROJECT_SCHEMA_VERSION = "2.3.0"
# Workflow position of a project (v2.3): where the user left off.
PIPELINE_STAGES = ("ingest", "rough_cut", "sequence", "deliver")


@dataclass
class ProjectData:
    """Encapsulates serializable workspace state for a video editing project."""

    project_name: str = "Untitled Project"
    version: str = PROJECT_SCHEMA_VERSION
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    input_dir: str | None = None
    input_dirs: list[str] = field(default_factory=list)
    clip_roles: dict[str, str] = field(default_factory=dict)
    soundtrack_path: str | None = None
    output_dir: str | None = None
    preset_name: str = "1080p30"
    config: dict[str, Any] = field(default_factory=dict)
    in_point: float | None = None
    out_point: float | None = None
    playhead_time: float = 0.0
    timeline_clips: list[dict[str, Any]] = field(default_factory=list)
    scored_scenes: list[dict[str, Any]] = field(default_factory=list)
    included_scene_indices: list[int] = field(default_factory=list)
    audio_analysis: dict[str, Any] = field(default_factory=dict)
    timeline_variants: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_variant_name: str | None = None
    deliverables: dict[str, Any] = field(default_factory=dict)
    # v2.1: edit timelines are the single source of truth for Deliver/Export.
    edit_timelines: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_timeline_key: str = "master"
    # v2.2: Deliver settings (canvas/fps/colour/audio/master...); empty = derive from timeline.
    deliver_settings: dict[str, Any] = field(default_factory=dict)
    # v2.3: staged workflow. ``pipeline_stage`` is one of PIPELINE_STAGES; ``audio_state`` is the REAL
    # soundtrack analysis (beats + downbeats, bar grid, sections, cut points, cadence cuts, tempo).
    pipeline_stage: str = "ingest"
    audio_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert project data to JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectData:
        """Construct ProjectData instance from dictionary with backward compatibility."""
        return cls(
            project_name=data.get("project_name", "Untitled Project"),
            version=data.get("version", "2.0.0"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
            input_dir=data.get("input_dir"),
            input_dirs=data.get("input_dirs", []),
            clip_roles=data.get("clip_roles", {}),
            soundtrack_path=data.get("soundtrack_path"),
            output_dir=data.get("output_dir"),
            preset_name=data.get("preset_name", "1080p30"),
            config=data.get("config", {}),
            in_point=data.get("in_point"),
            out_point=data.get("out_point"),
            playhead_time=float(data.get("playhead_time", 0.0)),
            timeline_clips=data.get("timeline_clips", []),
            scored_scenes=data.get("scored_scenes", []),
            included_scene_indices=data.get("included_scene_indices", []),
            audio_analysis=data.get("audio_analysis", {}),
            timeline_variants=data.get("timeline_variants", {}),
            active_variant_name=data.get("active_variant_name"),
            deliverables=data.get("deliverables", {}),
            edit_timelines=data.get("edit_timelines", {}),
            active_timeline_key=data.get("active_timeline_key", "master"),
            deliver_settings=data.get("deliver_settings") or {},
            pipeline_stage=_valid_stage(data.get("pipeline_stage")),
            audio_state=data.get("audio_state") or {},
        )

    def get_audio_state(self) -> AudioAnalysisResult | None:
        """Rebuild the stored soundtrack analysis (beats, bar grid, ...); None when absent or invalid."""
        if not self.audio_state:
            return None
        try:
            return audio_state_from_dict(self.audio_state)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Ignoring invalid stored audio_state: %s", exc)
            return None

    def get_deliver_settings(self) -> DeliverSettings | None:
        """Decode stored Deliver settings; None when absent (v2.0/2.1) or invalid."""
        if not self.deliver_settings:
            return None
        try:
            return deliver_settings_from_dict(self.deliver_settings)
        except (TypeError, ValueError) as exc:
            logger.warning("Ignoring invalid stored deliver_settings: %s", exc)
            return None

    def get_edit_timelines(self) -> dict[str, EditTimeline]:
        """Decode stored edit timelines; v2.0 projects are upgraded from their variants."""
        out = {k: edit_timeline_from_dict(v) for k, v in self.edit_timelines.items()}
        if out:
            return out
        return upgrade_variants_to_edit_timelines(self)


def _valid_stage(value: Any) -> str:
    return value if value in PIPELINE_STAGES else "ingest"


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

    @staticmethod
    def serialize_scored_scene(scene: ScoredScene, is_included: bool = True) -> dict[str, Any]:
        """Convert ScoredScene and inclusion state to a serializable dictionary."""
        return {**scored_scene_to_dict(scene), "included": is_included}

    @staticmethod
    def deserialize_scored_scene(d: dict[str, Any]) -> tuple[ScoredScene, bool]:
        """Reconstruct ScoredScene instance and inclusion state from dictionary."""
        scene = scored_scene_from_dict(d)
        is_inc = bool(d.get("included", True))
        scene.metadata["included"] = is_inc
        return scene, is_inc

    @staticmethod
    def serialize_audio_analysis(audio_res: AudioAnalysisResult) -> dict[str, Any]:
        """Convert AudioAnalysisResult into a serializable dictionary."""
        return {
            "tempo_bpm": audio_res.tempo_bpm,
            "duration": audio_res.duration,
            "sample_rate": audio_res.sample_rate,
            "cut_timestamps": list(audio_res.cut_timestamps),
            "beats": [
                {
                    "timestamp": b.timestamp,
                    "frame_idx": b.frame_idx,
                    "strength": b.strength,
                    "is_downbeat": b.is_downbeat,
                }
                for b in audio_res.beats
            ],
            "sections": [
                {
                    "section_type": s.section_type.value if hasattr(s.section_type, "value") else str(s.section_type),
                    "start_time": s.start_time,
                    "end_time": s.end_time,
                    "duration": s.duration,
                    "avg_energy": s.avg_energy,
                    "chroma_profile": list(s.chroma_profile),
                    "is_climax": s.is_climax,
                }
                for s in audio_res.sections
            ],
            "extracted_audio_path": str(audio_res.extracted_audio_path) if audio_res.extracted_audio_path else None,
        }

    @staticmethod
    def deserialize_audio_analysis(d: dict[str, Any]) -> AudioAnalysisResult:
        """Reconstruct AudioAnalysisResult from serialized dictionary."""
        beats = [
            BeatMarker(
                timestamp=float(b["timestamp"]),
                frame_idx=int(b["frame_idx"]),
                strength=float(b["strength"]),
                is_downbeat=bool(b["is_downbeat"]),
            )
            for b in d.get("beats", [])
        ]
        sections = []
        for s in d.get("sections", []):
            st_val = s.get("section_type", "INTRO")
            try:
                sec_type = SongSectionType(st_val)
            except Exception:
                sec_type = SongSectionType.INTRO
            sections.append(
                SongSection(
                    section_type=sec_type,
                    start_time=float(s["start_time"]),
                    end_time=float(s["end_time"]),
                    duration=float(s["duration"]),
                    avg_energy=float(s.get("avg_energy", 0.5)),
                    chroma_profile=list(s.get("chroma_profile", [])),
                    is_climax=bool(s.get("is_climax", False)),
                )
            )
        return AudioAnalysisResult(
            tempo_bpm=float(d.get("tempo_bpm", 120.0)),
            duration=float(d.get("duration", 0.0)),
            sample_rate=int(d.get("sample_rate", 44100)),
            cut_timestamps=[float(x) for x in d.get("cut_timestamps", [])],
            beats=beats,
            sections=sections,
            extracted_audio_path=Path(d["extracted_audio_path"]) if d.get("extracted_audio_path") else None,
        )

    @staticmethod
    def serialize_timeline_variant(variant: TimelineVariant) -> dict[str, Any]:
        """Convert TimelineVariant into a serializable dictionary."""
        scenes_data = [ProjectManager.serialize_scored_scene(s) for s in variant.sequence_scenes]
        return {
            "name": variant.name,
            "display_name": variant.display_name,
            "description": variant.description,
            "target_pacing": variant.target_pacing,
            "clip_count": variant.clip_count,
            "total_duration": variant.total_duration,
            "fcpxml_path": str(variant.fcpxml_path) if variant.fcpxml_path else None,
            "premiere_xml_path": str(variant.premiere_xml_path) if variant.premiere_xml_path else None,
            "otio_path": str(variant.otio_path) if variant.otio_path else None,
            "capcut_draft_path": str(variant.capcut_draft_path) if variant.capcut_draft_path else None,
            "render_path": str(variant.render_path) if variant.render_path else None,
            "sequence_scenes": scenes_data,
        }

    @staticmethod
    def deserialize_timeline_variant(d: dict[str, Any]) -> TimelineVariant:
        """Reconstruct TimelineVariant from serialized dictionary."""
        scenes = [ProjectManager.deserialize_scored_scene(s)[0] for s in d.get("sequence_scenes", [])]
        return TimelineVariant(
            name=d.get("name", ""),
            display_name=d.get("display_name", ""),
            description=d.get("description", ""),
            target_pacing=d.get("target_pacing", "moderate"),
            clip_count=int(d.get("clip_count", len(scenes))),
            total_duration=float(d.get("total_duration", 0.0)),
            fcpxml_path=Path(d["fcpxml_path"]) if d.get("fcpxml_path") else None,
            premiere_xml_path=Path(d["premiere_xml_path"]) if d.get("premiere_xml_path") else None,
            otio_path=Path(d["otio_path"]) if d.get("otio_path") else None,
            capcut_draft_path=Path(d["capcut_draft_path"]) if d.get("capcut_draft_path") else None,
            render_path=Path(d["render_path"]) if d.get("render_path") else None,
            sequence_scenes=scenes,
        )


def upgrade_variants_to_edit_timelines(data: ProjectData) -> dict[str, EditTimeline]:
    """Rebuild edit timelines for legacy (v2.0) projects from stored variant scene lists."""
    from src.modules.edit_timeline import clip_from_scene

    preset = PRESETS.get(data.preset_name)
    fps = float(data.config.get("fps") or (preset.fps if preset else 30.0))
    width = int(data.config.get("canvas_width") or (preset.width if preset else 1920))
    height = int(data.config.get("canvas_height") or (preset.height if preset else 1080))
    out: dict[str, EditTimeline] = {}
    for key, raw in data.timeline_variants.items():
        scenes = [ProjectManager.deserialize_scored_scene(s)[0] for s in raw.get("sequence_scenes", [])]
        if not scenes:
            continue
        clips = [clip_from_scene(sc, int(round(sc.scene.duration * fps))) for sc in scenes]
        out[key] = EditTimeline(
            name=str(raw.get("display_name") or key),
            fps=fps,
            canvas_width=width,
            canvas_height=height,
            clips=clips,
            soundtrack_path=Path(data.soundtrack_path) if data.soundtrack_path else None,
            variant_key=key,
        )
    return out


def project_from_result(
    result: PipelineExecutionResult,
    config: PipelineConfig,
    project_name: str,
    input_dirs: list[Path],
    soundtrack_path: Path | None,
    output_dir: Path,
) -> ProjectData:
    """Build a saveable project (Edit state) from a finished analysis run."""
    pm = ProjectManager
    audio = getattr(result, "audio_analysis", None)
    stage = getattr(result, "stage", "all")
    timelines = {k: edit_timeline_to_dict(v) for k, v in result.edit_timelines.items()}
    data = ProjectData(
        project_name=project_name,
        input_dir=str(input_dirs[0]) if input_dirs else None,
        input_dirs=[str(p) for p in input_dirs],
        soundtrack_path=str(soundtrack_path) if soundtrack_path else None,
        output_dir=str(output_dir),
        preset_name=config.preset_name,
        config=ProjectManager().serialize_pipeline_config(config),
        scored_scenes=[
            pm.serialize_scored_scene(s, is_included=s.metadata.get("included", True) is not False)
            for s in result.scored_scenes
        ],
        included_scene_indices=[
            i for i, s in enumerate(result.scored_scenes) if s.metadata.get("included", True) is not False
        ],
        timeline_variants={k: pm.serialize_timeline_variant(v) for k, v in result.timeline_variants.items()},
        active_variant_name=result.active_variant_name,
        edit_timelines=timelines,
        active_timeline_key="master" if "master" in timelines else next(iter(timelines), "master"),
        audio_analysis=pm.serialize_audio_analysis(audio) if audio is not None else {},
        audio_state=audio_state_to_dict(audio),
        pipeline_stage=(
            "rough_cut"
            if stage == "rough_cut"
            or (result.edit_timelines and all(t.kind == "rough_cut" for t in result.edit_timelines.values()))
            else "sequence"
        ),
    )
    return data


def save_result_as_project(
    result: PipelineExecutionResult,
    config: PipelineConfig,
    path: Path,
    input_dirs: list[Path],
    soundtrack_path: Path | None,
    output_dir: Path,
) -> Path:
    """Write a finished analysis run as a .tteproj (no recent-history side effects)."""
    data = project_from_result(
        result,
        config,
        path.stem,
        [Path(p).resolve() for p in input_dirs],
        Path(soundtrack_path).resolve() if soundtrack_path else None,
        Path(output_dir).resolve(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_project_file(path: Path) -> ProjectData:
    """Read a .tteproj without touching recent-project history."""
    return ProjectData.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
