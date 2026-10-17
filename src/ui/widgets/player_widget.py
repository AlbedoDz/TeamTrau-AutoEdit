"""Program Monitor Viewport and Transport Controller for NLE Playback."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


def format_smpte_timecode(seconds: float, fps: float = 30.0) -> str:
    """Format duration in seconds into SMPTE timecode (HH:MM:SS:FF)."""
    if seconds < 0:
        seconds = 0.0
    total_frames = int(round(seconds * fps))
    ff = total_frames % int(round(fps))
    total_seconds = int(seconds)
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def cv_frame_to_pixmap(frame: np.ndarray) -> QPixmap:
    """Convert OpenCV BGR image frame to PySide6 QPixmap."""
    h, w, ch = frame.shape
    bytes_per_line = ch * w
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(qimg)


@dataclass
class TimelineClipEntry:
    """Mapping of timeline placement to source clip range."""

    clip_path: Path
    source_in: float
    source_out: float
    timeline_in: float
    timeline_out: float


class MonitorCanvas(QWidget):
    """Drawing canvas for video frame presentation, zoom, and 9:16 HUD crop overlay."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 240)
        self.is_vertical: bool = True
        self.show_hud: bool = True
        self.current_pixmap: QPixmap | None = None
        self.hud_label_text: str = "9:16 Vertical"
        self.zoom_mode: str = "fit"

    def set_vertical_mode(self, vertical: bool) -> None:
        """Toggle 9:16 crop box guide visualization."""
        self.is_vertical = vertical
        self.update()

    def set_frame_pixmap(self, pixmap: QPixmap | None) -> None:
        """Update active video frame preview."""
        self.current_pixmap = pixmap
        self.update()

    @Slot(str)
    def set_zoom(self, zoom_text: str) -> None:
        """Set viewport zoom mode (Fit, 50%, 100%, 200%)."""
        self.zoom_mode = zoom_text.strip().lower()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. Fill background canvas
        rect = self.rect()
        painter.fillRect(rect, QColor("#09090b"))

        # 2. Render Video Frame if available, or placeholder backdrop
        if self.current_pixmap and not self.current_pixmap.isNull():
            orig_size = self.current_pixmap.size()
            if self.zoom_mode == "50%":
                scaled = self.current_pixmap.scaled(
                    int(orig_size.width() * 0.5),
                    int(orig_size.height() * 0.5),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            elif self.zoom_mode == "100%":
                scaled = self.current_pixmap
            elif self.zoom_mode == "200%":
                scaled = self.current_pixmap.scaled(
                    int(orig_size.width() * 2.0),
                    int(orig_size.height() * 2.0),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            else:  # "fit"
                scaled = self.current_pixmap.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )

            x_offset = (self.width() - scaled.width()) // 2
            y_offset = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x_offset, y_offset, scaled)
        else:
            # Draw subtle aspect grid
            pen_grid = QPen(QColor("#27272a"), 1, Qt.PenStyle.DotLine)
            painter.setPen(pen_grid)
            cx, cy = self.width() / 2, self.height() / 2
            painter.drawLine(int(cx), 0, int(cx), self.height())
            painter.drawLine(0, int(cy), self.width(), int(cy))

            # Empty state helper text
            font_title = QFont("Segoe UI", 12)
            font_title.setBold(True)
            painter.setFont(font_title)
            painter.setPen(QColor("#52525b"))
            text_rect = QRect(0, int(cy - 24), self.width(), 26)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, "📺 PROGRAM MONITOR")

            font_sub = QFont("Segoe UI", 9)
            painter.setFont(font_sub)
            painter.setPen(QColor("#71717a"))
            sub_rect = QRect(0, int(cy + 4), self.width(), 20)
            painter.drawText(
                sub_rect,
                Qt.AlignmentFlag.AlignCenter,
                "Select a clip in Media Assets, pick a scene, or run Pipeline to preview.",
            )

        # 3. Render 9:16 Crop Guide Overlay
        if self.is_vertical:
            canvas_w = float(self.width())
            canvas_h = float(self.height())
            target_ratio = 9.0 / 16.0
            box_h = canvas_h * 0.88
            box_w = box_h * target_ratio
            if box_w > canvas_w * 0.95:
                box_w = canvas_w * 0.95
                box_h = box_w / target_ratio

            bx = (canvas_w - box_w) / 2.0
            by = (canvas_h - box_h) / 2.0
            crop_rect = QRectF(bx, by, box_w, box_h)

            # Darken non-crop letterbox areas
            painter.fillRect(QRectF(0, 0, bx, canvas_h), QColor(0, 0, 0, 110))
            painter.fillRect(QRectF(bx + box_w, 0, canvas_w - (bx + box_w), canvas_h), QColor(0, 0, 0, 110))

            # Cyan bounding border with dashed pen
            pen_box = QPen(QColor("#06b6d4"), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen_box)
            painter.drawRect(crop_rect)

            # Corner brackets (HUD style)
            bracket_len = min(16.0, box_w * 0.15)
            pen_bracket = QPen(QColor("#38bdf8"), 3, Qt.PenStyle.SolidLine)
            painter.setPen(pen_bracket)

            # Top-left
            painter.drawLine(QPointF(bx, by), QPointF(bx + bracket_len, by))
            painter.drawLine(QPointF(bx, by), QPointF(bx, by + bracket_len))
            # Top-right
            painter.drawLine(QPointF(bx + box_w, by), QPointF(bx + box_w - bracket_len, by))
            painter.drawLine(QPointF(bx + box_w, by), QPointF(bx + box_w, by + bracket_len))
            # Bottom-left
            painter.drawLine(QPointF(bx, by + box_h), QPointF(bx + bracket_len, by + box_h))
            painter.drawLine(QPointF(bx, by + box_h), QPointF(bx, by + box_h - bracket_len))
            # Bottom-right
            painter.drawLine(QPointF(bx + box_w, by + box_h), QPointF(bx + box_w - bracket_len, by + box_h))
            painter.drawLine(QPointF(bx + box_w, by + box_h), QPointF(bx + box_w, by + box_h - bracket_len))

            # Top badge label
            if self.show_hud:
                badge_w, badge_h = 100.0, 20.0
                badge_x = bx + (box_w - badge_w) / 2.0
                badge_y = max(4.0, by - badge_h - 4.0)
                badge_rect = QRectF(badge_x, badge_y, badge_w, badge_h)
                painter.fillRect(badge_rect, QColor(6, 182, 212, 180))
                painter.setPen(QColor("#09090b"))
                font = QFont("Segoe UI", 9)
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, self.hud_label_text)


