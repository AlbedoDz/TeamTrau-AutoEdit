"""FCPXML 1.8 exporter for DaVinci Resolve (and Final Cut Pro) built from an OTIO timeline.

OpenTimelineIO has no FCPXML writer in this environment, and falling back to FCP7 XML in a
``.fcpxml`` file makes Resolve reject it. This writer targets FCPXML 1.8 - supported by
Resolve 17+ and FCP 10.4.x - and keeps what AutoEdit decides on the edit:

- spine of ``asset-clip`` / ``gap`` / ``transition`` in sequence-frame-exact rational time
- speed ramps as ``timeMap`` (source time consumed = output duration x speed)
- background music as a connected audio clip (lane -1), camera audio ducked or muted
- cross-dissolves, rough-cut defect markers (colour encoded as a "[RED] " value prefix)
- one ``<format>`` per distinct source (real width/height after rotation, exact frame rate,
  colour space) while the sequence keeps the deliver canvas; audio channels/rate and the
  source start timecode come from ``src.core.media_info.probe_media``
- reframing: ``adjust-conform`` (fit/fill) and ``adjust-transform`` (crop window of the 9:16
  reframer, stretch) computed by :func:`compute_reframe`; the crop ``<note>`` is kept as
  documentation

Times are exact fractions of a second (``Fraction``), never floats.

Verification log (DaVinci Resolve Studio 21.0.4.5, pixel-comparing stills against source frames)
- VERIFIED earlier: normal clips, gaps, cross-dissolves, markers, music on connected lane -1,
  retime (``start = in-point / speed`` + linear ``timeMap`` through the origin).
- NOT VERIFIED on Resolve (Resolve was not running when Phase 22 / step 4 was written, and the
  worker must never launch it): per-asset formats, asset ``start`` = source timecode (clip
  ``start`` = asset start + in-point), music anchored to a retimed first clip, and
  ``adjust-transform`` (position/scale semantics below). Re-run the stills comparison
  (rimport.py / rstill.py / match_stills.py) for 9:16 crop of a 16:9 source, 4K source in a
  1080p canvas, square canvas and a 01:00:00:00 start-timecode source before trusting them.
  Until then ``write_transforms=False`` falls back to conform + note only.
"""

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import opentimelineio as otio

from src.core.media_info import MediaInfo, probe_media
from src.core.timebase import frame_rate_fraction as frame_rate

logger = logging.getLogger(__name__)

FCPXML_VERSION = "1.8"
CROSS_DISSOLVE_UID = "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"

FitMode = Literal["auto", "fit", "fill", "stretch"]
VALID_FIT_MODES: tuple[str, ...] = ("auto", "fit", "fill", "stretch")
COLOR_SPACES: dict[str, str] = {
    "rec709": "1-1-1 (Rec. 709)",
    "rec2020_hlg": "9-18-9 (Rec. 2020 HLG)",
    "rec2020_pq": "9-16-9 (Rec. 2020 PQ)",
}
_REC2020_SDR = "9-1-9 (Rec. 2020)"
VALID_COLORS: tuple[str, ...] = (*COLOR_SPACES, "passthrough")
_ASPECT_TOLERANCE = 0.005


def fmt_time(t: Fraction) -> str:
    """Format a time in seconds as FCPXML rational ('0s', '10s', '1001/30000s')."""
    t = Fraction(t)
    if t.denominator == 1:
        return f"{t.numerator}s"
    return f"{t.numerator}/{t.denominator}s"


def _secs(rt: "otio.opentime.RationalTime") -> Fraction:
    return Fraction(rt.value).limit_denominator(1_000_000) / Fraction(rt.rate).limit_denominator(1001)


def _uri_to_path(url: str) -> Path | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme in ("", "file"):
            raw = unquote(parsed.path)
            if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
                raw = raw[1:]  # /C:/x -> C:/x
            return Path(url2pathname(raw)) if parsed.scheme == "file" else Path(raw)
    except ValueError:
        return None
    return None


_TC_RE = re.compile(r"^\s*(\d{1,3}):(\d{2}):(\d{2})([:;.])(\d{2,3})\s*$")


