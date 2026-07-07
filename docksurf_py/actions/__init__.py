"""actions/ — Container management, Compose, and resource-deletion mixins.

Facade package: re-exports everything the single-file `actions.py` used to
expose, so callers keep using `from docksurf_py.actions import X` unchanged.
"""

from docksurf_py.actions.clipboard import ClipboardHandler
from docksurf_py.actions.compose import ComposeActionHandler
from docksurf_py.actions.container import (
    EXEC_SHELL_CANDIDATES,
    ContainerActionHandler,
    _open_in_browser,
    build_cp_paths,
    build_exec_argv,
    select_exec_shell,
)
from docksurf_py.actions.context import ContextActionHandler
from docksurf_py.actions.deletion import DeletePlan, ResourceDeletionHandler
from docksurf_py.actions.images import ImageActionHandler
from docksurf_py.actions.inspect import InspectHandler
from docksurf_py.actions.networks import NetworkActionHandler
from docksurf_py.actions.prune import PruneHandler
from docksurf_py.actions.selection import SelectionHandler
from docksurf_py.actions.volumes import VolumeActionHandler

__all__ = [
    "ClipboardHandler",
    "ComposeActionHandler",
    "ContainerActionHandler",
    "ContextActionHandler",
    "DeletePlan",
    "ImageActionHandler",
    "InspectHandler",
    "NetworkActionHandler",
    "PruneHandler",
    "ResourceDeletionHandler",
    "SelectionHandler",
    "VolumeActionHandler",
    "EXEC_SHELL_CANDIDATES",
    "build_cp_paths",
    "build_exec_argv",
    "select_exec_shell",
    "_open_in_browser",
]
