"""DataTable subclasses carrying per-tab key bindings.

Textual's `Footer` shows a widget's own `BINDINGS` only while that widget
has focus, so declaring a tab's actions here (rather than only on
`DockSurfApp.BINDINGS`) is what makes the footer show `Stop`/`History`/`Size`
etc. only on the tab they apply to. The matching App-level entries stay
`show=False` and exist purely so the `?` help screen and command palette
(both of which only read `App.BINDINGS`) still surface every action.
"""

from textual.binding import Binding
from textual.widgets import DataTable


class ContainerTable(DataTable):
    """A Table specifically for Containers with context-aware bindings."""

    def __init__(self, *args, **kwargs) -> None:
        # DataTable's default ("css") makes the cursor row's CSS foreground
        # color override every cell's own color, including the status dot
        # prefixed to the Name cell, which would otherwise go flat/unreadable
        # on exactly the row a user is currently looking at.
        kwargs.setdefault("cursor_foreground_priority", "renderable")
        super().__init__(*args, **kwargs)

    BINDINGS = [
        Binding("s", "stop_container", "Stop"),
        Binding("S", "start_container", "Start"),
        Binding("x", "restart_container", "Restart"),
        Binding("p", "pause_container", "Pause/Unpause"),
        Binding("K", "kill_container", "Kill"),
        Binding("e", "exec_container", "Exec"),
        Binding("E", "exec_custom", "Exec (custom)", show=False),
        Binding("C", "copy_files", "Copy files", show=False),
        Binding("l", "view_logs", "Logs"),
        Binding("f", "follow_logs", "Follow"),
        Binding("c", "clear_logs", "Clear", show=False),
        Binding("z", "toggle_log_expand", "Expand Logs", show=False),
        # Log-viewer controls, active while the pane is open (the handlers
        # no-op when it isn't). Mirrored here — like l/f/z above — so they fire
        # while the container table keeps focus; bare action names resolve up to
        # the app's handlers.
        Binding("o", "log_options", "Log options", show=False),
        Binding("T", "toggle_timestamps", "Timestamps", show=False),
        Binding("W", "toggle_log_wrap", "Wrap", show=False),
        Binding("n", "next_match", "Next match", show=False),
        Binding("N", "prev_match", "Prev match", show=False),
        Binding("g", "log_top", "Log top", show=False),
        Binding("G", "log_bottom", "Log bottom", show=False),
        Binding("X", "export_logs", "Export logs", show=False),
        Binding("d", "delete", "Delete"),
        Binding("ctrl+u", "compose_up", "Compose Up"),
        Binding("ctrl+k", "compose_down", "Compose Down"),
        Binding("B", "rebuild_service", "Rebuild", show=False),
        Binding("t", "container_top", "Top"),
        Binding("space", "toggle_mark", "Mark / Collapse", show=False),
    ]


class ImageTable(DataTable):
    """A Table specifically for Images with context-aware bindings."""

    BINDINGS = [
        Binding("h", "image_history", "History"),
        Binding("y", "tag_image", "Tag"),
        Binding("a", "mark_all_dangling", "Mark dangling"),
    ]


class VolumeTable(DataTable):
    """A Table specifically for Volumes with context-aware bindings."""

    BINDINGS = [
        Binding("b", "volume_size", "Size"),
    ]


class NetworkTable(DataTable):
    """A Table specifically for Networks with context-aware bindings."""

    BINDINGS = [
        Binding("v", "network_connect", "Connect"),
        Binding("m", "network_disconnect", "Disconnect"),
    ]