def timecode_to_seconds(tc: str | None, fps: Fraction) -> tuple[Fraction, bool] | None:
    """Convert a source start timecode to exact seconds: ``(seconds, is_drop_frame)``.

    Non-drop timecode counts ``round(fps)`` frames per second (so ``01:00:00:00`` at 29.97 NDF is
    108000 frames = 3603.6 s); drop-frame (``;``) timecode skips 2 frames per minute (4 at 59.94,
    8 at 119.88) except every tenth minute. Returns None for missing/invalid timecode.
    """
    if not tc:
        return None
    m = _TC_RE.match(tc)
    if m is None:
        return None
    h, mi, sec, sep, fr = int(m[1]), int(m[2]), int(m[3]), m[4], int(m[5])
    nominal = max(1, round(float(fps)))
    drop = sep == ";" and fps.denominator == 1001 and nominal % 30 == 0
    frames = ((h * 60 + mi) * 60 + sec) * nominal + fr
    if drop:
        minutes = h * 60 + mi
        frames -= 2 * (nominal // 30) * (minutes - minutes // 10)
    if frames < 0:
        return None
    return Fraction(frames) / fps, drop


def colorspace_for(info: MediaInfo | None) -> str:
    """FCPXML ``colorSpace`` string derived from a source's colour tags (default Rec. 709)."""
    if info is None or not info.has_video:
        return COLOR_SPACES["rec709"]
    if info.color_transfer == "arib-std-b67":
        return COLOR_SPACES["rec2020_hlg"]
    if info.color_transfer == "smpte2084":
        return COLOR_SPACES["rec2020_pq"]
    if info.color_primaries == "bt2020" or (info.color_space or "").startswith("bt2020"):
        return _REC2020_SDR
    return COLOR_SPACES["rec709"]


@dataclass(frozen=True)
class Reframe:
    """How a clip is conformed to the sequence canvas (all values FCPXML-ready numbers)."""

    conform: Literal["fit", "fill", "none"]
    scale: tuple[float, float] | None = None  # relative to the conformed (fit) size
    position: tuple[float, float] | None = None  # percent of canvas height, +x right, +y up


def compute_reframe(
    src_w: int,
    src_h: int,
    canvas_w: int,
    canvas_h: int,
    crop: tuple[float, float, float, float] | None = None,
    fit_mode: str = "auto",
) -> Reframe | None:
    """Derive ``adjust-conform`` / ``adjust-transform`` for a clip; None when nothing is needed.

    Semantics (FCP / FCPXML convention, assumed for Resolve - see the module docstring):
    ``adjust-conform fit`` scales the whole source by f = min(cw/sw, ch/sh) to sit inside the
    canvas, ``fill`` by max(...) to cover it; ``adjust-transform scale`` multiplies the
    conformed size; ``position`` is a translation of the scaled image in percent of the frame
    HEIGHT (1080p: 1 unit = 10.8 px, 16:9 half-width = 88.89), x to the right, y UP.

    ``crop`` = (x, y, w, h) window in source pixels. It is honoured for ``auto`` and ``fill``;
    an explicit ``fit`` or ``stretch`` ignores it. The crop window is scaled to cover the
    canvas (absolute scale a = max(cw/w, ch/h)) and centred on it.
    """
    if fit_mode not in VALID_FIT_MODES:
        raise ValueError(f"fit_mode must be one of {list(VALID_FIT_MODES)}, got {fit_mode!r}")
    if min(src_w, src_h, canvas_w, canvas_h) <= 0:
        return None
    fit_scale = min(canvas_w / src_w, canvas_h / src_h)

    if crop is not None and fit_mode in ("auto", "fill"):
        x, y, w, h = crop
        if w > 0 and h > 0:
            abs_scale = max(canvas_w / w, canvas_h / h)
            rel = abs_scale / fit_scale
            dx = (x + w / 2) - src_w / 2
            dy = (y + h / 2) - src_h / 2
            pos = (-dx * abs_scale / canvas_h * 100, dy * abs_scale / canvas_h * 100)
            return Reframe("fit", (rel, rel), pos)

    if (src_w, src_h) == (canvas_w, canvas_h):
        return None
    same_aspect = abs(src_w / src_h - canvas_w / canvas_h) <= _ASPECT_TOLERANCE * (canvas_w / canvas_h)
    if fit_mode == "stretch" and not same_aspect:
        return Reframe("fit", ((canvas_w / src_w) / fit_scale, (canvas_h / src_h) / fit_scale), None)
    if fit_mode == "fill" and not same_aspect:
        return Reframe("fill")
    return Reframe("fit")


def _num(value: float) -> str:
    text = f"{value:.5f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


@dataclass
class _AssetRec:
    rid: str
    start: Fraction = Fraction(0)
    drop_frame: bool = False
    width: int = 0
    height: int = 0
    fps: Fraction = Fraction(0)  # media frame rate (0 = unknown)


class _Resources:
    """Assets, formats and effects referenced by the spine, with stable ids."""

    def __init__(
        self,
        root: ET.Element,
        fps: Fraction,
        width: int,
        height: int,
        color: str,
        derive_color: bool = False,
    ) -> None:
        self.root = root
        self.fps = fps
        self.frame = 1 / fps
        self.width, self.height = width, height
        self.color = color
        self.derive_color = derive_color  # sequence colourSpace follows the first source
        self._n = 1
        self._formats: dict[tuple[int, int, Fraction, str], str] = {}
        self.format_id = self._add_format(width, height, fps, color)
        self._assets: dict[str, _AssetRec] = {}
        self._effect_id: str | None = None

    def _next(self) -> str:
        rid = f"r{self._n}"
        self._n += 1
        return rid

    def _add_format(self, width: int, height: int, fps: Fraction, color: str) -> str:
        key = (width, height, fps, color)
        if key in self._formats:
            return self._formats[key]
        rid = self._next()
        ET.SubElement(
            self.root,
            "format",
            id=rid,
            name=f"AutoEdit{width}x{height}p{float(fps):.2f}".rstrip("0").rstrip("."),
            frameDuration=fmt_time(1 / fps),
            width=str(width),
            height=str(height),
            colorSpace=color,
        )
        self._formats[key] = rid
        return rid

    def set_sequence_color(self, color: str) -> None:
        """Re-label the sequence format (used when the colour is derived from the first source)."""
        seq = self.root.find(f"format[@id='{self.format_id}']")
        if seq is not None:
            seq.set("colorSpace", color)
        self._formats = {k: v for k, v in self._formats.items() if v != self.format_id}
        self.color = color
        self._formats[(self.width, self.height, self.fps, color)] = self.format_id

    def asset(
        self,
        url: str,
        name: str,
        duration: Fraction,
        audio_only: bool = False,
        info: MediaInfo | None = None,
    ) -> _AssetRec:
        if url in self._assets:
            return self._assets[url]
        rid = self._next()
        probed_audio = info is not None and info.has_audio
        has_audio = audio_only or probed_audio
        channels = (info.audio_channels if info is not None and probed_audio else 0) or 2
        rate = (info.audio_rate if info is not None and probed_audio else 0) or 48000
        rec = _AssetRec(rid)
        fmt_id = self.format_id
        # Audio-only assets keep the sequence format (verified on Resolve); pictures get their own.
        if not audio_only and info is not None and info.has_video and info.width > 0 and info.height > 0:
            src_fps = frame_rate(info.fps) if info.fps else self.fps
            fmt_id = self._add_format(info.width, info.height, src_fps, colorspace_for(info))
            rec.width, rec.height = info.width, info.height
            rec.fps = src_fps
            parsed = timecode_to_seconds(info.start_timecode, src_fps)
            if parsed is not None:
                rec.start, rec.drop_frame = parsed
        attrs = {
            "id": rid,
            "name": name,
            "src": url,
            "start": fmt_time(rec.start),
            "duration": fmt_time(duration),
            "hasVideo": "0" if audio_only else "1",
            "hasAudio": "1" if has_audio else "0",
            "format": fmt_id,
        }
        if has_audio:
            attrs.update(audioSources="1", audioChannels=str(channels), audioRate=str(rate))
        ET.SubElement(self.root, "asset", attrs)
        self._assets[url] = rec
        return rec

    def dissolve(self) -> str:
        if self._effect_id is None:
            self._effect_id = self._next()
            ET.SubElement(
                self.root, "effect", id=self._effect_id, name="Cross Dissolve", uid=CROSS_DISSOLVE_UID
            )
        return self._effect_id


def _autoedit_meta(item: "otio.schema.Clip") -> dict:
    meta = item.metadata.get("AutoEdit", {}) if item.metadata else {}
    try:
        return dict(meta)
    except (TypeError, ValueError):
        return {}


def _marker_value(mk: "otio.schema.Marker") -> str:
    """Marker title with its colour as a ``[RED] `` prefix (FCPXML 1.8 markers carry no colour)."""
    color = str(getattr(mk, "color", "") or "").strip().upper()
    return f"[{color}] {mk.name}" if color else str(mk.name)


def timeline_to_fcpxml(
    timeline: "otio.schema.Timeline",
    fps: float,
    width: int,
    height: int,
    camera_audio_db: float | None = -12.0,
    project_name: str | None = None,
    *,
    color: str | None = None,
    fit_mode: str = "auto",
    write_transforms: bool = True,
) -> ET.ElementTree:
    """Build the FCPXML element tree for ``timeline`` (V1 picture track + optional A1 music).

    ``camera_audio_db``: volume applied to picture clips when a soundtrack is present
    (None keeps them untouched; use a very low value such as -96 to mute).
    ``color``: sequence colour space ("rec709" | "rec2020_hlg" | "rec2020_pq"); "passthrough"
    or None follow the first source's colour tags. Asset formats always report the source's
    own tags. ``fit_mode`` (auto|fit|fill|stretch) drives ``adjust-conform`` / ``adjust-transform``;
    ``write_transforms=False`` keeps conform + note only.
    """
    if color is not None and color not in VALID_COLORS:
        raise ValueError(f"color must be one of {list(VALID_COLORS)} or None, got {color!r}")
    if fit_mode not in VALID_FIT_MODES:
        raise ValueError(f"fit_mode must be one of {list(VALID_FIT_MODES)}, got {fit_mode!r}")
    rate = frame_rate(fps)
    frame = 1 / rate
    root = ET.Element("fcpxml", version=FCPXML_VERSION)
    resources = ET.SubElement(root, "resources")
    explicit = color in COLOR_SPACES
    res = _Resources(
        resources,
        rate,
        width,
        height,
        COLOR_SPACES[color] if color in COLOR_SPACES else COLOR_SPACES["rec709"],
        derive_color=not explicit,
    )

    video_track = next(
        (t for t in timeline.tracks if t.kind == otio.schema.TrackKind.Video), None
    )
    audio_track = next(
        (t for t in timeline.tracks if t.kind == otio.schema.TrackKind.Audio), None
    )
    audio_clips = [c for c in (audio_track or []) if isinstance(c, otio.schema.Clip)]
    soundtrack = next(
        (c for c in audio_clips if _autoedit_meta(c).get("role") == "music"),
        # legacy OTIO without roles: a lone, untagged audio clip is the soundtrack
        audio_clips[0] if len(audio_clips) == 1 and not _autoedit_meta(audio_clips[0]) else None,
    )

    def to_frames(t: Fraction) -> Fraction:
        """Snap a duration to a whole number of sequence frames."""
        return Fraction(round(t / frame)) * frame

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name=project_name or timeline.name or "AutoEdit")
    project = ET.SubElement(event, "project", name=project_name or timeline.name or "AutoEdit")
    sequence = ET.SubElement(
        project,
        "sequence",
        format=res.format_id,
        tcStart="0s",
        tcFormat="NDF",
        audioLayout="stereo",
        audioRate="48k",
    )
    spine = ET.SubElement(sequence, "spine")

    cursor = Fraction(0)
    # Music anchor: the first normal-speed clip (verified); if every clip is retimed, the first
    # clip, using its LOCAL start (a retimed clip's local time runs 1:1 with the timeline).
    anchor_el: ET.Element | None = None
    anchor_start = Fraction(0)
    anchor_offset = Fraction(0)
    anchor_is_retimed = True
    seq_color_set = False
    pending_transition: otio.schema.Transition | None = None

    for item in video_track or []:
        if isinstance(item, otio.schema.Transition):
            pending_transition = item
            continue

        if isinstance(item, otio.schema.Gap):
            dur = to_frames(_secs(item.source_range.duration if item.source_range else item.duration()))
            ET.SubElement(spine, "gap", name="Gap", offset=fmt_time(cursor), duration=fmt_time(dur), start="0s")
            cursor += dur
            pending_transition = None
            continue

        if not isinstance(item, otio.schema.Clip):
            continue

        meta = _autoedit_meta(item)
        speed = max(0.1, float(meta.get("speed", 1.0) or 1.0))
        src_range = item.source_range
        out_dur = to_frames(_secs(src_range.duration))
        src_in = _secs(src_range.start_time)
        avail = item.media_reference.available_range
        asset_dur = _secs(avail.duration) if avail is not None else src_in + out_dur
        url = item.media_reference.target_url
        info = probe_media(_uri_to_path(url))
        rec = res.asset(url, Path(unquote(urlparse(url).path)).stem or item.name, asset_dur, info=info)
        if not seq_color_set and res.derive_color and info.has_video:
            res.set_sequence_color(colorspace_for(info))
            seq_color_set = True
        media_in = rec.start + src_in  # clip start in the asset's own timebase

        if pending_transition is not None and len(spine) > 0:
            tdur = to_frames(
                _secs(pending_transition.in_offset) + _secs(pending_transition.out_offset)
            )
            if tdur > 0:
                tr = ET.SubElement(
                    spine,
                    "transition",
                    name="Cross Dissolve",
                    offset=fmt_time(max(Fraction(0), cursor - tdur / 2)),
                    duration=fmt_time(tdur),
                )
                ET.SubElement(tr, "filter-video", ref=res.dissolve(), name="Cross Dissolve")
        pending_transition = None

        clip_el = ET.SubElement(
            spine,
            "asset-clip",
            ref=rec.rid,
            name=item.name or "clip",
            offset=fmt_time(cursor),
            start=fmt_time(media_in),
            duration=fmt_time(out_dur),
            tcFormat="DF" if rec.drop_frame else "NDF",
        )
        crop = None
        notes = []
        if all(k in meta for k in ("crop_x", "crop_y", "crop_width", "crop_height")):
            crop = (
                float(meta["crop_x"]),
                float(meta["crop_y"]),
                float(meta["crop_width"]),
                float(meta["crop_height"]),
            )
            notes.append(
                f"AutoEdit 9:16 crop window x={meta['crop_x']} y={meta['crop_y']} "
                f"w={meta['crop_width']} h={meta['crop_height']} ({meta.get('tracking_subject', 'subject')})"
            )
        if meta.get("ken_burns"):
            notes.append(f"AutoEdit Ken Burns: {meta['ken_burns']}")
        if notes:
            ET.SubElement(clip_el, "note").text = "; ".join(notes)
        is_retimed = abs(speed - 1.0) > 1e-6
        clip_start = media_in
        sp = Fraction(1)
        if is_retimed:
            # Retimed clips live in a LOCAL timeline where media = local * speed (verified
            # pixel-exact by importing into DaVinci Resolve Studio 21: Resolve takes the
            # in-point as start * speed). So `start` is the in-point divided by the speed and
            # timeMap is the pure linear map through the origin. With a source start
            # timecode the in-point is asset start + cut point (unverified on Resolve).
            # Verified on Resolve Studio 21 with a 01:00:00:00 source: for RETIMED clips Resolve
            # ignores the asset's source timecode and reads the in-point as start * speed relative
            # to the media start (non-retimed clips use asset start + in-point instead). So
            # start = in-point / speed, with the in-point snapped to the media frame grid
            # (otherwise Resolve floors 29.97 fps in-points one frame early) and a linear
            # timeMap through the origin.
            sp = Fraction(speed).limit_denominator(1000)
            snapped_in = Fraction(round(src_in * rec.fps)) / rec.fps if rec.fps else src_in
            clip_start = snapped_in / sp
            clip_el.set("start", fmt_time(clip_start))
            tm = ET.SubElement(clip_el, "timeMap")
            ET.SubElement(tm, "timept", time="0s", value="0s", interp="linear")
            local_end = clip_start + out_dur
            ET.SubElement(
                tm, "timept", time=fmt_time(local_end), value=fmt_time(local_end * sp), interp="linear"
            )
        reframe = (
            compute_reframe(rec.width, rec.height, width, height, crop, fit_mode)
            if rec.width > 0 and rec.height > 0
            else None
        )
        if reframe is not None:
            ET.SubElement(clip_el, "adjust-conform", type=reframe.conform)
            if write_transforms and (reframe.scale is not None or reframe.position is not None):
                tattrs: dict[str, str] = {}
                if reframe.position is not None:
                    tattrs["position"] = f"{_num(reframe.position[0])} {_num(reframe.position[1])}"
                if reframe.scale is not None:
                    tattrs["scale"] = f"{_num(reframe.scale[0])} {_num(reframe.scale[1])}"
                ET.SubElement(clip_el, "adjust-transform", tattrs)
        if soundtrack is not None and camera_audio_db is not None:
            ET.SubElement(clip_el, "adjust-volume", amount=f"{camera_audio_db:g}dB")
        for mk in item.markers:
            # marker start lives in the clip's local time, like the clip's own `start`
            mk_rel = _secs(mk.marked_range.start_time) - src_in
            ET.SubElement(
                clip_el,
                "marker",
                start=fmt_time(clip_start + mk_rel / sp),
                duration=fmt_time(frame),
                value=_marker_value(mk),
                **({"note": str(mk.comment)} if getattr(mk, "comment", "") else {}),
            )

        if anchor_el is None or (anchor_is_retimed and not is_retimed):
            anchor_el, anchor_start, anchor_offset, anchor_is_retimed = (
                clip_el,
                clip_start,
                cursor,
                is_retimed,
            )
        cursor += out_dur

    sequence.set("duration", fmt_time(cursor))

    if soundtrack is not None and anchor_el is not None:
        s_url = soundtrack.media_reference.target_url
        s_avail = soundtrack.media_reference.available_range
        s_rec = res.asset(
            s_url,
            Path(unquote(urlparse(s_url).path)).stem or "Soundtrack",
            _secs(s_avail.duration) if s_avail is not None else cursor,
            audio_only=True,
            info=probe_media(_uri_to_path(s_url)),
        )
        music = ET.Element(
            "asset-clip",
            ref=s_rec.rid,
            name=soundtrack.name or "Soundtrack",
            lane="-1",
            # connected-clip offsets live in the parent's local timebase: timeline 0 maps to
            # (parent start - parent offset)
            offset=fmt_time(anchor_start - anchor_offset),
            start=fmt_time(_secs(soundtrack.source_range.start_time)),
            duration=fmt_time(min(cursor, _secs(soundtrack.source_range.duration))),
            audioRole="music",
        )
        # schema order: note, timeMap, adjust-*, connected clips, markers
        param_tags = ("note", "timeMap", "adjust-conform", "adjust-transform", "adjust-volume")
        anchor_el.insert(len([c for c in anchor_el if c.tag in param_tags]), music)

    ET.indent(root, space="    ")
    return ET.ElementTree(root)


def write_fcpxml(
    timeline: "otio.schema.Timeline",
    path: Path,
    fps: float,
    width: int,
    height: int,
    camera_audio_db: float | None = -12.0,
    project_name: str | None = None,
    *,
    color: str | None = None,
    fit_mode: str = "auto",
    write_transforms: bool = True,
) -> Path:
    """Write ``timeline`` as a FCPXML 1.8 file (UTF-8, DOCTYPE fcpxml)."""
    tree = timeline_to_fcpxml(
        timeline,
        fps,
        width,
        height,
        camera_audio_db,
        project_name,
        color=color,
        fit_mode=fit_mode,
        write_transforms=write_transforms,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ET.tostring(tree.getroot(), encoding="unicode")
    tmp = path.with_name(path.name + ".part")
    tmp.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n' + body + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
