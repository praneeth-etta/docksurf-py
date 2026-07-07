"""SystemDfScreen — modal showing a `docker system df` breakdown."""

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class SystemDfScreen(ModalScreen):
    """Modal showing a `docker system df` breakdown.

    Display-only: the caller passes a pre-built Rich renderable (the controller
    formats the `SystemDf`), keeping this widget free of Docker/model imports.
    Dismisses on Escape or `w` (the key that opened it).
    """

    def __init__(self, content) -> None:
        super().__init__()
        self._content = content

    def on_key(self, event) -> None:
        if event.key in ("escape", "w"):
            # Stop the event — otherwise it bubbles to the app's global "w"
            # binding after dismiss() and immediately reopens the screen.
            event.stop()
            self.dismiss()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[b]Disk usage[/b]", id="df-title")
            yield Static(self._content)
            yield Button("Close", variant="primary", id="df-close")

    @on(Button.Pressed, "#df-close")
    def _close(self) -> None:
        self.dismiss()
