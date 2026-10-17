"""Subtitle sidecars (.srt / .ass) and ffmpeg burn-in filter helpers for the Deliver master.

Sentence times are expected in timeline seconds (the same clock as the rendered master).
"""

import logging
import re
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from src.core.types import DeliverSubtitles, SentenceSegment
from src.modules.kinetic_subtitle import export_kinetic_ass

logger = logging.getLogger(__name__)

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./:\\ \-]+$")


def format_srt_time(seconds: float) -> str:
    """SubRip timestamp ``HH:MM:SS,mmm`` (millisecond rounding, never negative)."""
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(sentences: Sequence[SentenceSegment], output_path: Path) -> Path:
    """Write a UTF-8 (no BOM) SubRip file; empty sentences are skipped and cues renumbered."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for s in sentences:
        text = (s.text or "").strip().replace("\r", "")
        # A blank line would end the cue early in SubRip.
        text = re.sub(r"\n\s*\n+", "\n", text)
        if not text:
            continue
        start = float(s.start_time)
        end = max(float(s.end_time), start + 0.04)
        blocks.append(f"{len(blocks) + 1}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n")
    # write_bytes: no platform newline translation (always LF) and no BOM.
    output_path.write_bytes("\n".join(blocks).encode("utf-8"))
    return output_path


def write_subtitle_sidecars(
    sentences: Sequence[SentenceSegment] | None,
    master_path: Path,
    mode: DeliverSubtitles,
    canvas: tuple[int, int],
) -> list[Path]:
    """Write the subtitle files that belong next to ``master_path`` for ``mode``.

    ``srt``/``burn`` -> ``<master>.srt``; ``ass`` -> ``<master>.ass`` plus the ``.srt`` (NLEs read SRT).
    Returns the written paths (empty when there is nothing to write).
    """
    if mode == "none" or not sentences:
        return []
    master_path = Path(master_path)
    written = [write_srt(sentences, master_path.with_suffix(".srt"))]
    if mode == "ass":
        written.append(
            export_kinetic_ass(
                sentences, master_path.with_suffix(".ass"), is_vertical=canvas[1] > canvas[0]
            )
        )
    return written


def escape_filter_path(path: Path | str) -> str:
    """Escape a file path for use as an ffmpeg filter option value (graph passed as one argv item).

    Two escaping levels apply (option value, then filtergraph): backslashes become forward slashes,
    then ``\\ ' :`` are escaped for the option parser and ``\\ ' [ ] , ;`` again for the graph parser.
    Spaces and Unicode need no escaping. ``C:\\a b\\it's.srt`` -> ``C\\\\:/a b/it\\\\\\'s.srt``.
    """
    s = str(path).replace("\\", "/")
    s = s.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")  # option level
    out: list[str] = []
    for ch in s:  # graph level
        out.append("\\" + ch if ch in "\\'[],;" else ch)
    return "".join(out)


def prepare_filter_path(path: Path, scratch_dir: Path | None = None) -> Path:
    """Return ``path`` itself when it is plain ASCII, else an ASCII-named temp copy.

    libass opens the file itself; a copy with a boring name sidesteps any code-page surprises
    with Unicode / apostrophes in the user's folders. The copy lives in the system temp dir.
    """
    path = Path(path)
    if _SAFE_PATH_RE.match(str(path)) and "'" not in str(path):
        return path
    base = Path(tempfile.gettempdir()) if scratch_dir is None else Path(scratch_dir)
    base.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="sub_", dir=base))
    dest = tmp_dir / ("subs" + path.suffix.lower())
    shutil.copyfile(path, dest)
    return dest


def burn_filter(
    subtitle_path: Path,
    canvas: tuple[int, int],
    font: str = "Arial",
    font_size: int | None = None,
) -> str:
    """ffmpeg filter that burns ``subtitle_path`` (.srt -> ``subtitles=``, .ass -> ``ass=``).

    SRT cues are laid out on libass' 384x288 virtual canvas, so ``font_size`` is in those units
    (default 20 landscape / 16 portrait). Arial covers Vietnamese diacritics.
    """
    w, h = canvas
    escaped = escape_filter_path(subtitle_path)
    if Path(subtitle_path).suffix.lower() == ".ass":
        return f"ass=filename={escaped}"
    portrait = h > w
    size = font_size if font_size else (16 if portrait else 20)
    margin_v = 60 if portrait else 20
    style = f"FontName={font},FontSize={size},Outline=2,Shadow=1,MarginV={margin_v}"
    # force_style is itself an option value containing ',' and '='; quote it at option level.
    return f"subtitles=filename={escaped}:force_style={escape_filter_path(style)}"
