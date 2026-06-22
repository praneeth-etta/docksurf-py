from rich.panel import Panel
from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


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


class LogsScreen(ModalScreen):
    """Scrollable log viewer modal."""

    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    DEFAULT_CSS = """
    LogsScreen {
        align: center middle;
    }
    LogsScreen > Vertical {
        width: 90%;
        height: 85%;
        border: thick $primary;
        background: $surface;
    }
    LogsScreen #logs-header {
        background: $primary;
        color: $text;
        padding: 0 1;
        height: 1;
        text-align: center;
    }
    LogsScreen ScrollableContainer {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, container_name: str, logs: str) -> None:
        super().__init__()
        self._container_name = container_name
        self._logs = logs

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"Logs: {self._container_name}  |  Esc / q to close",
                id="logs-header",
            )
            with ScrollableContainer():
                yield Static(self._logs or "(no output)", markup=False)
