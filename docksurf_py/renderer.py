"""
renderer.py — Table rendering and detail-pane mixins.

TableRenderer, SnapshotManager, ResourceFocusResolver, DetailPaneRenderer
are all mixin classes that compose into DockSurfApp via Python MRO.
"""

import logging
from typing import Any

from rich.markup import escape
from textual import work
from textual.widgets import DataTable, Input, LoadingIndicator, TabbedContent

from docksurf_py.constants import (
    REFRESH_LOADING_ID,
    SEARCH_BAR_ID,
    STATUS_BAR_ID,
    SafeMarkup,
    TabID,
    TableID,
    markup_green,
    markup_red,
    markup_yellow,
)
from docksurf_py.docker import format_relative_time
from docksurf_py.models import Container, DockerSnapshot, Image, Network, Volume
from docksurf_py.widgets import DetailPane, StatusBar

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


class TableRenderer:
    """Knows how to initialise columns and populate rows for every resource table."""

    TABLE_COLUMNS: dict[TableID, tuple[str, ...]] = {
        TableID.CONTAINERS: ("Name", "Image", "Status"),
        TableID.IMAGES: ("Repository", "Tag", "Size"),
        TableID.VOLUMES: ("Name", "Status"),
        TableID.NETWORKS: ("Name", "Driver", "Scope"),
    }

    def setup_tables(self) -> None:
        self._current_containers = []
        self._current_images = []
        self._current_volumes = []
        self._current_networks = []

        for table_id, columns in self.TABLE_COLUMNS.items():
            table = self.query_one(f"#{table_id}", DataTable)
            table.add_columns(*columns)
            table.cursor_type = "row"

    def _populate_container_table(
        self, table: DataTable, items: list[Container] | None = None
    ) -> None:
        self._current_containers = (
            items if items is not None else self.snapshot.containers
        )
        for c in self._current_containers:
            _safe_row(table, c.name, c.image_name, _status_markup(c))

    def _populate_image_table(
        self, table: DataTable, items: list[Image] | None = None
    ) -> None:
        self._current_images = items if items is not None else self.snapshot.images
        for i in self._current_images:
            _safe_row(table, i.repository, i.tag, i.size)

    def _populate_volume_table(
        self, table: DataTable, items: list[Volume] | None = None
    ) -> None:
        self._current_volumes = items if items is not None else self.snapshot.volumes
        for v in self._current_volumes:
            status = markup_green("In Use") if v.used_by else markup_yellow("Orphaned")
            raw = v.name[:50] + "..." if len(v.name) > 50 else v.name
            _safe_row(table, raw, status)

    def _populate_network_table(
        self, table: DataTable, items: list[Network] | None = None
    ) -> None:
        self._current_networks = items if items is not None else self.snapshot.networks
        for n in self._current_networks:
            _safe_row(table, n.name, n.driver, n.scope)


class SnapshotManager:
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
        self.snapshot = snapshot
        for table_id in self.TABLE_COLUMNS:
            self.query_one(f"#{table_id}", DataTable).clear(columns=False)

        self._populate_container_table(
            self.query_one(f"#{TableID.CONTAINERS}", DataTable)
        )
        self._populate_image_table(self.query_one(f"#{TableID.IMAGES}", DataTable))
        self._populate_volume_table(self.query_one(f"#{TableID.VOLUMES}", DataTable))
        self._populate_network_table(self.query_one(f"#{TableID.NETWORKS}", DataTable))

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


class ResourceFocusResolver:
    """Maps the active tab + cursor row to the concrete resource object."""

    def _get_focused_resource(self, tab_id: TabID, current_list: list) -> Any | None:
        if not self.snapshot:
            return None
        tabbed = self.query_one(TabbedContent)
        if tabbed.active != tab_id:
            return None
        table_id = f"table-{tab_id.removeprefix('tab-')}"
        table = self.query_one(f"#{table_id}", DataTable)
        row = table.cursor_row
        if not current_list or row is None or row >= len(current_list):
            return None
        return current_list[row]

    def _get_focused_container(self) -> Container | None:
        return self._get_focused_resource(
            TabID.CONTAINERS, getattr(self, "_current_containers", [])
        )

    def _get_focused_image(self) -> Image | None:
        return self._get_focused_resource(
            TabID.IMAGES, getattr(self, "_current_images", [])
        )

    def _get_focused_volume(self) -> Volume | None:
        return self._get_focused_resource(
            TabID.VOLUMES, getattr(self, "_current_volumes", [])
        )

    def _get_focused_network(self) -> Network | None:
        return self._get_focused_resource(
            TabID.NETWORKS, getattr(self, "_current_networks", [])
        )


class DetailPaneRenderer:
    """Formats and pushes resource details into the side pane on row highlight."""

    def _show_container_details(self, pane: DetailPane, row: int) -> None:
        containers = getattr(self, "_current_containers", [])
        if row >= len(containers):
            return
        c = containers[row]

        details = {
            "ID": c.id,
            "Image": c.image_name,
            "Image SHA": c.image_id,
            "Status": _status_markup(c),
            "Exit Code": "—" if c.running else str(c.exit_code),
            "Health": c.health if c.health else "—",
            "Created": format_relative_time(c.created),
            "Ports": c.ports if c.ports else "None",
            "Networks": "\n".join(c.networks) if c.networks else "None",
        }
        pane.update_details(f"Container: {c.name}", details, env_vars=c.env)

    def _show_image_details(self, pane: DetailPane, row: int) -> None:
        images = getattr(self, "_current_images", [])
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
            "Size": image.size or "N/A",
            "Created": format_relative_time(image.created),
            "Architecture": image.architecture or "N/A",
            "Used By": "\n".join(image.used_by) if image.used_by else "None",
            "Status": status,
        }
        pane.update_details(f"Image: {image.repository}:{image.tag}", details)

    def _show_volume_details(self, pane: DetailPane, row: int) -> None:
        volumes = getattr(self, "_current_volumes", [])
        if row >= len(volumes):
            return
        volume = volumes[row]

        details = {
            "Mountpoint": volume.mountpoint,
            "Driver": volume.driver,
            "Labels": volume.labels if volume.labels else "None",
            "Used By": (
                "\n".join(volume.used_by)
                if volume.used_by
                else markup_yellow("Orphaned (safe to delete)")
            ),
        }
        pane.update_details(f"Volume: {volume.name}", details)

    def _show_network_details(self, pane: DetailPane, row: int) -> None:
        networks = getattr(self, "_current_networks", [])
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
