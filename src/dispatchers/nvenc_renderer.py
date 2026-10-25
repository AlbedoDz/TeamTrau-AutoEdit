"""Standalone headless video rendering engine utilizing FFmpeg NVENC acceleration.

Rendering is two-stage:

1. The edit timeline is cut into *render units*. Almost every unit is a clip (or, around a
   cross-dissolve, a slice of a clip); each unit is rendered to its own normalized segment
   (canvas, exact fps, exact frame count, PCM camera audio). Each unit input is fast-seeked
   with ``-ss/-t`` so ffmpeg never decodes footage before the in-point, and units can be
   encoded in parallel.
2. Segments are joined with the concat demuxer (video stream-copied) and the soundtrack /
   camera audio mix is encoded once into the final container.

Segment codec (no double lossy encode): segments are encoded with the *final* video codec
(HEVC / H.264 / ProRes / DNxHR, with the master's pixel format and colour tags), so the join is a
stream copy and nothing is encoded twice. Segments are ``.mov``: unlike Matroska (millisecond
timestamps -> NTSC rates drift to e.g. 90000/2999) QuickTime keeps the exact 1001-based timebase, and
it carries every master codec. ``fps=`` / ``-r`` always use the exact rational (30000/1001).

Cross-dissolve (``EditClip.transition_in_frames`` = N on the incoming clip B, outgoing clip A):
the dissolve is centred on the cut ``c``: it covers timeline frames ``[c - N//2, c + N - N//2)``.
Instead of re-encoding the whole master through ``xfade``, that window becomes its own *transition
unit* (A's last ``N//2`` frames + ``N - N//2`` frames of source *after* A's out-point, blended with
``xfade=fade`` against B's ``N//2`` frames of source *before* B's in-point + B's first
``N - N//2`` frames, audio via ``acrossfade``). The neighbouring clip units are shortened by the
same amount (A loses ``N//2`` tail frames, B loses ``N - N//2`` head frames), so
``len(A_body) + N + len(B_body) = len(A) + len(B)``: the total length is exactly the timeline
length and every other frame stays stream-copied. Handles beyond the source clamp (hold first /
last frame). N is capped at ``min(len(A), len(B))`` and N < 2 is a hard cut.

The master is written to ``<name>.part`` and only renamed on success, so a failed
or cancelled render never leaves a corrupt deliverable behind.
"""

import dataclasses
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.core.config import PipelineConfig, check_nvenc_available
from src.core.ffmpeg_utils import (
    atempo_chain,
    has_audio_stream,
    is_hw_encoder_error,
    popen_tool,
    run_tool,
)
from src.core.media_info import MediaInfo, probe_media
from src.core.platform_caps import get_caps
from src.core.timebase import frame_rate_fraction
from src.core.types import (
    DeliverColor,
    DeliverSettings,
    EditClip,
    EditTimeline,
    RenderJobResult,
    ScoredScene,
    SentenceSegment,
    SongSection,
)
from src.dispatchers.encoder_ladder import hw_video_args, pick_hw_encoder
from src.dispatchers.watermark_helper import build_watermark_filter_lines
from src.modules.edit_timeline import build_edit_timeline, retarget_timeline
from src.modules.subtitle_export import (
    burn_filter,
    prepare_filter_path,
    write_srt,
    write_subtitle_sidecars,
)

logger = logging.getLogger(__name__)

AUDIO_RATE = 48000
PREVIEW_HEIGHT = 540
PRO_MASTERS: frozenset[str] = frozenset({"prores_422", "prores_hq", "dnxhr_hqx"})

# (colorspace, color_trc, color_primaries, color_range) written on the master.
COLOR_TAGS: dict[str, tuple[str, str, str, str]] = {
    "rec709": ("bt709", "bt709", "bt709", "tv"),
    "rec2020_hlg": ("bt2020nc", "arib-std-b67", "bt2020", "tv"),
    "rec2020_pq": ("bt2020nc", "smpte2084", "bt2020", "tv"),
}
_HDR_COLORS: frozenset[str] = frozenset({"rec2020_hlg", "rec2020_pq"})
_COLOR_TRANSFER: dict[str, str] = {"rec709": "bt709", "rec2020_hlg": "arib-std-b67", "rec2020_pq": "smpte2084"}

# zscale spelling of ffprobe/ffmpeg colour names.
_ZS_MATRIX = {"bt709": "709", "bt2020nc": "2020_ncl", "bt2020c": "2020_cl", "smpte170m": "170m", "bt470bg": "470bg"}
_ZS_PRIMARIES = {"bt709": "709", "bt2020": "2020", "smpte170m": "170m", "bt470bg": "470bg"}
_ZS_TRANSFER = {
    "bt709": "709", "smpte2084": "smpte2084", "arib-std-b67": "arib-std-b67",
    "smpte170m": "601", "bt2020-10": "2020_10",
}


# A render segment is exactly one clip of the edit timeline.
RenderSegment = EditClip


class RenderError(RuntimeError):
    """FFmpeg render failure carrying the tail of ffmpeg's stderr."""

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(f"{message}\n{stderr[-1500:]}" if stderr else message)
        self.stderr = stderr


def _even(v: float) -> int:
    return max(2, int(round(v / 2.0)) * 2)


def _fps_str(fps: Fraction) -> str:
    """Exact rational for ffmpeg: ``30`` or ``30000/1001`` (never a rounded float)."""
    return str(fps.numerator) if fps.denominator == 1 else f"{fps.numerator}/{fps.denominator}"


def _ss(seconds: float) -> str:
    """``-ss`` value floored to the millisecond so a frame exactly on the in-point is kept."""
    return f"{max(0.0, math.floor(seconds * 1000.0 + 1e-6) / 1000.0):.3f}"


# ----------------------------------------------------------------------
# Master container / codec / colour (pure helpers)
# ----------------------------------------------------------------------
def master_container(settings: DeliverSettings | None, requested: Path | str | None = None) -> tuple[str, str]:
    """(file extension, ffmpeg muxer) of the deliverable.

    HEVC / H.264 -> ``.mp4``; ProRes -> ``.mov``; DNxHR -> ``.mov`` unless the requested output
    path already ends in ``.mxf`` (the only container choice DeliverSettings leaves open).
    """
    master = settings.master if settings is not None else "hevc"
    if master in ("prores_422", "prores_hq"):
        return ".mov", "mov"
    if master == "dnxhr_hqx":
        if requested is not None and str(requested).lower().removesuffix(".part").endswith(".mxf"):
            return ".mxf", "mxf"
        return ".mov", "mov"
    return ".mp4", "mp4"


