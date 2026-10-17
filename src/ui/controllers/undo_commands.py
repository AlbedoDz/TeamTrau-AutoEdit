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

    def redo(self) -> None:
        """Execute clip deletion."""
        if self.clip in self.timeline_view.canvas.clips:
            self.timeline_view.canvas.clips.remove(self.clip)
            if self.timeline_view.canvas.selected_clip == self.clip:
                self.timeline_view.canvas.selected_clip = None
            max_end = max((c.end_time for c in self.timeline_view.canvas.clips), default=0.0)
            self.timeline_view.canvas.total_duration = max_end
            self.timeline_view.canvas._update_geometry_size()
            self.timeline_view.canvas.update()
            self.timeline_view._update_timecode_label()

    def undo(self) -> None:
        """Restore deleted clip to timeline."""
        self.timeline_view.insert_clip(self.clip)
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
        """Insert clip onto timeline."""
        self.timeline_view.insert_clip(self.clip)
        self.timeline_view.canvas.selected_clip = self.clip

    def undo(self) -> None:
        """Remove pasted clip from timeline."""
        if self.clip in self.timeline_view.canvas.clips:
            self.timeline_view.canvas.clips.remove(self.clip)
            if self.timeline_view.canvas.selected_clip == self.clip:
                self.timeline_view.canvas.selected_clip = None
            max_end = max((c.end_time for c in self.timeline_view.canvas.clips), default=0.0)
            self.timeline_view.canvas.total_duration = max_end
            self.timeline_view.canvas._update_geometry_size()
            self.timeline_view.canvas.update()
            self.timeline_view._update_timecode_label()


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
