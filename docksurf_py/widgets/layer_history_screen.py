"""LayerHistoryScreen — modal showing an image's `docker history` layer breakdown."""

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from docksurf_py.constants import BTN_LAYER_HISTORY_CLOSE_ID


class LayerHistoryScreen(ModalScreen):
    """Modal showing an image's `docker history` layer breakdown.

    Display-only (same convention as `SystemDfScreen`): the controller passes a
    pre-built Rich renderable. Dismisses on Escape or `h` (the key that opened
    it).
    """

    def __init__(self, title: str, content) -> None:
        super().__init__()
        self._title = title
        self._content = content

    def on_key(self, event) -> None:
        if event.key in ("escape", "h"):
            # Stop the event — otherwise it bubbles to the app's global "h"
            # binding after dismiss() and immediately reopens the screen.
            event.stop()
            self.dismiss()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{escape(self._title)}[/b]", id="layer-history-title")
            with VerticalScroll():
                yield Static(self._content)
            yield Button("Close", variant="primary", id=BTN_LAYER_HISTORY_CLOSE_ID)

    @on(Button.Pressed, f"#{BTN_LAYER_HISTORY_CLOSE_ID}")
    def _close(self) -> None:
        self.dismiss()
