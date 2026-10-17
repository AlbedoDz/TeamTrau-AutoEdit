"""Single ffprobe-based media description shared by every exporter and renderer.

``probe_media`` replaces the ad-hoc probes that used to live in the Premiere, CapCut and
OTIO exporters. It is cached by (resolved path, size, mtime_ns) and never raises: a missing,
unreadable or non-media file yields a ``MediaInfo`` with ``has_video=False``.
"""

import logging
import re
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.core.ffmpeg_utils import ffprobe_json, stream_rotation

logger = logging.getLogger(__name__)

HDR_TRANSFERS: frozenset[str] = frozenset({"smpte2084", "arib-std-b67"})
# ffmpeg names for logarithmic transfer curves (rarely tagged by cameras).
_LOG_TRANSFERS: frozenset[str] = frozenset({"log100", "log316"})
# Camera log profiles are usually left untagged; best effort from encoder/profile tags.
_LOG_TAG_RE = re.compile(
    r"(?<![a-z])(s-?log\d?|v-?log|c-?log\d?|n-?log|f-?log2?|d-?log|log-?c\d?|hlg3|slog\d?)(?![a-z])"
)
_STANDARD_TRANSFERS: frozenset[str] = frozenset(
    {
        "bt709", "smpte170m", "smpte240m", "gamma22", "gamma28", "linear", "iec61966-2-4",
        "bt1361e", "iec61966-2-1", "bt2020-10", "bt2020-12", "smpte2084", "smpte428", "arib-std-b67",
    }
)
_UNKNOWN_VALUES: frozenset[str] = frozenset({"", "unknown", "unspecified", "n/a"})


@dataclass(frozen=True)
class MediaInfo:
    """What the rest of the toolkit needs to know about a media file (None/0 = unknown)."""

    path: Path
    has_video: bool = False
    has_audio: bool = False
    duration: float = 0.0
    # Display size, AFTER applying the rotation tag (a rotated 1920x1080 clip is 1080x1920).
    width: int = 0
    height: int = 0
    rotation: int = 0
    fps: Fraction | None = None  # r_frame_rate, falling back to avg_frame_rate
    avg_fps: Fraction | None = None
    is_ntsc: bool = False  # fps denominator is 1001
    is_vfr: bool = False
    codec: str | None = None
    pix_fmt: str | None = None
    bit_depth: int | None = None
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    color_range: str | None = None
    is_hdr: bool = False
    is_log: bool = False
    audio_channels: int = 0
    audio_rate: int = 0
    audio_layout: str | None = None
    start_timecode: str | None = None

    @property
    def fps_float(self) -> float:
        """Frame rate as a float (0.0 when unknown)."""
        return float(self.fps) if self.fps else 0.0

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


def parse_rate(text: Any) -> Fraction | None:
    """Parse an ffprobe rate ('30000/1001', '25/1', '27.42') into a Fraction; None if invalid/zero."""
    if text is None:
        return None
    try:
        raw = str(text).strip()
        if not raw:
            return None
        if "/" in raw:
            num, den = raw.split("/", 1)
            den_f = Fraction(den.strip())
            if den_f == 0:
                return None
            value = Fraction(num.strip()) / den_f
        else:
            value = Fraction(raw)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _UNKNOWN_VALUES else text


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _bit_depth(stream: dict[str, Any], pix_fmt: str | None) -> int | None:
    raw = _int(stream.get("bits_per_raw_sample"))
    if raw > 0:
        return raw
    if not pix_fmt:
        return None
    m = re.search(r"(9|10|12|14|16)(?:le|be)$", pix_fmt)
    if m:
        return int(m.group(1))
    return 8


def _tag(tags: dict[str, Any] | None, key: str) -> str | None:
    for k, v in (tags or {}).items():
        if str(k).lower() == key and str(v).strip():
            return str(v).strip()
    return None


