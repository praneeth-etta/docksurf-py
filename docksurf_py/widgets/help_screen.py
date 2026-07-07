"""HelpScreen — keybindings cheat sheet built from the live BINDINGS list."""

from rich.table import Table
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


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
        tab_actions: dict[str, frozenset[str]] | None = None,
    ) -> None:
        super().__init__()
        self._app_bindings = app_bindings
        self._container_actions = container_actions
        self._project_actions = project_actions
        # Extra per-tab scopes ({scope_label: {action_name, ...}}), e.g.
        # {"Images tab": {"pull_image", ...}} — lets Image/Volume/Network
        # actions get their own scope column instead of "Global".
        self._tab_actions = tab_actions or {}

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
                for label, actions in self._tab_actions.items():
                    if action in actions:
                        scope = label
                        break
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
