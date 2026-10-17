"""Post-process OTIO's FCP7 XML (xmeml) so Adobe Premiere Pro can relink media cleanly.

OTIO writes ``<media><video/><audio/></media>`` with no channel info, so Premiere assumes
mono clips and later refuses to link a stereo file ("... has 2 audio channel(s) and the clip
was created with 1 audio channel(s)"). This pass reads the real media with ffprobe and:

- fills ``<file>`` definitions: video size, audio sample rate and ``channelcount``
- gives audio clipitems a ``<sourcetrack>`` and groups them into tracks by channel type
  (Stereo / Mono, ``premiereTrackType``)
- drops audio clipitems whose source has no audio stream
- declares the sequence audio format/outputs (stereo, 48 kHz)
- stores plain (not percent-encoded) file names
"""

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from src.core.media_info import probe_media

logger = logging.getLogger(__name__)


def _path_from_url(url: str) -> Path | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("file", ""):
        return None
    raw = unquote(parsed.path)
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return Path(url2pathname(raw))


def _probe(path: Path) -> dict:
    """{'width','height','channels','rate','has_audio'} for a media file (zeros when unknown).

    Width/height are display sizes (rotation applied), so a portrait iPhone clip is reported
    as 1080x1920 rather than its coded 1920x1080.
    """
    info = probe_media(path) if path else None
    result = {"width": 0, "height": 0, "channels": 0, "rate": 48000, "has_audio": False}
    if info is None:
        return result
    if info.has_video:
        result["width"], result["height"] = info.width, info.height
    if info.has_audio:
        result["has_audio"] = True
        result["channels"] = info.audio_channels or 2
        result["rate"] = info.audio_rate or 48000
    return result


def _set(parent: ET.Element, tag: str, text: str) -> ET.Element:
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    el.text = text
    return el


def _fill_file(file_el: ET.Element, info: dict) -> None:
    media = file_el.find("media")
    if media is None:
        media = ET.SubElement(file_el, "media")
    for child in list(media):
        media.remove(child)
    if info["width"]:
        video = ET.SubElement(media, "video")
        sc = ET.SubElement(video, "samplecharacteristics")
        _set(sc, "width", str(info["width"]))
        _set(sc, "height", str(info["height"]))
        _set(sc, "anamorphic", "FALSE")
        _set(sc, "pixelaspectratio", "square")
        _set(sc, "fielddominance", "none")
    if info["has_audio"]:
        audio = ET.SubElement(media, "audio")
        sc = ET.SubElement(audio, "samplecharacteristics")
        _set(sc, "depth", "16")
        _set(sc, "samplerate", str(info["rate"]))
        _set(audio, "channelcount", str(info["channels"]))


def _track_type(channels: int) -> str:
    return "Stereo" if channels == 2 else "Mono" if channels == 1 else "Multichannel"


def _param(effect: ET.Element, pid: str, name: str, value: str | None = None, **extra: str) -> ET.Element:
    par = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
    _set(par, "parameterid", pid)
    _set(par, "name", name)
    for k, v in extra.items():
        _set(par, k, v)
    if value is not None:
        _set(par, "value", value)
    return par


def _effect(filter_parent: ET.Element, name: str, effectid: str) -> ET.Element:
    flt = ET.SubElement(filter_parent, "filter")
    eff = ET.SubElement(flt, "effect")
    _set(eff, "name", name)
    _set(eff, "effectid", effectid)
    _set(eff, "effectcategory", "motion")
    _set(eff, "effecttype", "motion")
    _set(eff, "mediatype", "video")
    return eff


def _add_time_remap(item: ET.Element, speed: float) -> None:
    eff = _effect(item, "Time Remap", "timeremap")
    _param(eff, "variablespeed", "variablespeed", "0", valuemin="0", valuemax="1")
    _param(eff, "speed", "speed", f"{speed * 100:g}", valuemin="-100000", valuemax="100000")
    _param(eff, "reverse", "reverse", "FALSE")
    _param(eff, "frameblending", "frameblending", "FALSE")


