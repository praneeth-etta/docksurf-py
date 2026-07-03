"""
renderer.py — Table rendering and detail-pane mixins.

TableRenderer, SnapshotManager, ResourceFocusResolver, DetailPaneRenderer
are all mixin classes that compose into DockSurfApp via Python MRO.
"""

import logging
import os
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from textual import work
from textual.widgets import DataTable, Input, LoadingIndicator, TabbedContent

from docksurf_py.constants import (
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
    format_labels,
    format_ports,
    format_relative_time,
    format_size,
)
from docksurf_py.models import (
    ComposeProject,
    Container,
    DockerSnapshot,
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

        for entry in self._resource_registry.values():
            table = self.query_one(f"#{entry.table_id}", DataTable)
            table.add_columns(*entry.columns)
            table.cursor_type = "row"

    def _populate_container_table(
        self, table: DataTable, items: list[Container] | None = None
    ) -> None:
        if items is None:
            assert self.snapshot is not None, "populate with no items needs a snapshot"
            items = self.snapshot.containers

        projects, standalone = _group_by_project(items)

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
                SafeMarkup(f"[b]{glyph} {escape(project.name)}[/b]"),
                config,
                _project_status_markup(project),
            )
            if collapsed:
                continue
            members = project.containers
            for idx, c in enumerate(members):
                rows.append(c)
                branch = "└" if idx == len(members) - 1 else "├"
                name = SafeMarkup(f"  {branch} {escape(c.compose_service or c.name)}")
                _safe_row(table, name, c.image_name, _status_markup(c))

        for c in standalone:
            rows.append(c)
            _safe_row(table, c.name, c.image_name, _status_markup(c))

        self._current[TabID.CONTAINERS] = rows

    def _rerender_containers(self) -> None:
        """Re-run the container populate honouring any active search filter.

        Used after a group is collapsed/expanded — `_apply_filter` (with the
        current query, empty when the bar is closed) clears and repopulates the
        Containers table, which re-groups from scratch.
        """
        search_bar = self.query_one(f"#{SEARCH_BAR_ID}", Input)
        self._apply_filter(search_bar.value if search_bar.display else "")

    def _populate_image_table(
        self, table: DataTable, items: list[Image] | None = None
    ) -> None:
        if items is None:
            assert self.snapshot is not None, "populate with no items needs a snapshot"
            items = self.snapshot.images
        self._current[TabID.IMAGES] = items
        for i in items:
            _safe_row(table, i.repository, i.tag, format_size(i.size_bytes))

    def _populate_volume_table(
        self, table: DataTable, items: list[Volume] | None = None
    ) -> None:
        if items is None:
            assert self.snapshot is not None, "populate with no items needs a snapshot"
            items = self.snapshot.volumes
        self._current[TabID.VOLUMES] = items
        for v in items:
            status = markup_green("In Use") if v.used_by else markup_yellow("Orphaned")
            raw = v.name[:50] + "..." if len(v.name) > 50 else v.name
            _safe_row(table, raw, status)

    def _populate_network_table(
        self, table: DataTable, items: list[Network] | None = None
    ) -> None:
        if items is None:
            assert self.snapshot is not None, "populate with no items needs a snapshot"
            items = self.snapshot.networks
        self._current[TabID.NETWORKS] = items
        for n in items:
            _safe_row(table, n.name, n.driver, n.scope)


class SnapshotManager(_Base):
    """Fetches Docker state in a background thread and commits it to the UI."""

    _refresh_in_progress = False

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

        if not self.docker.is_connected:
            state = self.docker.connection
            logger.error(
                "Docker unavailable — status=%s context=%s host=%s",
                state.status.value,
                state.context,
                state.host,
            )
            self.notify(f"{state.message}\n{state.hint}", severity="error", timeout=12)

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
        self._auto_select_first()

        search_bar = self.query_one(f"#{SEARCH_BAR_ID}", Input)
        if search_bar.display and search_bar.value:
            self._apply_filter(search_bar.value)

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
                "Exit Code": "—" if c.running else str(c.exit_code),
                "Health": c.health if c.health else "—",
                "Created": format_relative_time(c.created),
                "Ports": format_ports(c.ports) if c.ports else "None",
                "Networks": "\n".join(c.networks) if c.networks else "None",
            }
        )
        pane.update_details(f"Container: {c.name}", details, env_vars=c.env)

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
