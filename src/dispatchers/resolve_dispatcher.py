"""DaVinci Resolve Studio API automation dispatcher on Windows 11."""

import logging
import os
import sys
from pathlib import Path
from typing import Any

from src.core.config import PipelineConfig

logger = logging.getLogger(__name__)


class ResolveDispatcher:
    """Interfaces with DaVinci Resolve Studio API to initialize projects, import timelines, and queue renders."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self._resolve_app: Any = None
        self._setup_environment()

    def _setup_environment(self) -> None:
        """Configure Windows environment variables and sys.path for DaVinciResolveScript."""
        script_module_dir = str(self.config.resolve_script_path.resolve())
        dll_path = str(self.config.resolve_exe_path.parent / "fusionscript.dll")

        if "RESOLVE_SCRIPT_API" not in os.environ:
            os.environ["RESOLVE_SCRIPT_API"] = str(self.config.resolve_script_path.parent)
        if "RESOLVE_SCRIPT_LIB" not in os.environ and Path(dll_path).exists():
            os.environ["RESOLVE_SCRIPT_LIB"] = dll_path

        if script_module_dir not in sys.path and Path(script_module_dir).exists():
            sys.path.append(script_module_dir)

    def connect(self) -> bool:
        """Attempt connection to active DaVinci Resolve instance."""
        try:
            import DaVinciResolveScript as dvr_script  # type: ignore

            app = dvr_script.scriptapp("Resolve")
            if app is not None:
                self._resolve_app = app
                logger.info("Connected successfully to DaVinci Resolve Studio instance.")
                return True
        except ImportError as exc:
            logger.warning("Could not import DaVinciResolveScript module: %s", exc)
        except Exception as exc:
            logger.warning("DaVinci Resolve connection attempt failed: %s", exc)

        return False

    def dispatch_project(
        self,
        fcpxml_path: Path,
        source_media_paths: list[Path],
        project_name: str = "AutoEdit_Project",
        render_output_dir: Path | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Initialize project, set canvas dimensions, import media & timeline, and append to render queue."""
        result_info: dict[str, Any] = {
            "connected": False,
            "project_name": project_name,
            "canvas": f"{self.config.canvas_width}x{self.config.canvas_height}@{self.config.fps}fps",
            "imported_timeline": False,
            "queued_render": False,
            "dry_run": dry_run,
        }

        if dry_run:
            logger.info(
                "[Dry-Run] Resolve Dispatcher verified: Canvas=%dx%d @ %.1ffps, Timeline=%s",
                self.config.canvas_width,
                self.config.canvas_height,
                self.config.fps,
                fcpxml_path.name,
            )
            result_info["connected"] = True
            result_info["imported_timeline"] = True
            result_info["queued_render"] = True
            return result_info

        if not self.connect() or self._resolve_app is None:
            msg = (
                "DaVinci Resolve is not running or external scripting is disabled. "
                "Enable in Resolve: Preferences -> System -> General -> External scripting using: Local"
            )
            logger.error(msg)
            result_info["error"] = msg
            return result_info

        result_info["connected"] = True
        pm = self._resolve_app.GetProjectManager()
        proj = pm.GetCurrentProject()
        if not proj:
            proj = pm.CreateProject(project_name)
        if not proj:
            proj = pm.LoadProject(project_name)

        if not proj:
            result_info["error"] = f"Failed to create or load project '{project_name}'"
            return result_info

        # Configure sequence canvas dimensions and framerate
        proj.SetSetting("timelineResolutionWidth", str(self.config.canvas_width))
        proj.SetSetting("timelineResolutionHeight", str(self.config.canvas_height))
        proj.SetSetting("timelineFrameRate", str(int(round(self.config.fps))))

        media_pool = proj.GetMediaPool()

        # Import raw footage to Media Pool
        valid_media = [str(p.resolve()) for p in source_media_paths if p.exists()]
        if valid_media:
            media_pool.ImportMedia(valid_media)

        # Import FCPXML Timeline
        imported_tl = media_pool.ImportTimelineFromFile(str(fcpxml_path.resolve()))
        if imported_tl:
            result_info["imported_timeline"] = True
            logger.info("Imported timeline into Resolve Media Pool: %s", fcpxml_path.name)
        else:
            logger.warning("Resolve returned null when importing timeline: %s", fcpxml_path.name)

        # Append to Render Queue if target directory is specified
        if render_output_dir is not None:
            render_output_dir.mkdir(parents=True, exist_ok=True)
            export_name = f"{project_name}_master"
            proj.SetRenderSettings(
                {
                    "SelectAllFrames": 1,
                    "TargetDir": str(render_output_dir.resolve()),
                    "CustomName": export_name,
                    "FormatWidth": self.config.canvas_width,
                    "FormatHeight": self.config.canvas_height,
                }
            )
            job_id = proj.AddRenderJob()
            if job_id:
                result_info["queued_render"] = True
                result_info["render_job_id"] = str(job_id)
                logger.info("Appended job to Resolve Render Queue (Job ID: %s)", job_id)

        return result_info
