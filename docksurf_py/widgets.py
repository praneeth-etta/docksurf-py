import subprocess
import threading

from rich.panel import Panel
from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, RichLog, Static


class DetailPane(Static):
    """A custom widget that displays a beautiful key-value table."""

    def update_details(self, title: str, data: dict) -> None:
        table = Table(show_header=False, expand=True, box=None)
        table.add_column("Property", style="cyan", justify="right", width=15)
        table.add_column("Value")

        for key, value in data.items():
            table.add_row(f"[b]{key}[/b]", str(value))

        self.update(Panel(table, title=f"[b]{title}[/b]", border_style="blue"))

    def clear_details(self) -> None:
        self.update(Panel("Select an item to view details.", border_style="dim"))


class ConfirmDialog(ModalScreen):
    """A modal confirmation dialog that dismisses with True or False."""

    DEFAULT_CSS = """
    ConfirmDialog {
        align: center middle;
    }
    ConfirmDialog > Vertical {
        width: 62;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 2 4;
    }
    ConfirmDialog Label {
        text-align: center;
        width: 100%;
        margin-bottom: 2;
    }
    ConfirmDialog Horizontal {
        align: center middle;
        height: auto;
    }
    ConfirmDialog Button {
        margin: 0 2;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message)
            with Horizontal():
                yield Button("Confirm", variant="error", id="confirm")
                yield Button("Cancel", variant="default", id="cancel")

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)


class LogPane(Widget):
    """Inline log viewer that lives in the right panel, expandable to full width."""

    class ToggleExpand(Message):
        """Posted when the user clicks the expand/collapse button."""

    DEFAULT_CSS = """
    LogPane {
        width: 60%;
        height: 100%;
        display: none;
    }
    LogPane.expanded {
        width: 100%;
    }
    LogPane #log-pane-toolbar {
        height: 1;
        background: $primary;
    }
    LogPane #log-pane-header {
        width: 1fr;
        color: $text;
        padding: 0 1;
        height: 1;
    }
    LogPane #expand-btn {
        width: auto;
        min-width: 5;
        height: 1;
        border: none;
        background: $primary-darken-1;
        color: $text;
    }
    LogPane #expand-btn:hover {
        background: $primary-lighten-1;
    }
    LogPane #log-pane-view {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._container_id: str = ""
        self._container_name: str = ""
        self._following = False
        self._process = None
        self._expanded = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="log-pane-toolbar"):
            yield Label("", id="log-pane-header")
            yield Button("⛶ Expand", id="expand-btn")
        yield RichLog(id="log-pane-view", markup=False, highlight=False)

    @on(Button.Pressed, "#expand-btn")
    def _on_expand_pressed(self) -> None:
        self.post_message(self.ToggleExpand())

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        btn = self.query_one("#expand-btn", Button)
        if expanded:
            self.add_class("expanded")
            btn.label = "⊡ Collapse"
        else:
            self.remove_class("expanded")
            btn.label = "⛶ Expand"

    def load(self, container_id: str, container_name: str, logs: str) -> None:
        self.stop_follow()
        self._container_id = container_id
        self._container_name = container_name
        self._update_header()
        log_view = self.query_one("#log-pane-view", RichLog)
        log_view.clear()
        lines = logs.splitlines()
        if lines:
            for line in lines:
                log_view.write(line)
        else:
            log_view.write("(no logs — press f to stream live output)")

    def _update_header(self) -> None:
        state = " [bold green][FOLLOWING][/]" if self._following else "  |  L to close"
        self.query_one("#log-pane-header", Label).update(
            f"Logs: {self._container_name}{state}"
        )

    def toggle_follow(self) -> None:
        if self._following:
            self.stop_follow()
        else:
            self._start_follow()
        self._update_header()

    def _start_follow(self) -> None:
        self._process = subprocess.Popen(
            ["docker", "logs", "-f", self._container_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._following = True
        threading.Thread(target=self._stream_logs, daemon=True).start()

    def _stream_logs(self) -> None:
        log_view = self.query_one("#log-pane-view", RichLog)
        if not self._process or not self._process.stdout:
            return
        for line in self._process.stdout:
            if not self._following:
                break
            self.call_from_thread(log_view.write, line.rstrip())

    def stop_follow(self) -> None:
        self._following = False
        if self._process:
            self._process.terminate()
            self._process.wait(timeout=2)
            self._process = None

    def on_unmount(self) -> None:
        self.stop_follow()


class SearchBar(Input):
    """Search bar that closes itself on Escape."""

    BINDINGS = [("escape", "close", "Close")]

    def action_close(self) -> None:
        self.display = False
        self.value = ""
        self.app.query_one(type(self)).post_message(Input.Changed(self, ""))
