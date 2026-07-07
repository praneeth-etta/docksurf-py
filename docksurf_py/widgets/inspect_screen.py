"""InspectScreen — scrollable, searchable `docker inspect`-style JSON viewer."""

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog

from docksurf_py.constants import (
    BTN_INSPECT_CLOSE_ID,
    INSPECT_SEARCH_ID,
    INSPECT_VIEW_ID,
)
from docksurf_py.widgets.log_pane import _highlight_match


class InspectScreen(ModalScreen):
    """Scrollable, searchable `docker inspect`-style JSON viewer.

    Display-only: the caller passes the already-formatted JSON text (built by
    the controller from `DockerClient.inspect_resource`), keeping this widget
    free of Docker/model imports — same convention as `SystemDfScreen`. The
    filter reuses `LogPane`'s line-filter idea (only matching lines shown,
    term highlighted via the shared `_highlight_match`) since JSON dumps can
    be long. `/` opens the filter, Escape closes the filter then the screen,
    and `i` — the key that opened this screen — closes it outright as long as
    the filter isn't the focused widget (so typing "i" into the filter just
    types "i").
    """

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self._title = title
        self._lines = text.splitlines()
        self._filter = ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{escape(self._title)}[/b]", id="inspect-title")
            yield Input(placeholder="Filter... (Esc to clear)", id=INSPECT_SEARCH_ID)
            yield RichLog(id=INSPECT_VIEW_ID, markup=True, highlight=False)
            yield Button("Close", variant="primary", id=BTN_INSPECT_CLOSE_ID)

    def on_mount(self) -> None:
        self.query_one(f"#{INSPECT_SEARCH_ID}", Input).display = False
        self._render_lines()
        # A freshly-pushed ModalScreen auto-focuses its first focusable
        # descendant, which would otherwise be the (hidden) filter Input —
        # swallowing the "/" keypress as text instead of opening the filter.
        self.query_one(f"#{INSPECT_VIEW_ID}", RichLog).focus()

    def _render_lines(self) -> None:
        log_view = self.query_one(f"#{INSPECT_VIEW_ID}", RichLog)
        log_view.clear()
        term = self._filter
        for line in self._lines:
            if not term or term.lower() in line.lower():
                log_view.write(_highlight_match(line, term))

    @on(Input.Changed, f"#{INSPECT_SEARCH_ID}")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._filter = event.value
        self._render_lines()

    @on(Input.Submitted, f"#{INSPECT_SEARCH_ID}")
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        self.query_one(f"#{INSPECT_VIEW_ID}", RichLog).focus()

    def _close_search(self, search_bar: Input) -> None:
        search_bar.display = False
        search_bar.value = ""
        self._filter = ""
        self._render_lines()

    def on_key(self, event) -> None:
        search_bar = self.query_one(f"#{INSPECT_SEARCH_ID}", Input)
        if event.key == "slash" and not search_bar.display:
            event.stop()
            search_bar.display = True
            search_bar.focus()
        elif event.key == "escape":
            event.stop()
            if search_bar.display:
                self._close_search(search_bar)
            else:
                self.dismiss()
        elif event.key == "i" and not search_bar.has_focus:
            event.stop()
            self.dismiss()

    @on(Button.Pressed, f"#{BTN_INSPECT_CLOSE_ID}")
    def _close(self) -> None:
        self.dismiss()
