"""
widgets.py — The View Layer.

All components here are intentionally "dumb":
  - They handle user input and screen updates.
  - No subprocess calls; all Docker I/O lives in docker.py.
  - All string IDs come from constants.py.
"""

import re
import threading
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
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
    BTN_INSPECT_CLOSE_ID,
    BTN_PROMPT_CANCEL_ID,
    BTN_PROMPT_OK_ID,
    BTN_PRUNE_CANCEL_ID,
    INSPECT_SEARCH_ID,
    INSPECT_VIEW_ID,
    LOG_PANE_HEADER_ID,
    LOG_PANE_SEARCH_ID,
    LOG_PANE_TOOLBAR_ID,
    LOG_PANE_VIEW_ID,
    PRUNE_TARGETS,
    SafeMarkup,
)


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
        Binding("d", "delete", "Delete"),
        Binding("u", "compose_up", "Compose Up"),
        Binding("k", "compose_down", "Compose Down"),
        Binding("t", "container_top", "Top"),
        Binding("space", "toggle_mark", "Mark / Collapse", show=False),
    ]


class DetailPane(VerticalScroll):
    """A custom container that displays a key-value table and collapsible extras.

    The `_stats_panel` and `_top_panel` regions show live resource usage and
    (on-demand) running processes for the selected container; both update
    independently of `update_details` (which rebuilds the main panel +
    collapsibles) so neither resets the other or the collapsibles. Both
    renderables are built by the controller, keeping this widget display-only
    (no Docker/model imports).
    """

    _panel: Static
    _stats_panel: Static
    _top_panel: Static
    _env_collapsible: "Collapsible | None" = None
    _health_collapsible: "Collapsible | None" = None

    def compose(self) -> ComposeResult:
        self._panel = Static(
            Panel("Select an item to view details.", border_style="dim")
        )
        yield self._panel
        self._stats_panel = Static("")
        yield self._stats_panel
        self._top_panel = Static("")
        yield self._top_panel

    def update_live_stats(self, content) -> None:
        self._stats_panel.update(content)

    def clear_live_stats(self) -> None:
        self._stats_panel.update("")

    def update_processes(self, content) -> None:
        self._top_panel.update(content)

    def clear_processes(self) -> None:
        self._top_panel.update("")

    def update_details(
        self,
        title: str,
        data: dict,
        env_vars: list[str] | None = None,
        health_log: str | None = None,
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
        if self._health_collapsible is not None:
            self._health_collapsible.remove()
            self._health_collapsible = None

        if env_vars:
            env_static = Static(escape("\n".join(env_vars)))
            env_static.styles.padding = (1, 2)
            self._env_collapsible = Collapsible(
                env_static, title="Environment Variables", collapsed=True
            )
            self.mount(self._env_collapsible)

        if health_log:
            health_static = Static(escape(health_log))
            health_static.styles.padding = (1, 2)
            self._health_collapsible = Collapsible(
                health_static, title="Health checks (recent)", collapsed=True
            )
            self.mount(self._health_collapsible)

    def clear_details(self) -> None:
        self._panel.update(Panel("Select an item to view details.", border_style="dim"))
        self._stats_panel.update("")
        self._top_panel.update("")
        if self._env_collapsible is not None:
            self._env_collapsible.remove()
            self._env_collapsible = None
        if self._health_collapsible is not None:
            self._health_collapsible.remove()
            self._health_collapsible = None


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


def _highlight_match(line: str, term: str) -> str:
    """Return a Rich markup string with every occurrence of term highlighted."""
    if isinstance(line, SafeMarkup):
        return line
    if not term:
        return escape(line)
    parts = re.split(f"({re.escape(term)})", line, flags=re.IGNORECASE)
    return "".join(
        f"[bold yellow]{escape(p)}[/]" if p.lower() == term.lower() else escape(p)
        for p in parts
    )


class LogSource(Protocol):
    """Structural contract for whatever a log stream factory returns.

    Matches `docker.LogStream` without importing it, so the view layer stays
    a leaf module.
    """

    def __iter__(self) -> Iterator[str]: ...
    def stop(self) -> None: ...


class LogPane(Widget):
    """Inline log viewer that lives in the right panel, expandable to full width."""

    class ToggleExpand(Message):
        """Posted when the user clicks the expand/collapse button."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._container_id: str = ""
        self._container_name: str = ""
        self._following = False
        self._paused = False
        self._generation: int = 0
        self._log_stream: LogSource | None = None
        self._expanded = False
        self._stream_factory: Callable[[str], LogSource] | None = None
        self._line_buffer: list[str] = []
        self._filter: str = ""
        self._filter_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id=LOG_PANE_TOOLBAR_ID):
            yield Label("", id=LOG_PANE_HEADER_ID)
            yield Button("⛶ Expand", id=BTN_EXPAND_ID)
        yield Input(placeholder="Filter logs... (Esc to clear)", id=LOG_PANE_SEARCH_ID)
        yield RichLog(id=LOG_PANE_VIEW_ID, markup=True, highlight=False)

    def on_mount(self) -> None:
        self.query_one(f"#{LOG_PANE_SEARCH_ID}", Input).display = False

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
        stream_factory: Callable[[str], LogSource],
    ) -> None:
        self.stop_follow()
        self._container_id = container_id
        self._container_name = container_name
        self._stream_factory = stream_factory
        self._line_buffer = []
        self._filter = ""
        search_bar = self.query_one(f"#{LOG_PANE_SEARCH_ID}", Input)
        search_bar.value = ""
        search_bar.display = False
        self.query_one(f"#{LOG_PANE_VIEW_ID}", RichLog).clear()
        self._start_follow()
        self._update_header()

    def _update_header(self) -> None:
        filter_hint = (
            f"  |  filter: [bold]{escape(self._filter)}[/]" if self._filter else ""
        )
        if self._following and self._paused:
            state = "  |  [bold yellow][PAUSED][/]"
        elif self._following:
            state = "  |  [bold green][FOLLOWING][/]"
        else:
            state = "  |  [dim]stream ended[/]  |  L to close"
        self.query_one(f"#{LOG_PANE_HEADER_ID}", Label).update(
            f"Logs: {escape(self._container_name)}{state}{filter_hint}"
        )

    def toggle_follow(self) -> None:
        if self._following:
            was_paused = self._paused
            self._paused = not self._paused
            if was_paused and not self._paused:
                self._render_to_view()
        else:
            self._start_follow()
        self._update_header()

    def clear_log(self) -> None:
        self._line_buffer = []
        self.query_one(f"#{LOG_PANE_VIEW_ID}", RichLog).clear()

    def toggle_search(self) -> None:
        search_bar = self.query_one(f"#{LOG_PANE_SEARCH_ID}", Input)
        if search_bar.display:
            self._close_search()
        else:
            search_bar.display = True
            search_bar.focus()

    def _close_search(self) -> None:
        search_bar = self.query_one(f"#{LOG_PANE_SEARCH_ID}", Input)
        search_bar.display = False
        search_bar.value = ""
        self._filter = ""
        self._render_to_view()
        self._update_header()

    @on(Input.Changed, f"#{LOG_PANE_SEARCH_ID}")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._filter = event.value
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(0.2, self._render_to_view)

    @on(Input.Submitted, f"#{LOG_PANE_SEARCH_ID}")
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        self.query_one(f"#{LOG_PANE_VIEW_ID}", RichLog).focus()

    def on_key(self, event) -> None:
        search_bar = self.query_one(f"#{LOG_PANE_SEARCH_ID}", Input)
        if event.key == "slash" and not search_bar.display:
            event.stop()
            self.toggle_search()
        elif event.key == "escape" and search_bar.display:
            event.stop()
            self._close_search()

    def _render_to_view(self) -> None:
        log_view = self.query_one(f"#{LOG_PANE_VIEW_ID}", RichLog)
        log_view.clear()
        term = self._filter
        for line in self._line_buffer:
            if not term or term.lower() in line.lower():
                log_view.write(_highlight_match(line, term))
        self._update_header()

    def _store_line(self, line: str) -> None:
        return self._line_buffer.append(line)

    def _render_line(self, line: str) -> None:
        if not self._filter or self._filter.lower() in line.lower():
            log_view = self.query_one(f"#{LOG_PANE_VIEW_ID}", RichLog)
            log_view.write(_highlight_match(line, self._filter))

    def _start_follow(self) -> None:
        if not self._stream_factory:
            return
        self._generation += 1
        generation = self._generation
        log_stream = self._stream_factory(self._container_id)
        self._log_stream = log_stream
        self._following = True
        self._paused = False
        threading.Thread(
            target=self._stream_logs, args=(generation, log_stream), daemon=True
        ).start()

    def _stream_logs(self, generation: int, log_stream: LogSource) -> None:
        for line in log_stream:
            if not self._following or self._generation != generation:
                break

            try:
                self.app.call_from_thread(self._store_line, line)
                if not self._paused:
                    self.app.call_from_thread(self._render_line, line)
            except Exception:
                break
        if self._generation == generation:
            self._following = False
            try:
                self.app.call_from_thread(self._update_header)
            except Exception:
                pass

    def stop_follow(self) -> None:
        self._following = False
        self._paused = False
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


class HelpScreen(ModalScreen):
    """Keybindings cheat sheet — built from the live BINDINGS list, not a
    hand-copied one, so it can't drift from what's actually bound.
    """

    # Genuine keyboard shortcuts that never go through the action/BINDINGS
    # system (LogPane intercepts them directly in on_key), so they can't be
    # derived — listed here explicitly instead of mislabeled as a BINDINGS
    # entry.
    _EXTRA_ROWS = (
        ("Tab", "Switch between tab panels", "Global"),
        ("↑ / ↓", "Navigate rows in a table", "Global"),
        ("/", "Filter logs (Esc to clear)", "Log pane open"),
    )

    def __init__(
        self,
        app_bindings: list,
        container_actions: frozenset[str],
        project_actions: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self._app_bindings = app_bindings
        self._container_actions = container_actions
        self._project_actions = project_actions

    def on_key(self, event) -> None:
        if event.key in ("escape", "question_mark"):
            # Stop the event — otherwise it bubbles to the app's global "?"
            # binding after dismiss() and immediately reopens the screen.
            event.stop()
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

            if action in self._project_actions:
                scope = "Compose project"
            elif action in self._container_actions:
                scope = "Container only"
            else:
                scope = "Global"
            table.add_row(f"[bold]{key}[/bold]", description, scope)

        table.add_section()
        for key, description, scope in self._EXTRA_ROWS:
            table.add_row(f"[bold]{key}[/bold]", description, scope)

        with Vertical():
            yield Label("[b]Help[/b]", id="help-title")
            yield Static(table)
            yield Button("Close", variant="primary", id="help-close")

    @on(Button.Pressed, "#help-close")
    def _close(self) -> None:
        self.dismiss()


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


class PruneScreen(ModalScreen):
    """Prune-target picker — dismisses with the chosen target key or `None`.

    One button per target (stopped containers / dangling images / unused
    volumes / unused networks / system-wide), plus Cancel. Digits 1-5 are
    shortcuts matching button order; Escape cancels. This screen only picks
    *what* to prune — the confirm dialog and the actual pruning happen
    afterward, driven by the caller.
    """

    _LABELS = {
        "containers": "1. Stopped containers",
        "images": "2. Dangling images",
        "volumes": "3. Unused volumes",
        "networks": "4. Unused networks",
        "system": "5. System-wide prune",
    }

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[b]Prune[/b]", id="prune-title")
            for target in PRUNE_TARGETS:
                yield Button(self._LABELS[target], id=f"prune-{target}")
            yield Button("Cancel", variant="default", id=BTN_PRUNE_CANCEL_ID)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
            return
        if event.key.isdigit():
            index = int(event.key) - 1
            if 0 <= index < len(PRUNE_TARGETS):
                event.stop()
                self.dismiss(PRUNE_TARGETS[index])

    @on(Button.Pressed)
    def _on_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == BTN_PRUNE_CANCEL_ID:
            self.dismiss(None)
            return
        target = button_id.removeprefix("prune-")
        if target in PRUNE_TARGETS:
            self.dismiss(target)


@dataclass(frozen=True)
class PromptField:
    """One labeled text input in a `PromptScreen`."""

    label: str
    value: str = ""
    placeholder: str = ""


class PromptScreen(ModalScreen):
    """A small multi-field text-input modal.

    One `Label` + `Input` per field, pre-filled from `PromptField.value`.
    Enter on any field but the last moves focus to the next one; Enter on the
    last field (or the OK button) dismisses with `[input.value, ...]` in
    field order. Escape or Cancel dismisses with `None`.
    """

    def __init__(self, title: str, fields: list[PromptField]) -> None:
        super().__init__()
        self._title = title
        self._fields = fields
        self._inputs: list[Input] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{escape(self._title)}[/b]", id="prompt-title")
            for i, field in enumerate(self._fields):
                yield Label(field.label)
                yield Input(
                    value=field.value,
                    placeholder=field.placeholder,
                    id=f"prompt-input-{i}",
                )
            with Horizontal():
                yield Button("OK", variant="primary", id=BTN_PROMPT_OK_ID)
                yield Button("Cancel", variant="default", id=BTN_PROMPT_CANCEL_ID)

    def on_mount(self) -> None:
        self._inputs = list(self.query(Input))
        if self._inputs:
            self._inputs[0].focus()

    def _submit(self) -> None:
        self.dismiss([i.value for i in self._inputs])

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        if not self._inputs:
            return
        if event.input is self._inputs[-1]:
            self._submit()
        else:
            idx = self._inputs.index(event.input)
            self._inputs[idx + 1].focus()

    @on(Button.Pressed, f"#{BTN_PROMPT_OK_ID}")
    def _on_ok(self) -> None:
        self._submit()

    @on(Button.Pressed, f"#{BTN_PROMPT_CANCEL_ID}")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


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
