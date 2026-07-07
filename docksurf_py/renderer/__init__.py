"""renderer/ — Table rendering, snapshot lifecycle, and detail-pane mixins.

Facade package: re-exports everything the single-file `renderer.py` used to
expose, so callers keep using `from docksurf_py.renderer import X` unchanged.
"""

from docksurf_py.renderer.detail_pane_renderer import DetailPaneRenderer
from docksurf_py.renderer.focus_resolver import ResourceFocusResolver
from docksurf_py.renderer.snapshot_manager import SnapshotManager
from docksurf_py.renderer.table_renderer import TableRenderer, _group_by_project

__all__ = [
    "TableRenderer",
    "SnapshotManager",
    "ResourceFocusResolver",
    "DetailPaneRenderer",
    "_group_by_project",
]
