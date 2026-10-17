"""Standalone DaVinci Resolve import worker (stdlib only).

``run_dispatch`` holds the project/import logic and is used both in-process by
``ResolveDispatcher`` and as a CLI (``python resolve_worker.py '<json payload>'``).

Why a separate process: Resolve's ``fusionscript`` only loads under the Python versions the
installed Resolve was built for (Resolve Studio 21 needs 3.13 here, while the project venv is
3.11). The dispatcher therefore falls back to running this file with a compatible interpreter.
"""

import json
import os
import sys
import time
from typing import Any

EXIT_FUSION_IMPORT_FAILED = 3
EXIT_NOT_CONNECTED = 4

DEFAULT_SCRIPT_API = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
DEFAULT_SCRIPT_LIB = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"


def frame_rate_setting(fps: float) -> str:
    """Resolve expects '29.97' / '23.976' / '30', never a rounded integer for NTSC rates."""
    return f"{float(fps):g}"


def unique_project_name(existing: Any, name: str) -> str:
    names = set(existing.values()) if isinstance(existing, dict) else set(existing or [])
    return name if name not in names else f"{name}_{time.strftime('%Y%m%d_%H%M%S')}"


def run_dispatch(app: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a NEW Resolve project, import media + FCPXML, optionally queue a render.

    The currently open project is saved and left untouched (never reused or reconfigured).
    """
    info: dict[str, Any] = {
        "connected": True,
        "project_name": payload["project_name"],
        "canvas": f"{payload['width']}x{payload['height']}@{payload['fps']}fps",
        "imported_timeline": False,
        "queued_render": False,
        "dry_run": False,
    }
    pm = app.GetProjectManager()
    if pm.GetCurrentProject():
        pm.SaveProject()
    name = unique_project_name(pm.GetProjectListInCurrentFolder(), payload["project_name"])
    proj = pm.CreateProject(name)
    if not proj:
        info["error"] = f"Failed to create project '{name}'"
        return info
    info["project_name"] = name

    proj.SetSetting("timelineResolutionWidth", str(payload["width"]))
    proj.SetSetting("timelineResolutionHeight", str(payload["height"]))
    proj.SetSetting("timelineFrameRate", frame_rate_setting(payload["fps"]))

    media_pool = proj.GetMediaPool()
    valid = [p for p in payload.get("media", []) if os.path.exists(p)]
    if valid:
        media_pool.ImportMedia(valid)

    timeline = media_pool.ImportTimelineFromFile(
        payload["fcpxml"], {"timelineName": name, "importSourceClips": True}
    )
    if timeline:
        info["imported_timeline"] = True
        proj.SetCurrentTimeline(timeline)

    render_dir = payload.get("render_dir")
    if render_dir and timeline:
        os.makedirs(render_dir, exist_ok=True)
        proj.SetRenderSettings(
            {
                "SelectAllFrames": 1,
                "TargetDir": render_dir,
                "CustomName": f"{name}_master",
                "FormatWidth": payload["width"],
                "FormatHeight": payload["height"],
            }
        )
        job_id = proj.AddRenderJob()
        if job_id:
            info["queued_render"] = True
            info["render_job_id"] = str(job_id)
    return info


def _main(argv: list[str]) -> int:
    payload = json.loads(argv[1])
    os.environ.setdefault("RESOLVE_SCRIPT_API", payload.get("script_api") or DEFAULT_SCRIPT_API)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", payload.get("script_lib") or DEFAULT_SCRIPT_LIB)
    sys.path.append(os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules"))
    try:
        import DaVinciResolveScript as dvr  # type: ignore
    except (ImportError, SystemError) as exc:
        print("RESULT:" + json.dumps({"error": f"fusionscript import failed: {exc}", "fusion_import_failed": True}))
        return EXIT_FUSION_IMPORT_FAILED
    app = dvr.scriptapp("Resolve")
    if app is None:
        print("RESULT:" + json.dumps({"error": "Resolve not reachable (is External scripting set to Local?)", "not_connected": True}))
        return EXIT_NOT_CONNECTED
    print("RESULT:" + json.dumps(run_dispatch(app, payload), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
