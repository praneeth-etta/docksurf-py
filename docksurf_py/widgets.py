"""
widgets.py — The View Layer.

All components here are intentionally "dumb":
  - They handle user input and screen updates.
  - No subprocess calls; all Docker I/O lives in docker.py.
  - All string IDs come from constants.py.
"""

import threading
from typing import Callable

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Collapsible,
    DataTable,
    Input,
    Label,
    RichLog,
    Static,
)

from docksurf_py.constants import (
    BTN_CANCEL_ID,
    BTN_CONFIRM_ID,
    BTN_EXPAND_ID,
    LOG_PANE_HEADER_ID,
    LOG_PANE_TOOLBAR_ID,
    LOG_PANE_VIEW_ID,
    SafeMarkup,
)
from docksurf_py.docker import LogStream


class ContainerTable(DataTable):
    """A Table specifically for Containers with context-aware bindings."""

    BINDINGS = [
        Binding("s", "stop_container", "Stop"),
        Binding("S", "start_container", "Start"),
        Binding("x", "restart_container", "Restart"),
        Binding("e", "exec_container", "Exec"),
        Binding("l", "view_logs", "Logs (toggle)"),
        Binding("f", "follow_logs", "Follow"),
        Binding("z", "toggle_log_expand", "Expand Logs", show=False),
        Binding("d", "delete", "Delete"),
    ]


class DetailPane(VerticalScroll):
    """A custom container that displays a key-value table and collapsible extras."""

    _panel: Static
    _env_collapsible: "Collapsible | None" = None

    def compose(self) -> ComposeResult:
        self._panel = Static(
            Panel("Select an item to view details.", border_style="dim")
        )
        yield self._panel

    def update_details(
        self, title: str, data: dict, env_vars: list[str] | None = None
    ) -> None:
        safe_title = title if isinstance(title, SafeMarkup) else escape(title)

        table = Table(show_header=False, expand=True, box=None)
        table.add_column("Property", style="cyan", justify="right", width=15)
        table.add_column("Value")
        for key, value in data.items():
            safe_value = (
                str(value) if isinstance(value, SafeMarkup) else escape(str(value))
            )
            table.add_row(f"[b]{key}[/b]", safe_value)

        self._panel.update(
            Panel(table, title=f"[b]{safe_title}[/b]", border_style="blue")
        )

        if self._env_collapsible is not None:
            self._env_collapsible.remove()
            self._env_collapsible = None

        if env_vars:
            env_static = Static(escape("\n".join(env_vars)))
            env_static.styles.padding = (1, 2)
            self._env_collapsible = Collapsible(
                env_static, title="Environment Variables", collapsed=True
            )
            self.mount(self._env_collapsible)

    def clear_details(self) -> None:
        self._panel.update(Panel("Select an item to view details.", border_style="dim"))
        if self._env_collapsible is not None:
            self._env_collapsible.remove()
            self._env_collapsible = None


