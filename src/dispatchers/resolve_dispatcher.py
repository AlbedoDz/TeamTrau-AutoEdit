"""DaVinci Resolve Studio API automation dispatcher on Windows 11."""

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.core.config import PipelineConfig
from src.core.platform_caps import detect_os
from src.core.platform_paths import fusionscript_name
from src.dispatchers.resolve_worker import run_dispatch

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
        dll_path = str(self.config.resolve_exe_path.parent / fusionscript_name(detect_os()))

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
        fit_mode: str = "auto",
        color: str | None = None,
        master: str = "hevc",
    ) -> dict[str, Any]:
        """Initialize project, set canvas dimensions, import media & timeline, and append to render queue.

        ``fit_mode`` (auto|fit|fill|stretch) selects Resolve's "Mismatched resolution files"
        scaling; ``color`` (rec709|rec2020_hlg|rec2020_pq|passthrough) is passed through;
        ``master`` (hevc|h264|prores_422|prores_hq|dnxhr_hqx, or mp4/h265/mov+prores) selects the
        render format/codec when ``render_output_dir`` is given.
        """
        result_info: dict[str, Any] = {
            "connected": False,
            "project_name": project_name,
            "canvas": f"{self.config.canvas_width}x{self.config.canvas_height}@{self.config.fps}fps",
            "fit_mode": fit_mode,
            "color": color,
            "master": master,
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

        payload = {
            "fcpxml": str(Path(fcpxml_path).resolve()),
            "media": [str(Path(p).resolve()) for p in source_media_paths if Path(p).exists()],
            "project_name": project_name,
            "width": self.config.canvas_width,
            "height": self.config.canvas_height,
            "fps": self.config.fps,
            "fit_mode": fit_mode,
            "color": color,
            "master": master,
            "render_dir": str(render_output_dir.resolve()) if render_output_dir is not None else None,
            "script_api": os.environ.get("RESOLVE_SCRIPT_API"),
            "script_lib": os.environ.get("RESOLVE_SCRIPT_LIB"),
        }

        # 1) In-process when fusionscript loads under this interpreter.
        if self._resolve_app is not None or self.connect():
            if self._resolve_app is not None:
                return run_dispatch(self._resolve_app, payload)

        # 2) Otherwise run the worker with a Python version Resolve supports.
        worker_result = self._dispatch_in_subprocess(payload)
        if worker_result is not None:
            return {**result_info, **worker_result}

        msg = (
            "DaVinci Resolve is not running or external scripting is disabled. "
            "Enable in Resolve: Preferences -> System -> General -> External scripting using: Local "
            "(Resolve's fusionscript may also need a different Python version: set AUTOEDIT_RESOLVE_PYTHON)."
        )
        logger.error(msg)
        result_info["error"] = msg
        return result_info

    @staticmethod
    def _worker_interpreters() -> list[list[str]]:
        """Interpreters to try for the Resolve worker: env override, then py launcher versions."""
        cands: list[list[str]] = []
        override = os.environ.get("AUTOEDIT_RESOLVE_PYTHON")
        if override:
            cands.append([override])
        launcher = shutil.which("py")
        if launcher:
            cands += [[launcher, f"-3.{minor}"] for minor in (13, 12, 11, 10)]
        cands.append([sys.executable])
        return cands

    def _dispatch_in_subprocess(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        worker = Path(__file__).with_name("resolve_worker.py")
        for cmd in self._worker_interpreters():
            try:
                proc = subprocess.run(
                    [*cmd, str(worker), json.dumps(payload)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("Resolve worker %s failed to start: %s", cmd, exc)
                continue
            line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")), None)
            if line is None:
                continue
            data = json.loads(line[len("RESULT:"):])
            if data.get("fusion_import_failed"):
                continue  # wrong Python version for this Resolve build; try the next one
            if data.get("not_connected"):
                return None
            data.setdefault("connected", True)
            return data
        return None
