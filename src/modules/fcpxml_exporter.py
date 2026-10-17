"""FCPXML 1.8 exporter for DaVinci Resolve (and Final Cut Pro) built from an OTIO timeline.

OpenTimelineIO has no FCPXML writer in this environment, and falling back to FCP7 XML in a
``.fcpxml`` file makes Resolve reject it. This writer targets FCPXML 1.8 - supported by
Resolve 17+ and FCP 10.4.x - and keeps what AutoEdit decides on the edit:

- spine of ``asset-clip`` / ``gap`` / ``transition`` in sequence-frame-exact rational time
- speed ramps as ``timeMap`` (source time consumed = output duration x speed)
- background music as a connected audio clip (lane -1), camera audio ducked or muted
- cross-dissolves, rough-cut defect markers, 9:16 crop window as a clip note

Times are exact fractions of a second (``Fraction``), never floats.
"""

import logging
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import opentimelineio as otio

from src.core.ffmpeg_utils import has_audio_stream

logger = logging.getLogger(__name__)

FCPXML_VERSION = "1.8"
CROSS_DISSOLVE_UID = "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"

_NTSC = {24: 1001, 30: 1001, 60: 1001, 48: 1001}


def frame_rate(fps: float) -> Fraction:
    """Exact frame rate, mapping NTSC-style rates (23.976, 29.97, 59.94) to n/1001."""
    for whole, den in _NTSC.items():
        if abs(fps - whole * 1000 / den) < 0.01:
            return Fraction(whole * 1000, den)
    return Fraction(fps).limit_denominator(1000)


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


class _Resources:
    """Assets, formats and effects referenced by the spine, with stable ids."""

    def __init__(self, root: ET.Element, fps: Fraction, width: int, height: int) -> None:
        self.root = root
        self.fps = fps
        self.frame = 1 / fps
        self._n = 1
        self.format_id = self._next()
        ET.SubElement(
            root,
            "format",
            id=self.format_id,
            name=f"AutoEdit{width}x{height}p{float(fps):.2f}".rstrip("0").rstrip("."),
            frameDuration=fmt_time(self.frame),
            width=str(width),
            height=str(height),
            colorSpace="1-1-1 (Rec. 709)",
        )
        self._assets: dict[str, str] = {}
        self._effect_id: str | None = None

    def _next(self) -> str:
        rid = f"r{self._n}"
        self._n += 1
        return rid

    def asset(self, url: str, name: str, duration: Fraction, audio_only: bool = False) -> str:
        if url in self._assets:
            return self._assets[url]
        rid = self._next()
        path = _uri_to_path(url)
        has_audio = audio_only or (path is not None and has_audio_stream(path))
        attrs = {
            "id": rid,
            "name": name,
            "src": url,
            "start": "0s",
            "duration": fmt_time(duration),
            "hasVideo": "0" if audio_only else "1",
            "hasAudio": "1" if has_audio else "0",
            "format": self.format_id,
        }
        if has_audio:
            attrs.update(audioSources="1", audioChannels="2", audioRate="48000")
        ET.SubElement(self.root, "asset", attrs)
        self._assets[url] = rid
        return rid

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


def timeline_to_fcpxml(
    timeline: "otio.schema.Timeline",
    fps: float,
    width: int,
    height: int,
    camera_audio_db: float | None = -12.0,
    project_name: str | None = None,
) -> ET.ElementTree:
    """Build the FCPXML element tree for ``timeline`` (V1 picture track + optional A1 music).

    ``camera_audio_db``: volume applied to picture clips when a soundtrack is present
    (None keeps them untouched; use a very low value such as -96 to mute).
    """
    rate = frame_rate(fps)
    frame = 1 / rate
    root = ET.Element("fcpxml", version=FCPXML_VERSION)
    resources = ET.SubElement(root, "resources")
    res = _Resources(resources, rate, width, height)

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
    first_clip_el: ET.Element | None = None
    first_clip_start = Fraction(0)
    first_clip_offset = Fraction(0)
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
        asset_id = res.asset(url, Path(unquote(urlparse(url).path)).stem or item.name, asset_dur)

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
            ref=asset_id,
            name=item.name or "clip",
            offset=fmt_time(cursor),
            start=fmt_time(src_in),
            duration=fmt_time(out_dur),
            tcFormat="NDF",
        )
        notes = []
        if all(k in meta for k in ("crop_x", "crop_y", "crop_width", "crop_height")):
            notes.append(
                f"AutoEdit 9:16 crop window x={meta['crop_x']} y={meta['crop_y']} "
                f"w={meta['crop_width']} h={meta['crop_height']} ({meta.get('tracking_subject', 'subject')})"
            )
        if meta.get("ken_burns"):
            notes.append(f"AutoEdit Ken Burns: {meta['ken_burns']}")
        if notes:
            ET.SubElement(clip_el, "note").text = "; ".join(notes)
        if abs(speed - 1.0) > 1e-6:
            tm = ET.SubElement(clip_el, "timeMap")
            ET.SubElement(tm, "timept", time="0s", value=fmt_time(src_in), interp="linear")
            ET.SubElement(
                tm,
                "timept",
                time=fmt_time(out_dur),
                value=fmt_time(src_in + out_dur * Fraction(speed).limit_denominator(1000)),
                interp="linear",
            )
        if soundtrack is not None and camera_audio_db is not None:
            ET.SubElement(clip_el, "adjust-volume", amount=f"{camera_audio_db:g}dB")
        for mk in item.markers:
            ET.SubElement(
                clip_el,
                "marker",
                start=fmt_time(_secs(mk.marked_range.start_time)),
                duration=fmt_time(frame),
                value=str(mk.name),
                **({"note": str(mk.comment)} if getattr(mk, "comment", "") else {}),
            )

        if first_clip_el is None and abs(speed - 1.0) <= 1e-6:
            first_clip_el, first_clip_start, first_clip_offset = clip_el, src_in, cursor
        cursor += out_dur

    sequence.set("duration", fmt_time(cursor))

    if soundtrack is not None and first_clip_el is not None:
        s_url = soundtrack.media_reference.target_url
        s_avail = soundtrack.media_reference.available_range
        s_id = res.asset(
            s_url,
            Path(unquote(urlparse(s_url).path)).stem or "Soundtrack",
            _secs(s_avail.duration) if s_avail is not None else cursor,
            audio_only=True,
        )
        music = ET.Element(
            "asset-clip",
            ref=s_id,
            name=soundtrack.name or "Soundtrack",
            lane="-1",
            # connected-clip offsets live in the parent's source timebase
            offset=fmt_time(first_clip_start - first_clip_offset),
            start=fmt_time(_secs(soundtrack.source_range.start_time)),
            duration=fmt_time(min(cursor, _secs(soundtrack.source_range.duration))),
            audioRole="music",
        )
        # schema order: note, timeMap, adjust-*, connected clips, markers
        insert_at = len([c for c in first_clip_el if c.tag in ("note", "timeMap", "adjust-volume")])
        first_clip_el.insert(insert_at, music)

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
) -> Path:
    """Write ``timeline`` as a FCPXML 1.8 file (UTF-8, DOCTYPE fcpxml)."""
    tree = timeline_to_fcpxml(timeline, fps, width, height, camera_audio_db, project_name)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ET.tostring(tree.getroot(), encoding="unicode")
    tmp = path.with_name(path.name + ".part")
    tmp.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n' + body + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