def _start_timecode(data: dict[str, Any], video: dict[str, Any] | None) -> str | None:
    candidates = [(data.get("format") or {}).get("tags")]
    if video is not None:
        candidates.append(video.get("tags"))
    candidates.extend(s.get("tags") for s in data.get("streams", []) or [])
    for tags in candidates:
        tc = _tag(tags, "timecode")
        if tc:
            return tc
    return None


def _is_log(transfer: str | None, data: dict[str, Any], video: dict[str, Any] | None) -> bool:
    if transfer in _LOG_TRANSFERS:
        return True
    if transfer is not None and transfer in _STANDARD_TRANSFERS:
        return False
    haystack: list[str] = []
    for tags in ((data.get("format") or {}).get("tags"), (video or {}).get("tags")):
        haystack.extend(str(v).lower() for v in (tags or {}).values())
    return any(_LOG_TAG_RE.search(h) for h in haystack)


def _build(path: Path, data: dict[str, Any] | None) -> MediaInfo:
    if not data:
        return MediaInfo(path=path)
    streams = data.get("streams", []) or []
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and (s.get("disposition", {}) or {}).get("attached_pic", 0) != 1
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}

    duration = 0.0
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0 and video is not None:
        try:
            duration = float(video.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0

    fields: dict[str, Any] = {"path": path, "duration": duration}
    if audio is not None:
        fields.update(
            has_audio=True,
            audio_channels=_int(audio.get("channels")),
            audio_rate=_int(audio.get("sample_rate")),
            audio_layout=_clean(audio.get("channel_layout")),
        )
    if video is not None:
        width, height = _int(video.get("width")), _int(video.get("height"))
        rotation = stream_rotation(video)
        if rotation in (90, 270):
            width, height = height, width
        r_rate = parse_rate(video.get("r_frame_rate"))
        avg_rate = parse_rate(video.get("avg_frame_rate"))
        fps = r_rate or avg_rate
        pix_fmt = _clean(video.get("pix_fmt"))
        transfer = _clean(video.get("color_transfer"))
        fields.update(
            has_video=True,
            width=width,
            height=height,
            rotation=rotation,
            fps=fps,
            avg_fps=avg_rate,
            is_ntsc=bool(fps and fps.denominator == 1001),
            is_vfr=bool(r_rate and avg_rate and abs(float(r_rate) - float(avg_rate)) > 0.05),
            codec=_clean(video.get("codec_name")),
            pix_fmt=pix_fmt,
            bit_depth=_bit_depth(video, pix_fmt),
            color_space=_clean(video.get("color_space")),
            color_transfer=transfer,
            color_primaries=_clean(video.get("color_primaries")),
            color_range=_clean(video.get("color_range")),
            is_hdr=transfer in HDR_TRANSFERS,
            is_log=_is_log(transfer, data, video),
            start_timecode=_start_timecode(data, video),
        )
    elif audio is not None:
        fields["start_timecode"] = _start_timecode(data, None)
    return MediaInfo(**fields)


@lru_cache(maxsize=1024)
def _probe_cached(path_str: str, size: int, mtime_ns: int) -> MediaInfo:
    path = Path(path_str)
    try:
        return _build(path, ffprobe_json(path))
    except Exception as exc:  # never let a corrupt probe result break an export
        logger.debug("probe_media failed for %s: %s", path, exc)
        return MediaInfo(path=path)


def probe_media(path: Path | str | None) -> MediaInfo:
    """Describe a media file (cached by resolved path, size and mtime). Never raises."""
    if path is None:
        return MediaInfo(path=Path(""))
    p = Path(path)
    try:
        st = p.stat()
        resolved = str(p.resolve())
    except OSError:
        return MediaInfo(path=p)
    return _probe_cached(resolved, st.st_size, st.st_mtime_ns)


def clear_media_cache() -> None:
    """Forget all cached probe results (tests, or after files changed in place)."""
    _probe_cached.cache_clear()
