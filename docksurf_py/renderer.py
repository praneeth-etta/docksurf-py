"""
renderer.py — Table rendering and detail-pane mixins.

TableRenderer, SnapshotManager, ResourceFocusResolver, DetailPaneRenderer
are all mixin classes that compose into DockSurfApp via Python MRO.
"""

import logging
import os
import threading
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from textual import work
from textual.timer import Timer
from textual.widgets import DataTable, Input, LoadingIndicator, TabbedContent

from docksurf_py.constants import (
    DETAIL_PANE_ID,
    MARK_GLYPH,
    REFRESH_LOADING_ID,
    SEARCH_BAR_ID,
    STATUS_BAR_ID,
    SafeMarkup,
    TabID,
    markup_green,
    markup_red,
    markup_yellow,
)
from docksurf_py.docker import (
    EventStream,
    format_labels,
    format_ports,
    format_relative_time,
    format_size,
    format_uptime,
)
from docksurf_py.models import (
    ComposeProject,
    Container,
    DockerSnapshot,
    HealthProbe,
    Image,
    Network,
    Volume,
)
from docksurf_py.widgets import DetailPane, StatusBar

if TYPE_CHECKING:
    from docksurf_py.app import AppContext

    _Base = AppContext
else:
    # Real runtime base is `object` — `AppContext` only exists for mypy to
    # check these mixins' bodies against; see app.py's `AppContext` docstring.
    _Base = object

logger = logging.getLogger(__name__)