def _add_basic_motion(item: ET.Element, scale_pct: float, dx: float, dy: float) -> None:
    eff = _effect(item, "Basic Motion", "basic")
    _param(eff, "scale", "Scale", f"{scale_pct:.4f}", valuemin="0", valuemax="1000")
    _param(eff, "rotation", "Rotation", "0", valuemin="-8640", valuemax="8640")
    center = _param(eff, "center", "Center")
    val = ET.SubElement(center, "value")
    _set(val, "horiz", f"{dx:.6f}")
    _set(val, "vert", f"{dy:.6f}")


def _video_clip_plan(timeline, probes: dict[str, dict], canvas: tuple[int, int], fps: float) -> list[dict]:
    """Per OTIO video clip (in XML clipitem order): speed + scale/center needed in Premiere."""
    import opentimelineio as otio

    plan: list[dict] = []
    cw, ch = canvas
    track = next((t for t in timeline.tracks if t.kind == otio.schema.TrackKind.Video), None)
    for c in track or []:
        if not isinstance(c, otio.schema.Clip):
            continue
        meta = dict(c.metadata.get("AutoEdit", {}) or {})
        entry = {"name": c.name, "speed": float(meta.get("speed", 1.0) or 1.0),
                 "out_frames": c.source_range.duration.rescaled_to(fps).value if c.source_range else 0,
                 "crop": None, "url": getattr(c.media_reference, "target_url", "")}
        if all(k in meta for k in ("crop_x", "crop_y", "crop_width", "crop_height")) and meta.get("tracking_subject") != "blur_backdrop":
            entry["crop"] = (meta["crop_x"], meta["crop_y"], meta["crop_width"], meta["crop_height"])
        plan.append(entry)
    return plan


