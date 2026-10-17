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

from src.core.ffmpeg_utils import ffprobe_json

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
    """{'width','height','channels','rate','has_audio'} for a media file (zeros when unknown)."""
    info = {"width": 0, "height": 0, "channels": 0, "rate": 48000, "has_audio": False}
    data = ffprobe_json(path) if path else None
    if not data:
        return info
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video" and not info["width"] and (s.get("disposition", {}) or {}).get("attached_pic", 0) != 1:
            info["width"], info["height"] = int(s.get("width") or 0), int(s.get("height") or 0)
        elif kind == "audio" and not info["has_audio"]:
            info["has_audio"] = True
            info["channels"] = int(s.get("channels") or 2)
            info["rate"] = int(s.get("sample_rate") or 48000)
    return info


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


def finalize_premiere_xml(xml_path: Path) -> Path:
    """Rewrite ``xml_path`` in place with accurate media/channel definitions."""
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
