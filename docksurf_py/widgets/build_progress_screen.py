"""BuildProgressScreen — live `docker compose up --build` progress log."""

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog

from docksurf_py.constants import BTN_BUILD_PROGRESS_CLOSE_ID, BUILD_PROGRESS_VIEW_ID


class BuildProgressScreen(ModalScreen):
    """Live rebuild progress — a scrolling status log, sibling to
    `PullProgressScreen`.

    Display-only: the controller streams the build on a background worker and
    calls `append(line)` per line via `call_from_thread`. On success the
    controller dismisses this modal itself (and opens the container's logs);
    on failure it's left up so the user can read the build error. Escape or
    Close dismisses; a write after dismiss just fails harmlessly and stops the
    pump.
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        # A build line can arrive (via call_from_thread) before this modal
        # finishes mounting — buffer and flush on_mount instead of letting a
        # bare query_one raise and kill the pump.
        self._pending_lines: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{escape(self._title)}[/b]", id="build-progress-title")
            yield RichLog(id=BUILD_PROGRESS_VIEW_ID, markup=True, highlight=False)
            yield Button("Close", variant="primary", id=BTN_BUILD_PROGRESS_CLOSE_ID)

    def on_mount(self) -> None:
        if not self._pending_lines:
            return
        log = self.query_one(f"#{BUILD_PROGRESS_VIEW_ID}", RichLog)
        for line in self._pending_lines:
            log.write(line)
        self._pending_lines.clear()

    def append(self, line: str) -> None:
        if not self.is_mounted:
            self._pending_lines.append(line)
            return
        self.query_one(f"#{BUILD_PROGRESS_VIEW_ID}", RichLog).write(line)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss()

    @on(Button.Pressed, f"#{BTN_BUILD_PROGRESS_CLOSE_ID}")
    def _close(self) -> None:
        self.dismiss()
