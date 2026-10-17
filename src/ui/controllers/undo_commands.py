"""Undo and Redo Commands for NLE Timeline and Scene Curation operations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtGui import QUndoCommand

if TYPE_CHECKING:
    from src.ui.widgets.scene_grid import SceneGridWidget
    from src.ui.widgets.timeline_view import TimelineClipItem, TimelineViewWidget

logger = logging.getLogger(__name__)


class ClipDeleteCommand(QUndoCommand):
    """Undoable command for deleting a clip from the multi-track timeline."""

    def __init__(
        self,
        timeline_view: TimelineViewWidget,
        clip: TimelineClipItem,
        parent: QUndoCommand | None = None,
    ) -> None:
        super().__init__(f"Delete Clip: {clip.name}", parent)
        self.timeline_view = timeline_view
        self.clip = clip
        self._index: int | None = None

    def redo(self) -> None:
        """Execute clip deletion (ripple), updating the underlying edit timeline."""
        index = self.timeline_view.remove_clip(self.clip)
        if index is not None:
            self._index = index

    def undo(self) -> None:
        """Restore deleted clip to its original position on the timeline."""
        self.timeline_view.insert_clip(self.clip, self._index)
        self.timeline_view.canvas.selected_clip = self.clip


class ClipPasteCommand(QUndoCommand):
    """Undoable command for pasting or inserting a clip onto the timeline."""

    def __init__(
        self,
        timeline_view: TimelineViewWidget,
        clip: TimelineClipItem,
        parent: QUndoCommand | None = None,
    ) -> None:
        super().__init__(f"Paste Clip: {clip.name}", parent)
        self.timeline_view = timeline_view
        self.clip = clip

    def redo(self) -> None:
        """Insert clip onto timeline (ripple), updating the underlying edit timeline."""
        self.timeline_view.insert_clip(self.clip)
        self.timeline_view.canvas.selected_clip = self.clip

    def undo(self) -> None:
        """Remove pasted clip from timeline."""
        self.timeline_view.remove_clip(self.clip)


class SceneToggleCommand(QUndoCommand):
    """Undoable command for toggling inclusion state of a scored scene."""

    def __init__(
        self,
        scene_grid: SceneGridWidget,
        scene_idx: int,
        target_included: bool,
        parent: QUndoCommand | None = None,
    ) -> None:
        action_name = "Include" if target_included else "Exclude"
        super().__init__(f"{action_name} Scene #{scene_idx + 1}", parent)
        self.scene_grid = scene_grid
        self.scene_idx = scene_idx
        self.target_included = target_included
        self.previous_included = scene_grid.is_scene_included(scene_idx)

    def redo(self) -> None:
        """Apply target inclusion state."""
        self.scene_grid.set_scene_included(self.scene_idx, self.target_included)

    def undo(self) -> None:
        """Revert to previous inclusion state."""
        self.scene_grid.set_scene_included(self.scene_idx, self.previous_included)
