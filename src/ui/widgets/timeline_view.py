"""Multi-Track Timeline and Beat-Grid Inspector Widget.

Provides an interactive visual representation of assembled video and audio sequences:
- Timecode ruler with zoom/pan capabilities.
- Track V2: Teaser Hook fast-cuts.
- Track V1: Master Video sequence aligned to cut points.
- Track A1: BGM Audio track with song section coloring (INTRO, BUILDUP, DROP, OUTRO),
  energy curves, beat markers, and downbeat indicators.
- Interactive playhead scrubbing and clip inspector.
"""

import logging
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal, Slot
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QHelpEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from src.core.types import (
    BeatMarker,
    EditClip,
    EditTimeline,
    MusicalBarGrid,
    ScoredScene,
    SongSection,
    SongSectionType,
    TimelineVariant,
)
from src.modules.audio_analyzer import AudioAnalysisResult

logger = logging.getLogger(__name__)


def format_timecode(seconds: float) -> str:
    """Format seconds into MM:SS.mmm format."""
    total_ms = int(round(seconds * 1000.0))
    mins = total_ms // 60000
    secs = (total_ms % 60000) // 1000
    ms = total_ms % 1000
    return f"{mins:02d}:{secs:02d}.{ms:03d}"


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a QApplication)
# ---------------------------------------------------------------------------

# Variant pills: canonical order (rough cut first) and icon names. Unknown keys keep "<name> (<dur>s)".
VARIANT_PILL_NAMES: dict[str, str] = {
    "rough_cut": "🔨 Rough Cut",
    "master": "✨ Master",
    "beat_driven": "⚡ Beat-Driven",
    "cinematic": "🎬 Cinematic",
    "social_teaser": "📱 Social Teaser",
}
VARIANT_ORDER: tuple[str, ...] = tuple(VARIANT_PILL_NAMES)


def variant_pill_label(key: str, name: str, duration: float) -> str:
    """Pill text for an edit timeline: icon + friendly name + duration."""
    return f"{VARIANT_PILL_NAMES.get(key, name)} ({duration:.1f}s)"


def sort_variant_keys(keys: Iterable[str]) -> list[str]:
    """Known variants in canonical order (rough_cut first), unknown ones after, original order kept."""
    ordered = list(keys)
    rank = {k: i for i, k in enumerate(VARIANT_ORDER)}
    return sorted(ordered, key=lambda k: (rank.get(k, len(rank)), ordered.index(k)))


@dataclass(frozen=True)
class BarLine:
    """One bar line of the ruler. ``level`` = 1 for a plain bar, else the phrase length (2/4/8) it starts."""

    time: float
    bar_number: int  # 1-based index in the bar grid
    level: int
    labelled: bool


