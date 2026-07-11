"""PullProgressScreen — live `docker pull` progress log."""

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog

from docksurf_py.constants import BTN_PULL_PROGRESS_CLOSE_ID, PULL_PROGRESS_VIEW_ID


class PullProgressScreen(ModalScreen):
    """Live `docker pull` progress — a scrolling status log.

    Display-only: the controller streams the pull on a background worker and
    calls `append(line)` per formatted progress line via `call_from_thread`.
    Escape or Close dismisses; the background pull is guarded by the controller
    (a write after dismiss just fails harmlessly and stops the pump).
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        # A pull chunk can arrive (via call_from_thread) before this modal
        # finishes mounting, buffer and flush on_mount instead of
        # letting a bare query_one raise and kill the pump.
        self._pending_lines: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{escape(self._title)}[/b]", id="pull-progress-title")
            yield RichLog(id=PULL_PROGRESS_VIEW_ID, markup=True, highlight=False)
            yield Button("Close", variant="primary", id=BTN_PULL_PROGRESS_CLOSE_ID)

    def on_mount(self) -> None:
        if not self._pending_lines:
            return
        log = self.query_one(f"#{PULL_PROGRESS_VIEW_ID}", RichLog)
        for line in self._pending_lines:
            log.write(line)
        self._pending_lines.clear()

    def append(self, line: str) -> None:
        if not self.is_mounted:
            self._pending_lines.append(line)
            return
        self.query_one(f"#{PULL_PROGRESS_VIEW_ID}", RichLog).write(line)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss()

    @on(Button.Pressed, f"#{BTN_PULL_PROGRESS_CLOSE_ID}")
    def _close(self) -> None:
        self.dismiss()
