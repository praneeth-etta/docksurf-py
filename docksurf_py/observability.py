"""
observability.py — Live observability mixin.

LiveStatsController streams real-time resource usage for the currently-focused
running container into the detail pane, and (system df) is wired here too. It's
a mixin composed into DockSurfApp via MRO, mirroring search.py's structure.
"""

import logging
import threading
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from textual import work
from textual.widgets import TabbedContent

from docksurf_py.constants import DETAIL_PANE_ID, SafeMarkup, TabID
from docksurf_py.docker import StatsStream, format_size
from docksurf_py.models import Container, ContainerStats, ContainerTop, SystemDf
from docksurf_py.widgets import DetailPane, SystemDfScreen

if TYPE_CHECKING:
    from docksurf_py.app import AppContext

    _Base = AppContext
else:
    # Real runtime base is `object` — `AppContext` only exists for mypy to
    # check these mixins' bodies against; see app.py's `AppContext` docstring.
    _Base = object

logger = logging.getLogger(__name__)


def _bar(percent: float, width: int = 12) -> str:
    """A fixed-width colour-coded usage bar as Rich markup."""
    filled = max(0, min(width, round(percent / 100 * width)))
    return f"[green]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"


def _render_stats(stats: ContainerStats, name: str) -> Panel:
    """Build the live-stats renderable for the detail pane."""
    table = Table(show_header=False, expand=True, box=None)
    table.add_column("k", style="cyan", justify="right", width=4)
    table.add_column("v")
    table.add_row("CPU", f"{_bar(stats.cpu_percent)} {stats.cpu_percent:.1f}%")
    mem = (
        f"{format_size(stats.mem_used)} / {format_size(stats.mem_limit)} "
        f"({stats.mem_percent:.1f}%)"
    )
    table.add_row("MEM", f"{_bar(stats.mem_percent)} {mem}")
    table.add_row(
        "NET", f"↓ {format_size(stats.net_rx)}   ↑ {format_size(stats.net_tx)}"
    )
    table.add_row(
        "BLK", f"↓ {format_size(stats.blk_read)}   ↑ {format_size(stats.blk_write)}"
    )
    return Panel(
        table, title=f"[b]Live stats: {escape(name)}[/b]", border_style="green"
    )


def _render_top(top: ContainerTop, name: str) -> Panel:
    """Build the running-processes renderable for the detail pane.

    Every column is single-line and ellipsized (`no_wrap=True`) — wrapping
    the last column (`fold`) was tried first, but in the pane's realistic
    (narrow) width a long `CMD` value wraps into a tall column of a couple of
    characters per line, blowing out the panel's height. Non-last columns are
    additionally capped to a small `max_width` so Rich's column-width solver
    gives the remaining space to `CMD` (typically last, and the most useful
    field) instead of splitting it evenly.
    """
    table = Table(box=None, expand=True)
    for i, title in enumerate(top.titles):
        is_last = i == len(top.titles) - 1
        table.add_column(
            title,
            overflow="ellipsis",
            no_wrap=True,
            max_width=None if is_last else max(len(title), 5),
        )
    for process in top.processes:
        table.add_row(*process)
    return Panel(
        table, title=f"[b]Processes: {escape(name)}[/b]", border_style="magenta"
    )