def color_tag_args(color: str, passthrough: MediaInfo | None = None) -> list[str]:
    """``-colorspace/-color_trc/-color_primaries/-color_range`` for ``color`` (passthrough: the source's)."""
    if color in COLOR_TAGS:
        space, trc, prim, rng = COLOR_TAGS[color]
    elif color == "passthrough" and passthrough is not None:
        space, trc, prim, rng = (
            passthrough.color_space, passthrough.color_transfer,
            passthrough.color_primaries, passthrough.color_range,
        )
    else:
        return []
    args: list[str] = []
    for flag, value in (
        ("-colorspace", space), ("-color_trc", trc), ("-color_primaries", prim), ("-color_range", rng),
    ):
        if value and value.lower() not in ("unknown", "unspecified", ""):
            args += [flag, value]
    return args


@lru_cache(maxsize=1)
def available_filters() -> frozenset[str]:
    """Names of the filters the ffmpeg on PATH was built with (empty set if it cannot be queried)."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return frozenset()
    try:
        res = run_tool([ffmpeg_bin, "-hide_banner", "-filters"], timeout=30)
    except (subprocess.SubprocessError, OSError):
        return frozenset()
    names: set[str] = set()
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) in (2, 3) and set(parts[0]) <= set("TSC."):
            names.add(parts[1])
    return frozenset(names)


def color_conversion_chain(
    info: MediaInfo | None, color: DeliverColor, pix_fmt: str, filters: frozenset[str] | None = None
) -> list[str]:
    """Filters that convert a source's colour volume to ``color`` (empty = nothing to do).

    * HDR source -> ``rec709``: tone-map. ``zscale``: linearise (npl=100), ``tonemap=hable``, back to
      BT.709 limited; if only ``tonemapx`` exists, use it; if neither exists the frames are passed
      through untouched (logged) - colours will look flat but the render never fails.
    * SDR source -> ``rec2020_hlg/pq``: re-encode into BT.2020 primaries with the target transfer
      (SDR white mapped to ~100 nits / HLG reference white; an approximation, not an inverse
      tone-map).  HDR source of the *other* HDR transfer (HLG<->PQ): linear-light re-encode
      (npl=1000).
    * ``passthrough`` or matching source: nothing.
    """
    if info is None or not info.has_video or color == "passthrough":
        return []
    filters = available_filters() if filters is None else filters
    src_tr = (info.color_transfer or "").lower()
    src_prim = (info.color_primaries or "").lower()
    src_mat = (info.color_space or "").lower()
    src_hdr = bool(info.is_hdr)

    if color == "rec709":
        if not src_hdr:
            return []
        if "zscale" in filters:
            tin = _ZS_TRANSFER.get(src_tr, "smpte2084")
            pin = _ZS_PRIMARIES.get(src_prim, "2020")
            min_ = _ZS_MATRIX.get(src_mat, "2020_ncl")
            return [
                f"zscale=tin={tin}:min={min_}:pin={pin}:rin=tv:t=linear:npl=100",
                "format=gbrpf32le",
                "tonemap=tonemap=hable:desat=0",
                "zscale=p=709:t=709:m=709:r=tv",
                f"format={pix_fmt}",
            ]
        if "tonemapx" in filters:
            return [f"tonemapx=tonemap=hable:desat=0:p=bt709:t=bt709:m=bt709:r=tv:format={pix_fmt}"]
        logger.warning("ffmpeg has neither zscale nor tonemapx: HDR source rendered without tone-mapping.")
        return []

    # Rec.2020 HLG / PQ targets
    target_tr = _COLOR_TRANSFER[color]
    if src_hdr and src_tr == target_tr:
        return []
    if "zscale" not in filters:
        logger.warning("ffmpeg has no zscale: %s master is tagged only, pixels are not converted.", color)
        return []
    if src_hdr:
        tin = _ZS_TRANSFER.get(src_tr, "smpte2084")
        pin = _ZS_PRIMARIES.get(src_prim, "2020")
        min_ = _ZS_MATRIX.get(src_mat, "2020_ncl")
        npl = 1000
    else:
        tin = _ZS_TRANSFER.get(src_tr, "709")
        pin = _ZS_PRIMARIES.get(src_prim, "709")
        min_ = _ZS_MATRIX.get(src_mat, "709")
        npl = 100
    # Primaries are converted in the first (linearising) pass: doing it after the RGB conversion makes
    # zscale fail with "no path between colorspaces".
    return [
        f"zscale=tin={tin}:min={min_}:pin={pin}:rin=tv:t=linear:npl={npl}:p=2020",
        "format=gbrpf32le",
        f"zscale=t={target_tr}",
        "zscale=m=2020_ncl:r=tv",
        f"format={pix_fmt}",
    ]


# ----------------------------------------------------------------------
# Render units (cross-dissolve planning)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class _Slice:
    """A run of ``frames`` output frames cut from one clip.

    ``seg.source_in`` is already clamped to >= 0 and ``seg.available`` adjusted. ``lead_frames`` first
    frames repeat the first source frame (dissolve pre-roll before the start of the media).
    ``kb_first``/``kb_total`` locate the first *source* frame inside the clip for Ken Burns continuity.
    """

    seg: EditClip
    frames: int
    lead_frames: int = 0
    kb_first: int = 0
    kb_total: int = 0


@dataclass(frozen=True)
class _Unit:
    slices: tuple[_Slice, ...]  # 1 = plain clip slice, 2 = dissolve (outgoing, incoming)
    start_frame: int  # position on the timeline
    frames: int

    @property
    def is_transition(self) -> bool:
        return len(self.slices) == 2


def plan_units(segments: Sequence[EditClip], fps: Fraction) -> list[_Unit]:
    """Cut the timeline into clip bodies and dissolve units (see the module docstring for the maths)."""
    n = len(segments)
    trans = [0] * n  # trans[i]: dissolve length between clip i-1 and clip i
    for i in range(1, n):
        want = int(segments[i].transition_in_frames)
        cap = min(want, segments[i - 1].frames, segments[i].frames)
        trans[i] = cap if cap >= 2 else 0

    def clip_slice(i: int, first_src: int, frames: int, lead: int = 0) -> _Slice:
        """Slice of clip ``i`` whose first *source* frame is clip-local frame ``first_src``."""
        seg = segments[i]
        delta = first_src / float(fps) * seg.speed
        return _Slice(
            dataclasses.replace(seg, source_in=seg.source_in + delta, available=max(0.0, seg.available - delta)),
            frames, lead_frames=lead, kb_first=first_src, kb_total=seg.frames,
        )

    units: list[_Unit] = []
    cursor = 0
    for i, seg in enumerate(segments):
        head = trans[i] - trans[i] // 2 if trans[i] else 0
        tail = trans[i + 1] // 2 if i + 1 < n and trans[i + 1] else 0
        body = seg.frames - head - tail
        if trans[i]:
            n_i = trans[i]
            n1 = n_i // 2
            # Incoming pre-roll: n1 frames of source before B's in-point, clamped at the media start
            # (the first ``lead`` frames then repeat B's first frame).
            lead = max(0, min(n1, math.ceil(n1 - seg.source_in * float(fps) / seg.speed - 1e-9)))
            a_slice = clip_slice(i - 1, segments[i - 1].frames - n1, n_i)
            b_slice = clip_slice(i, -n1 + lead, n_i, lead=lead)
            units.append(_Unit((a_slice, b_slice), cursor - n1, n_i))
        if body > 0:
            units.append(_Unit((clip_slice(i, head, body),), cursor + head, body))
        cursor += seg.frames
    return units


class NvencRenderer:
    """Builds per-segment filter graphs and renders headless video via FFmpeg NVENC."""

    def __init__(
        self,
        config: PipelineConfig,
        settings: DeliverSettings | None = None,
        intra_only: bool = False,
    ) -> None:
        if intra_only and (settings is None or settings.master != "h264"):
            raise ValueError("intra_only requires DeliverSettings with master='h264'")
        self.intra_only = intra_only
        self.config = config
        self.nvenc_available = check_nvenc_available()
        pref = config.hw_encoder
        if pref == "none":
            self.hw_encoders: dict[str, str | None] = {"h264": None, "hevc": None}
        elif pref == "nvenc":
            self.hw_encoders = {"h264": "h264_nvenc", "hevc": "hevc_nvenc"}
        else:
            caps = get_caps()
            self.hw_encoders = {
                "h264": pick_hw_encoder("h264", caps.h264_encoders, pref),
                "hevc": pick_hw_encoder("hevc", caps.hevc_encoders, pref),
            }
        self.settings = settings
        # Set by render_segments for ``color == "passthrough"`` from the actual sources.
        self._passthrough_ten_bit = False
        self._passthrough_info: MediaInfo | None = None

    @property
    def hardware_available(self) -> bool:
        """Whether any hardware encoder is selected or forced via hw_encoder='nvenc'."""
        return self.config.hw_encoder == "nvenc" or any(
            v is not None for v in self.hw_encoders.values()
        )

    def _nvenc_ok(self) -> bool:
        return self.config.hw_encoder == "nvenc" or (
            self.config.hw_encoder == "auto" and self.nvenc_available
        )

    # ------------------------------------------------------------------
    # Timeline -> segments
    # ------------------------------------------------------------------
    def build_segments(
        self,
        scored_scenes: list[ScoredScene],
        cut_timestamps: list[float],
        include_teaser: bool | None = None,
        sections: list[SongSection] | None = None,
    ) -> list[EditClip]:
        """Turn analysed scenes + beat cuts into frame-exact output segments."""
        if not scored_scenes:
            raise ValueError("No scenes provided for rendering.")
        if len(cut_timestamps) < 2:
            raise ValueError("cut_timestamps must have at least 2 boundary timestamps.")
        return build_edit_timeline(
            self.config,
            scored_scenes,
            cut_timestamps,
            sections=sections,
            include_teaser=include_teaser,
        ).clips

    # ------------------------------------------------------------------
    # Derived render parameters
    # ------------------------------------------------------------------
    @property
    def fps_fraction(self) -> Fraction:
        return frame_rate_fraction(self.config.fps)

    @property
    def audio_rate(self) -> int:
        return self.settings.audio_rate if self.settings is not None else AUDIO_RATE

    @property
    def audio_layout(self) -> str:
        return "mono" if self.settings is not None and self.settings.audio_channels == 1 else "stereo"

    @property
    def fit_mode(self) -> str:
        return self.settings.fit_mode if self.settings is not None else "auto"

    def _is_pro(self, preview: bool = False) -> bool:
        return self.settings is not None and not preview and self.settings.master in PRO_MASTERS

    def ten_bit(self, preview: bool = False) -> bool:
        """Whether the master video is 10-bit (HDR targets, ProRes/DNxHR, 10-bit passthrough)."""
        s = self.settings
        if s is None or preview:
            return False
        if s.master in PRO_MASTERS:
            return True
        if s.master == "h264":
            return False  # NVENC H.264 has no 10-bit profile; keep H.264 masters 8-bit.
        return s.color in _HDR_COLORS or (s.color == "passthrough" and self._passthrough_ten_bit)

    def video_pix_fmt(self, preview: bool = False) -> str:
        s = self.settings
        if s is None or preview:
            return self.config.nvenc_pix_fmt
        if s.master in PRO_MASTERS:
            return "yuv422p10le"
        return "yuv420p10le" if self.ten_bit(preview) else "yuv420p"

    def segment_suffix(self, preview: bool = False) -> str:
        return ".mov"

    def segment_muxer(self, preview: bool = False) -> str:
        return "mov"

    # ------------------------------------------------------------------
    # Command builders (pure, unit-testable)
    # ------------------------------------------------------------------
    def canvas_size(self, preview: bool = False) -> tuple[int, int]:
        w, h = self.config.canvas_width, self.config.canvas_height
        if preview and h > PREVIEW_HEIGHT:
            return _even(w * PREVIEW_HEIGHT / h), PREVIEW_HEIGHT
        return w, h

    def encoder_args(self, use_hw: bool, preview: bool = False) -> tuple[str, list[str]]:
        """Legacy encoder selection (settings-less masters and the 540p preview)."""
        cfg = self.config
        pix = ["-pix_fmt", cfg.nvenc_pix_fmt]
        if preview:
            if use_hw:
                enc = self.hw_encoders.get("h264") or ("h264_nvenc" if self._nvenc_ok() else None)
                if enc == "h264_nvenc":
                    return "h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "30", *pix]
                if enc is not None:
                    return enc, hw_video_args(
                        enc,
                        cq=30,
                        bitrate=None,
                        maxrate=None,
                        bufsize=None,
                        ten_bit=False,
                        intra_only=self.intra_only,
                    )
            return "libx264", ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", *pix]
        if use_hw:
            enc = self.hw_encoders.get("hevc") or ("hevc_nvenc" if self._nvenc_ok() else None)
            if enc == "hevc_nvenc":
                return "hevc_nvenc", [
                    "-c:v", "hevc_nvenc",
                    "-preset", cfg.nvenc_preset,
                    "-cq", str(cfg.nvenc_cq),
                    "-b:v", cfg.nvenc_bitrate,
                    "-maxrate", cfg.nvenc_maxrate,
                    "-bufsize", cfg.nvenc_bufsize,
                    *pix,
                ]
            if enc is not None:
                return enc, hw_video_args(
                    enc,
                    cq=cfg.nvenc_cq,
                    bitrate=cfg.nvenc_bitrate,
                    maxrate=cfg.nvenc_maxrate,
                    bufsize=cfg.nvenc_bufsize,
                    ten_bit=False,
                    intra_only=self.intra_only,
                )
        return "libx265", [
            "-c:v", "libx265",
            "-preset", cfg.cpu_encoder_preset,
            "-crf", str(cfg.nvenc_cq),
            "-x265-params", "log-level=error:repeat-headers=1",
            *pix,
        ]

    def master_encoder_args(
        self,
        settings: DeliverSettings | None,
        use_hw: bool,
        preview: bool = False,
        ten_bit: bool | None = None,
    ) -> tuple[str, list[str]]:
        """(encoder name, ffmpeg args) for the master video stream, including pixel format and colour tags.

        ``hevc`` -> hevc_nvenc / libx265 (main10 + yuv420p10le for HDR / 10-bit passthrough, else
        main + yuv420p); ``h264`` -> h264_nvenc / libx264 (8-bit); ``prores_422``/``prores_hq`` ->
        prores_ks profile 2/3, yuv422p10le, vendor apl0; ``dnxhr_hqx`` -> dnxhd dnxhr_hqx yuv422p10le.
        ``settings is None`` or ``preview`` -> the legacy encoder_args.
        """
        if settings is None or preview:
            return self.encoder_args(use_hw, preview)
        cfg = self.config
        master = settings.master
        tags = color_tag_args(settings.color, self._passthrough_info)
        if master in ("prores_422", "prores_hq"):
            profile = "2" if master == "prores_422" else "3"
            return "prores_ks", [
                "-c:v", "prores_ks", "-profile:v", profile, "-vendor", "apl0",
                "-pix_fmt", "yuv422p10le", *tags,
            ]
        if master == "dnxhr_hqx":
            return "dnxhd", [
                "-c:v", "dnxhd", "-profile:v", "dnxhr_hqx", "-pix_fmt", "yuv422p10le", *tags,
            ]
        if ten_bit is None:
            ten_bit = master == "hevc" and (
                settings.color in _HDR_COLORS or (settings.color == "passthrough" and self._passthrough_ten_bit)
            )
        pix = "yuv420p10le" if (ten_bit and master == "hevc") else "yuv420p"
        if master == "h264":
            if use_hw:
                enc = self.hw_encoders.get("h264") or ("h264_nvenc" if self._nvenc_ok() else None)
                if enc == "h264_nvenc":
                    nv_args = [
                        "-c:v", "h264_nvenc", "-preset", cfg.nvenc_preset, "-profile:v", "high",
                        "-cq", str(cfg.nvenc_cq), "-b:v", cfg.nvenc_bitrate, "-maxrate", cfg.nvenc_maxrate,
                        "-bufsize", cfg.nvenc_bufsize, "-pix_fmt", pix, *tags,
                    ]
                    if self.intra_only:
                        nv_args += ["-g", "0", "-bf", "0"]
                    return "h264_nvenc", nv_args
                if enc is not None:
                    return enc, [
                        *hw_video_args(
                            enc,
                            cq=cfg.nvenc_cq,
                            bitrate=cfg.nvenc_bitrate,
                            maxrate=cfg.nvenc_maxrate,
                            bufsize=cfg.nvenc_bufsize,
                            ten_bit=False,
                            intra_only=self.intra_only,
                        ),
                        *tags,
                    ]
            cpu_args = [
                "-c:v", "libx264", "-preset", cfg.cpu_encoder_preset, "-crf", str(cfg.nvenc_cq),
                "-pix_fmt", pix, *tags,
            ]
            if self.intra_only:
                cpu_args += ["-g", "1", "-bf", "0"]
            return "libx264", cpu_args
        # hevc
        if use_hw:
            enc = self.hw_encoders.get("hevc") or ("hevc_nvenc" if self._nvenc_ok() else None)
            if enc == "hevc_nvenc":
                return "hevc_nvenc", [
                    "-c:v", "hevc_nvenc", "-preset", cfg.nvenc_preset,
                    "-profile:v", "main10" if ten_bit else "main",
                    "-cq", str(cfg.nvenc_cq), "-b:v", cfg.nvenc_bitrate, "-maxrate", cfg.nvenc_maxrate,
                    "-bufsize", cfg.nvenc_bufsize, "-pix_fmt", pix, *tags,
                ]
            if enc is not None:
                return enc, [
                    *hw_video_args(
                        enc,
                        cq=cfg.nvenc_cq,
                        bitrate=cfg.nvenc_bitrate,
                        maxrate=cfg.nvenc_maxrate,
                        bufsize=cfg.nvenc_bufsize,
                        ten_bit=bool(ten_bit),
                        intra_only=self.intra_only,
                    ),
                    *tags,
                ]
        return "libx265", [
            "-c:v", "libx265", "-preset", cfg.cpu_encoder_preset, "-crf", str(cfg.nvenc_cq),
            "-x265-params", "log-level=error:repeat-headers=1", "-pix_fmt", pix, *tags,
        ]

    def _encoder(self, use_hw: bool, preview: bool) -> tuple[str, list[str]]:
        return self.master_encoder_args(self.settings, use_hw, preview, self.ten_bit(preview))

    # -- geometry ------------------------------------------------------
    def _geometry_filters(self, seg: RenderSegment, w: int, h: int, src: str, out: str) -> list[str]:
        """Scale / crop / backdrop the source label ``src`` onto a WxH canvas as ``out``.

        ``fit_mode``: ``fit`` letterboxes (scale + pad), ``fill`` covers and centre-crops, ``stretch``
        ignores the aspect ratio - all for any canvas shape. ``auto`` keeps the portrait reframing
        logic (crop window / blur backdrop when the canvas is taller than wide) and letterboxes
        otherwise. A crop window flagged ``crop_needs_recompute`` belongs to another aspect ratio
        and is ignored.
        """
        mode = self.fit_mode
        if mode == "stretch":
            return [f"[{src}]scale={w}:{h}[{out}]"]
        if mode == "fill":
            return [
                f"[{src}]scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2,"
                f"crop={w}:{h}[{out}]"
            ]
        cw = None if seg.crop_needs_recompute else seg.crop_window
        is_vertical = mode == "auto" and w < h
        is_blur_backdrop = is_vertical and (
            (cw is not None and cw.tracking_subject == "blur_backdrop")
            or getattr(self.config, "reframing_mode", "auto_hybrid") == "blur_backdrop"
        )
        if is_blur_backdrop:
            return [
                f"[{src}]split=2[bg][fg]",
                f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                f"boxblur=20:5,eq=brightness=-0.15[bgb]",
                f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs]",
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[{out}]",
            ]
        if cw is not None and is_vertical:
            return [
                f"[{src}]crop={cw.width}:{cw.height}:{cw.x}:{cw.y},scale={w}:{h}[{out}]"
            ]
        return [
            f"[{src}]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black[{out}]"
        ]

    # -- filter graphs -------------------------------------------------
    def _video_lines(
        self,
        sl: _Slice,
        canvas: tuple[int, int],
        in_label: str,
        out_label: str,
        tag: str,
        src_info: MediaInfo | None,
        preview: bool,
    ) -> list[str]:
        """Geometry, Ken Burns, exact-fps conform, colour conversion for one slice -> ``out_label``."""
        seg = sl.seg
        w, h = canvas
        fps = self.fps_fraction
        fps_s = _fps_str(fps)
        pix = self.video_pix_fmt(preview)
        lines = [f"[{in_label}]setpts=(PTS-STARTPTS)/{seg.speed:.4f}[vsp{tag}]"]
        lines += self._geometry_filters(seg, w, h, f"vsp{tag}", f"vgeo{tag}")

        label = f"vgeo{tag}"
        if seg.ken_burns in ("zoom_in", "zoom_out"):
            total = max(1, sl.kb_total or seg.frames)
            k = max(1.0, float(self.config.ken_burns_scale_factor)) - 1.0
            idx = f"max(0,on+({sl.kb_first}))" if sl.kb_first or sl.lead_frames else "on"
            z = f"1+{k:.4f}*{idx}/{total}" if seg.ken_burns == "zoom_in" else f"{1 + k:.4f}-{k:.4f}*{idx}/{total}"
            lines.append(
                f"[{label}]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d=1:s={w}x{h}:fps={fps_s}[vkb{tag}]"
            )
            label = f"vkb{tag}"

        out_dur = float(sl.frames / fps)
        pad = f"tpad=start_mode=clone:start_duration={float(sl.lead_frames / fps):.6f}:" if sl.lead_frames else "tpad="
        chain = [
            "setsar=1",
            f"fps={fps_s}",
            f"{pad}stop_mode=clone:stop_duration={out_dur + 1:.3f}",
            f"trim=end_frame={sl.frames}",
            "setpts=PTS-STARTPTS",
        ]
        if self.settings is not None and not preview:
            chain += color_conversion_chain(src_info, self.settings.color, pix)
        chain.append(f"format={pix}")  # xfade needs both inputs in one pixel format
        lines.append(f"[{label}]" + ",".join(chain) + f"[{out_label}]")
        return lines

    def _audio_lines(
        self, sl: _Slice, in_label: str, out_label: str, source_has_audio: bool, pro_audio: bool
    ) -> list[str]:
        fps = self.fps_fraction
        rate = self.audio_rate
        n_samples = int(round(sl.frames * rate / fps))
        fmt = "s32" if pro_audio else "s16"
        norm = f"aresample={rate},aformat=sample_fmts={fmt}:channel_layouts={self.audio_layout}"
        if not source_has_audio:
            return [
                f"anullsrc=r={rate}:cl={self.audio_layout},atrim=end_sample={n_samples},"
                f"aformat=sample_fmts={fmt}:channel_layouts={self.audio_layout}[{out_label}]"
            ]
        tempo = atempo_chain(sl.seg.speed)
        tempo = f"{tempo}," if tempo else ""
        delay = f"adelay={float(sl.lead_frames / fps) * 1000.0:.4f}:all=1," if sl.lead_frames else ""
        return [
            f"[{in_label}]asetpts=PTS-STARTPTS,{tempo}{norm},{delay}apad,"
            f"atrim=end_sample={n_samples},asetpts=PTS-STARTPTS[{out_label}]"
        ]

    def _unit_filter(
        self,
        unit: _Unit,
        canvas: tuple[int, int],
        with_audio: bool,
        sources_have_audio: Sequence[bool],
        watermark_input: int | None,
        src_infos: Sequence[MediaInfo | None],
        preview: bool = False,
        subtitle_file: Path | None = None,
    ) -> str:
        fps = self.fps_fraction
        pix = self.video_pix_fmt(preview)
        lines: list[str] = []
        if unit.is_transition:
            a, b = unit.slices
            lines += self._video_lines(a, canvas, "0:v", "vA", "A", src_infos[0], preview)
            lines += self._video_lines(b, canvas, "1:v", "vB", "B", src_infos[1], preview)
            lines.append(
                f"[vA][vB]xfade=transition=fade:duration={float(unit.frames / fps):.6f}:offset=0,"
                f"trim=end_frame={unit.frames},setpts=PTS-STARTPTS[vx]"
            )
            tail = "vx"
        else:
            lines += self._video_lines(unit.slices[0], canvas, "0:v", "vx", "", src_infos[0], preview)
            tail = "vx"

        post: list[str] = []
        if subtitle_file is not None:
            offset = float(unit.start_frame / fps)
            post += [
                f"setpts=PTS+{offset:.6f}/TB",
                burn_filter(
                    subtitle_file, canvas, self.config.burn_subtitle_font,
                    self.config.burn_subtitle_font_size or None,
                ),
                "setpts=PTS-STARTPTS",
            ]
        post.append(f"format={pix}")
        base_label = "vbase" if watermark_input is not None else "vseg"
        lines.append(f"[{tail}]" + ",".join(post) + f"[{base_label}]")
        if watermark_input is not None:
            lines.extend(
                line.rstrip(";")
                for line in build_watermark_filter_lines(
                    wm_input_idx=watermark_input,
                    base_v_label="vbase",
                    out_v_label="vseg",
                    position=self.config.watermark_position,
                    opacity=self.config.watermark_opacity,
                    margin=self.config.watermark_margin,
                )
            )

        if with_audio:
            pro = self._is_pro(preview)
            if unit.is_transition:
                a, b = unit.slices
                lines += self._audio_lines(a, "0:a", "aA", sources_have_audio[0], pro)
                lines += self._audio_lines(b, "1:a", "aB", sources_have_audio[1], pro)
                d = float(unit.frames / fps)
                n_samples = int(round(unit.frames * self.audio_rate / fps))
                lines.append(
                    f"[aA][aB]acrossfade=d={d:.6f}:c1=qsin:c2=qsin,apad,"
                    f"atrim=end_sample={n_samples},asetpts=PTS-STARTPTS[aseg]"
                )
            else:
                lines += self._audio_lines(unit.slices[0], "0:a", "aseg", sources_have_audio[0], pro)
        return ";".join(lines)

    def build_segment_filter(
        self,
        seg: RenderSegment,
        canvas: tuple[int, int],
        with_audio: bool,
        source_has_audio: bool,
        watermark_input: int | None = None,
        src_info: MediaInfo | None = None,
        preview: bool = False,
        timeline_start_frame: int = 0,
        subtitle_file: Path | None = None,
    ) -> str:
        """Filter graph for one whole-clip segment: input 0 is the seeked source clip."""
        unit = _Unit((_Slice(seg, seg.frames, kb_total=seg.frames),), timeline_start_frame, seg.frames)
        return self._unit_filter(
            unit, canvas, with_audio, [source_has_audio], watermark_input, [src_info], preview, subtitle_file
        )

    def _unit_command(
        self,
        ffmpeg_bin: str,
        unit: _Unit,
        out_path: Path,
        use_hw: bool,
        with_audio: bool,
        preview: bool = False,
        hwdecode: bool = False,
        subtitle_file: Path | None = None,
    ) -> list[str]:
        canvas = self.canvas_size(preview)
        fps = self.fps_fraction
        wm = self._watermark_path()
        probe_colour = self.settings is not None and not preview
        enc, enc_args = self._encoder(use_hw, preview)

        cmd = [ffmpeg_bin, "-y", "-hide_banner", "-nostdin", "-loglevel", "error"]
        has_audio: list[bool] = []
        infos: list[MediaInfo | None] = []
        for sl in unit.slices:
            seg = sl.seg
            n_src = sl.frames - sl.lead_frames
            src_dur = min(seg.available, float(n_src / fps) * seg.speed)
            has_audio.append(with_audio and has_audio_stream(seg.source_path))
            infos.append(probe_media(seg.source_path) if probe_colour else None)
            if hwdecode and enc.endswith("_nvenc"):
                # NVDEC decode; no -hwaccel_output_format, so frames are downloaded to system
                # memory and every CPU filter (scale/crop/zoompan/overlay/...) keeps working.
                cmd += ["-hwaccel", "cuda"]
            cmd += ["-ss", _ss(seg.source_in), "-t", f"{src_dur + 0.25:.3f}"]
            cmd += ["-i", str(Path(seg.source_path).resolve())]
        wm_idx = None
        if wm is not None:
            cmd += ["-loop", "1", "-i", str(wm)]
            wm_idx = len(unit.slices)

        graph = self._unit_filter(
            unit, canvas, with_audio, has_audio, wm_idx, infos, preview, subtitle_file
        )
        cmd += ["-filter_complex", graph, "-map", "[vseg]"]
        cmd += enc_args
        cmd += ["-r", _fps_str(fps)]
        if with_audio:
            codec = "pcm_s24le" if self._is_pro(preview) else "pcm_s16le"
            cmd += ["-map", "[aseg]", "-c:a", codec]
        cmd += ["-f", self.segment_muxer(preview), str(out_path)]
        return cmd

    def build_segment_command(
        self,
        ffmpeg_bin: str,
        seg: RenderSegment,
        out_path: Path,
        use_hw: bool,
        with_audio: bool,
        preview: bool = False,
        hwdecode: bool = False,
    ) -> list[str]:
        unit = _Unit((_Slice(seg, seg.frames, kb_total=seg.frames),), 0, seg.frames)
        return self._unit_command(ffmpeg_bin, unit, out_path, use_hw, with_audio, preview, hwdecode)

    def _ducking_ratio(self) -> float:
        """Sidechain ratio giving ~``ducking_attenuation_db`` of music reduction under speech.

        Gain reduction = overshoot * (1 - 1/ratio); speech is assumed ~20 dB over the 0.05 threshold,
        so ratio = 1 / (1 - |att|/20), clamped to [2, 20] (-14 dB -> 3.33).
        """
        att = abs(float(self.config.ducking_attenuation_db))
        return max(2.0, min(20.0, 1.0 / max(0.05, 1.0 - att / 20.0)))

    def build_concat_command(
        self,
        ffmpeg_bin: str,
        list_file: Path,
        out_path: Path,
        total_duration: float,
        segments_have_audio: bool,
        soundtrack_path: Path | None,
        soundtrack_in: float = 0.0,
        encoder: str = "hevc_nvenc",
        preview: bool = False,
    ) -> list[str]:
        """Join stream-copied video segments and encode the final audio mix once."""
        s = None if preview else self.settings
        pro = s is not None and s.master in PRO_MASTERS
        rate = self.audio_rate if s is not None else AUDIO_RATE
        layout = self.audio_layout if s is not None else "stereo"
        fmt = ":sample_fmts=s32" if pro else ""
        cmd = [ffmpeg_bin, "-y", "-hide_banner", "-nostdin", "-loglevel", "error"]
        cmd += ["-f", "concat", "-safe", "0", "-i", str(list_file)]
        norm = f"aresample={rate},aformat=channel_layouts={layout}"
        dur = f"{total_duration:.6f}"
        limiter = (
            f"alimiter=limit={float(self.config.master_audio_limit):.2f}:level=disabled:latency=1,"
            f"aresample={rate},aformat=sample_rates={rate}:channel_layouts={layout}{fmt}"
        )
        mixed: str | None = None  # graph ending in [amix]; the limiter + final format follow
        if soundtrack_path is not None:
            cmd += ["-ss", f"{max(0.0, soundtrack_in):.3f}", "-i", str(Path(soundtrack_path).resolve())]
            music = f"[1:a]{norm},apad,atrim=duration={dur},asetpts=PTS-STARTPTS"
            if segments_have_audio:
                if self.config.workflow_mode == "unboxing_narration":
                    ratio = self._ducking_ratio()
                    mixed = (
                        f"{music}[mus];[0:a]{norm},asplit=2[cam][sc];"
                        f"[mus][sc]sidechaincompress=threshold=0.05:ratio={ratio:.2f}:attack=20:release=300[duck];"
                        f"[duck][cam]amix=inputs=2:duration=first:normalize=0[amix]"
                    )
                else:
                    mixed = (
                        f"{music}[mus];[0:a]{norm},volume={self.config.camera_audio_mix_db}dB[cam];"
                        f"[mus][cam]amix=inputs=2:duration=first:normalize=0[amix]"
                    )
            else:
                mixed = f"{music}[amix]"
        elif segments_have_audio:
            mixed = f"[0:a]{norm}[amix]"

        cmd += ["-map", "0:v"]
        if mixed is not None:
            # sidechaincompress/amix occasionally drop the last ~0.17 s under CPU load (seen in
            # test_narration_workflow_ducking_graph_renders), so pin the mix to the exact length.
            graph = f"{mixed};[amix]apad=whole_dur={dur},atrim=duration={dur},{limiter}[aout]"
            audio_codec = ["-c:a", "pcm_s24le"] if pro else ["-c:a", "aac", "-b:a", str(self.config.master_audio_bitrate)]
            cmd += ["-filter_complex", graph, "-map", "[aout]", *audio_codec]
            if s is not None:
                cmd += ["-ar", str(rate), "-ac", "1" if layout == "mono" else "2"]
        cmd += ["-c:v", "copy"]
        if "hevc" in encoder or encoder == "libx265":
            cmd += ["-tag:v", "hvc1"]
        if s is not None:
            cmd += color_tag_args(s.color, self._passthrough_info)
        muxer = master_container(s, out_path)[1] if s is not None else "mp4"
        cmd += ["-t", dur]
        if muxer == "mp4":
            cmd += ["-movflags", "+faststart"]
        cmd += ["-f", muxer, str(out_path)]
        return cmd

    def _watermark_path(self) -> Path | None:
        cfg = self.config
        if cfg.watermark_enabled and cfg.watermark_path and Path(cfg.watermark_path).exists():
            return Path(cfg.watermark_path).resolve()
        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _run(
        self,
        cmd: list[str],
        cancel_event: threading.Event | None,
        job_handle: Any | None,
    ) -> tuple[int, str]:
        proc = popen_tool(cmd)
        if job_handle is not None and hasattr(job_handle, "_child_process"):
            job_handle._child_process = proc
        stderr = ""
        while True:
            try:
                _out, stderr = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    proc.terminate()
                    try:
                        proc.communicate(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
                    raise InterruptedError("Rendering cancelled by user.") from None
        return proc.returncode, stderr or ""

    def _derive(self, settings: DeliverSettings | None, **config_updates: Any) -> "NvencRenderer":
        """A renderer sharing this one's NVENC probe, with ``settings`` and config overrides applied.

        If ``settings`` is None, the receiver's settings are kept.
        """
        cfg = self.config.model_copy(update=config_updates) if config_updates else self.config
        other = NvencRenderer.__new__(NvencRenderer)
        other.config = cfg
        other.nvenc_available = self.nvenc_available
        other.hw_encoders = dict(self.hw_encoders)
        other.settings = settings if settings is not None else self.settings
        other.intra_only = self.intra_only if (other.settings is not None and other.settings.master == "h264") else False
        other._passthrough_ten_bit = False
        other._passthrough_info = None
        return other

    def render_segments(
        self,
        segments: list[RenderSegment],
        soundtrack_path: Path | None,
        output_file: Path,
        force_cpu: bool = False,
        preview: bool = False,
        soundtrack_in: float = 0.0,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        job_handle: Any | None = None,
        settings: DeliverSettings | None = None,
        sentences: Sequence[SentenceSegment] | None = None,
    ) -> RenderJobResult:
        """Render an explicit, ordered list of segments to ``output_file``.

        With ``settings`` the output container follows the master codec (the returned
        ``output_path`` carries the adjusted suffix); the canvas / fps come from ``self.config``.
        """
        if settings is not None and settings is not self.settings:
            derived = self._derive(
                settings,
                canvas_width=settings.canvas_width,
                canvas_height=settings.canvas_height,
                fps=settings.fps,
            )
            return derived.render_segments(
                segments, soundtrack_path, output_file, force_cpu, preview, soundtrack_in,
                progress_callback, cancel_event, job_handle, settings, sentences,
            )
        if not segments:
            raise ValueError("No segments provided for rendering.")
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise FileNotFoundError("FFmpeg executable not found on system PATH.")

        output_file = Path(output_file)
        active = self.settings if not preview else None
        if active is not None:
            ext, _muxer = master_container(active, output_file)
            output_file = output_file.with_suffix(ext)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        part_file = output_file.with_name(output_file.name + ".part")
        fps = self.fps_fraction
        total_frames = sum(s.frames for s in segments)
        total_duration = float(total_frames / fps)

        has_soundtrack = soundtrack_path is not None and Path(soundtrack_path).exists()
        with_audio = (not has_soundtrack) or bool(self.config.keep_video_audio)
        use_hw = (self.hardware_available or self._nvenc_ok()) and not force_cpu

        if active is not None and active.color == "passthrough":
            infos = [probe_media(s.source_path) for s in segments]
            self._passthrough_ten_bit = any(i.is_hdr or (i.bit_depth or 0) >= 10 for i in infos)
            self._passthrough_info = next((i for i in infos if i.has_video), None)

        units = plan_units(segments, fps)
        seg_suffix = self.segment_suffix(preview)

        work_root = Path(self.config.cache_dir).resolve() / "render"
        work_root.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="seg_", dir=work_root))

        subtitle_file: Path | None = None
        if active is not None and active.subtitles == "burn" and sentences:
            srt_path = write_srt(sentences, work_dir / "burn.srt")
            subtitle_file = prepare_filter_path(srt_path)

        def notify(pct: float, msg: str) -> None:
            if progress_callback is not None:
                progress_callback(pct, msg)

        try:
            while True:
                seg_paths = [work_dir / f"seg_{i:05d}{seg_suffix}" for i in range(len(units))]
                chosen_enc, _ = self._encoder(use_hw, preview)
                hwdec = {"on": bool(self.config.nvenc_hwdecode) and use_hw and chosen_enc.endswith("_nvenc")}
                done = 0
                lock = threading.Lock()

                def render_one(i: int, hw: bool = use_hw) -> None:
                    nonlocal done
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("Rendering cancelled by user.")
                    cmd = self._unit_command(
                        ffmpeg_bin, units[i], seg_paths[i], hw, with_audio, preview,
                        hwdecode=hwdec["on"], subtitle_file=subtitle_file,
                    )
                    rc, err = self._run(cmd, cancel_event, job_handle)
                    if rc != 0 and hwdec["on"]:
                        # Unsupported codec/profile for NVDEC: software-decode this and the
                        # remaining segments but keep the NVENC encoder.
                        logger.warning("NVDEC decode failed (%s). Retrying with software decode.", err[-160:])
                        hwdec["on"] = False
                        cmd = self._unit_command(
                            ffmpeg_bin, units[i], seg_paths[i], hw, with_audio, preview,
                            subtitle_file=subtitle_file,
                        )
                        rc, err = self._run(cmd, cancel_event, job_handle)
                    if rc != 0:
                        raise RenderError(f"Segment {i + 1}/{len(units)} failed (code {rc}).", err)
                    with lock:
                        done += 1
                        notify(95.0 * done / len(units), f"Rendered segment {done}/{len(units)}")

                try:
                    # First segment alone: validates encoder/driver before fanning out.
                    render_one(0)
                    workers = 1 if preview else self.config.render_workers
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = [pool.submit(render_one, i) for i in range(1, len(units))]
                        try:
                            for f in futures:
                                f.result()
                        except BaseException:
                            pool.shutdown(wait=True, cancel_futures=True)
                            raise
                except RenderError as exc:
                    if use_hw and is_hw_encoder_error(exc.stderr):
                        logger.warning(
                            "Hardware encoder %s failed (%s). Re-rendering on CPU encoder.",
                            chosen_enc,
                            exc.stderr[-200:],
                        )
                        use_hw = False
                        for p in work_dir.glob("seg_*"):
                            p.unlink(missing_ok=True)
                        continue
                    raise
                break

            encoder, _ = self._encoder(use_hw, preview)
            list_file = work_dir / "concat.txt"
            list_file.write_text(
                "".join(
                    "file '{}'\n".format(p.as_posix().replace("'", "'\\''")) for p in seg_paths
                ),
                encoding="utf-8",
            )
            notify(96.0, "Muxing master and mixing audio...")
            cmd = self.build_concat_command(
                ffmpeg_bin,
                list_file,
                part_file,
                total_duration,
                with_audio,
                Path(soundtrack_path) if has_soundtrack and soundtrack_path else None,
                soundtrack_in,
                encoder,
                preview,
            )
            rc, err = self._run(cmd, cancel_event, job_handle)
            if rc != 0:
                raise RenderError(f"FFmpeg concat/mux failed with exit code {rc}.", err)
            os.replace(part_file, output_file)
            if active is not None and active.subtitles != "none" and sentences:
                canvas = (self.config.canvas_width, self.config.canvas_height)
                write_subtitle_sidecars(sentences, output_file, active.subtitles, canvas)
            notify(100.0, "Render complete.")

            file_size = output_file.stat().st_size
            logger.info(
                "Render complete: %s (%.2f MB, encoder=%s, %d segments)",
                output_file.name, file_size / (1024 * 1024), encoder, len(units),
            )
            return RenderJobResult(
                output_path=output_file,
                encoder_used=encoder,
                duration=total_duration,
                file_size_bytes=file_size,
                return_code=0,
            )
        finally:
            if job_handle is not None and hasattr(job_handle, "_child_process"):
                job_handle._child_process = None
            part_file.unlink(missing_ok=True)
            shutil.rmtree(work_dir, ignore_errors=True)

    def render_edit_timeline(
        self,
        edit_tl: EditTimeline,
        output_file: Path,
        force_cpu: bool = False,
        preview: bool = False,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        job_handle: Any | None = None,
        settings: DeliverSettings | None = None,
        sentences: list[SentenceSegment] | None = None,
    ) -> RenderJobResult:
        """Render exactly what is on the edit timeline.

        Without ``settings`` canvas and fps come from the timeline (legacy behaviour). With
        ``settings`` they come from the Deliver settings: the timeline is retargeted (cut points kept
        in seconds, frames re-snapped to the new rate) and the master codec, colour, audio, fit mode
        and subtitles follow the settings. ``sentences`` feed the ``.srt`` / ``.ass`` sidecars and
        the burn-in.
        """
        if settings is not None:
            edit_tl = retarget_timeline(edit_tl, settings)
        canvas_fps = {
            "canvas_width": edit_tl.canvas_width,
            "canvas_height": edit_tl.canvas_height,
            "fps": edit_tl.fps,
        }
        if settings is not None:
            canvas_fps = {
                "canvas_width": settings.canvas_width,
                "canvas_height": settings.canvas_height,
                "fps": settings.fps,
            }
        if edit_tl.kind == "rough_cut":
            canvas_fps["keep_video_audio"] = True
        renderer = self._derive(settings, **canvas_fps)
        return renderer.render_segments(
            edit_tl.clips,
            edit_tl.soundtrack_path,
            output_file,
            force_cpu=force_cpu,
            preview=preview,
            soundtrack_in=edit_tl.soundtrack_in,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            job_handle=job_handle,
            sentences=sentences,
        )

    def render_sequence(
        self,
        scored_scenes: list[ScoredScene],
        cut_timestamps: list[float],
        soundtrack_path: Path | None,
        output_file: Path,
        force_cpu: bool = False,
        include_teaser: bool | None = None,
        sections: list[SongSection] | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        job_handle: Any | None = None,
        preview: bool = False,
    ) -> RenderJobResult:
        """Render analysed scenes on beat cuts with background music to a master file."""
        segments = self.build_segments(scored_scenes, cut_timestamps, include_teaser, sections)
        return self.render_segments(
            segments,
            soundtrack_path,
            output_file,
            force_cpu=force_cpu,
            preview=preview,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            job_handle=job_handle,
        )
