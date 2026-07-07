"""DetailPane — key-value table plus collapsible extras for the side panel."""

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

from docksurf_py.constants import SafeMarkup


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