class PlayerWidget(QWidget):
    """Program Monitor Viewport widget with professional NLE transport and live video preview."""

    time_changed = Signal(float)  # current time in seconds
    jump_cut_requested = Signal(int)  # -1 or +1
    snapshot_saved = Signal(str)  # saved snapshot path
    theater_mode_toggled = Signal(bool)  # theater mode state

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("playerWidget")

        self.fps: float = 30.0
        self.duration: float = 180.0
        self.current_time: float = 0.0
        self.shuttle_rate: float = 1.0
        self.is_playing: bool = False

        # Live Playback Architecture
        self._mode: str = "idle"  # "idle", "source", "timeline"
        self._source_clip_path: Path | None = None
        self._source_cap: cv2.VideoCapture | None = None
        self._source_in: float = 0.0
        self._source_out: float = 0.0
        self._timeline_clips: list[TimelineClipEntry] = []
        self._caps_cache: dict[str, cv2.VideoCapture] = {}
        self._theater_mode: bool = False

        self._timer = QTimer(self)
        self._timer.setInterval(int(1000.0 / self.fps))
        self._timer.timeout.connect(self._on_playback_tick)

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Center Video Canvas
        self.canvas = MonitorCanvas(self)

        # Top Overlay Toolbar
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(8, 4, 8, 2)
        top_bar.setSpacing(6)

        self.lbl_mode = QLabel("🎬 Idle (No Media)", self)
        self.lbl_mode.setStyleSheet(
            "background: #18181b; color: #71717a; border: 1px solid #27272a; "
            "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 11px;"
        )
        self.lbl_mode.setToolTip("Active Program Monitor Preview Mode")
        top_bar.addWidget(self.lbl_mode)

        self.btn_mode_toggle = QPushButton("🔄 Toggle Mode", self)
        self.btn_mode_toggle.setObjectName("secondaryButton")
        self.btn_mode_toggle.setFixedHeight(24)
        self.btn_mode_toggle.setToolTip("Switch preview between Source Footage and Master Timeline")
        self.btn_mode_toggle.clicked.connect(self._on_toggle_preview_mode)
        top_bar.addWidget(self.btn_mode_toggle)

        top_bar.addSpacing(4)

        self.combo_zoom = QComboBox(self)
        self.combo_zoom.addItems(["Fit", "50%", "100%", "200%"])
        self.combo_zoom.setFixedWidth(70)
        self.combo_zoom.setToolTip("Monitor Viewport Zoom Level")
        self.combo_zoom.currentTextChanged.connect(self.canvas.set_zoom)
        top_bar.addWidget(self.combo_zoom)

        self.combo_ratio = QComboBox(self)
        self.combo_ratio.addItem("9:16 Vertical HUD", True)
        self.combo_ratio.addItem("16:9 Landscape Full", False)
        self.combo_ratio.setToolTip("Toggle 9:16 Mobile Guide Overlay")
        self.combo_ratio.currentIndexChanged.connect(self._on_ratio_changed)
        top_bar.addWidget(self.combo_ratio)

        top_bar.addStretch()

        self.lbl_rate = QLabel("1.0x", self)
        self.lbl_rate.setStyleSheet(
            "background: #18181b; color: #a1a1aa; border-radius: 4px; padding: 2px 6px; font-weight: bold;"
        )
        self.lbl_rate.setToolTip("Active J-K-L Shuttle Playback Multiplier")
        top_bar.addWidget(self.lbl_rate)

        layout.addLayout(top_bar)
        layout.addWidget(self.canvas, stretch=1)

        # Bottom Transport Bar
        transport = QHBoxLayout()
        transport.setContentsMargins(8, 4, 8, 8)
        transport.setSpacing(6)

        # Timecode display (Left)
        self.lbl_timecode = QLabel("00:00:00:00 / 00:03:00:00", self)
        font_tc = QFont("Consolas", 11)
        font_tc.setBold(True)
        self.lbl_timecode.setFont(font_tc)
        self.lbl_timecode.setStyleSheet("color: #38bdf8; padding: 2px 8px;")
        transport.addWidget(self.lbl_timecode)

        transport.addStretch()

        # Transport Buttons: Jump Start, Step Back, Play/Pause, Step Fwd, Jump End
        self.btn_jump_start = QPushButton("⏮", self)
        self.btn_jump_start.setFixedSize(32, 28)
        self.btn_jump_start.setToolTip("Jump to Start (Home)")
        self.btn_jump_start.clicked.connect(self.seek_to_start)
        transport.addWidget(self.btn_jump_start)

        self.btn_step_back = QPushButton("◀", self)
        self.btn_step_back.setFixedSize(32, 28)
        self.btn_step_back.setToolTip("Step 1 Frame Backward (Left Arrow / J)")
        self.btn_step_back.clicked.connect(lambda: self.step_frame(-1))
        transport.addWidget(self.btn_step_back)

        self.btn_play_pause = QPushButton("▶", self)
        self.btn_play_pause.setObjectName("primaryButton")
        self.btn_play_pause.setFixedSize(40, 28)
        self.btn_play_pause.setToolTip("Play / Pause (Space / K)")
        self.btn_play_pause.clicked.connect(self.toggle_play_pause)
        transport.addWidget(self.btn_play_pause)

        self.btn_step_fwd = QPushButton("▶|", self)
        self.btn_step_fwd.setFixedSize(32, 28)
        self.btn_step_fwd.setToolTip("Step 1 Frame Forward (Right Arrow / L)")
        self.btn_step_fwd.clicked.connect(lambda: self.step_frame(1))
        transport.addWidget(self.btn_step_fwd)

        self.btn_jump_end = QPushButton("⏭", self)
        self.btn_jump_end.setFixedSize(32, 28)
        self.btn_jump_end.setToolTip("Jump to End (End)")
        self.btn_jump_end.clicked.connect(self.seek_to_end)
        transport.addWidget(self.btn_jump_end)

        transport.addStretch()

        # Tools: Snapshot / Fullscreen (Right)
        self.btn_snapshot = QPushButton("📷 Snapshot", self)
        self.btn_snapshot.setFixedHeight(28)
        self.btn_snapshot.setObjectName("secondaryButton")
        self.btn_snapshot.setToolTip("Export Current Frame as PNG (Ctrl+Shift+S)")
        self.btn_snapshot.clicked.connect(self._on_take_snapshot)
        transport.addWidget(self.btn_snapshot)

        self.btn_fullscreen = QPushButton("⛶ Theater", self)
        self.btn_fullscreen.setFixedHeight(28)
        self.btn_fullscreen.setObjectName("secondaryButton")
        self.btn_fullscreen.setToolTip("Toggle Theater Cinema Viewport (F11)")
        self.btn_fullscreen.clicked.connect(self._on_toggle_theater_mode)
        transport.addWidget(self.btn_fullscreen)

        layout.addLayout(transport)
        self._update_timecode_display()

    # --- Live Video Engine API ---

    def _get_cap_for_file(self, path: Path) -> cv2.VideoCapture | None:
        """Retrieve or open an OpenCV VideoCapture handle with caching."""
        key = str(path.resolve())
        cap = self._caps_cache.get(key)
        if cap is not None and cap.isOpened():
            return cap
        if not path.is_file():
            return None
        try:
            new_cap = cv2.VideoCapture(str(path))
            if new_cap.isOpened():
                if len(self._caps_cache) >= 8:
                    oldest_key, oldest_cap = next(iter(self._caps_cache.items()))
                    try:
                        oldest_cap.release()
                    except Exception:
                        pass
                    del self._caps_cache[oldest_key]
                self._caps_cache[key] = new_cap
                return new_cap
        except Exception as exc:
            logger.debug("Failed opening video capture for %s: %s", path, exc)
        return None

    def load_source_video(
        self,
        clip_path: Path | str,
        start_time: float = 0.0,
        end_time: float = -1.0,
    ) -> bool:
        """Load an individual source video clip into the program monitor."""
        p = Path(clip_path)
        if not p.is_file():
            return False
        cap = self._get_cap_for_file(p)
        if cap is None or not cap.isOpened():
            return False

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        file_dur = (total_frames / fps) if fps > 0 else 10.0

        self._mode = "source"
        self._source_clip_path = p
        self._source_cap = cap
        self._source_in = max(0.0, start_time)
        self._source_out = file_dur if end_time <= 0 else min(file_dur, end_time)

        clip_dur = self._source_out - self._source_in
        if clip_dur <= 0.05:
            clip_dur = file_dur
            self._source_in = 0.0
            self._source_out = file_dur

        self.set_fps(fps)
        self.set_duration(clip_dur)
        self.current_time = 0.0
        self.lbl_mode.setText(f"📺 Source: {p.name}")
        self.lbl_mode.setStyleSheet(
            "background: #1e1b4b; color: #a5b4fc; border: 1px solid #6366f1; "
            "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 11px;"
        )
        self._update_timecode_display()
        self._render_current_frame()
        return True

    def load_timeline_sequence(
        self,
        scenes: list[Any],
        total_duration: float | None = None,
    ) -> None:
        """Load assembled sequence of scenes for master timeline playback."""
        if not scenes:
            return

        entries: list[TimelineClipEntry] = []
        cur_t = 0.0
        for s in scenes:
            if not getattr(s, "scene", None) or not s.scene.clip_path.exists():
                continue
            src_in = s.scene.start_time
            src_out = s.scene.end_time
            dur = max(0.05, src_out - src_in)
            speed = getattr(s, "speed_multiplier", 1.0)
            if speed and speed > 0:
                dur = dur / speed
            entries.append(
                TimelineClipEntry(
                    clip_path=s.scene.clip_path,
                    source_in=src_in,
                    source_out=src_out,
                    timeline_in=cur_t,
                    timeline_out=cur_t + dur,
                )
            )
            cur_t += dur

        if not entries:
            return

        self._timeline_clips = entries
        self._mode = "timeline"
        seq_dur = total_duration if total_duration and total_duration > 0 else cur_t
        self.set_duration(seq_dur)
        self.lbl_mode.setText("🎬 Master Timeline")
        self.lbl_mode.setStyleSheet(
            "background: #022c22; color: #6ee7b7; border: 1px solid #10b981; "
            "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 11px;"
        )
        self._update_timecode_display()
        self._render_current_frame()

    def switch_to_timeline_mode(self) -> bool:
        """Switch viewport preview to master timeline if clips are loaded."""
        if self._timeline_clips:
            self._mode = "timeline"
            self.lbl_mode.setText("🎬 Master Timeline")
            self.lbl_mode.setStyleSheet(
                "background: #022c22; color: #6ee7b7; border: 1px solid #10b981; "
                "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 11px;"
            )
            self._render_current_frame()
            return True
        return False

    def _render_current_frame(self) -> None:
        """Render frame corresponding to current_time in active mode."""
        if self._mode == "source":
            if self._source_cap is not None and self._source_cap.isOpened():
                target_sec = self._source_in + self.current_time
                self._source_cap.set(cv2.CAP_PROP_POS_MSEC, target_sec * 1000.0)
                ret, frame = self._source_cap.read()
                if ret and frame is not None:
                    pixmap = cv_frame_to_pixmap(frame)
                    self.canvas.set_frame_pixmap(pixmap)
                    return
        elif self._mode == "timeline":
            if self._timeline_clips:
                active_entry: TimelineClipEntry | None = None
                for entry in self._timeline_clips:
                    if entry.timeline_in <= self.current_time <= entry.timeline_out:
                        active_entry = entry
                        break
                if active_entry is not None:
                    offset = self.current_time - active_entry.timeline_in
                    src_sec = active_entry.source_in + offset
                    cap = self._get_cap_for_file(active_entry.clip_path)
                    if cap is not None and cap.isOpened():
                        cap.set(cv2.CAP_PROP_POS_MSEC, src_sec * 1000.0)
                        ret, frame = cap.read()
                        if ret and frame is not None:
                            pixmap = cv_frame_to_pixmap(frame)
                            self.canvas.set_frame_pixmap(pixmap)
                            return
        if self._mode == "idle":
            self.canvas.set_frame_pixmap(None)

    # --- Transport & Playhead Methods ---

    def set_duration(self, duration: float) -> None:
        """Update total sequence duration."""
        self.duration = max(0.1, duration)
        self._update_timecode_display()

    def set_fps(self, fps: float) -> None:
        """Update monitor framerate."""
        self.fps = max(1.0, fps)
        self._timer.setInterval(int(1000.0 / self.fps))
        self._update_timecode_display()

    def set_time(self, time_sec: float) -> None:
        """Set current playback time without re-emitting time_changed."""
        clamped = max(0.0, min(self.duration, time_sec))
        if abs(clamped - self.current_time) > 1e-4:
            self.current_time = clamped
            self._update_timecode_display()
            self._render_current_frame()

    def seek_time(self, time_sec: float) -> None:
        """Seek playhead and emit time_changed signal."""
        clamped = max(0.0, min(self.duration, time_sec))
        self.current_time = clamped
        self._update_timecode_display()
        self._render_current_frame()
        self.time_changed.emit(self.current_time)

    def seek_to_start(self) -> None:
        self.seek_time(0.0)

    def seek_to_end(self) -> None:
        self.seek_time(self.duration)

    def step_frame(self, direction: int = 1) -> None:
        """Step exactly 1 frame forward (+1) or backward (-1)."""
        step = 1.0 / self.fps
        self.seek_time(self.current_time + (step * direction))

    def step_seconds(self, delta: float) -> None:
        """Step playhead by delta seconds."""
        self.seek_time(self.current_time + delta)

    def set_shuttle_rate(self, rate: float) -> None:
        """Set shuttle multiplier (e.g. 0.0 for stop, 1x, 2x, 4x, 8x, or negative for reverse)."""
        self.shuttle_rate = rate
        if abs(rate) < 1e-4:
            self.stop()
            self.lbl_rate.setText("1.0x")
            self.lbl_rate.setStyleSheet(
                "background: #18181b; color: #a1a1aa; border-radius: 4px; padding: 2px 6px; font-weight: bold;"
            )
        else:
            self.lbl_rate.setText(f"{rate:+.1f}x" if rate < 0 else f"{rate:.1f}x")
            self.lbl_rate.setStyleSheet(
                "background: #0284c7; color: #ffffff; border-radius: 4px; padding: 2px 6px; font-weight: bold;"
            )
            if not self.is_playing:
                self.play()

    def play(self) -> None:
        """Start playback simulation."""
        self.is_playing = True
        self.btn_play_pause.setText("⏸")
        self._timer.start()

    def stop(self) -> None:
        """Pause playback simulation."""
        self.is_playing = False
        self.btn_play_pause.setText("▶")
        self._timer.stop()

    def toggle_play_pause(self) -> None:
        """Toggle between play and pause states."""
        if self.is_playing:
            self.stop()
        else:
            self.play()

    def set_vertical_mode(self, vertical: bool) -> None:
        """Update 9:16 guide visibility."""
        idx = 0 if vertical else 1
        if self.combo_ratio.currentIndex() != idx:
            self.combo_ratio.setCurrentIndex(idx)
        self.canvas.set_vertical_mode(vertical)

    @Slot()
    def _on_playback_tick(self) -> None:
        """Advance playhead during playback according to active shuttle multiplier."""
        delta = (1.0 / self.fps) * self.shuttle_rate
        new_time = self.current_time + delta
        if new_time >= self.duration:
            new_time = self.duration
            self.stop()
        elif new_time <= 0.0:
            new_time = 0.0
            self.stop()
        self.current_time = new_time
        self._update_timecode_display()
        self._render_current_frame()
        self.time_changed.emit(self.current_time)

    @Slot(int)
    def _on_ratio_changed(self, index: int) -> None:
        vertical = bool(self.combo_ratio.currentData())
        self.canvas.set_vertical_mode(vertical)

    @Slot()
    def _on_toggle_preview_mode(self) -> None:
        """Toggle between Source preview and Timeline preview."""
        if self._mode == "source" and self._timeline_clips:
            self.switch_to_timeline_mode()
        elif self._mode == "timeline" and self._source_clip_path:
            self.load_source_video(self._source_clip_path, self._source_in, self._source_out)

    @Slot()
    def _on_take_snapshot(self) -> None:
        """Export active video frame snapshot to PNG/JPEG."""
        pixmap = self.canvas.current_pixmap
        if not pixmap or pixmap.isNull():
            QMessageBox.information(
                self,
                "Snapshot",
                "No video frame loaded in viewport to export.",
            )
            return

        default_name = f"snapshot_{int(self.current_time * 1000):06d}.png"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Keyframe Snapshot",
            default_name,
            "PNG Image (*.png);;JPEG Image (*.jpg)",
        )
        if path:
            pixmap.save(path)
            self.snapshot_saved.emit(path)

    @Slot()
    def _on_toggle_theater_mode(self) -> None:
        """Toggle theater view mode for full viewport presentation."""
        self._theater_mode = not self._theater_mode
        self.btn_fullscreen.setText("⛶ Normal" if self._theater_mode else "⛶ Theater")
        self.theater_mode_toggled.emit(self._theater_mode)

    def _update_timecode_display(self) -> None:
        tc_curr = format_smpte_timecode(self.current_time, self.fps)
        tc_tot = format_smpte_timecode(self.duration, self.fps)
        self.lbl_timecode.setText(f"{tc_curr} / {tc_tot}")

    def close(self) -> bool:
        """Clean up active video capture handles upon widget destruction."""
        self.stop()
        for cap in self._caps_cache.values():
            try:
                cap.release()
            except Exception:
                pass
        self._caps_cache.clear()
        self._source_cap = None
        return super().close()
