"""Program Monitor Viewport and Transport Controller for NLE Playback."""

import logging

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
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


class MonitorCanvas(QWidget):
    """Drawing canvas for video frame presentation and 9:16 HUD crop overlay."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 240)
        self.is_vertical: bool = True
        self.show_hud: bool = True
        self.current_pixmap: QPixmap | None = None
        self.hud_label_text: str = "9:16 Vertical"

    def set_vertical_mode(self, vertical: bool) -> None:
        """Toggle 9:16 crop box guide visualization."""
        self.is_vertical = vertical
        self.update()

    def set_frame_pixmap(self, pixmap: QPixmap | None) -> None:
        """Update active video frame preview."""
        self.current_pixmap = pixmap
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. Fill background canvas
        rect = self.rect()
        painter.fillRect(rect, QColor("#09090b"))

        # 2. Render Video Frame if available, or placeholder backdrop
        if self.current_pixmap and not self.current_pixmap.isNull():
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

        # 3. Render 9:16 Crop Guide Overlay
        if self.is_vertical:
            canvas_w = float(self.width())
            canvas_h = float(self.height())
            # Maintain 9:16 aspect ratio box centered in viewport
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
    """Program Monitor Viewport widget with professional NLE transport controls."""

    time_changed = Signal(float)  # current time in seconds
    jump_cut_requested = Signal(int)  # -1 or +1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("playerWidget")

        self.fps: float = 30.0
        self.duration: float = 180.0
        self.current_time: float = 0.0
        self.shuttle_rate: float = 1.0
        self.is_playing: bool = False

        self._timer = QTimer(self)
        self._timer.setInterval(int(1000.0 / self.fps))
        self._timer.timeout.connect(self._on_playback_tick)

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Top Overlay Toolbar
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(8, 4, 8, 2)

        self.combo_zoom = QComboBox(self)
        self.combo_zoom.addItems(["Fit", "50%", "100%", "200%"])
        self.combo_zoom.setFixedWidth(70)
        top_bar.addWidget(self.combo_zoom)

        self.combo_ratio = QComboBox(self)
        self.combo_ratio.addItem("9:16 Vertical HUD", True)
        self.combo_ratio.addItem("16:9 Landscape Full", False)
        self.combo_ratio.currentIndexChanged.connect(self._on_ratio_changed)
        top_bar.addWidget(self.combo_ratio)

        top_bar.addStretch()

        self.lbl_rate = QLabel("1.0x", self)
        self.lbl_rate.setStyleSheet(
            "background: #18181b; color: #a1a1aa; border-radius: 4px; padding: 2px 6px; font-weight: bold;"
        )
        top_bar.addWidget(self.lbl_rate)

        layout.addLayout(top_bar)

        # Center Video Canvas
        self.canvas = MonitorCanvas(self)
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
        self.btn_jump_start.setFixedSize(30, 28)
        self.btn_jump_start.setToolTip("Jump to start (Home)")
        self.btn_jump_start.clicked.connect(self.seek_to_start)
        transport.addWidget(self.btn_jump_start)

        self.btn_step_back = QPushButton("◀", self)
        self.btn_step_back.setFixedSize(30, 28)
        self.btn_step_back.setToolTip("Step 1 frame backward (Left Arrow)")
        self.btn_step_back.clicked.connect(lambda: self.step_frame(-1))
        transport.addWidget(self.btn_step_back)

        self.btn_play_pause = QPushButton("▶", self)
        self.btn_play_pause.setObjectName("primaryButton")
        self.btn_play_pause.setFixedSize(38, 28)
        self.btn_play_pause.setToolTip("Play / Pause (Space)")
        self.btn_play_pause.clicked.connect(self.toggle_play_pause)
        transport.addWidget(self.btn_play_pause)

        self.btn_step_fwd = QPushButton("▶", self)
        self.btn_step_fwd.setFixedSize(30, 28)
        self.btn_step_fwd.setToolTip("Step 1 frame forward (Right Arrow)")
        self.btn_step_fwd.clicked.connect(lambda: self.step_frame(1))
        transport.addWidget(self.btn_step_fwd)

        self.btn_jump_end = QPushButton("⏭", self)
        self.btn_jump_end.setFixedSize(30, 28)
        self.btn_jump_end.setToolTip("Jump to end (End)")
        self.btn_jump_end.clicked.connect(self.seek_to_end)
        transport.addWidget(self.btn_jump_end)

        transport.addStretch()

        # Tools: Snapshot / Fullscreen (Right)
        self.btn_snapshot = QPushButton("📷", self)
        self.btn_snapshot.setFixedSize(30, 28)
        self.btn_snapshot.setToolTip("Export keyframe snapshot")
        transport.addWidget(self.btn_snapshot)

        self.btn_fullscreen = QPushButton("⛶", self)
        self.btn_fullscreen.setFixedSize(30, 28)
        self.btn_fullscreen.setToolTip("Toggle theater mode")
        transport.addWidget(self.btn_fullscreen)

        layout.addLayout(transport)
        self._update_timecode_display()

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

    def seek_time(self, time_sec: float) -> None:
        """Seek playhead and emit time_changed signal."""
        clamped = max(0.0, min(self.duration, time_sec))
        self.current_time = clamped
        self._update_timecode_display()
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
        self.time_changed.emit(self.current_time)

    @Slot(int)
    def _on_ratio_changed(self, index: int) -> None:
        vertical = bool(self.combo_ratio.currentData())
        self.canvas.set_vertical_mode(vertical)

    def _update_timecode_display(self) -> None:
        tc_curr = format_smpte_timecode(self.current_time, self.fps)
        tc_tot = format_smpte_timecode(self.duration, self.fps)
        self.lbl_timecode.setText(f"{tc_curr} / {tc_tot}")
