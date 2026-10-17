"""Centralized Keyboard Shortcut System with Focus Guard for NLE Editing."""

import logging
from typing import Final

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTextEdit,
    QWidget,
)

logger = logging.getLogger(__name__)


class ShortcutManager(QObject):
    """Centralized NLE keyboard event filter with strict Focus Guard protection.

    Interprets editing gestures (J-K-L shuttle, Space play/pause, I/O ranges,
    P/X/F curation, navigation, zoom, and system dialogs) while protecting native
    text and numerical input fields from shortcut hijacking.
    """

    # Playback & Shuttle signals
    play_pause_requested = Signal()
    shuttle_changed = Signal(float)  # multiplier e.g. -8.0, -4.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 4.0, 8.0

    # Navigation signals
    step_frame_requested = Signal(int)  # -1 for backward, +1 for forward
    step_seconds_requested = Signal(float)  # -1.0 or +1.0
    jump_cut_requested = Signal(int)  # -1 for prev cut, +1 for next cut
    seek_extreme_requested = Signal(str)  # "start" or "end"

    # In / Out selection signals
    mark_in_requested = Signal()
    mark_out_requested = Signal()
    clear_in_out_requested = Signal()

    # Scene curation signals
    scene_action_requested = Signal(str)  # "pin", "reject", "favorite"

    # Timeline zoom signals
    zoom_requested = Signal(float)  # scale multiplier e.g. 1.2 or 0.8
    zoom_fit_requested = Signal()

    # System dialog and project signals
    open_export_requested = Signal()
    open_preferences_requested = Signal()
    new_project_requested = Signal()
    open_project_requested = Signal()
    save_project_requested = Signal()
    save_project_as_requested = Signal()
    import_media_requested = Signal()

    # Edit and clipboard signals
    undo_requested = Signal()
    redo_requested = Signal()
    cut_requested = Signal()
    copy_requested = Signal()
    paste_requested = Signal()
    delete_requested = Signal()
    select_all_requested = Signal()
    deselect_all_requested = Signal()

    enabled: bool = True

    _FWD_RATES: Final[tuple[float, ...]] = (1.0, 2.0, 4.0, 8.0)
    _REV_RATES: Final[tuple[float, ...]] = (-1.0, -2.0, -4.0, -8.0)
    _INPUT_WIDGET_TYPES: Final[tuple[type, ...]] = (
        QLineEdit,
        QTextEdit,
        QPlainTextEdit,
        QSpinBox,
        QDoubleSpinBox,
    )

    def __init__(self, target_window: QWidget) -> None:
        super().__init__(target_window)
        self.target_window = target_window
        self._fwd_idx: int = -1
        self._rev_idx: int = -1
        self._k_held: bool = False
        self.enabled: bool = True

    def install(self) -> None:
        """Install this manager as an event filter on target window."""
        self.target_window.installEventFilter(self)

    def uninstall(self) -> None:
        """Remove this manager from target window event filter chain."""
        self.target_window.removeEventFilter(self)

    def _is_focus_guarded(self) -> bool:
        """Check if active focus belongs to an editable text/numeric input widget."""
        focus_widget = self.target_window.focusWidget()
        if focus_widget is None:
            return False
        return isinstance(focus_widget, self._INPUT_WIDGET_TYPES)

    def reset_shuttle(self) -> None:
        """Reset shuttle indexes back to neutral."""
        self._fwd_idx = -1
        self._rev_idx = -1

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Intercept key events to route professional NLE shortcuts."""
        if not self.enabled:
            return super().eventFilter(watched, event)

        event_type = event.type()
        if event_type == QEvent.Type.KeyRelease:
            if isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_K:
                self._k_held = False
            return super().eventFilter(watched, event)

        if event_type != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return super().eventFilter(watched, event)

        # Focus Guard Invariant: NEVER intercept typing inside input widgets
        if self._is_focus_guarded():
            return False

        key = event.key()
        modifiers = event.modifiers()
        is_shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        is_ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        is_alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)

        # 1. Project & File Actions (Ctrl+N, Ctrl+O, Ctrl+S, Ctrl+Shift+S, Ctrl+I, Ctrl+M, Ctrl+E, Ctrl+,)
        if is_ctrl and not is_alt:
            if is_shift:
                if key == Qt.Key.Key_S:
                    self.save_project_as_requested.emit()
                    return True
                if key == Qt.Key.Key_Z:
                    self.redo_requested.emit()
                    return True
            else:
                if key == Qt.Key.Key_N:
                    self.new_project_requested.emit()
                    return True
                if key == Qt.Key.Key_O:
                    self.open_project_requested.emit()
                    return True
                if key == Qt.Key.Key_S:
                    self.save_project_requested.emit()
                    return True
                if key == Qt.Key.Key_I:
                    self.import_media_requested.emit()
                    return True
                if key in (Qt.Key.Key_E, Qt.Key.Key_M):
                    self.open_export_requested.emit()
                    return True
                if key == Qt.Key.Key_Comma:
                    self.open_preferences_requested.emit()
                    return True
                if key == Qt.Key.Key_Z:
                    self.undo_requested.emit()
                    return True
                if key == Qt.Key.Key_Y:
                    self.redo_requested.emit()
                    return True
                if key == Qt.Key.Key_X:
                    self.cut_requested.emit()
                    return True
                if key == Qt.Key.Key_C:
                    self.copy_requested.emit()
                    return True
                if key == Qt.Key.Key_V:
                    self.paste_requested.emit()
                    return True
                if key == Qt.Key.Key_A:
                    self.select_all_requested.emit()
                    return True
                if key == Qt.Key.Key_D:
                    self.deselect_all_requested.emit()
                    return True

        # Delete / Backspace without modifiers
        if not is_ctrl and not is_shift and not is_alt:
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self.delete_requested.emit()
                return True

        # 2. Play / Pause (Space)
        if key == Qt.Key.Key_Space:
            self.reset_shuttle()
            self.play_pause_requested.emit()
            return True

        # 3. J-K-L Shuttle Controls
        if key == Qt.Key.Key_K:
            self._k_held = True
            self.reset_shuttle()
            self.shuttle_changed.emit(0.0)
            return True

        if key == Qt.Key.Key_L:
            if self._k_held:
                # K + L = Slow Forward (0.5x)
                self.shuttle_changed.emit(0.5)
            else:
                self._rev_idx = -1
                self._fwd_idx = (self._fwd_idx + 1) % len(self._FWD_RATES)
                self.shuttle_changed.emit(self._FWD_RATES[self._fwd_idx])
            return True

        if key == Qt.Key.Key_J:
            if self._k_held:
                # K + J = Slow Reverse (-0.5x)
                self.shuttle_changed.emit(-0.5)
            else:
                self._fwd_idx = -1
                self._rev_idx = (self._rev_idx + 1) % len(self._REV_RATES)
                self.shuttle_changed.emit(self._REV_RATES[self._rev_idx])
            return True

        # 4. Navigation (Arrow keys, Home, End)
        if key == Qt.Key.Key_Left:
            if is_shift:
                self.step_seconds_requested.emit(-1.0)
            else:
                self.step_frame_requested.emit(-1)
            return True

        if key == Qt.Key.Key_Right:
            if is_shift:
                self.step_seconds_requested.emit(1.0)
            else:
                self.step_frame_requested.emit(1)
            return True

        if key == Qt.Key.Key_Up:
            self.jump_cut_requested.emit(-1)
            return True

        if key == Qt.Key.Key_Down:
            self.jump_cut_requested.emit(1)
            return True

        if key == Qt.Key.Key_Home:
            self.seek_extreme_requested.emit("start")
            return True

        if key == Qt.Key.Key_End:
            self.seek_extreme_requested.emit("end")
            return True

        # 5. In / Out Points
        if key == Qt.Key.Key_I and not is_ctrl and not is_alt:
            self.mark_in_requested.emit()
            return True

        if key == Qt.Key.Key_O and not is_ctrl and not is_alt:
            self.mark_out_requested.emit()
            return True

        if key == Qt.Key.Key_X:
            if is_alt:
                # Alt + X: Clear In/Out range
                self.clear_in_out_requested.emit()
                return True
            # X without Alt: Reject selected scene
            if not is_ctrl:
                self.scene_action_requested.emit("reject")
                return True

        # 6. Scene Curation Hotkeys
        if key == Qt.Key.Key_P and not is_ctrl and not is_alt:
            self.scene_action_requested.emit("pin")
            return True

        if key == Qt.Key.Key_F and not is_ctrl and not is_alt:
            self.scene_action_requested.emit("favorite")
            return True

        # 7. Timeline Zoom
        if key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
            self.zoom_requested.emit(1.25)
            return True

        if key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.zoom_requested.emit(0.8)
            return True

        if key == Qt.Key.Key_Z and is_shift:
            self.zoom_fit_requested.emit()
            return True

        return super().eventFilter(watched, event)
