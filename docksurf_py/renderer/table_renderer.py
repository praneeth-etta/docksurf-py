"""TableRenderer — column setup and row-population for the four resource tables."""

import os
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable

from rich.markup import escape
from textual.widgets import DataTable, Input, Static, TabbedContent

from docksurf_py.constants import (
    EMPTY_STATE_HINTS,
    EMPTY_STATE_IDS,
    MARK_GLYPH,
    SEARCH_BAR_ID,
    STATUS_GREEN,
    STATUS_RED,
    STATUS_YELLOW,
    SafeMarkup,
    TabID,
    markup_green,
    markup_red,
    markup_yellow,
)
from docksurf_py.docker import format_relative_time, format_size
from docksurf_py.models import (
    ComposeProject,
    Container,
    ContainerDetail,
    HealthProbe,
    Image,
    Network,
    Volume,
)
from docksurf_py.renderer.common import _Base
from docksurf_py.session import save_session

if TYPE_CHECKING:
    from docksurf_py.app import ResourceEntry

_NEEDS_SNAPSHOT_MSG = "populate with no items needs a snapshot"


def _status_color(c: Container) -> str:
    """The color a container's state maps to — shared by `_status_markup`
    (Status column/field text) and `DetailPaneRenderer` (detail panel border),
    so the two never drift apart."""
    if c.running:
        return STATUS_GREEN
    if c.state in ("exited", "dead"):
        return STATUS_RED
    return STATUS_YELLOW  # paused, restarting, created


def _status_markup(c: Container) -> SafeMarkup:
    if c.running:
        label = escape(c.status)
        if c.health == "healthy":
            label += " ✓"
        elif c.health == "unhealthy":
            label += " ✗"
        elif c.health == "starting":
            label += " …"
    elif c.state in ("exited", "dead"):
        suffix = f" ({c.exit_code})" if c.exit_code != 0 else ""
        label = escape(c.status) + suffix
    else:
        label = escape(c.status)  # paused, restarting, created
    return SafeMarkup(f"[{_status_color(c)}]{label}[/]")


def _safe_row(table: DataTable, *values) -> None:
    """Add a row, escaping plain strings and passing SafeMarkup through unchanged."""
    table.add_row(*(v if isinstance(v, SafeMarkup) else escape(str(v)) for v in values))


def _mark_cell(
    marked: set[tuple[str, str]], key: tuple[str, str] | None
) -> SafeMarkup | str:
    """Leading-column cell for a row: the mark glyph if selected, else blank."""
    return MARK_GLYPH if key is not None and key in marked else ""


def _health_markup(c: Container) -> SafeMarkup:
    """Colored health status for the Health column."""
    if c.health == "healthy":
        return markup_green("healthy")
    if c.health == "unhealthy":
        return markup_red("unhealthy")
    if c.health == "starting":
        return markup_yellow("starting")
    return SafeMarkup("[dim]—[/]")


def _format_health_log(log: list[HealthProbe], limit: int = 5) -> str | None:
    """Render the most recent healthcheck probes as plain text, or None if empty.

    Plain text (not markup): the detail pane escapes it like the env-vars block.
    """
    if not log:
        return None
    blocks = []
    for probe in log[-limit:]:
        marker = "✓ pass" if probe.exit_code == 0 else f"✗ exit {probe.exit_code}"
        when = format_relative_time(probe.start) if probe.start else "—"
        output = probe.output.strip() or "(no output)"
        blocks.append(f"[{marker}]  {when}\n{output}")
    return "\n\n".join(blocks)


def _group_by_project(
    containers: list[Container],
    key: Callable[[Container], Any] | None = None,
    reverse: bool = False,
) -> tuple[list[ComposeProject], list[Container]]:
    """Split containers into Compose projects (sorted) and standalone ones.

    Project ordering is always alphabetical (a header isn't a sortable data
    row). Services within a project are sorted by `key`/`reverse` when a
    column sort is active, else by service name (the original default). A
    project's config-file/working-dir come from its first service (they're
    identical across a project's containers).
    """
    grouped: dict[str, list[Container]] = defaultdict(list)
    standalone: list[Container] = []
    for c in containers:
        if c.is_compose:
            grouped[c.compose_project].append(c)
        else:
            standalone.append(c)

    projects: list[ComposeProject] = []
    for name in sorted(grouped):
        if key is not None:
            members = sorted(grouped[name], key=key, reverse=reverse)
        else:
            members = sorted(grouped[name], key=lambda c: c.compose_service)
        first = members[0]
        projects.append(
            ComposeProject(
                name=name,
                containers=members,
                config_files=first.compose_config_files,
                working_dir=first.compose_working_dir,
            )
        )
    return projects, standalone


def _project_status_color(project: ComposeProject) -> str:
    return STATUS_GREEN if project.all_running else STATUS_YELLOW


def _project_status_markup(project: ComposeProject) -> SafeMarkup:
    summary = f"{project.running_count}/{project.total_count} running"
    return SafeMarkup(f"[{_project_status_color(project)}]{summary}[/]")