def finalize_premiere_xml(
    xml_path: Path,
    timeline=None,
    canvas: tuple[int, int] | None = None,
    fps: float | None = None,
) -> Path:
    """Rewrite ``xml_path`` in place with accurate media/channel/sequence definitions.

    With ``timeline`` (the OTIO timeline the XML came from) and ``canvas``, also writes the
    sequence frame size, Time Remap for speed ramps and Basic Motion scale/center so footage
    is fitted to (or cropped for) the canvas instead of imported at native pixel size.
    """
    xml_path = Path(xml_path)
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # 1) probe every distinct source once, keyed by <file id>
    probes: dict[str, dict] = {}
    for f in root.iter("file"):
        fid = f.get("id")
        url = (f.findtext("pathurl") or "").strip()
        if fid and url and fid not in probes:
            p = _path_from_url(url)
            probes[fid] = _probe(p) if p is not None and p.exists() else {
                "width": 0, "height": 0, "channels": 2, "rate": 48000, "has_audio": True,
            }
        if url:
            plain = Path(unquote(urlparse(url).path)).name
            name_el = f.find("name")
            if name_el is not None and plain:
                name_el.text = plain
            if fid in probes and len(list(f)) > 0:
                _fill_file(f, probes[fid])

    # 1b) sequence video format = the chosen canvas (otherwise Premiere copies the first clip)
    seq = next(root.iter("sequence"), None)
    if seq is not None and canvas is not None:
        seq_rate = seq.find("rate")
        seq_video = seq.find("media/video")
        if seq_video is not None:
            fmt = seq_video.find("format")
            if fmt is None:
                fmt = ET.Element("format")
                seq_video.insert(0, fmt)
            for ch in list(fmt):
                fmt.remove(ch)
            sc = ET.SubElement(fmt, "samplecharacteristics")
            if seq_rate is not None:
                sc.append(ET.fromstring(ET.tostring(seq_rate)))
            _set(sc, "width", str(canvas[0]))
            _set(sc, "height", str(canvas[1]))
            _set(sc, "anamorphic", "FALSE")
            _set(sc, "pixelaspectratio", "square")
            _set(sc, "fielddominance", "none")

        # 1c) per-clip speed (Time Remap) and fit/crop (Basic Motion)
        if timeline is not None:
            seq_fps = fps or 30.0
            plan = _video_clip_plan(timeline, probes, canvas, seq_fps)
            v_track = seq.find("media/video/track")
            items = v_track.findall("clipitem") if v_track is not None else []
            if len(items) == len(plan):
                file_info_by_url = {
                    (f.findtext("pathurl") or "").strip(): probes.get(f.get("id") or "", {})
                    for f in root.iter("file") if f.findtext("pathurl")
                }
                for item, entry in zip(items, plan):
                    sp = entry["speed"]
                    rate_el = item.find("rate/timebase")
                    clip_rate = float(rate_el.text) if rate_el is not None and rate_el.text else seq_fps
                    if abs(sp - 1.0) > 1e-6:
                        in_f = int(float(item.findtext("in") or 0))
                        consumed = round(entry["out_frames"] / seq_fps * clip_rate * sp)
                        _set(item, "in", str(in_f))
                        _set(item, "out", str(in_f + consumed))
                        _add_time_remap(item, sp)
                    info = file_info_by_url.get(entry["url"]) or {}
                    sw, sh = info.get("width", 0), info.get("height", 0)
                    if sw and sh:
                        cw, ch = canvas
                        if entry["crop"]:
                            x, y, w, h = entry["crop"]
                            k = ch / h  # crop window (canvas aspect) fills the canvas
                            dx = ((sw / 2) - (x + w / 2)) * k / cw
                            dy = ((sh / 2) - (y + h / 2)) * k / ch
                            _add_basic_motion(item, k * 100.0, dx, dy)
                        elif (sw, sh) != (cw, ch):
                            k = min(cw / sw, ch / sh)  # "set to frame size": letterbox fit
                            _add_basic_motion(item, k * 100.0, 0.0, 0.0)
            else:
                logger.warning(
                    "Premiere XML: %d clipitems vs %d timeline clips; skipped speed/scale fix-up",
                    len(items), len(plan),
                )

    # 2) rebuild each <audio> section's tracks grouped by channel type
    for seq_audio in root.iter("audio"):
        tracks = [t for t in seq_audio.findall("track")]
        if not tracks:
            continue  # a <file><media><audio> definition, not a sequence audio section

        groups: dict[int, list[ET.Element]] = {}
        for track in tracks:
            for item in track.findall("clipitem"):
                fid = (item.find("file") is not None and item.find("file").get("id")) or ""
                info = probes.get(fid, {"has_audio": True, "channels": 2})
                if not info["has_audio"]:
                    continue  # silent source: an audio clipitem would break relinking
                if item.find("sourcetrack") is None:
                    st = ET.SubElement(item, "sourcetrack")
                    _set(st, "mediatype", "audio")
                    _set(st, "trackindex", "1")
                groups.setdefault(int(info["channels"]) or 2, []).append(item)

        for t in tracks:
            seq_audio.remove(t)

        # sequence-level audio format, inserted before the tracks
        for tag in ("numOutputChannels", "format", "outputs"):
            for old in seq_audio.findall(tag):
                seq_audio.remove(old)
        head: list[ET.Element] = []
        n_out = ET.Element("numOutputChannels")
        n_out.text = "2"
        head.append(n_out)
        fmt = ET.Element("format")
        sc = ET.SubElement(fmt, "samplecharacteristics")
        _set(sc, "depth", "16")
        _set(sc, "samplerate", "48000")
        head.append(fmt)
        outputs = ET.Element("outputs")
        grp = ET.SubElement(outputs, "group")
        _set(grp, "index", "1")
        _set(grp, "numchannels", "2")
        _set(grp, "downmix", "0")
        for ch in (1, 2):
            c = ET.SubElement(grp, "channel")
            _set(c, "index", str(ch))
        head.append(outputs)
        for i, el in enumerate(head):
            seq_audio.insert(i, el)

        for channels in sorted(groups, reverse=True):
            tr = ET.SubElement(
                seq_audio,
                "track",
                {
                    "currentExplodedTrackIndex": "0",
                    "totalExplodedTrackCount": str(2 if channels == 2 else max(1, channels)),
                    "premiereTrackType": _track_type(channels),
                },
            )
            for item in groups[channels]:
                tr.append(item)

    ET.indent(tree, space="    ")
    tmp = xml_path.with_name(xml_path.name + ".part")
    tree.write(tmp, encoding="UTF-8", xml_declaration=True)
    tmp.replace(xml_path)
    return xml_path
