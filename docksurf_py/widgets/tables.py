"""ContainerTable — a DataTable subclass with container-specific key bindings."""

from textual.binding import Binding
from textual.widgets import DataTable


class ContainerTable(DataTable):
    """A Table specifically for Containers with context-aware bindings."""

    BINDINGS = [
        Binding("s", "stop_container", "Stop"),
        Binding("S", "start_container", "Start"),
        Binding("x", "restart_container", "Restart"),
        Binding("p", "pause_container", "Pause/Unpause"),
        Binding("K", "kill_container", "Kill"),
        Binding("e", "exec_container", "Exec"),
        Binding("E", "exec_custom", "Exec (custom)", show=False),
        Binding("C", "copy_files", "Copy files", show=False),
        Binding("l", "view_logs", "Logs (toggle)"),
        Binding("f", "follow_logs", "Follow"),
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
        Binding("t", "container_top", "Top"),
        Binding("space", "toggle_mark", "Mark / Collapse", show=False),
    ]