class TableRenderer(_Base):
    """Knows how to initialise columns and populate rows for every resource table.

    Column layout lives on each tab's `ResourceEntry` in `self._resource_registry`
    (built by `DockSurfApp`), not here — this class just drives it.
    """

    def setup_tables(self) -> None:
        self._current: dict[TabID, list] = {
            tab_id: [] for tab_id in self._resource_registry
        }
        # Tabs whose tables haven't been repopulated since the last snapshot.
        # Inactive tabs are refreshed lazily when they become active.
        self._dirty_tabs: set[TabID] = set()

        # Compose project names whose service rows are currently hidden.
        self._collapsed_projects: set[str] = set()

        # Multi-select: per-tab sets of marked row-keys (see `_row_key`).
        self._marked: dict[TabID, set[tuple[str, str]]] = {
            tab_id: set() for tab_id in self._resource_registry
        }

        # On-demand per-volume disk sizes (populated by the `b` size action),
        # keyed by volume name; read by `_show_volume_details`.
        self._volume_sizes: dict[str, int] = {}

        # On-demand per-image Architecture, keyed by image id;
        # lazily fetched on row-select by ImageActionHandler and read by
        # `_show_image_details`.
        self._image_architectures: dict[str, str] = {}

        # On-demand container details (env, health log, start time, restart count),
        # keyed by container id. Refreshed on row selection/refresh but retained
        # between fetches so the detail pane keeps the last-known values.
        self._container_details: dict[str, ContainerDetail] = {}

        # Whether to reveal env vars that look like secrets (`R`).
        self._reveal_secrets: bool = False

        # Per-tab column sort: (column, reverse), or `None` for insertion order.
        # Restored from the previous session; unknown columns are ignored.
        self._sort_state: dict[TabID, tuple[str, bool] | None] = {
            tab_id: self._session.sort_state.get(tab_id.value)
            for tab_id in self._resource_registry
        }

        for tab_id, entry in self._resource_registry.items():
            table = self.query_one(f"#{entry.table_id}", DataTable)
            self._add_table_columns(table, tab_id, entry)
            table.cursor_type = "row"

    def _add_table_columns(
        self, table: DataTable, tab_id: TabID, entry: "ResourceEntry"
    ) -> None:
        """(Re)build a table's columns, marking the active sort column.

        Called at initial setup and again whenever the sort column/direction
        changes (`_on_header_selected`), since Textual has no API to relabel
        an existing column in place.
        """
        # Leading mark column, added separately (not part of ResourceEntry.
        # columns) so every populate method just prepends one cell per row.
        table.add_column("", width=2)
        sort_state = self._sort_state.get(tab_id)
        for col in entry.columns:
            label = col
            if sort_state is not None and sort_state[0] == col:
                label = f"{col} {'▼' if sort_state[1] else '▲'}"
            table.add_column(label)

    def _on_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Handle a header click and toggle that column's sort.

        Not `@on`-decorated here: this class is a plain mixin (`_Base` is
        `object` at runtime — see the module docstring), so Textual's
        `_MessagePumpMeta` never processes it and any `@on` decorator on a
        mixin method is silently inert. The real dispatch entry point is
        `DockSurfApp._on_data_table_header_selected` in app.py, which is a
        real method on the App class and just forwards here.
        """
        active = self.query_one(TabbedContent).active
        entry = self._resource_registry.get(active)
        if entry is None or event.data_table.id != entry.table_id:
            return
        # Column 0 is the leading mark-glyph column — not sortable.
        if event.column_index == 0:
            return
        col_name = entry.columns[event.column_index - 1]
        if col_name not in entry.sort_keys:
            return

        current = self._sort_state.get(active)
        reverse = not current[1] if current and current[0] == col_name else False
        self._sort_state[active] = (col_name, reverse)
        if self._persist_session:
            self._session.sort_state[active.value] = (col_name, reverse)
            save_session(self._session)

        table = event.data_table
        table.clear(columns=True)
        self._add_table_columns(table, active, entry)
        self._rerender_active_table()

    # TODO: Refactor to reduce complexity.
    def _populate_container_table(
        self, table: DataTable, items: list[Container] | None = None
    ) -> None:
        if items is None:
            assert self.snapshot is not None, _NEEDS_SNAPSHOT_MSG
            items = self.snapshot.containers

        sort_state = self._sort_state.get(TabID.CONTAINERS)
        sort_key = None
        reverse = False
        if sort_state is not None:
            col_name, reverse = sort_state
            sort_key = self._resource_registry[TabID.CONTAINERS].sort_keys.get(col_name)

        projects, standalone = _group_by_project(items, key=sort_key, reverse=reverse)
        marked = self._marked[TabID.CONTAINERS]

        # `_current` is the row-backing list: each row index maps to either a
        # `ComposeProject` header or a `Container` service/standalone row.
        rows: list[ComposeProject | Container] = []
        for project in projects:
            collapsed = project.name in self._collapsed_projects
            glyph = "▸" if collapsed else "▾"
            config = (
                os.path.basename(project.config_files.split(",")[0].strip())
                if project.config_files
                else ""
            )
            rows.append(project)
            _safe_row(
                table,
                "",  # project headers can't be marked
                SafeMarkup(f"[b]{glyph} {escape(project.name)}[/b]"),
                config,
                _project_status_markup(project),
                "",
                "",
            )
            if collapsed:
                continue
            members = project.containers
            for idx, c in enumerate(members):
                rows.append(c)
                branch = "└" if idx == len(members) - 1 else "├"
                name = SafeMarkup(f"  {branch} {escape(c.compose_service or c.name)}")
                _safe_row(
                    table,
                    _mark_cell(marked, self._row_key(c)),
                    name,
                    c.image_name,
                    _status_markup(c),
                    _health_markup(c),
                    c.uptime_hint or "—",
                )

        for c in standalone:
            rows.append(c)
            _safe_row(
                table,
                _mark_cell(marked, self._row_key(c)),
                c.name,
                c.image_name,
                _status_markup(c),
                _health_markup(c),
                c.uptime_hint or "—",
            )

        self._current[TabID.CONTAINERS] = rows

    def _rerender_active_table(self) -> None:
        """Re-populate the active tab's table honouring any active search filter.

        `_apply_filter` (with the current query, empty when the bar is closed)
        clears and repopulates whichever tab is active — used after a mark-clear
        or a bulk action, where several rows' mark cells changed at once.
        """
        search_bar = self.query_one(f"#{SEARCH_BAR_ID}", Input)
        self._apply_filter(search_bar.value if search_bar.display else "")

    def _rerender_containers(self) -> None:
        """Re-run the container populate honouring any active search filter.

        Used after a group is collapsed/expanded.
        """
        self._rerender_active_table()

    def _populate_image_table(
        self, table: DataTable, items: list[Image] | None = None
    ) -> None:
        if items is None:
            assert self.snapshot is not None, _NEEDS_SNAPSHOT_MSG
            items = self.snapshot.images
        self._current[TabID.IMAGES] = items
        marked = self._marked[TabID.IMAGES]
        for i in items:
            _safe_row(
                table,
                _mark_cell(marked, self._row_key(i)),
                i.repository,
                i.tag,
                format_size(i.size_bytes),
            )

    def _populate_volume_table(
        self, table: DataTable, items: list[Volume] | None = None
    ) -> None:
        if items is None:
            assert self.snapshot is not None, _NEEDS_SNAPSHOT_MSG
            items = self.snapshot.volumes
        self._current[TabID.VOLUMES] = items
        marked = self._marked[TabID.VOLUMES]
        for v in items:
            status = markup_green("In Use") if v.used_by else markup_yellow("Orphaned")
            raw = v.name[:50] + "..." if len(v.name) > 50 else v.name
            _safe_row(table, _mark_cell(marked, self._row_key(v)), raw, status)

    def _populate_network_table(
        self, table: DataTable, items: list[Network] | None = None
    ) -> None:
        if items is None:
            assert self.snapshot is not None, _NEEDS_SNAPSHOT_MSG
            items = self.snapshot.networks
        self._current[TabID.NETWORKS] = items
        marked = self._marked[TabID.NETWORKS]
        for n in items:
            _safe_row(
                table, _mark_cell(marked, self._row_key(n)), n.name, n.driver, n.scope
            )

    def _sort_items(self, tab_id: TabID, entry: "ResourceEntry", items: list) -> list:
        """Apply the active column sort (if any) to a snapshot/filtered list.

        Used by both `SnapshotManager._apply_snapshot` (plain refresh) and
        `ResourceSearchController._apply_filter` (search active), so a sort
        survives both a live `docker events` refresh and search filtering
        instead of only applying while the search bar happens to be open.
        """
        sort_state = self._sort_state.get(tab_id)
        if sort_state is None:
            return items
        col_name, reverse = sort_state
        sort_key = entry.sort_keys.get(col_name)
        if sort_key is None:
            return items
        return sorted(items, key=sort_key, reverse=reverse)

    def _update_empty_state(
        self,
        tab_id: TabID,
        entry: "ResourceEntry",
        items: list,
        query: str = "",
    ) -> None:
        """Swap the table for a placeholder message when `items` is empty.

        Distinguishes three causes so the message actually helps: an active
        search with no matches, a disconnected daemon (reusing the same
        classified `ConnectionState` the status bar shows), or genuinely zero
        resources of that type.
        """
        table = self.query_one(f"#{entry.table_id}", DataTable)
        empty = self.query_one(f"#{EMPTY_STATE_IDS[tab_id]}", Static)
        if items:
            table.display = True
            empty.display = False
            return
        table.display = False
        empty.display = True
        if query:
            message = f"No {entry.label}s match {query!r}"
        elif not self.docker.is_connected:
            state = self.docker.connection
            message = state.message
            if state.hint:
                message += f"\n{state.hint}"
        else:
            message = EMPTY_STATE_HINTS.get(tab_id, f"No {entry.label}s found")
        empty.update(escape(message))