def _render_df(df: SystemDf) -> Table:
    """Build the `docker system df` breakdown table for the modal screen."""
    table = Table(box=None, expand=True)
    table.add_column("Type", style="cyan")
    table.add_column("Total", justify="right")
    table.add_column("Active", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Reclaimable", justify="right")
    for e in df.entries:
        pct = (
            f" ({e.reclaimable_bytes / e.size_bytes * 100:.0f}%)"
            if e.size_bytes
            else ""
        )
        table.add_row(
            e.kind,
            str(e.total_count),
            str(e.active_count),
            format_size(e.size_bytes),
            f"{format_size(e.reclaimable_bytes)}{pct}",
        )
    table.add_section()
    total_pct = (
        f" ({df.total_reclaimable / df.total_size * 100:.0f}%)" if df.total_size else ""
    )
    table.add_row(
        "[b]Total[/b]",
        "",
        "",
        f"[b]{format_size(df.total_size)}[/b]",
        f"[b]{format_size(df.total_reclaimable)}{total_pct}[/b]",
    )
    return table


class LiveStatsController(_Base):
    """Streams stats for the focused running container; owns the `w` df screen.

    Follows the `LogPane` streaming pattern — a `_stats_generation` counter plus
    a daemon thread marshalling samples back via `call_from_thread` — but keyed
    to the current selection rather than a toggle. `_sync_stats()` is idempotent:
    it only (re)starts the stream when the focused target actually changes, so a
    periodic/event refresh that re-selects the same container doesn't restart it.
    """

    _stats_target: str | None = None
    _stats_generation: int = 0
    _stats_stream: StatsStream | None = None
    _top_target: str | None = None

    def _sync_stats(self) -> None:
        item = None
        if self.query_one(TabbedContent).active == TabID.CONTAINERS:
            item = self._get_focused_resource(TabID.CONTAINERS)
        target = item.id if isinstance(item, Container) and item.running else None

        if target == self._stats_target:
            return
        self._stats_target = target

        self._stop_stats_stream()
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        if target is None:
            pane.clear_live_stats()
            return
        pane.update_live_stats(SafeMarkup("[dim]collecting stats…[/]"))
        assert isinstance(item, Container)
        self._start_stats_stream(target, item.name)

    def _start_stats_stream(self, container_id: str, name: str) -> None:
        self._stats_generation += 1
        generation = self._stats_generation
        stream = self.docker.stream_stats(container_id)
        self._stats_stream = stream
        threading.Thread(
            target=self._pump_stats, args=(generation, stream, name), daemon=True
        ).start()

    def _pump_stats(self, generation: int, stream: StatsStream, name: str) -> None:
        for stats in stream:
            if self._stats_generation != generation:
                break
            try:
                self.call_from_thread(
                    self._render_stats_sample, generation, stats, name
                )
            except Exception:
                break

    def _render_stats_sample(
        self, generation: int, stats: ContainerStats, name: str
    ) -> None:
        # A late sample from a superseded stream must not overwrite the pane.
        if self._stats_generation != generation:
            return
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        pane.update_live_stats(_render_stats(stats, name))

    def _stop_stats_stream(self) -> None:
        self._stats_generation += 1  # invalidate any in-flight pump thread
        if self._stats_stream is not None:
            self._stats_stream.stop()
            self._stats_stream = None

    def stop_stats(self) -> None:
        """Stop streaming entirely (app teardown)."""
        self._stats_target = None
        self._stop_stats_stream()
        self._top_target = None

    @work(thread=True)
    def action_system_df(self) -> None:
        self.call_from_thread(self.notify, "Computing disk usage…")
        df: SystemDf = self.docker.system_df()
        self.call_from_thread(self.push_screen, SystemDfScreen(_render_df(df)))

    # --- `t`: on-demand running-process snapshot ---

    def _sync_top(self) -> None:
        """Invalidate a stale process list when the selection moves on.

        Unlike stats, `top` is on-demand (`t` toggles/fetches it), so this
        never re-fetches automatically — it only clears the panel when the
        focused container id no longer matches whatever it was fetched for.
        """
        item = None
        if self.query_one(TabbedContent).active == TabID.CONTAINERS:
            item = self._get_focused_resource(TabID.CONTAINERS)
        focused_id = item.id if isinstance(item, Container) else None

        if self._top_target is not None and focused_id != self._top_target:
            self._top_target = None
            self.query_one(f"#{DETAIL_PANE_ID}", DetailPane).clear_processes()

    def action_container_top(self) -> None:
        if self.query_one(TabbedContent).active != TabID.CONTAINERS:
            self.notify(
                "Switch to the Containers tab and select a container",
                severity="warning",
            )
            return
        c = self._get_focused_container()
        if c is None:
            self.notify(
                "Switch to the Containers tab and select a container",
                severity="warning",
            )
            return
        if not c.running:
            self.notify(f"{escape(c.name)} is not running", severity="warning")
            return

        if self._top_target == c.id:
            # Already showing this container's processes — toggle off.
            self._top_target = None
            self.query_one(f"#{DETAIL_PANE_ID}", DetailPane).clear_processes()
            return

        self._top_target = c.id
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        pane.update_processes(SafeMarkup("[dim]fetching processes…[/]"))
        self._fetch_top(c.id, c.name)

    @work(thread=True)
    def _fetch_top(self, container_id: str, name: str) -> None:
        top = self.docker.container_top(container_id)
        self.call_from_thread(self._render_top_result, container_id, name, top)

    def _render_top_result(
        self, container_id: str, name: str, top: ContainerTop | None
    ) -> None:
        # The selection may have moved (or `t` toggled off) since the fetch
        # started — a stale result must not overwrite what's shown now.
        if self._top_target != container_id:
            return
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        if top is None:
            self._top_target = None
            pane.clear_processes()
            self.notify(
                f"Could not fetch processes for {escape(name)}", severity="error"
            )
            return
        pane.update_processes(_render_top(top, name))