class ConfirmDialog(ModalScreen):
    """A modal confirmation dialog that dismisses with True or False."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message)
            with Horizontal():
                yield Button("Confirm", variant="error", id=BTN_CONFIRM_ID)
                yield Button("Cancel", variant="default", id=BTN_CANCEL_ID)

    @on(Button.Pressed, f"#{BTN_CONFIRM_ID}")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, f"#{BTN_CANCEL_ID}")
    def _cancel(self) -> None:
        self.dismiss(False)


class LogPane(Widget):
    """Inline log viewer that lives in the right panel, expandable to full width."""

    class ToggleExpand(Message):
        """Posted when the user clicks the expand/collapse button."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._container_id: str = ""
        self._container_name: str = ""
        self._following = False
        self._log_stream: LogStream | None = None
        self._expanded = False
        self._stream_factory: Callable[[str], LogStream] | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id=LOG_PANE_TOOLBAR_ID):
            yield Label("", id=LOG_PANE_HEADER_ID)
            yield Button("⛶ Expand", id=BTN_EXPAND_ID)
        yield RichLog(id=LOG_PANE_VIEW_ID, markup=False, highlight=False)

    @on(Button.Pressed, f"#{BTN_EXPAND_ID}")
    def _on_expand_pressed(self) -> None:
        self.post_message(self.ToggleExpand())

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        btn = self.query_one(f"#{BTN_EXPAND_ID}", Button)
        if expanded:
            self.add_class("expanded")
            btn.label = "⊡ Collapse"
        else:
            self.remove_class("expanded")
            btn.label = "⛶ Expand"

    def load(
        self,
        container_id: str,
        container_name: str,
        stream_factory: Callable[[str], LogStream],
    ) -> None:
        self.stop_follow()
        self._container_id = container_id
        self._container_name = container_name
        self._stream_factory = stream_factory
        log_view = self.query_one(f"#{LOG_PANE_VIEW_ID}", RichLog)
        log_view.clear()
        self._start_follow()
        self._update_header()

    def _update_header(self) -> None:
        state = " [bold green][FOLLOWING][/]" if self._following else "  |  L to close"
        self.query_one(f"#{LOG_PANE_HEADER_ID}", Label).update(
            f"Logs: {escape(self._container_name)}{state}"
        )

    def toggle_follow(self) -> None:
        if self._following:
            self.stop_follow()
        else:
            self._start_follow()
        self._update_header()

    def _start_follow(self) -> None:
        if not self._stream_factory:
            return

        self._log_stream = self._stream_factory(self._container_id)
        self._following = True
        threading.Thread(target=self._stream_logs, daemon=True).start()

    def _stream_logs(self) -> None:
        log_view = self.query_one(f"#{LOG_PANE_VIEW_ID}", RichLog)
        for line in self._log_stream:
            if not self._following:
                break
            self.app.call_from_thread(log_view.write, line)

    def stop_follow(self) -> None:
        self._following = False
        if self._log_stream is not None:
            self._log_stream.stop()
            self._log_stream = None

    def on_unmount(self) -> None:
        self.stop_follow()


class SearchBar(Input):
    """Search bar that closes itself on Escape."""

    BINDINGS = [("escape", "close", "Close")]

    def action_close(self) -> None:
        self.display = False
        self.value = ""
        self.app.query_one(type(self)).post_message(Input.Changed(self, ""))


class HelpScreen(ModalScreen):
    """Keybindings cheat sheet"""

    _CONTAINER_ONLY = frozenset(
        {
            "view_logs",
            "close_logs",
            "follow_logs",
            "toggle_log_expand",
            "exec_container",
            "stop_container",
            "start_container",
            "restart_container",
            "delete",
        }
    )

    def __init__(self, app_bindings: list) -> None:
        super().__init__()
        self._app_bindings = app_bindings

    def on_key(self, event) -> None:
        if event.key in ("escape", "question_mark"):
            self.dismiss()

    def compose(self) -> ComposeResult:
        table = Table(title="Keybindings", box=None, expand=True, show_edge=False)
        table.add_column("Key", style="cyan bold", width=8)
        table.add_column("Action", style="white")
        table.add_column("Applies To", style="dim", width=18)

        for item in self._app_bindings:
            if isinstance(item, tuple):
                key, action, description = item
            else:
                key, action, description = item.key, item.action, item.description

            if not description:
                continue

            scope = "Container only" if action in self._CONTAINER_ONLY else "Global"
            table.add_row(f"[bold]{key}[/bold]", description, scope)

        table.add_section()
        table.add_row("[bold]Tab[/bold]", "Switch between tab panels", "Global")
        table.add_row("[bold]↑ / ↓[/bold]", "Navigate rows in a table", "Global")

        with Vertical():
            yield Label("[b]Help[/b]", id="help-title")
            yield Static(table)
            yield Button("Close", variant="primary", id="help-close")

    @on(Button.Pressed, "#help-close")
    def _close(self) -> None:
        self.dismiss()


class StatusBar(Static):
    """Displays global resource counts and status."""

    def on_mount(self) -> None:
        self.update_stats([], [], [])

    def update_stats(
        self,
        containers: list,
        images: list,
        volumes: list,
        context: str = "",
    ) -> None:
        running = sum(1 for c in containers if c.running)
        stopped = len(containers) - running
        orphaned_volumes = sum(1 for v in volumes if not v.used_by)

        context_part = (
            f"  |  [bold cyan]Context:[/bold cyan] {context}" if context else ""
        )
        text = (
            f"[bold cyan]Containers:[/bold cyan]"
            f" {running} running / {stopped} stopped  |  "
            f"[bold cyan]Images:[/bold cyan] {len(images)} total  |  "
            f"[bold cyan]Volumes:[/bold cyan] {orphaned_volumes} orphaned"
            f"{context_part}"
        )
        self.update(text)