def _status_markup(c: Container) -> SafeMarkup:
    if c.running:
        label = escape(c.status)
        if c.health == "healthy":
            label += " ✓"
        elif c.health == "unhealthy":
            label += " ✗"
        elif c.health == "starting":
            label += " …"
        return markup_green(label)
    if c.state in ("exited", "dead"):
        suffix = f" ({c.exit_code})" if c.exit_code != 0 else ""
        return markup_red(escape(c.status) + suffix)
    return markup_yellow(escape(c.status))  # paused, restarting, created


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
) -> tuple[list[ComposeProject], list[Container]]:
    """Split containers into Compose projects (sorted) and standalone ones.

    Services within a project are sorted by service name; a project's
    config-file/working-dir come from its first service (they're identical
    across a project's containers).
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


def _project_status_markup(project: ComposeProject) -> SafeMarkup:
    summary = f"{project.running_count}/{project.total_count} running"
    return markup_green(summary) if project.all_running else markup_yellow(summary)


class TableRenderer(_Base):
    """Knows how to initialise columns and populate rows for every resource table.

    Column layout lives on each tab's `ResourceEntry` in `self._resource_registry`
    (built by `DockSurfApp`), not here — this class just drives it.
    """

    def setup_tables(self) -> None:
        self._current: dict[TabID, list] = {
            tab_id: [] for tab_id in self._resource_registry
        }
        # Compose project names whose service rows are currently hidden.
        self._collapsed_projects: set[str] = set()
        # Multi-select: per-tab sets of marked row-keys (see `_row_key`).
        self._marked: dict[TabID, set[tuple[str, str]]] = {
            tab_id: set() for tab_id in self._resource_registry
        }

        for entry in self._resource_registry.values():
            table = self.query_one(f"#{entry.table_id}", DataTable)
            # Leading mark column, added separately (not part of ResourceEntry.
            # columns) so every populate method just prepends one cell per row.
            table.add_column("", width=2)
            table.add_columns(*entry.columns)
            table.cursor_type = "row"

    def _populate_container_table(
        self, table: DataTable, items: list[Container] | None = None
    ) -> None:
        if items is None:
            assert self.snapshot is not None, "populate with no items needs a snapshot"
            items = self.snapshot.containers

        projects, standalone = _group_by_project(items)
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
                    format_uptime(c.started_at),
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
                format_uptime(c.started_at),
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
            assert self.snapshot is not None, "populate with no items needs a snapshot"
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
            assert self.snapshot is not None, "populate with no items needs a snapshot"
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
            assert self.snapshot is not None, "populate with no items needs a snapshot"
            items = self.snapshot.networks
        self._current[TabID.NETWORKS] = items
        marked = self._marked[TabID.NETWORKS]
        for n in items:
            _safe_row(
                table, _mark_cell(marked, self._row_key(n)), n.name, n.driver, n.scope
            )


class SnapshotManager(_Base):
    """Fetches Docker state in a background thread and commits it to the UI."""

    _refresh_in_progress = False
    _refresh_pending = False
    _event_stream: EventStream | None = None
    _event_stop: threading.Event | None = None
    _refresh_debounce: Timer | None = None

    # Docker action *prefixes* that fire constantly and never change what we
    # render. Matched against the part before ":" because exec actions arrive
    # as e.g. "exec_create: /bin/sh -c <healthcheck>" — the command is appended.
    # `health_status` fires on every healthcheck probe (even when unchanged), so
    # it's the dominant source of refresh spam; a real state change (start/die/
    # etc.) still triggers a refresh that repaints the Health column.
    _EVENT_NOISE = frozenset(
        {
            "exec_create",
            "exec_start",
            "exec_die",
            "exec_detach",
            "top",
            "health_status",
        }
    )

    def _is_noise_event(self, action: str) -> bool:
        return action.split(":", 1)[0].strip() in self._EVENT_NOISE

    def start_refresh(self) -> None:
        if self._refresh_in_progress:
            logger.debug("Refresh skipped — already in progress")
            return
        self._refresh_in_progress = True
        logger.info("Refresh started")
        self.query_one(f"#{REFRESH_LOADING_ID}", LoadingIndicator).display = True
        self.populate_tables()

    @work(thread=True)
    def populate_tables(self) -> None:
        try:
            snapshot = self.docker.fetch_snapshot()
        except Exception as exc:
            logger.exception("Snapshot fetch failed")
            self.call_from_thread(self._finish_refresh, None, str(exc))
        else:
            logger.info(
                "Snapshot fetched — containers=%d images=%d volumes=%d networks=%d",
                len(snapshot.containers),
                len(snapshot.images),
                len(snapshot.volumes),
                len(snapshot.networks),
            )
            self.call_from_thread(self._finish_refresh, snapshot, None)

    def _apply_snapshot(self, snapshot: DockerSnapshot) -> None:
        self.snapshot: DockerSnapshot | None = snapshot

        # A fetch worker can complete while the app is tearing down (e.g. the
        # user quit mid-refresh); the widgets are gone, so committing would
        # raise NoMatches. Skip — there's nothing left to paint.
        if not self.is_running:
            return

        if not self.docker.is_connected:
            state = self.docker.connection
            logger.error(
                "Docker unavailable — status=%s context=%s host=%s",
                state.status.value,
                state.context,
                state.host,
            )
            self.notify(f"{state.message}\n{state.hint}", severity="error", timeout=12)

        # Remember what the user had selected so a background refresh doesn't
        # yank the cursor back to the top (and needlessly restart the stats
        # stream) on every `docker events` tick.
        active = self.query_one(TabbedContent).active
        focus_key = self._focused_row_key(active)

        # Drop marks on resources that no longer exist — checked against the
        # full snapshot (not a search-filtered view), before any repopulate.
        for tab_id, entry in self._resource_registry.items():
            live_keys = {
                key
                for item in entry.snapshot_items(snapshot)
                if (key := self._row_key(item)) is not None
            }
            self._marked[tab_id] &= live_keys

        for entry in self._resource_registry.values():
            table = self.query_one(f"#{entry.table_id}", DataTable)
            table.clear(columns=False)
            entry.populate(table)

        status_bar = self.query_one(f"#{STATUS_BAR_ID}", StatusBar)
        status_bar.update_stats(
            snapshot.containers,
            snapshot.images,
            snapshot.volumes,
            context=self.docker.connection.context,
        )

        search_bar = self.query_one(f"#{SEARCH_BAR_ID}", Input)
        if search_bar.display and search_bar.value:
            self._apply_filter(search_bar.value)

        # Restore selection last — after any filter re-populate — so it wins.
        self._restore_selection(active, focus_key)

    @staticmethod
    def _row_key(item: Any) -> tuple[str, str] | None:
        """Stable identity for a row-backing object, for selection restore."""
        if isinstance(item, Container):
            return ("container", item.id)
        if isinstance(item, ComposeProject):
            return ("project", item.name)
        if isinstance(item, Image):
            return ("image", item.id)
        if isinstance(item, Volume):
            return ("volume", item.name)
        if isinstance(item, Network):
            return ("network", item.name)
        return None

    def _focused_row_key(self, tab_id: TabID) -> tuple[str, str] | None:
        item = self._get_focused_resource(tab_id)
        return self._row_key(item) if item is not None else None

    def _restore_selection(
        self, tab_id: TabID, focus_key: tuple[str, str] | None
    ) -> None:
        """Re-select the row matching `focus_key`, or fall back to the first row."""
        entry = self._resource_registry.get(tab_id)
        if focus_key is None or entry is None:
            self._auto_select_first()
            return
        for idx, item in enumerate(self._current.get(tab_id, [])):
            if self._row_key(item) == focus_key:
                self.query_one(f"#{entry.table_id}", DataTable).move_cursor(row=idx)
                pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
                try:
                    entry.show_details(pane, idx)
                except IndexError:
                    pane.clear_details()
                # Cursor may not have moved (same index) so RowHighlighted won't
                # fire — sync stats/top here in case the item's state changed.
                self._sync_stats()
                self._sync_top()
                return
        # The previously-focused resource is gone — select the first row.
        self._auto_select_first()

    def _finish_refresh(
        self, snapshot: DockerSnapshot | None, error: str | None
    ) -> None:
        try:
            if snapshot is not None:
                self._apply_snapshot(snapshot)
                logger.info("Refresh complete")
            elif error:
                logger.warning("Refresh failed: %s", error)
                self.notify(f"Refresh failed: {error}", severity="error")
        finally:
            self._refresh_in_progress = False
            self.query_one(f"#{REFRESH_LOADING_ID}", LoadingIndicator).display = False
            # An event arrived mid-refresh — run once more so the final state lands.
            if self._refresh_pending:
                self._refresh_pending = False
                self.start_refresh()

    # --- Event-driven auto-refresh ---

    @work(thread=True)
    def start_event_listener(self) -> None:
        """Watch `docker events` and auto-refresh the UI, so `r` is a fallback.

        Runs on its own daemon worker; if the stream ends (daemon down or a
        hiccup) it backs off and re-subscribes, so it "just works" across a
        daemon restart the same way `ensure_connected()` does.
        """
        stop = threading.Event()
        self._event_stop = stop
        while not stop.is_set():
            stream = self.docker.stream_events()
            self._event_stream = stream
            try:
                for event in stream:
                    if stop.is_set():
                        break
                    if self._is_noise_event(event.get("Action", "")):
                        continue
                    self.call_from_thread(self._on_docker_event)
            except Exception:
                logger.exception("Event listener error")
            # Stream ended (daemon down or a hiccup) — back off, then
            # re-subscribe; the wait returns immediately once we're told to stop.
            stop.wait(2.0)

    def _on_docker_event(self) -> None:
        # Coalesce bursts (e.g. `compose up` emits many events) into one refresh.
        if self._refresh_debounce is not None:
            self._refresh_debounce.stop()
        self._refresh_debounce = self.set_timer(0.4, self._debounced_refresh)

    def _debounced_refresh(self) -> None:
        self._refresh_debounce = None
        if self._refresh_in_progress:
            self._refresh_pending = True
        else:
            self.start_refresh()

    def stop_event_listener(self) -> None:
        if self._event_stop is not None:
            self._event_stop.set()
        if self._event_stream is not None:
            self._event_stream.stop()


class ResourceFocusResolver(_Base):
    """Maps the active tab + cursor row to the concrete resource object."""

    def _get_focused_resource(self, tab_id: TabID) -> Any | None:
        if not self.snapshot:
            return None
        tabbed = self.query_one(TabbedContent)
        if tabbed.active != tab_id:
            return None
        entry = self._resource_registry.get(tab_id)
        if entry is None:
            return None
        current_list = self._current.get(tab_id, [])
        table = self.query_one(f"#{entry.table_id}", DataTable)
        row = table.cursor_row
        if not current_list or row is None or row >= len(current_list):
            return None
        return current_list[row]

    def _get_focused_container(self) -> Container | None:
        item = self._get_focused_resource(TabID.CONTAINERS)
        return item if isinstance(item, Container) else None

    def _focused_is_project_header(self) -> bool:
        """True when the cursor sits on a Compose project header row."""
        item = self._get_focused_resource(TabID.CONTAINERS)
        return isinstance(item, ComposeProject)

    def _get_focused_project(self) -> ComposeProject | None:
        """Resolve the Compose project for the focused row.

        Works whether the cursor is on a project header or on one of its
        service rows. Members are rebuilt from the full snapshot (not the
        possibly-filtered/collapsed view) so project-wide actions cover every
        service, not just the visible ones.
        """
        item = self._get_focused_resource(TabID.CONTAINERS)
        if isinstance(item, ComposeProject):
            name = item.name
        elif isinstance(item, Container) and item.is_compose:
            name = item.compose_project
        else:
            return None

        if not self.snapshot:
            return None
        members = [c for c in self.snapshot.containers if c.compose_project == name]
        if not members:
            return None
        first = members[0]
        return ComposeProject(
            name=name,
            containers=members,
            config_files=first.compose_config_files,
            working_dir=first.compose_working_dir,
        )


class DetailPaneRenderer(_Base):
    """Formats and pushes resource details into the side pane on row highlight."""

    def _show_container_details(self, pane: DetailPane, row: int) -> None:
        items = self._current.get(TabID.CONTAINERS, [])
        if row >= len(items):
            return
        item = items[row]
        if isinstance(item, ComposeProject):
            self._show_project_details(pane, item)
            return
        c = item

        details = {
            "ID": c.id,
            "Image": c.image_name,
        }
        if c.is_compose:
            details["Project"] = c.compose_project
            details["Service"] = c.compose_service
        details.update(
            {
                "Image SHA": c.image_id,
                "Status": _status_markup(c),
                "Health": _health_markup(c),
                "Uptime": format_uptime(c.started_at),
                "Restarts": str(c.restart_count),
                "Exit Code": "—" if c.running else str(c.exit_code),
                "Created": format_relative_time(c.created),
                "Ports": format_ports(c.ports) if c.ports else "None",
                "Networks": "\n".join(c.networks) if c.networks else "None",
            }
        )
        pane.update_details(
            f"Container: {c.name}",
            details,
            env_vars=c.env,
            health_log=_format_health_log(c.health_log),
        )

    def _show_project_details(self, pane: DetailPane, project: ComposeProject) -> None:
        services = "\n".join(
            f"{c.compose_service or c.name}: {'running' if c.running else c.status}"
            for c in project.containers
        )
        details = {
            "Services": _project_status_markup(project),
            "Config File": project.config_files or "—",
            "Working Dir": project.working_dir or "—",
            "Containers": services or "None",
        }
        pane.update_details(f"Project: {project.name}", details)

    def _show_image_details(self, pane: DetailPane, row: int) -> None:
        images = self._current.get(TabID.IMAGES, [])
        if row >= len(images):
            return
        image = images[row]

        if image.used_by:
            status = markup_green("In Use")
        elif image.is_dangling:
            status = markup_red("Dangling (safe to delete)")
        else:
            status = markup_yellow("Unused (not referenced by any container)")

        details = {
            "ID": image.id.removeprefix("sha256:")[:12] if image.id else "N/A",
            "Size": format_size(image.size_bytes),
            "Created": format_relative_time(image.created),
            "Architecture": image.architecture or "N/A",
            "Used By": "\n".join(image.used_by) if image.used_by else "None",
            "Status": status,
        }
        pane.update_details(f"Image: {image.repository}:{image.tag}", details)

    def _show_volume_details(self, pane: DetailPane, row: int) -> None:
        volumes = self._current.get(TabID.VOLUMES, [])
        if row >= len(volumes):
            return
        volume = volumes[row]

        details = {
            "Mountpoint": volume.mountpoint,
            "Driver": volume.driver,
            "Labels": format_labels(volume.labels) if volume.labels else "None",
            "Used By": (
                "\n".join(volume.used_by)
                if volume.used_by
                else markup_yellow("Orphaned (safe to delete)")
            ),
        }
        pane.update_details(f"Volume: {volume.name}", details)

    def _show_network_details(self, pane: DetailPane, row: int) -> None:
        networks = self._current.get(TabID.NETWORKS, [])
        if row >= len(networks):
            return
        network = networks[row]

        details = {
            "ID": network.id,
            "Subnet": network.subnet,
            "Gateway": network.gateway,
            "Used By": "\n".join(network.used_by) if network.used_by else "None",
        }
        pane.update_details(f"Network: {network.name}", details)
