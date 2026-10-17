"""Desktop Studio UI Widget Library."""

from src.ui.widgets.agent_console_dialog import AgentConsoleDialog
from src.ui.widgets.export_dialog import ExportDialog
from src.ui.widgets.export_panel import ExportPanelWidget
from src.ui.widgets.inspector_panel import InspectorPanelWidget
from src.ui.widgets.media_browser import MediaBrowserWidget
from src.ui.widgets.player_widget import MonitorCanvas, PlayerWidget
from src.ui.widgets.scene_grid import SceneGridWidget
from src.ui.widgets.settings_dialog import PreferencesDialog
from src.ui.widgets.timeline_view import TimelineCanvas, TimelineViewWidget

__all__ = [
    "AgentConsoleDialog",
    "ExportDialog",
    "ExportPanelWidget",
    "InspectorPanelWidget",
    "MediaBrowserWidget",
    "MonitorCanvas",
    "PlayerWidget",
    "PreferencesDialog",
    "SceneGridWidget",
    "TimelineCanvas",
    "TimelineViewWidget",
]
