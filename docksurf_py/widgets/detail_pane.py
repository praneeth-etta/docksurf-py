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

    The `_stats_panel`, `_top_panel`, and `_topology_panel` regions show live
    resource usage, (on-demand) running processes, and the network-topology
    diagram for the selected item; each updates independently of
    `update_details` (which rebuilds the main panel + collapsibles) so none
    resets the others or the collapsibles. All three renderables are built by
    the controller, keeping this widget display-only (no Docker/model
    imports).
    """

    _panel: Static
    _stats_panel: Static
    _top_panel: Static
    _topology_panel: Static
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
        self._topology_panel = Static("")
        yield self._topology_panel

    def update_topology(self, content) -> None:
        self._topology_panel.update(content)

    def clear_topology(self) -> None:
        self._topology_panel.update("")

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
        sections: dict[str, dict],
        env_text: str | None = None,
        env_masked: bool = True,
        health_log: str | None = None,
        border_style: str = "blue",
    ) -> None:
        """Update the details panel and its optional collapsible sections.

        The main panel displays fields grouped into named sections. `sections`
        maps each section heading to a dictionary of field names and values.
        A falsy heading (``""``) suppresses the section header, which is useful
        for rendering an ungrouped set of fields.
        """
        safe_title = title if isinstance(title, SafeMarkup) else escape(title)

        table = Table(show_header=False, expand=True, box=None, pad_edge=False)
        table.add_column("Property", style="cyan", justify="left")
        table.add_column("Value", ratio=1)
        for i, (heading, fields) in enumerate(sections.items()):
            if heading:
                if i:
                    table.add_row("", "")
                table.add_row(f"[b dim]{escape(heading.upper())}[/b dim]", "")
            for key, value in fields.items():
                safe_value = (
                    str(value) if isinstance(value, SafeMarkup) else escape(str(value))
                )
                table.add_row(f"[b]{key}[/b]", safe_value)

        self._panel.update(
            Panel(table, title=f"[b]{safe_title}[/b]", border_style=border_style)
        )

        if self._env_collapsible is not None:
            self._env_collapsible.remove()
            self._env_collapsible = None
        if self._health_collapsible is not None:
            self._health_collapsible.remove()
            self._health_collapsible = None

        if env_text:
            env_static = Static(escape(env_text), id="env-content")
            env_static.styles.padding = (1, 2)
            suffix = "masked — R to reveal" if env_masked else "revealed — R to mask"
            self._env_collapsible = Collapsible(
                env_static,
                title=f"Environment Variables ({suffix})",
                collapsed=True,
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
        self._topology_panel.update("")
        if self._env_collapsible is not None:
            self._env_collapsible.remove()
            self._env_collapsible = None
        if self._health_collapsible is not None:
            self._health_collapsible.remove()
            self._health_collapsible = None
