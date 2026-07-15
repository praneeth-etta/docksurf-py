"""widgets/ — The View Layer.

All components here are intentionally "dumb":
  - They handle user input and screen updates.
  - No subprocess calls; all Docker I/O lives in docker/.
  - All string IDs come from constants.py.

Facade package: re-exports everything the single-file `widgets.py` used to
expose, so callers keep using `from docksurf_py.widgets import X` unchanged.
"""

from docksurf_py.widgets.container_picker_screen import ContainerPickerScreen
from docksurf_py.widgets.detail_pane import DetailPane
from docksurf_py.widgets.dialogs import ConfirmDialog, PromptField, PromptScreen
from docksurf_py.widgets.help_screen import HelpScreen
from docksurf_py.widgets.inspect_screen import InspectScreen
from docksurf_py.widgets.layer_history_screen import LayerHistoryScreen
from docksurf_py.widgets.log_pane import (
    LogOptionsScreen,
    LogPane,
    LogSource,
    _buffer_to_text,
    _highlight_match,
    _render_log_line,
)
from docksurf_py.widgets.prune_screen import PruneScreen
from docksurf_py.widgets.pull_progress_screen import PullProgressScreen
from docksurf_py.widgets.search_bar import SearchBar
from docksurf_py.widgets.status_bar import ConnectionIndicator, StatusBar
from docksurf_py.widgets.system_df_screen import SystemDfScreen
from docksurf_py.widgets.tables import (
    ContainerTable,
    ImageTable,
    NetworkTable,
    VolumeTable,
)
from docksurf_py.widgets.whale_screen import WhaleScreen

__all__ = [
    "ContainerTable",
    "ImageTable",
    "VolumeTable",
    "NetworkTable",
    "DetailPane",
    "ConfirmDialog",
    "PromptField",
    "PromptScreen",
    "LogSource",
    "LogPane",
    "LogOptionsScreen",
    "HelpScreen",
    "SystemDfScreen",
    "WhaleScreen",
    "InspectScreen",
    "PruneScreen",
    "PullProgressScreen",
    "LayerHistoryScreen",
    "ContainerPickerScreen",
    "SearchBar",
    "StatusBar",
    "ConnectionIndicator",
    "_highlight_match",
    "_render_log_line",
    "_buffer_to_text",
]
