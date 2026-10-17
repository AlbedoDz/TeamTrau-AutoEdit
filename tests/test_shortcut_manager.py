"""Unit tests for ShortcutManager and Focus Guard invariant."""

import os

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTextEdit,
    QWidget,
)

from src.ui.app import create_app
from src.ui.controllers.shortcut_manager import ShortcutManager

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(scope="session")
def qapp():
    return create_app()


def create_key_event(
    event_type: QEvent.Type,
    key: Qt.Key,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> QKeyEvent:
    """Helper to construct synthetic QKeyEvent instances."""
    return QKeyEvent(event_type, key, modifiers)


def test_focus_guard_bypasses_input_widgets(qapp):
    """Verify Focus Guard strictly passes through keys when an input widget has focus."""
    window = QWidget()
    manager = ShortcutManager(window)
    manager.install()

    space_emitted = []
    manager.play_pause_requested.connect(lambda: space_emitted.append(True))

    # Test each guarded widget class
    guarded_classes = [QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox]
    for widget_cls in guarded_classes:
        widget = widget_cls(window)
        widget.setFocus()
        assert window.focusWidget() == widget

        # Space key event while input widget is focused
        event = create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Space)
        handled = manager.eventFilter(window, event)

        # Must NOT handle or intercept
        assert handled is False
        assert len(space_emitted) == 0


def test_playback_and_shuttle_controls(qapp):
    """Verify Space, J, K, L shuttle gestures and rates."""
    window = QWidget()
    window.clearFocus()
    manager = ShortcutManager(window)
    manager.install()

    play_emitted = []
    shuttle_rates = []

    manager.play_pause_requested.connect(lambda: play_emitted.append(True))
    manager.shuttle_changed.connect(lambda r: shuttle_rates.append(r))

    # Space -> Play/Pause
    ev_space = create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Space)
    assert manager.eventFilter(window, ev_space) is True
    assert len(play_emitted) == 1

    # L -> Forward shuttle sequence: 1x -> 2x -> 4x -> 8x
    ev_l = create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_L)
    for expected in [1.0, 2.0, 4.0, 8.0, 1.0]:
        assert manager.eventFilter(window, ev_l) is True
        assert shuttle_rates[-1] == expected

    # K -> Stop / reset
    ev_k_press = create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_K)
    ev_k_rel = create_key_event(QEvent.Type.KeyRelease, Qt.Key.Key_K)
    assert manager.eventFilter(window, ev_k_press) is True
    assert shuttle_rates[-1] == 0.0
    manager.eventFilter(window, ev_k_rel)

    # J -> Reverse shuttle sequence: -1x -> -2x -> -4x -> -8x
    ev_j = create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_J)
    for expected in [-1.0, -2.0, -4.0, -8.0]:
        assert manager.eventFilter(window, ev_j) is True
        assert shuttle_rates[-1] == expected

    # K held + L -> Slow forward 0.5x
    manager.eventFilter(window, ev_k_press)
    assert manager.eventFilter(window, ev_l) is True
    assert shuttle_rates[-1] == 0.5

    # Release K
    ev_k_rel = create_key_event(QEvent.Type.KeyRelease, Qt.Key.Key_K)
    manager.eventFilter(window, ev_k_rel)

    # K held + J -> Slow reverse -0.5x
    manager.eventFilter(window, ev_k_press)
    assert manager.eventFilter(window, ev_j) is True
    assert shuttle_rates[-1] == -0.5


def test_navigation_and_jump_cuts(qapp):
    """Verify arrow keys, Shift modifiers, and cut jumping."""
    window = QWidget()
    manager = ShortcutManager(window)

    frames = []
    seconds = []
    cuts = []
    extremes = []

    manager.step_frame_requested.connect(lambda f: frames.append(f))
    manager.step_seconds_requested.connect(lambda s: seconds.append(s))
    manager.jump_cut_requested.connect(lambda c: cuts.append(c))
    manager.seek_extreme_requested.connect(lambda e: extremes.append(e))

    # Single frame navigation
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Left))
    assert frames == [-1]
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Right))
    assert frames == [-1, 1]

    # Shift + Arrow: 1 second step
    manager.eventFilter(
        window,
        create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier),
    )
    assert seconds == [-1.0]
    manager.eventFilter(
        window,
        create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier),
    )
    assert seconds == [-1.0, 1.0]

    # Cut jump
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Up))
    assert cuts == [-1]
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Down))
    assert cuts == [-1, 1]

    # Home / End
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Home))
    assert extremes == ["start"]
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_End))
    assert extremes == ["start", "end"]


def test_in_out_and_curation_hotkeys(qapp):
    """Verify I, O, Alt+X, P, X, and F curation hotkeys."""
    window = QWidget()
    manager = ShortcutManager(window)

    ins = []
    outs = []
    cleared = []
    curations = []

    manager.mark_in_requested.connect(lambda: ins.append(True))
    manager.mark_out_requested.connect(lambda: outs.append(True))
    manager.clear_in_out_requested.connect(lambda: cleared.append(True))
    manager.scene_action_requested.connect(lambda a: curations.append(a))

    # I & O
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_I))
    assert len(ins) == 1
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_O))
    assert len(outs) == 1

    # Alt + X -> clear range
    manager.eventFilter(
        window,
        create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_X, Qt.KeyboardModifier.AltModifier),
    )
    assert len(cleared) == 1

    # X without Alt -> Reject scene
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_X))
    assert curations == ["reject"]

    # P -> Pin scene
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_P))
    assert curations == ["reject", "pin"]

    # F -> Favorite scene
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_F))
    assert curations == ["reject", "pin", "favorite"]


def test_zoom_and_system_shortcuts(qapp):
    """Verify timeline zoom and system dialog accelerators."""
    window = QWidget()
    manager = ShortcutManager(window)

    zooms = []
    fit_zooms = []
    exports = []
    prefs = []

    manager.zoom_requested.connect(lambda z: zooms.append(z))
    manager.zoom_fit_requested.connect(lambda: fit_zooms.append(True))
    manager.open_export_requested.connect(lambda: exports.append(True))
    manager.open_preferences_requested.connect(lambda: prefs.append(True))

    # Zoom in (= / +) and zoom out (-)
    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Equal))
    assert len(zooms) == 1
    assert zooms[0] > 1.0

    manager.eventFilter(window, create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Minus))
    assert len(zooms) == 2
    assert zooms[1] < 1.0

    # Shift + Z -> Zoom Fit
    manager.eventFilter(
        window,
        create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.ShiftModifier),
    )
    assert len(fit_zooms) == 1

    # Ctrl + E -> Export dialog
    manager.eventFilter(
        window,
        create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier),
    )
    assert len(exports) == 1

    # Ctrl + , -> Preferences dialog
    manager.eventFilter(
        window,
        create_key_event(QEvent.Type.KeyPress, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier),
    )
    assert len(prefs) == 1