def _median_interval(times: Sequence[float]) -> float:
    gaps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    return gaps[len(gaps) // 2] if gaps else 0.0


def compute_bar_lines(
    grid: MusicalBarGrid | None,
    px_per_sec: float,
    start_t: float,
    end_t: float,
    *,
    min_bar_px: float = 3.0,
    min_label_bar_px: float = 12.0,
    min_label_gap_px: float = 28.0,
) -> list[BarLine]:
    """Bar / phrase lines for the visible time range ``[start_t, end_t]`` of a ruler.

    Density adapts to zoom: plain bar lines vanish when bars are closer than ``min_bar_px`` (phrase
    lines remain while they are at least that far apart), and bar numbers are skipped entirely when
    bars are closer than ``min_label_bar_px``. Numbers go on phrase starts (>= 4 bars, or 2-bar
    phrases when no longer ones exist), at least ``min_label_gap_px`` apart. Only the visible slice of
    the grid is touched (bisect), so cost does not grow with track length.
    """
    if grid is None or not grid.bar_times or px_per_sec <= 0:
        return []
    bars = grid.bar_times
    bar_len = _median_interval(bars)
    if bar_len <= 0:
        return []
    bar_px = bar_len * px_per_sec
    level_of: dict[float, int] = {}
    for lvl, starts in ((2, grid.phrase_2), (4, grid.phrase_4), (8, grid.phrase_8)):
        for t in starts:
            level_of[round(t, 3)] = max(level_of.get(round(t, 3), 1), lvl)
    has_long = any(v >= 4 for v in level_of.values())
    label_level = 4 if has_long else 2

    lo = max(0, bisect_left(bars, start_t - bar_len) - 1)
    hi = min(len(bars), bisect_right(bars, end_t + bar_len) + 1)
    out: list[BarLine] = []
    last_label_x = -1e18
    for i in range(lo, hi):
        t = bars[i]
        lvl = level_of.get(round(t, 3), 1)
        if lvl == 1 and bar_px < min_bar_px:
            continue
        if lvl > 1 and bar_px * lvl < min_bar_px:
            continue
        labelled = False
        if bar_px >= min_label_bar_px and lvl >= label_level:
            x = t * px_per_sec
            if x - last_label_x >= min_label_gap_px:
                labelled = True
                last_label_x = x
        out.append(BarLine(t, i + 1, lvl, labelled))
    return out


def lock_badge_text(snap_error_sec: float | None, fps: float) -> tuple[str, bool] | None:
    """"downbeat lock ✔ ±N ms" badge text and whether the error is within half a frame.

    ``None`` when the timeline carries no snap error (rough cuts, legacy projects).
    """
    if snap_error_sec is None:
        return None
    ms = abs(float(snap_error_sec)) * 1000.0
    half_frame_ms = 500.0 / fps if fps > 0 else 0.0
    ok = ms <= half_frame_ms + 0.05
    return (f"downbeat lock {'✔' if ok else '⚠'} ±{ms:.0f} ms", ok)


def _fmt_sec(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def clip_sub_window_text(clip: EditClip | None, fps: float) -> str | None:
    """"sub-window 19.2–27.2 s of 41 s" for a clip that uses only part of its source scene."""
    if clip is None or clip.scene_start is None or clip.scene_end is None or fps <= 0:
        return None
    scene_len = float(clip.scene_end) - float(clip.scene_start)
    win_len = clip.frames / fps * (clip.speed if clip.speed > 0 else 1.0)
    if scene_len - win_len <= 0.25:
        return None
    a = float(clip.source_in) - float(clip.scene_start)
    return f"sub-window {_fmt_sec(a)}–{_fmt_sec(a + win_len)} s of {_fmt_sec(scene_len)} s"


@dataclass
class TimelineClipItem:
    """Represents a discrete clip on the visual timeline sequence."""

    clip_id: str
    name: str
    track: str  # "V1", "V2", "A1"
    start_time: float
    end_time: float
    duration: float
    source_path: Path
    source_start: float
    source_end: float
    composite_score: float = 0.5
    motion_energy: float = 0.5
    is_teaser: bool = False
    scene_ref: ScoredScene | None = None
    edit_clip: EditClip | None = None  # Backing clip of the EditTimeline (None for legacy items)


class TimelineCanvas(QWidget):
    """Custom painted multi-track canvas with zoom, pan, and playhead scrubbing."""

    playhead_moved = Signal(float)
    clip_clicked = Signal(object)
    section_clicked = Signal(object)

    LEFT_MARGIN = 75  # Width reserved for track header labels
    RULER_HEIGHT = 26
    V2_HEIGHT = 36
    V1_HEIGHT = 54
    A1_HEIGHT = 64

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.px_per_sec: float = 30.0  # Zoom level
        self.total_duration: float = 0.0
        self.playhead_time: float = 0.0
        self.clips: list[TimelineClipItem] = []
        self.audio_analysis: AudioAnalysisResult | None = None
        self.bar_grid: MusicalBarGrid | None = None  # bar / phrase lattice drawn on the ruler
        self.fps: float = 30.0
        self.has_teaser_track: bool = False
        self.selected_clip: TimelineClipItem | None = None
        self.selected_section: SongSection | None = None

        self._dragging_playhead: bool = False
        self.setMouseTracking(True)
        self.setMinimumHeight(self._compute_total_height())
        self._update_geometry_size()

    def _compute_total_height(self) -> int:
        h = self.RULER_HEIGHT + self.V1_HEIGHT + self.A1_HEIGHT + 20
        if self.has_teaser_track:
            h += self.V2_HEIGHT
        return h

    def _update_geometry_size(self) -> None:
        min_width = self.LEFT_MARGIN + int(max(10.0, self.total_duration) * self.px_per_sec) + 100
        self.setMinimumWidth(min_width)
        self.setFixedHeight(self._compute_total_height())
        self.updateGeometry()
        self.update()

    def set_zoom(self, px_per_sec: float) -> None:
        """Set horizontal scale in pixels per second."""
        self.px_per_sec = max(5.0, min(150.0, float(px_per_sec)))
        self._update_geometry_size()

    def set_playhead(self, time_sec: float) -> None:
        """Position playhead cursor and redraw dirty region."""
        new_time = max(0.0, min(self.total_duration, time_sec))
        if abs(new_time - self.playhead_time) < 1e-4:
            return

        old_x = self.LEFT_MARGIN + int(self.playhead_time * self.px_per_sec)
        self.playhead_time = new_time
        new_x = self.LEFT_MARGIN + int(new_time * self.px_per_sec)

        h = self.height()
        r_old = QRect(old_x - 12, 0, 24, h)
        r_new = QRect(new_x - 12, 0, 24, h)
        if abs(old_x - new_x) < 40:
            self.update(r_old.united(r_new))
        else:
            self.update(r_old)
            self.update(r_new)

    def seek_playhead(self, time_sec: float) -> None:
        """Position playhead cursor and redraw (alias for set_playhead)."""
        self.set_playhead(time_sec)

    # -------------------------------------------------------------------------
    # Painting Pipeline
    # -------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        """Render multi-track timeline, clips, song sections, and playhead."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        rect = event.rect()
        width = self.width()

        # Background
        painter.fillRect(rect, QColor("#121214"))

        # Calculate track Y offsets
        y_ruler = 0
        y_v2 = self.RULER_HEIGHT if self.has_teaser_track else 0
        y_v1 = self.RULER_HEIGHT + (self.V2_HEIGHT if self.has_teaser_track else 0)
        y_a1 = y_v1 + self.V1_HEIGHT

        # 1. Paint Track Headers (Left sidebar)
        self._paint_track_headers(painter, y_ruler, y_v2, y_v1, y_a1)

        # Visible time bounds
        visible_start_t = max(0.0, (rect.left() - self.LEFT_MARGIN) / self.px_per_sec)
        visible_end_t = (rect.right() - self.LEFT_MARGIN) / self.px_per_sec

        # 2. Paint Timecode Ruler
        self._paint_ruler(painter, y_ruler, width, visible_start_t, visible_end_t)
        self._paint_bar_grid(painter, y_ruler, y_a1 + self.A1_HEIGHT, visible_start_t, visible_end_t)

        # 3. Paint Track Grid Lines & Separators
        self._paint_track_lanes(painter, y_v2, y_v1, y_a1, width)

        # 4. Paint A1 Music Track (Sections, Beats, Downbeats)
        self._paint_audio_track(painter, y_a1, visible_start_t, visible_end_t)

        # 5. Paint Video Clips (V2 & V1)
        self._paint_video_clips(painter, y_v2, y_v1, visible_start_t, visible_end_t)

        # 6. Paint Playhead Needle
        self._paint_playhead(painter)

        painter.end()

    def _paint_track_headers(self, painter: QPainter, y_ruler: int, y_v2: int, y_v1: int, y_a1: int) -> None:
        """Paint static left lane header pills."""
        header_rect = QRect(0, 0, self.LEFT_MARGIN, self.height())
        painter.fillRect(header_rect, QColor("#16161a"))
        painter.setPen(QColor("#27272a"))
        painter.drawLine(self.LEFT_MARGIN, 0, self.LEFT_MARGIN, self.height())

        font = QFont("Segoe UI", 9)
        font.setBold(True)
        painter.setFont(font)

        # Ruler Header
        painter.setPen(QColor("#71717a"))
        painter.drawText(QRect(6, y_ruler, self.LEFT_MARGIN - 12, self.RULER_HEIGHT), Qt.AlignmentFlag.AlignVCenter, "TIMECODE")

        # V2 Header
        if self.has_teaser_track:
            painter.setPen(QColor("#ec4899"))  # Pink for teaser
            painter.drawText(QRect(6, y_v2, self.LEFT_MARGIN - 12, self.V2_HEIGHT), Qt.AlignmentFlag.AlignVCenter, "V2 TEASER")

        # V1 Header
        painter.setPen(QColor("#38bdf8"))  # Cyan for master video
        painter.drawText(QRect(6, y_v1, self.LEFT_MARGIN - 12, self.V1_HEIGHT), Qt.AlignmentFlag.AlignVCenter, "V1 VIDEO")

        # A1 Header
        painter.setPen(QColor("#a855f7"))  # Purple for audio/music
        painter.drawText(QRect(6, y_a1, self.LEFT_MARGIN - 12, self.A1_HEIGHT), Qt.AlignmentFlag.AlignVCenter, "A1 MUSIC")

    def _paint_ruler(self, painter: QPainter, y: int, width: int, start_t: float, end_t: float) -> None:
        """Draw time markings and second tick intervals."""
        ruler_rect = QRect(self.LEFT_MARGIN, y, width - self.LEFT_MARGIN, self.RULER_HEIGHT)
        painter.fillRect(ruler_rect, QColor("#18181b"))
        painter.setPen(QColor("#27272a"))
        painter.drawLine(self.LEFT_MARGIN, y + self.RULER_HEIGHT - 1, width, y + self.RULER_HEIGHT - 1)

        # Dynamic tick step based on zoom
        if self.px_per_sec >= 60.0:
            step_sec = 1.0
        elif self.px_per_sec >= 20.0:
            step_sec = 5.0
        elif self.px_per_sec >= 8.0:
            step_sec = 10.0
        else:
            step_sec = 30.0

        first_tick = int(start_t / step_sec) * step_sec
        font = QFont("Consolas", 8)
        painter.setFont(font)
        painter.setPen(QColor("#a1a1aa"))

        curr_t = first_tick
        while curr_t <= end_t + step_sec:
            x = self.LEFT_MARGIN + int(curr_t * self.px_per_sec)
            painter.drawLine(x, y + self.RULER_HEIGHT - 6, x, y + self.RULER_HEIGHT - 1)
            time_str = f"{int(curr_t // 60):02d}:{int(curr_t % 60):02d}"
            painter.drawText(QRect(x + 3, y + 2, 45, self.RULER_HEIGHT - 4), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, time_str)
            curr_t += step_sec

    def _paint_bar_grid(self, painter: QPainter, y: int, y_bottom: int, start_t: float, end_t: float) -> None:
        """Bar lines (thin) and 2/4/8-bar phrase lines (heavier) on the ruler; phrase lines >= 4 bars
        also run faintly through the lanes. Only the visible slice of the grid is walked."""
        lines = compute_bar_lines(self.bar_grid, self.px_per_sec, start_t, end_t)
        if not lines:
            return
        font = QFont("Consolas", 7)
        painter.setFont(font)
        ruler_bottom = y + self.RULER_HEIGHT - 1
        for ln in lines:
            x = self.LEFT_MARGIN + int(ln.time * self.px_per_sec)
            if ln.level >= 8:
                painter.setPen(QPen(QColor("#e4e4e7"), 2))
                top = y + 4
            elif ln.level >= 4:
                painter.setPen(QPen(QColor("#a1a1aa"), 2))
                top = y + 8
            elif ln.level >= 2:
                painter.setPen(QPen(QColor("#71717a"), 1))
                top = y + 12
            else:
                painter.setPen(QPen(QColor("#3f3f46"), 1))
                top = y + 17
            painter.drawLine(x, top, x, ruler_bottom)
            if ln.level >= 4:
                painter.setPen(QPen(QColor(255, 255, 255, 22), 1))
                painter.drawLine(x, ruler_bottom + 1, x, y_bottom)
            if ln.labelled:
                painter.setPen(QColor("#fbbf24") if ln.level >= 8 else QColor("#a1a1aa"))
                painter.drawText(x + 3, y + 10, str(ln.bar_number))

    def _paint_track_lanes(self, painter: QPainter, y_v2: int, y_v1: int, y_a1: int, width: int) -> None:
        """Paint horizontal grid lines separating tracks."""
        painter.setPen(QColor("#27272a"))
        if self.has_teaser_track:
            painter.drawLine(self.LEFT_MARGIN, y_v2 + self.V2_HEIGHT - 1, width, y_v2 + self.V2_HEIGHT - 1)
        painter.drawLine(self.LEFT_MARGIN, y_v1 + self.V1_HEIGHT - 1, width, y_v1 + self.V1_HEIGHT - 1)
        painter.drawLine(self.LEFT_MARGIN, y_a1 + self.A1_HEIGHT - 1, width, y_a1 + self.A1_HEIGHT - 1)

    def _paint_audio_track(self, painter: QPainter, y_a1: int, start_t: float, end_t: float) -> None:
        """Draw musical sections, downbeats, beat markers, and energy blocks."""
        if not self.audio_analysis:
            return

        # 1. Song Sections Background Blocks
        font_sec = QFont("Segoe UI", 8)
        font_sec.setBold(True)
        painter.setFont(font_sec)

        for sec in self.audio_analysis.sections:
            if sec.end_time < start_t or sec.start_time > end_t:
                continue

            x1 = self.LEFT_MARGIN + int(sec.start_time * self.px_per_sec)
            x2 = self.LEFT_MARGIN + int(sec.end_time * self.px_per_sec)
            w = max(2, x2 - x1)
            sec_rect = QRect(x1, y_a1 + 2, w, self.A1_HEIGHT - 4)

            # Palette according to Section Type
            color, label_color = self._get_section_colors(sec.section_type)
            painter.fillRect(sec_rect, color)

            # Highlight selected section
            if self.selected_section == sec:
                painter.setPen(QPen(QColor("#ffffff"), 2))
                painter.drawRect(sec_rect)

            # Section Label
            painter.setPen(label_color)
            label_text = f"{sec.section_type.value} [E:{sec.avg_energy:.2f}]"
            if sec.is_climax:
                label_text += " [CLIMAX]"
            painter.drawText(sec_rect.adjusted(6, 4, -4, -4), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, label_text)

        # 2. Beat Markers and Downbeats
        for beat in self.audio_analysis.beats:
            t = beat.timestamp
            if t < start_t or t > end_t:
                continue

            x = self.LEFT_MARGIN + int(t * self.px_per_sec)
            if beat.is_downbeat:
                # Solid prominent gold tick for 4/4 downbeats
                painter.setPen(QPen(QColor("#facc15"), 2))
                painter.drawLine(x, y_a1 + self.A1_HEIGHT - 16, x, y_a1 + self.A1_HEIGHT - 2)
            else:
                # Subdued cyan tick for normal rhythm beats
                painter.setPen(QPen(QColor("#38bdf8"), 1, Qt.PenStyle.DashLine))
                painter.drawLine(x, y_a1 + self.A1_HEIGHT - 10, x, y_a1 + self.A1_HEIGHT - 2)

    def _paint_video_clips(self, painter: QPainter, y_v2: int, y_v1: int, start_t: float, end_t: float) -> None:
        """Draw video sequence clips on V1 and V2 tracks."""
        font_clip = QFont("Segoe UI", 8)
        font_clip.setBold(True)
        painter.setFont(font_clip)

        for clip in self.clips:
            if clip.end_time < start_t or clip.start_time > end_t:
                continue

            track_y = y_v2 if clip.track == "V2" else y_v1
            track_h = self.V2_HEIGHT if clip.track == "V2" else self.V1_HEIGHT

            x1 = self.LEFT_MARGIN + int(clip.start_time * self.px_per_sec)
            x2 = self.LEFT_MARGIN + int(clip.end_time * self.px_per_sec)
            w = max(4, x2 - x1)
            clip_rect = QRect(x1 + 1, track_y + 3, w - 2, track_h - 6)

            # Background styling
            bg_color = QColor("#1e293b") if clip.track == "V1" else QColor("#500724")
            painter.fillRect(clip_rect, bg_color)

            # Top Quality Score Accent Strip
            score_color = self._get_score_accent_color(clip.composite_score)
            painter.fillRect(QRect(clip_rect.left(), clip_rect.top(), clip_rect.width(), 3), score_color)

            # Selection Border
            if self.selected_clip == clip:
                painter.setPen(QPen(QColor("#ffffff"), 2))
                painter.drawRect(clip_rect)
            else:
                painter.setPen(QColor("#334155"))
                painter.drawRect(clip_rect)

            # Sub-window marker: the clip is a slice of a longer scene
            if clip.edit_clip is not None and clip.edit_clip.scene_start is not None and w > 6:
                if clip_sub_window_text(clip.edit_clip, self.fps) is not None:
                    painter.fillRect(QRect(clip_rect.left() + 1, clip_rect.bottom() - 3, clip_rect.width() - 2, 3), QColor("#a855f7"))

            # Clip Label & Duration
            painter.setPen(QColor("#f8fafc"))
            dur_str = f"({clip.duration:.1f}s)"
            text = f"{clip.name} {dur_str}" if w > 50 else clip.name
            painter.drawText(clip_rect.adjusted(4, 4, -4, -4), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    def _paint_playhead(self, painter: QPainter) -> None:
        """Render interactive playhead needle across all tracks."""
        x = self.LEFT_MARGIN + int(self.playhead_time * self.px_per_sec)
        h = self.height()

        # Needle line
        painter.setPen(QPen(QColor("#ef4444"), 2))
        painter.drawLine(x, 0, x, h)

        # Head marker triangle
        head = QPolygonF([
            QPoint(x - 6, 0),
            QPoint(x + 6, 0),
            QPoint(x, 12),
        ])
        painter.setBrush(QBrush(QColor("#ef4444")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(head)

    # -------------------------------------------------------------------------
    # Mouse & Scrubbing Interactions
    # -------------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse clicks for playhead positioning or clip/section inspection."""
        if event.button() == Qt.MouseButton.LeftButton:
            x = event.position().x()
            y = event.position().y()

            # Click on playhead or scrub in ruler/track
            if x >= self.LEFT_MARGIN:
                time_at_x = (x - self.LEFT_MARGIN) / self.px_per_sec
                self.playhead_time = max(0.0, min(self.total_duration, time_at_x))
                self._dragging_playhead = True
                self.playhead_moved.emit(self.playhead_time)
                self.update()

                # Identify clicked clip
                clicked_clip = self._find_clip_at(x, y)
                if clicked_clip:
                    self.selected_clip = clicked_clip
                    self.clip_clicked.emit(clicked_clip)
                    self.update()
                    return

                # Identify clicked song section
                clicked_sec = self._find_section_at(x, y)
                if clicked_sec:
                    self.selected_section = clicked_sec
                    self.section_clicked.emit(clicked_sec)
                    self.update()
                    return

    def clip_tooltip(self, clip: TimelineClipItem) -> str:
        """Hover text of a clip: name, duration and (for slices of long scenes) the sub-window."""
        text = f"{clip.name} ({clip.duration:.2f}s)"
        sub = clip_sub_window_text(clip.edit_clip, self.fps)
        return f"{text}\n{sub}" if sub else text

    def event(self, e: QEvent) -> bool:
        if e.type() == QEvent.Type.ToolTip and isinstance(e, QHelpEvent):
            clip = self._find_clip_at(float(e.pos().x()), float(e.pos().y()))
            if clip is not None:
                QToolTip.showText(e.globalPos(), self.clip_tooltip(clip), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Update playhead position while dragging."""
        if self._dragging_playhead and (event.buttons() & Qt.MouseButton.LeftButton):
            x = event.position().x()
            time_at_x = (x - self.LEFT_MARGIN) / self.px_per_sec
            self.playhead_time = max(0.0, min(self.total_duration, time_at_x))
            self.playhead_moved.emit(self.playhead_time)
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """End dragging playhead state."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging_playhead = False

    def _find_clip_at(self, x: float, y: float) -> TimelineClipItem | None:
        """Return TimelineClipItem under cursor coordinates if any."""
        t = (x - self.LEFT_MARGIN) / self.px_per_sec
        y_v2 = self.RULER_HEIGHT if self.has_teaser_track else 0
        y_v1 = self.RULER_HEIGHT + (self.V2_HEIGHT if self.has_teaser_track else 0)

        for clip in self.clips:
            if clip.start_time <= t <= clip.end_time:
                if clip.track == "V2" and (y_v2 <= y < y_v2 + self.V2_HEIGHT):
                    return clip
                if clip.track == "V1" and (y_v1 <= y < y_v1 + self.V1_HEIGHT):
                    return clip
        return None

    def _find_section_at(self, x: float, y: float) -> SongSection | None:
        """Return SongSection under cursor coordinates if in A1 track."""
        if not self.audio_analysis:
            return None
        y_v1 = self.RULER_HEIGHT + (self.V2_HEIGHT if self.has_teaser_track else 0)
        y_a1 = y_v1 + self.V1_HEIGHT
        if not (y_a1 <= y < y_a1 + self.A1_HEIGHT):
            return None

        t = (x - self.LEFT_MARGIN) / self.px_per_sec
        for sec in self.audio_analysis.sections:
            if sec.start_time <= t <= sec.end_time:
                return sec
        return None

    # -------------------------------------------------------------------------
    # Color Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _get_section_colors(sec_type: SongSectionType) -> tuple[QColor, QColor]:
        """Return background and text label colors for musical section."""
        if sec_type == SongSectionType.INTRO:
            return QColor("#1e293b"), QColor("#94a3b8")  # Slate blue
        if sec_type == SongSectionType.BUILDUP:
            return QColor("#451a03"), QColor("#fde68a")  # Amber
        if sec_type == SongSectionType.DROP:
            return QColor("#701a75"), QColor("#fbcfe8")  # Vivid purple/magenta
        return QColor("#18181b"), QColor("#71717a")  # Outro dark gray

    @staticmethod
    def _get_score_accent_color(score: float) -> QColor:
        """Get score indicator strip color."""
        if score >= 0.70:
            return QColor("#22c55e")
        if score >= 0.40:
            return QColor("#eab308")
        return QColor("#ef4444")


class TimelineViewWidget(QWidget):
    """Complete Multi-Track Timeline & Beat-Grid Inspector Widget."""

    playhead_changed = Signal(float)
    clip_selected = Signal(object)
    section_selected = Signal(object)
    variant_switched = Signal(str, object)
    timeline_edited = Signal()  # Emitted after a user edit (delete / paste / undo / redo)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._variants: dict[str, TimelineVariant] = {}
        self._active_variant_key: str | None = None
        self._variant_buttons: dict[str, QPushButton] = {}
        # EditTimeline model (single source of truth for Deliver)
        self._edit_timelines: dict[str, EditTimeline] = {}
        self._edit_base: EditTimeline | None = None
        self._tempo_bpm: float = 0.0
        # REAL soundtrack analysis (beats, bar grid, sections) from the last sequence run / project.
        self._audio_state: AudioAnalysisResult | None = None
        self._init_ui()

    def _init_ui(self) -> None:
        """Build toolbar, canvas scroll area, and bottom inspector bar."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 1. Top Control Bar (Zoom, Playhead info, Legend)
        ctrl_group = QGroupBox("Timeline Controls & Navigation", self)
        ctrl_layout = QHBoxLayout(ctrl_group)
        ctrl_layout.setContentsMargins(8, 6, 8, 6)
        ctrl_layout.setSpacing(10)

        # Playhead & Time Info
        self.lbl_timecode = QLabel("Playhead: 00:00.000 / 00:00.000 (BPM: --)", self)
        font_mono = QFont("Consolas", 10)
        font_mono.setBold(True)
        self.lbl_timecode.setFont(font_mono)
        self.lbl_timecode.setStyleSheet("color: #38bdf8;")
        ctrl_layout.addWidget(self.lbl_timecode)

        self.lbl_lock_badge = QLabel("", self)
        self.lbl_lock_badge.setStyleSheet(
            "color: #4ade80; font-size: 11px; font-weight: bold; border: 1px solid #14532d; "
            "border-radius: 8px; padding: 1px 8px;"
        )
        self.lbl_lock_badge.setToolTip(
            "Largest distance between an ideal bar cut and the frame it was snapped to"
        )
        self.lbl_lock_badge.setVisible(False)
        ctrl_layout.addWidget(self.lbl_lock_badge)
        ctrl_layout.addStretch()

        # Legend Pills
        legend = QLabel("Legend: [V1 Video] [Teaser V2] [Intro] [Buildup] [Drop] [Downbeat]")
        legend.setStyleSheet("color: #71717a; font-size: 11px;")
        ctrl_layout.addWidget(legend)

        # Zoom Controls
        ctrl_layout.addWidget(QLabel("Zoom:"))
        self.btn_zoom_out = QPushButton("🔍-", self)
        self.btn_zoom_out.setFixedSize(32, 28)
        self.btn_zoom_out.setObjectName("secondaryButton")
        self.btn_zoom_out.setToolTip("Zoom Out Timeline (Ctrl+-)")
        self.btn_zoom_out.clicked.connect(self._on_zoom_out)
        ctrl_layout.addWidget(self.btn_zoom_out)

        self.slider_zoom = QSlider(Qt.Orientation.Horizontal, self)
        self.slider_zoom.setRange(5, 120)
        self.slider_zoom.setValue(30)
        self.slider_zoom.setFixedWidth(120)
        self.slider_zoom.setToolTip("Timeline Zoom Slider (Pixels per Second)")
        self.slider_zoom.valueChanged.connect(self._on_zoom_slider_changed)
        ctrl_layout.addWidget(self.slider_zoom)

        self.btn_zoom_in = QPushButton("🔍+", self)
        self.btn_zoom_in.setFixedSize(32, 28)
        self.btn_zoom_in.setObjectName("secondaryButton")
        self.btn_zoom_in.setToolTip("Zoom In Timeline (Ctrl++)")
        self.btn_zoom_in.clicked.connect(self._on_zoom_in)
        ctrl_layout.addWidget(self.btn_zoom_in)

        self.btn_fit = QPushButton("↔ Fit", self)
        self.btn_fit.setObjectName("secondaryButton")
        self.btn_fit.setToolTip("Fit Entire Timeline in View (Shift+Z)")
        self.btn_fit.clicked.connect(self.fit_to_window)
        ctrl_layout.addWidget(self.btn_fit)

        layout.addWidget(ctrl_group)

        # 1.5. Variant Switcher Bar (Phase 4)
        self.variant_bar = QFrame(self)
        self.variant_bar.setStyleSheet(
            "background-color: #18181b; border: 1px solid #27272a; border-radius: 4px;"
        )
        self.variant_bar_layout = QHBoxLayout(self.variant_bar)
        self.variant_bar_layout.setContentsMargins(8, 4, 8, 4)
        self.variant_bar_layout.setSpacing(8)

        lbl_var_header = QLabel("🎬 Sequence Variations:", self)
        lbl_var_header.setStyleSheet("color: #e4e4e7; font-weight: bold; font-size: 11px;")
        self.variant_bar_layout.addWidget(lbl_var_header)

        self.variant_buttons_layout = QHBoxLayout()
        self.variant_buttons_layout.setSpacing(6)
        self.variant_bar_layout.addLayout(self.variant_buttons_layout)
        self.variant_bar_layout.addStretch()
        self.variant_bar.setVisible(False)
        layout.addWidget(self.variant_bar)

        # 2. Scrollable Canvas Area
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.canvas = TimelineCanvas(self)
        self.canvas.playhead_moved.connect(self._on_canvas_playhead_moved)
        self.canvas.clip_clicked.connect(self._on_canvas_clip_clicked)
        self.canvas.section_clicked.connect(self._on_canvas_section_clicked)
        self.scroll_area.setWidget(self.canvas)
        layout.addWidget(self.scroll_area, stretch=1)

        # 3. Bottom Inspector Pane
        self.info_panel = QFrame(self)
        self.info_panel.setFrameShape(QFrame.Shape.StyledPanel)
        self.info_panel.setStyleSheet("background-color: #16161a; border: 1px solid #27272a; border-radius: 4px;")
        info_layout = QHBoxLayout(self.info_panel)
        info_layout.setContentsMargins(10, 6, 10, 6)

        self.lbl_selected_info = QLabel("Select a clip or musical section on the timeline to inspect metadata.", self)
        self.lbl_selected_info.setStyleSheet("color: #a1a1aa;")
        info_layout.addWidget(self.lbl_selected_info)
        layout.addWidget(self.info_panel)

    def set_variants(
        self,
        variants: dict[str, TimelineVariant],
        active_key: str | None = None,
    ) -> None:
        """Populate variant switcher tabs and activate default variant."""
        self._variants = variants
        self._edit_timelines = {}
        self._edit_base = None
        self._variant_buttons.clear()

        # Clear existing buttons in variant_buttons_layout
        while self.variant_buttons_layout.count() > 0:
            item = self.variant_buttons_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not variants:
            self.variant_bar.setVisible(False)
            return

        for k, v in variants.items():
            btn = QPushButton(f"{v.display_name} ({v.total_duration:.1f}s)", self)
            btn.setObjectName("secondaryButton")
            btn.setToolTip(v.description)
            btn.clicked.connect(lambda _, key=k: self.switch_variant(key))
            self.variant_buttons_layout.addWidget(btn)
            self._variant_buttons[k] = btn

        self.variant_bar.setVisible(len(variants) >= 1)

        target = active_key if active_key in variants else list(variants.keys())[0]
        self.switch_variant(target, emit_signal=False)

    def switch_variant(self, variant_key: str, emit_signal: bool = True) -> None:
        """Switch active narrative variant, updating buttons, canvas, and emitting signal."""
        if variant_key in self._edit_timelines:
            self._switch_edit_timeline(variant_key, emit_signal)
            return
        if variant_key not in self._variants:
            return

        self._active_variant_key = variant_key
        var = self._variants[variant_key]

        # Update button visual states
        for k, btn in self._variant_buttons.items():
            if k == variant_key:
                btn.setObjectName("primaryButton")
                btn.setStyleSheet(
                    "background-color: #0284c7; color: #ffffff; font-weight: bold; padding: 4px 10px; border-radius: 4px;"
                )
            else:
                btn.setObjectName("secondaryButton")
                btn.setStyleSheet(
                    "background-color: #27272a; color: #a1a1aa; font-weight: normal; padding: 4px 10px; border-radius: 4px;"
                )

        # Reload timeline canvas with variant scenes
        if var.sequence_scenes:
            self.load_timeline_data(
                var.sequence_scenes,
                audio_result=self.canvas.audio_analysis,
                include_teaser=(var.name != "cinematic"),
            )

        if emit_signal:
            self.variant_switched.emit(variant_key, var)

    # -------------------------------------------------------------------------
    # EditTimeline Model API (Edit -> Deliver)
    # -------------------------------------------------------------------------

    def set_edit_timelines(
        self,
        timelines: dict[str, EditTimeline],
        active_key: str | None = None,
        tempo_bpm: float = 0.0,
    ) -> None:
        """Populate the variant bar from edit timelines and activate one (no signal emitted)."""
        self._variants = {}
        self._edit_timelines = dict(timelines)
        self._edit_base = None
        self._tempo_bpm = tempo_bpm
        self._rebuild_variant_buttons()
        if not self._edit_timelines:
            self._active_variant_key = None
            return
        target = active_key if active_key in self._edit_timelines else next(iter(self._edit_timelines))
        self.switch_variant(target, emit_signal=False)

    def set_audio_state(self, audio: AudioAnalysisResult | None) -> None:
        """Real soundtrack analysis used for the A1 beats, sections and the ruler's bar grid.

        Applies to music timelines; call before ``set_edit_timelines``. ``None`` falls back to the
        beats synthesised from the timeline's cut points.
        """
        self._audio_state = audio

    def get_edit_timelines(self) -> dict[str, EditTimeline]:
        """Return all edit timelines, with the active one reflecting the user's current edits."""
        out = dict(self._edit_timelines)
        key = self._active_variant_key
        if key in out and self._edit_base is not None:
            out[key] = self.get_edit_timeline()
        return out

    def get_active_timeline_key(self) -> str | None:
        """Return the key of the active edit timeline (None when none are loaded)."""
        return self._active_variant_key if self._edit_timelines else None

    def load_edit_timeline(
        self,
        timeline: EditTimeline,
        audio_result: AudioAnalysisResult | None = None,
    ) -> None:
        """Display an EditTimeline on the canvas (V2 teaser / V1 sequence / A1 sections & cuts)."""
        self._edit_base = timeline
        fps = timeline.fps if timeline.fps > 0 else 30.0
        canvas = self.canvas
        canvas.clips.clear()
        canvas.selected_clip = None
        canvas.selected_section = None

        cur_frames = 0
        teaser_idx = 0
        main_idx = 0
        for c in timeline.clips:
            if c.is_teaser:
                teaser_idx += 1
                clip_id = f"TEASER_{teaser_idx - 1}"
                name = f"Hook {teaser_idx}: {c.label or Path(c.source_path).stem}"
            else:
                main_idx += 1
                clip_id = f"CLIP_{main_idx - 1}"
                name = f"{main_idx:02d}: {c.label or Path(c.source_path).stem}"
            start = cur_frames / fps
            dur = c.frames / fps
            canvas.clips.append(
                TimelineClipItem(
                    clip_id=clip_id,
                    name=name,
                    track="V2" if c.is_teaser else "V1",
                    start_time=start,
                    end_time=start + dur,
                    duration=dur,
                    source_path=Path(c.source_path),
                    source_start=c.source_in,
                    source_end=c.source_in + dur * (c.speed if c.speed > 0 else 1.0),
                    is_teaser=c.is_teaser,
                    edit_clip=c,
                )
            )
            cur_frames += c.frames

        canvas.has_teaser_track = any(c.is_teaser for c in timeline.clips)
        canvas.fps = fps
        is_music = timeline.kind == "music"
        real_audio = audio_result is None and is_music and self._audio_state is not None
        if real_audio:
            audio_result = self._audio_state
        canvas.bar_grid = None
        if is_music:
            canvas.bar_grid = timeline.bar_grid or getattr(self._audio_state, "bar_grid", None)
        self._update_lock_badge(timeline)
        if audio_result is None:
            cuts = timeline.cut_points()
            audio_result = AudioAnalysisResult(
                tempo_bpm=self._tempo_bpm,
                duration=timeline.duration,
                beats=[BeatMarker(t, int(round(t * fps)), 1.0, False) for t in cuts[1:-1]],
                cut_timestamps=cuts,
                sample_rate=0,
                sections=list(timeline.sections),
            )
        canvas.audio_analysis = audio_result
        canvas.total_duration = (
            timeline.duration
            if real_audio
            else max(timeline.duration, audio_result.duration if audio_result else 0.0)
        )
        canvas.playhead_time = 0.0
        self.lbl_selected_info.setText("Select a clip or musical section on the timeline to inspect metadata.")
        self._update_timecode_label()
        canvas._update_geometry_size()

    def _update_lock_badge(self, timeline: EditTimeline | None) -> None:
        """Show "downbeat lock ✔ ±N ms" for music timelines that carry a snap error."""
        badge = None
        if timeline is not None and timeline.kind == "music":
            badge = lock_badge_text(timeline.cut_snap_error_max_sec, timeline.fps)
        if badge is None:
            self.lbl_lock_badge.setVisible(False)
            self.lbl_lock_badge.setText("")
            return
        text, ok = badge
        self.lbl_lock_badge.setText(text)
        color, border = ("#4ade80", "#14532d") if ok else ("#facc15", "#713f12")
        self.lbl_lock_badge.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: bold; border: 1px solid {border}; "
            "border-radius: 8px; padding: 1px 8px;"
        )
        self.lbl_lock_badge.setVisible(True)

    def get_edit_timeline(self) -> EditTimeline:
        """Return the CURRENT edit state (after deletes / pastes) as an EditTimeline."""
        base = self._edit_base
        if base is None:
            base = EditTimeline(name="Edit", fps=30.0, canvas_width=1920, canvas_height=1080)
        fps = base.fps if base.fps > 0 else 30.0
        ordered = sorted(self.canvas.clips, key=lambda c: c.start_time)
        clips = [self._item_to_edit_clip(item, fps) for item in ordered]
        return replace(base, clips=clips, sections=list(base.sections))

    @staticmethod
    def _item_to_edit_clip(item: TimelineClipItem, fps: float) -> EditClip:
        """Return the backing EditClip of an item, synthesizing one for legacy items."""
        if item.edit_clip is not None:
            return item.edit_clip
        avail = max(item.source_end - item.source_start, item.duration)
        if item.scene_ref is not None:
            avail = max(avail, item.scene_ref.scene.end_time - item.source_start)
        return EditClip(
            source_path=item.source_path,
            source_in=item.source_start,
            frames=max(1, int(round(item.duration * fps))),
            available=avail,
            is_teaser=item.is_teaser,
            label=item.name,
        )

    def _rebuild_variant_buttons(self) -> None:
        """Recreate the variant switcher buttons from the loaded edit timelines."""
        self._variant_buttons.clear()
        while self.variant_buttons_layout.count() > 0:
            item = self.variant_buttons_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for k in sort_variant_keys(self._edit_timelines):
            tl = self._edit_timelines[k]
            btn = QPushButton(variant_pill_label(k, tl.name, tl.duration), self)
            btn.setObjectName("secondaryButton")
            btn.setToolTip(f"Edit timeline: {k}")
            btn.clicked.connect(lambda _, key=k: self.switch_variant(key))
            self.variant_buttons_layout.addWidget(btn)
            self._variant_buttons[k] = btn
        self.variant_bar.setVisible(bool(self._edit_timelines))

    def _style_variant_buttons(self, active_key: str) -> None:
        for k, btn in self._variant_buttons.items():
            if k == active_key:
                btn.setObjectName("primaryButton")
                btn.setStyleSheet(
                    "background-color: #0284c7; color: #ffffff; font-weight: bold; padding: 4px 10px; border-radius: 4px;"
                )
            else:
                btn.setObjectName("secondaryButton")
                btn.setStyleSheet(
                    "background-color: #27272a; color: #a1a1aa; font-weight: normal; padding: 4px 10px; border-radius: 4px;"
                )

    def _switch_edit_timeline(self, key: str, emit_signal: bool) -> None:
        """Commit edits of the outgoing timeline, then display the incoming one."""
        prev = self._active_variant_key
        if prev in self._edit_timelines and self._edit_base is not None and prev != key:
            self._edit_timelines[prev] = self.get_edit_timeline()
        self._active_variant_key = key
        self._style_variant_buttons(key)
        tl = self._edit_timelines[key]
        self.load_edit_timeline(tl)
        if emit_signal:
            self.variant_switched.emit(key, tl)

    def _relayout_clips(self) -> None:
        """Ripple clips back-to-back in list order and refresh canvas geometry."""
        t = 0.0
        for c in self.canvas.clips:
            c.start_time = t
            c.end_time = t + c.duration
            t = c.end_time
        self.canvas.total_duration = t
        self.canvas._update_geometry_size()
        self.canvas.update()
        self._update_timecode_label()

    def _after_edit(self) -> None:
        """Refresh derived UI after a user edit and notify listeners."""
        key = self._active_variant_key
        if self._edit_base is not None and key in self._variant_buttons and key in self._edit_timelines:
            tl = self.get_edit_timeline()
            self._variant_buttons[key].setText(variant_pill_label(key, tl.name, tl.duration))
        self.timeline_edited.emit()

    def get_variants(self) -> dict[str, TimelineVariant]:
        """Return currently loaded timeline variants dictionary."""
        return dict(self._variants)

    def get_active_variant_key(self) -> str | None:
        """Return key of active narrative variant."""
        return self._active_variant_key

    # -------------------------------------------------------------------------
    # Public Data Loading API
    # -------------------------------------------------------------------------

    def load_timeline_data(
        self,
        scored_scenes: list[ScoredScene],
        audio_result: AudioAnalysisResult | None = None,
        include_teaser: bool = True,
        teaser_duration: float = 3.0,
        fps: float = 30.0,
    ) -> None:
        """Construct multi-track clip items aligning scored scenes with cut intervals."""
        self.canvas.clips.clear()
        self.canvas.audio_analysis = audio_result
        self.canvas.selected_clip = None
        self.canvas.selected_section = None

        if not scored_scenes:
            self.canvas.total_duration = 0.0
            self._update_timecode_label()
            self.canvas._update_geometry_size()
            return

        cut_timestamps = audio_result.cut_timestamps if audio_result else [0.0, sum(s.scene.duration for s in scored_scenes)]
        total_time = cut_timestamps[-1] if cut_timestamps else 0.0

        # Check teaser feasibility
        has_teaser = include_teaser and (total_time >= teaser_duration + 4.0)
        self.canvas.has_teaser_track = has_teaser

        current_timeline_t = 0.0

        # 1. Teaser clips on V2
        if has_teaser:
            num_teaser = min(5, max(3, len(scored_scenes)))
            sub_dur = teaser_duration / num_teaser
            for j in range(num_teaser):
                scene = scored_scenes[j % len(scored_scenes)]
                sc = scene.scene
                clip_dur = min(sub_dur, max(1.0 / fps, sc.duration))
                teaser_item = TimelineClipItem(
                    clip_id=f"TEASER_{j}",
                    name=f"Hook {j+1}: {sc.clip_path.stem}",
                    track="V2",
                    start_time=current_timeline_t,
                    end_time=current_timeline_t + clip_dur,
                    duration=clip_dur,
                    source_path=sc.clip_path,
                    source_start=sc.start_time,
                    source_end=sc.start_time + clip_dur,
                    composite_score=scene.composite_score,
                    motion_energy=scene.motion_energy,
                    is_teaser=True,
                    scene_ref=scene,
                )
                self.canvas.clips.append(teaser_item)
                current_timeline_t += clip_dur

        # 2. Master sequence clips on V1
        num_slots = len(cut_timestamps) - 1
        if num_slots > 0:
            for i in range(num_slots):
                slot_start = cut_timestamps[i]
                slot_end = cut_timestamps[i + 1]
                slot_dur = max(1.0 / fps, slot_end - slot_start)

                scene = scored_scenes[i % len(scored_scenes)]
                sc = scene.scene
                v1_item = TimelineClipItem(
                    clip_id=f"CLIP_{i}",
                    name=f"{i+1:02d}: {sc.clip_path.stem}",
                    track="V1",
                    start_time=current_timeline_t,
                    end_time=current_timeline_t + slot_dur,
                    duration=slot_dur,
                    source_path=sc.clip_path,
                    source_start=sc.start_time,
                    source_end=min(sc.end_time, sc.start_time + slot_dur),
                    composite_score=scene.composite_score,
                    motion_energy=scene.motion_energy,
                    is_teaser=False,
                    scene_ref=scene,
                )
                self.canvas.clips.append(v1_item)
                current_timeline_t += slot_dur

        self.canvas.total_duration = max(current_timeline_t, total_time)
        self.canvas.playhead_time = 0.0
        self._update_timecode_label()
        self.canvas._update_geometry_size()

    def clear_timeline(self) -> None:
        """Reset timeline sequence and audio tracks."""
        self.canvas.clips.clear()
        self.canvas.audio_analysis = None
        self.canvas.bar_grid = None
        self._update_lock_badge(None)
        self.canvas.has_teaser_track = False
        self.canvas.total_duration = 0.0
        self.canvas.playhead_time = 0.0
        self.canvas.selected_clip = None
        self.canvas.selected_section = None
        self.lbl_selected_info.setText("Select a clip or musical section on the timeline to inspect metadata.")
        if self._edit_timelines:
            self._edit_timelines = {}
            self._active_variant_key = None
            self._rebuild_variant_buttons()
        self._edit_base = None
        self._update_timecode_label()
        self.canvas._update_geometry_size()

    def set_playhead_time(self, time_sec: float) -> None:
        """Set playhead time from external event."""
        self.canvas.set_playhead(time_sec)
        self._update_timecode_label()

    def seek_playhead(self, time_sec: float) -> None:
        """Seek playhead position and update timecode label (alias for set_playhead_time)."""
        self.set_playhead_time(time_sec)

    def fit_to_window(self) -> None:
        """Calculate and apply zoom so the entire sequence fits visible width."""
        if self.canvas.total_duration <= 0.0:
            return
        viewport_w = self.scroll_area.viewport().width() - self.canvas.LEFT_MARGIN - 20
        if viewport_w > 50:
            fit_zoom = max(5.0, min(120.0, viewport_w / self.canvas.total_duration))
            self.slider_zoom.setValue(int(fit_zoom))
            self.canvas.set_zoom(fit_zoom)

    def zoom_fit(self) -> None:
        """Alias for fit_to_window."""
        self.fit_to_window()

    def delete_selected_clip(self) -> TimelineClipItem | None:
        """Remove currently selected clip from timeline sequence and return it."""
        clip = self.canvas.selected_clip
        if clip and self.remove_clip(clip) is not None:
            return clip
        return None

    def remove_clip(self, clip: TimelineClipItem) -> int | None:
        """Ripple-delete a clip; returns its former index (None if it was not on the timeline)."""
        if clip not in self.canvas.clips:
            return None
        index = self.canvas.clips.index(clip)
        self.canvas.clips.remove(clip)
        if self.canvas.selected_clip == clip:
            self.canvas.selected_clip = None
            self.lbl_selected_info.setText("Select a clip or musical section on the timeline to inspect metadata.")
        self._relayout_clips()
        self._after_edit()
        return index

    def insert_clip(self, clip: TimelineClipItem, index: int | None = None) -> None:
        """Insert or restore a clip (ripple); without ``index`` it goes in start-time order."""
        if clip in self.canvas.clips:
            return
        if index is None:
            index = len(self.canvas.clips)
            for i, other in enumerate(self.canvas.clips):
                if other.start_time >= clip.start_time:
                    index = i
                    break
        self.canvas.clips.insert(max(0, min(index, len(self.canvas.clips))), clip)
        self.canvas.has_teaser_track = self.canvas.has_teaser_track or clip.track == "V2"
        self._relayout_clips()
        self._after_edit()

    def deselect_all(self) -> None:
        """Clear active clip or musical section selection."""
        self.canvas.selected_clip = None
        self.canvas.selected_section = None
        self.lbl_selected_info.setText("Select a clip or musical section on the timeline to inspect metadata.")
        self.canvas.update()

    # -------------------------------------------------------------------------
    # Internal Slots & Helpers
    # -------------------------------------------------------------------------

    @Slot(int)
    def _on_zoom_slider_changed(self, value: int) -> None:
        self.canvas.set_zoom(float(value))

    @Slot()
    def _on_zoom_in(self) -> None:
        new_val = min(120, self.slider_zoom.value() + 10)
        self.slider_zoom.setValue(new_val)

    @Slot()
    def _on_zoom_out(self) -> None:
        new_val = max(5, self.slider_zoom.value() - 10)
        self.slider_zoom.setValue(new_val)

    @Slot(float)
    def _on_canvas_playhead_moved(self, time_sec: float) -> None:
        self._update_timecode_label()
        self.playhead_changed.emit(time_sec)

    @Slot(object)
    def _on_canvas_clip_clicked(self, clip: TimelineClipItem) -> None:
        info = (
            f"<b>Selected Clip:</b> {clip.name} [{clip.track}] | "
            f"Timeline: {format_timecode(clip.start_time)} - {format_timecode(clip.end_time)} ({clip.duration:.2f}s) | "
            f"Score: <b>{clip.composite_score:.2f}</b> | Energy: {clip.motion_energy:.2f} | "
            f"Source: {clip.source_path.name}"
        )
        self.lbl_selected_info.setText(info)
        self.clip_selected.emit(clip)

    @Slot(object)
    def _on_canvas_section_clicked(self, sec: SongSection) -> None:
        climax_txt = " [CLIMAX DROP]" if sec.is_climax else ""
        info = (
            f"<b>Song Section:</b> <font color='#38bdf8'>{sec.section_type.value}{climax_txt}</font> | "
            f"Duration: {format_timecode(sec.start_time)} - {format_timecode(sec.end_time)} ({sec.duration:.2f}s) | "
            f"Average Energy: <b>{sec.avg_energy:.2f}</b>"
        )
        self.lbl_selected_info.setText(info)
        self.section_selected.emit(sec)

    def _update_timecode_label(self) -> None:
        curr_str = format_timecode(self.canvas.playhead_time)
        tot_str = format_timecode(self.canvas.total_duration)
        audio = self.canvas.audio_analysis
        bpm_str = f"{audio.tempo_bpm:.1f}" if audio and audio.tempo_bpm > 0 else "--"
        self.lbl_timecode.setText(f"Playhead: {curr_str} / {tot_str} (BPM: {bpm_str})")
