"""StatusBar — global resource counts and connection status."""

from rich.markup import escape
from textual.widgets import Static


class StatusBar(Static):
    """Displays global resource counts and status.

    `set_connection_state` and `update_stats` can be called independently of
    each other (a connection-state change can land between snapshots), so
    each caches its own inputs and both funnel into `_repaint`.
    """

    def on_mount(self) -> None:
        self._counts: tuple[list, list, list, str] = ([], [], [], "")
        self._conn_text = ""
        self._repaint()

    def update_stats(
        self,
        containers: list,
        images: list,
        volumes: list,
        context: str = "",
    ) -> None:
        self._counts = (containers, images, volumes, context)
        self._repaint()

    def set_connection_state(self, connected: bool, message: str = "") -> None:
        """Primitive `bool`/`str` args, not a `ConnectionState` — this module
        stays a leaf with no `connection.py` import; the caller converts."""
        self._conn_text = (
            "" if connected else f"  |  [bold red]● {escape(message)}[/bold red]"
        )
        self._repaint()

    def _repaint(self) -> None:
        containers, images, volumes, context = self._counts
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
            f"{context_part}{self._conn_text}"
        )
        self.update(text)
