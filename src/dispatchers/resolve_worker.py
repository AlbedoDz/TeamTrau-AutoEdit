"""Standalone DaVinci Resolve import worker (stdlib only).

``run_dispatch`` holds the project/import logic and is used both in-process by
``ResolveDispatcher`` and as a CLI (``python resolve_worker.py '<json payload>'``).

Why a separate process: Resolve's ``fusionscript`` only loads under the Python versions the
installed Resolve was built for (Resolve Studio 21 needs 3.13 here, while the project venv is
3.11). The dispatcher therefore falls back to running this file with a compatible interpreter.

Phase 22 / step 4 note: the project/timeline settings, ``sourceClipsPath`` import option and the
render format/codec selection below were written while Resolve was NOT running, so the setting
KEY NAMES that are not part of the documented scripting README (drop-frame, "Mismatched
resolution files", codec ids) are discovered at run time from ``GetSetting("")`` /
``GetRenderCodecs`` and every miss is reported in ``result["warnings"]`` instead of failing.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.core.platform_caps import detect_os, resolve_paths  # noqa: E402
from src.core.platform_paths import fusionscript_name  # noqa: E402

EXIT_FUSION_IMPORT_FAILED = 3
EXIT_NOT_CONNECTED = 4

_OS = detect_os()
_PATHS = resolve_paths(_OS)
DEFAULT_SCRIPT_API = str(_PATHS.api_dir)
DEFAULT_SCRIPT_LIB = str(_PATHS.lib_path.with_name(fusionscript_name(_OS)))

# Project "Mismatched resolution files" (Image Scaling) values, by AutoEdit fit mode. "auto" maps
# to fit because the exporter's adjust-transform scale is relative to the fit-conformed size.
SCALING_PRESETS: dict[str, str] = {
    "auto": "Scale entire image to fit",
    "fit": "Scale entire image to fit",
    "fill": "Scale full frame with crop",
    "stretch": "Stretch frame to all corners",
}
# Setting-name candidates (tried after any key discovered via GetSetting("")).
_SCALING_KEY_CANDIDATES = ("timelineInputResMismatchMode", "inputScalingPreset", "timelineInputResMismatch")
_DROP_FRAME_KEY_CANDIDATES = ("timelineDropFrame", "timelineDropFrameTimecode")
_SCALING_KEY_RE = re.compile(r"mismatch|inputscal", re.IGNORECASE)

# master -> (render format id, acceptable codec names/ids). Aliases: mp4/h265, mov+prores.
_HEVC = ("mp4", ("H265", "H.265", "HEVC"))
_PRORES_HQ = ("mov", ("ProRes422HQ", "Apple ProRes 422 HQ"))
MASTER_FORMATS: dict[str, tuple[str, tuple[str, ...]]] = {
    "hevc": _HEVC,
    "h265": _HEVC,
    "mp4": _HEVC,
    "h264": ("mp4", ("H264", "H.264")),
    "prores_422": ("mov", ("ProRes422", "Apple ProRes 422")),
    "prores_hq": _PRORES_HQ,
    "prores": _PRORES_HQ,
    "mov+prores": _PRORES_HQ,
    "dnxhr_hqx": ("mov", ("DNxHRHQX", "DNxHR HQX")),
}


def frame_rate_setting(fps: float) -> str:
    """Resolve expects '29.97' / '23.976' / '30', never a rounded integer for NTSC rates."""
    return f"{float(fps):g}"


def unique_project_name(existing: Any, name: str) -> str:
    names = set(existing.values()) if isinstance(existing, dict) else set(existing or [])
    return name if name not in names else f"{name}_{time.strftime('%Y%m%d_%H%M%S')}"


def source_clips_path(media: list[str]) -> str | None:
    """Folder Resolve should search for source clips: the media folder (or their common parent)."""
    dirs = sorted({os.path.dirname(os.path.abspath(p)) for p in media})
    if not dirs:
        return None
    if len(dirs) == 1:
        return dirs[0]
    try:
        common = os.path.commonpath(dirs)
    except ValueError:  # different drives
        return dirs[0]
    return common if os.path.dirname(common) != common else dirs[0]  # never a drive root


def master_format_codec(master: str | None) -> tuple[str, tuple[str, ...]]:
    """Render (format, codec candidates) for a master name; unknown names fall back to HEVC/mp4."""
    return MASTER_FORMATS.get(str(master or "hevc").strip().lower(), _HEVC)


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def resolve_render_codec(proj: Any, fmt: str, candidates: tuple[str, ...]) -> str | None:
    """Pick the codec id Resolve offers for ``fmt`` matching one of ``candidates``.

    Without a usable ``GetRenderCodecs`` listing the first candidate is used as-is.
    """
    try:
        codecs = proj.GetRenderCodecs(fmt)
    except Exception:
        codecs = None
    if not isinstance(codecs, dict):
        return candidates[0]
    wanted = {_norm(c) for c in candidates}
    for display, code in codecs.items():
        if _norm(display) in wanted or _norm(code) in wanted:
            return str(code)
    return None


def _all_settings(proj: Any) -> dict[str, Any]:
    try:
        data = proj.GetSetting("")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _set_first(proj: Any, keys: tuple[str, ...], value: str, known: dict[str, Any]) -> str | None:
    """SetSetting on the first key Resolve accepts; returns the key used (None if none worked)."""
    present = [k for k in keys if k in known] if known else []
    for key in present or list(keys):
        try:
            if proj.SetSetting(key, value):
                return key
        except Exception:
            continue
    return None


def apply_timeline_settings(proj: Any, payload: dict[str, Any], info: dict[str, Any]) -> None:
    """Canvas, frame rate, drop-frame and image scaling for a freshly created project."""
    warnings: list[str] = info.setdefault("warnings", [])
    proj.SetSetting("timelineResolutionWidth", str(payload["width"]))
    proj.SetSetting("timelineResolutionHeight", str(payload["height"]))
    proj.SetSetting("timelineFrameRate", frame_rate_setting(payload["fps"]))
    known = _all_settings(proj)
    if _set_first(proj, _DROP_FRAME_KEY_CANDIDATES, "0", known) is None:
        warnings.append("could not set timeline drop-frame (setting not exposed by this Resolve)")

    fit_mode = str(payload.get("fit_mode") or "auto").lower()
    scaling = SCALING_PRESETS.get(fit_mode)
    if scaling is None:
        warnings.append(f"unknown fit_mode {fit_mode!r}; image scaling left at the Resolve default")
        return
    discovered = tuple(k for k in known if _SCALING_KEY_RE.search(k))
    key = _set_first(proj, discovered + _SCALING_KEY_CANDIDATES, scaling, known if discovered else {})
    if key is None:
        warnings.append("could not set 'Mismatched resolution files' (key not exposed by this Resolve)")
    else:
        info["image_scaling"] = {key: scaling}


def configure_render(proj: Any, payload: dict[str, Any], name: str, render_dir: str, info: dict[str, Any]) -> None:
    """Format/codec, frame rate and output location for the queued render."""
    warnings: list[str] = info.setdefault("warnings", [])
    fmt, candidates = master_format_codec(payload.get("master"))
    codec = resolve_render_codec(proj, fmt, candidates)
    if codec is None:
        warnings.append(f"codec {candidates[0]!r} not offered for {fmt!r}; render keeps Resolve's default codec")
    elif proj.SetCurrentRenderFormatAndCodec(fmt, codec):
        info["render_format"], info["render_codec"] = fmt, codec
    else:
        warnings.append(f"SetCurrentRenderFormatAndCodec({fmt!r}, {codec!r}) was rejected")
    proj.SetRenderSettings(
        {
            "SelectAllFrames": 1,
            "TargetDir": render_dir,
            "CustomName": f"{name}_master",
            "FormatWidth": payload["width"],
            "FormatHeight": payload["height"],
            "FrameRate": float(frame_rate_setting(payload["fps"])),
            "ExportVideo": True,
            "ExportAudio": True,
        }
    )


def run_dispatch(app: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a NEW Resolve project, import media + FCPXML, optionally queue a render.

    The currently open project is saved and left untouched (never reused or reconfigured).
    Optional payload keys: ``fit_mode`` (auto|fit|fill|stretch), ``color``, ``master``.
    """
    info: dict[str, Any] = {
        "connected": True,
        "project_name": payload["project_name"],
        "canvas": f"{payload['width']}x{payload['height']}@{payload['fps']}fps",
        "fit_mode": payload.get("fit_mode") or "auto",
        "color": payload.get("color"),
        "master": payload.get("master") or "hevc",
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

    apply_timeline_settings(proj, payload, info)

    media_pool = proj.GetMediaPool()
    valid = [p for p in payload.get("media", []) if os.path.exists(p)]
    if valid:
        media_pool.ImportMedia(valid)

    options: dict[str, Any] = {"timelineName": name, "importSourceClips": True}
    clips_dir = source_clips_path(valid)
    timeline = None
    if clips_dir:
        timeline = media_pool.ImportTimelineFromFile(payload["fcpxml"], {**options, "sourceClipsPath": clips_dir})
    if not timeline:  # builds that reject sourceClipsPath: retry with the verified options
        timeline = media_pool.ImportTimelineFromFile(payload["fcpxml"], options)
    if timeline:
        info["imported_timeline"] = True
        proj.SetCurrentTimeline(timeline)

    render_dir = payload.get("render_dir")
    if render_dir and timeline:
        os.makedirs(render_dir, exist_ok=True)
        configure_render(proj, payload, name, render_dir, info)
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
