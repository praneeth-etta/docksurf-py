"""
app.py — The Controller Layer.

Responsibilities:
  - Route user actions to docker.py (infrastructure).
  - Push results into widgets.py (view).
"""

import subprocess
from typing import Callable, TypeVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    LoadingIndicator,
    TabbedContent,
    TabPane,
)

from docksurf_py.constants import (
    DETAIL_PANE_ID,
    LOG_PANE_ID,
    MAIN_CONTAINER_ID,
    REFRESH_LOADING_ID,
    SEARCH_BAR_ID,
    STATUS_BAR_ID,
    TabID,
    TableID,
    markup_green,
    markup_red,
    markup_yellow,
)
from docksurf_py.docker import (
    Container,
    DockerClient,
    DockerSnapshot,
    Image,
    Network,
    Volume,
    format_relative_time,
)
from docksurf_py.widgets import (
    ConfirmDialog,
    ContainerTable,
    DetailPane,
    HelpScreen,
    LogPane,
    SearchBar,
    StatusBar,
)

T = TypeVar("T")


def _status_markup(status: str) -> str:
    lower = status.lower()
    if "up" in lower or "running" in lower:
        return markup_green(status)
    if "exited" in lower or "dead" in lower:
        return markup_red(status)
    return markup_yellow(status)


class TableRenderer:
    """Knows how to initialise columns and populate rows for every resource table."""

    TABLE_COLUMNS: dict[TableID, tuple[str, ...]] = {
        TableID.CONTAINERS: ("Name", "Image", "Status"),
        TableID.IMAGES: ("Repository", "Tag", "Size"),
        TableID.VOLUMES: ("Name", "Status"),
        TableID.NETWORKS: ("Name", "Driver", "Scope"),
    }

    def setup_tables(self) -> None:
        for table_id, columns in self.TABLE_COLUMNS.items():
            table = self.query_one(f"#{table_id}", DataTable)
            table.add_columns(*columns)
            table.cursor_type = "row"

    def _populate_container_table(
        self,
        table: DataTable,
        items: list[Container] | None = None,
    ) -> None:
        for c in items if items is not None else self.snapshot.containers:
            table.add_row(c.name, c.image_name, _status_markup(c.status))

    def _populate_image_table(
        self,
        table: DataTable,
        items: list[Image] | None = None,
    ) -> None:
        for i in items if items is not None else self.snapshot.images:
            table.add_row(i.repository, i.tag, i.size)

    def _populate_volume_table(
        self,
        table: DataTable,
        items: list[Volume] | None = None,
    ) -> None:
        for v in items if items is not None else self.snapshot.volumes:
            status = markup_green("In Use") if v.used_by else markup_yellow("Orphaned")
            name = v.name[:50] + "..." if len(v.name) > 50 else v.name
            table.add_row(name, status)

    def _populate_network_table(
        self,
        table: DataTable,
        items: list[Network] | None = None,
    ) -> None:
        for n in items if items is not None else self.snapshot.networks:
            table.add_row(n.name, n.driver, n.scope)


class SnapshotManager:
    """Fetches Docker state in a background thread and commits it to the UI."""

    _refresh_in_progress = False

    def start_refresh(self) -> None:
        if self._refresh_in_progress:
            return
        self._refresh_in_progress = True
        self.query_one(f"#{REFRESH_LOADING_ID}", LoadingIndicator).display = True
        self.populate_tables()

    @work(thread=True)
    def populate_tables(self) -> None:
        try:
            snapshot = self.docker.fetch_snapshot()
        except Exception as exc:
            self.call_from_thread(self._finish_refresh, None, str(exc))
        else:
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
        status_bar.update_stats(snapshot.containers, snapshot.images, snapshot.volumes)

    def _finish_refresh(
        self, snapshot: DockerSnapshot | None, error: str | None
    ) -> None:
        try:
            if snapshot is not None:
                self._apply_snapshot(snapshot)
            elif error:
                self.notify(f"Refresh failed: {error}", severity="error")
        finally:
            self._refresh_in_progress = False
            self.query_one(f"#{REFRESH_LOADING_ID}", LoadingIndicator).display = False


class ResourceFocusResolver:
    """Maps the active tab + cursor row to the concrete resource object."""

    _TAB_RESOURCES: dict[TabID, Callable[[DockerSnapshot], list]] = {
        TabID.CONTAINERS: lambda s: s.containers,
        TabID.IMAGES: lambda s: s.images,
        TabID.VOLUMES: lambda s: s.volumes,
        TabID.NETWORKS: lambda s: s.networks,
    }

    def _get_focused_resource(self, tab_id: TabID, resources: list[T]) -> T | None:
        if not self.snapshot:
            return None
        tabbed = self.query_one(TabbedContent)
        if tabbed.active != tab_id:
            return None
        table_id = f"table-{tab_id.removeprefix('tab-')}"
        table = self.query_one(f"#{table_id}", DataTable)
        row = table.cursor_row
        if not resources or row is None or row >= len(resources):
            return None
        return resources[row]

    def _get_focused(self, tab_id: TabID):
        if not self.snapshot:
            return None
        return self._get_focused_resource(
            tab_id, self._TAB_RESOURCES[tab_id](self.snapshot)
        )

    def _get_focused_container(self) -> Container | None:
        return self._get_focused(TabID.CONTAINERS)

    def _get_focused_image(self) -> Image | None:
        return self._get_focused(TabID.IMAGES)

    def _get_focused_volume(self) -> Volume | None:
        return self._get_focused(TabID.VOLUMES)

    def _get_focused_network(self) -> Network | None:
        return self._get_focused(TabID.NETWORKS)


class DetailPaneRenderer:
    """Formats and pushes resource details into the side pane on row highlight."""

    def _show_container_details(self, pane: DetailPane, row: int) -> None:
        c = self.snapshot.containers[row]
        status_color = (
            markup_green if ("Up" in c.status or "running" in c.status) else markup_red
        )
        details = {
            "ID": c.id,
            "Image": c.image_name,
            "Image SHA": c.image_id,
            "Status": status_color(c.status),
            "Created": format_relative_time(c.created),
            "Ports": c.ports if c.ports else "None",
            "Networks": "\n".join(c.networks) if c.networks else "None",
            "Mounts": "\n".join(c.mounts) if c.mounts else "None",
        }
        pane.update_details(f"Container: {c.name}", details, env_vars=c.env)

    def _show_image_details(self, pane: DetailPane, row: int) -> None:
        image = self.snapshot.images[row]
        if image.used_by:
            status = markup_green("In Use")
        elif image.is_dangling:
            status = markup_red("Dangling (safe to delete)")
        else:
            status = markup_yellow("Unused (not referenced by any container)")

        details = {
            "ID": (image.id[:24] + "...")
            if image.id and len(image.id) > 24
            else (image.id or "N/A"),
            "Size": image.size or "N/A",
            "Created": format_relative_time(image.created),
            "Architecture": image.architecture or "N/A",
            "Used By": "\n".join(image.used_by) if image.used_by else "None",
            "Status": status,
        }
        pane.update_details(f"Image: {image.repository}:{image.tag}", details)

    def _show_volume_details(self, pane: DetailPane, row: int) -> None:
        volume = self.snapshot.volumes[row]
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
        network = self.snapshot.networks[row]
        details = {
            "ID": network.id,
            "Subnet": network.subnet,
            "Gateway": network.gateway,
            "Used By": "\n".join(network.used_by) if network.used_by else "None",
        }
        pane.update_details(f"Network: {network.name}", details)

    @on(DataTable.RowHighlighted)
    def update_details(self, event: DataTable.RowHighlighted) -> None:
        if not self.snapshot:
            return
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        table_id = event.control.id
        try:
            if table_id == TableID.CONTAINERS:
                self._show_container_details(pane, event.cursor_row)
            elif table_id == TableID.IMAGES:
                self._show_image_details(pane, event.cursor_row)
            elif table_id == TableID.VOLUMES:
                self._show_volume_details(pane, event.cursor_row)
            elif table_id == TableID.NETWORKS:
                self._show_network_details(pane, event.cursor_row)
        except IndexError:
            pane.clear_details()

    @on(TabbedContent.TabActivated)
    def clear_on_tab_switch(self) -> None:
        self.query_one(f"#{DETAIL_PANE_ID}", DetailPane).clear_details()


class ContainerActionHandler:
    """Start, stop, restart, exec, and log actions scoped to containers."""

    _CONTAINER_TAB_HINT = "Switch to the Containers tab and select a container"

    def _run_on_focused_container(
        self,
        command: Callable[[str], tuple[bool, str]],
        success_msg: Callable[[Container], str],
        guard: Callable[[Container], str | None] = lambda _: None,
    ) -> None:
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return
        if reason := guard(c):
            self.notify(reason, severity="information")
            return
        ok, err = command(c.id)
        if ok:
            self.notify(success_msg(c))
            self.populate_tables()
        else:
            self.notify(f"Error: {err}", severity="error")

    def action_stop_container(self) -> None:
        self._run_on_focused_container(
            command=self.docker.stop_container,
            success_msg=lambda c: f"Stopped {c.name}",
            guard=lambda c: (
                f"{c.name} is not running" if "Up" not in c.status else None
            ),
        )

    def action_start_container(self) -> None:
        self._run_on_focused_container(
            command=self.docker.start_container,
            success_msg=lambda c: f"Started {c.name}",
            guard=lambda c: (
                f"{c.name} is already running" if "Up" in c.status else None
            ),
        )

    def action_restart_container(self) -> None:
        self._run_on_focused_container(
            command=self.docker.restart_container,
            success_msg=lambda c: f"Restarted {c.name}",
        )

    def action_exec_container(self) -> None:
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return
        if "Up" not in c.status:
            self.notify(f"{c.name} is not running", severity="warning")
            return
        with self.suspend():
            subprocess.run(["docker", "exec", "-it", c.id, "sh"])

    def action_view_logs(self) -> None:
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        log_pane.load(c.id, c.name, self.docker.fetch_logs(c.id))
        self.query_one(f"#{DETAIL_PANE_ID}", DetailPane).display = False
        log_pane.display = True

    def action_close_logs(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.stop_follow()
        if log_pane.has_class("expanded"):
            self._set_log_expanded(log_pane, False)
        log_pane.display = False
        self.query_one(f"#{DETAIL_PANE_ID}", DetailPane).display = True

    def action_follow_logs(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.toggle_follow()

    def action_toggle_log_expand(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        self._set_log_expanded(log_pane, not log_pane.has_class("expanded"))

    @on(LogPane.ToggleExpand)
    def on_log_pane_toggle_expand(self) -> None:
        self.action_toggle_log_expand()

    def _set_log_expanded(self, log_pane: LogPane, expanded: bool) -> None:
        self.query_one(TabbedContent).display = not expanded
        log_pane.set_expanded(expanded)


class ResourceDeletionHandler:
    """Confirmation dialogs and dispatched remove calls for all resource types."""

    def _apply_if_confirmed(
        self, confirmed: bool, command_fn, success_msg: str
    ) -> None:
        if not confirmed:
            return
        ok, err = command_fn()
        if ok:
            self.notify(success_msg)
            self.populate_tables()
        else:
            self.notify(f"Error: {err}", severity="error")

    async def action_delete(self) -> None:
        if not self.snapshot:
            return
        active = self.query_one(TabbedContent).active

        if active == TabID.CONTAINERS:
            c = self._get_focused_container()
            if c is None:
                self.notify("No container selected", severity="warning")
                return
            is_running = "Up" in c.status
            msg = (
                f"Force-remove RUNNING container '{c.name}'?"
                if is_running
                else f"Remove container '{c.name}'?"
            )
            confirmed = await self.push_screen_wait(ConfirmDialog(msg))
            self._apply_if_confirmed(
                confirmed,
                lambda: self.docker.remove_container(c.id, force=is_running),
                f"Removed container: {c.name}",
            )

        elif active == TabID.IMAGES:
            img = self._get_focused_image()
            if img is None:
                self.notify("No image selected", severity="warning")
                return
            in_use = bool(img.used_by)
            msg = (
                f"Force-remove IN-USE image '{img.repository}:{img.tag}'?"
                if in_use
                else f"Remove image '{img.repository}:{img.tag}'?"
            )
            confirmed = await self.push_screen_wait(ConfirmDialog(msg))
            self._apply_if_confirmed(
                confirmed,
                lambda: self.docker.remove_image(img.id, force=in_use),
                f"Remove image {img.repository}:{img.tag}",
            )

        elif active == TabID.VOLUMES:
            vol = self._get_focused_volume()
            if vol is None:
                self.notify("No volume selected", severity="warning")
                return
            if vol.used_by:
                self.notify(
                    f"Volume '{vol.name}' is in use — stop containers first",
                    severity="warning",
                )
                return
            confirmed = await self.push_screen_wait(
                ConfirmDialog(f"Remove volume '{vol.name}'?")
            )
            self._apply_if_confirmed(
                confirmed,
                lambda: self.docker.remove_volume(vol.name),
                f"Removed volume {vol.name}",
            )

        elif active == TabID.NETWORKS:
            net = self._get_focused_network()
            if net is None:
                self.notify("No network selected", severity="warning")
                return
            if net.name in ("bridge", "host", "none"):
                self.notify(
                    f"Cannot remove built-in network '{net.name}'",
                    severity="warning",
                )
                return
            confirmed = await self.push_screen_wait(
                ConfirmDialog(f"Remove network '{net.name}'?")
            )
            self._apply_if_confirmed(
                confirmed,
                lambda: self.docker.remove_network(net.name),
                f"Removed network {net.name}",
            )


class ResourceSearchController:
    """Opens, closes, and applies the live filter bar across all resource tabs."""

    def action_open_search(self) -> None:
        search_bar = self.query_one(f"#{SEARCH_BAR_ID}", Input)
        search_bar.display = True
        search_bar.focus()

    @on(Input.Changed, f"#{SEARCH_BAR_ID}")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    @on(Input.Submitted, f"#{SEARCH_BAR_ID}")
    def on_search_escape(self, event: Input.Submitted) -> None:
        self._close_search()

    def _close_search(self) -> None:
        search_bar = self.query_one(f"#{SEARCH_BAR_ID}", Input)
        search_bar.display = False
        search_bar.value = ""
        self._apply_filter("")

    def _apply_filter(self, query: str) -> None:
        if not self.snapshot:
            return

        q = query.lower()
        active = self.query_one(TabbedContent).active

        if active == TabID.CONTAINERS:
            filtered = [
                c
                for c in self.snapshot.containers
                if q in c.name.lower()
                or q in c.image_name.lower()
                or q in c.status.lower()
            ]
            table = self.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.clear(columns=False)
            self._populate_container_table(table, filtered)

        elif active == TabID.IMAGES:
            filtered = [
                i
                for i in self.snapshot.images
                if q in (i.repository or "").lower() or q in (i.tag or "").lower()
            ]
            table = self.query_one(f"#{TableID.IMAGES}", DataTable)
            table.clear(columns=False)
            self._populate_image_table(table, filtered)

        elif active == TabID.VOLUMES:
            filtered = [
                v
                for v in self.snapshot.volumes
                if q in v.name.lower() or q in v.driver.lower()
            ]
            table = self.query_one(f"#{TableID.VOLUMES}", DataTable)
            table.clear(columns=False)
            self._populate_volume_table(table, filtered)

        elif active == TabID.NETWORKS:
            filtered = [
                n
                for n in self.snapshot.networks
                if q in n.name.lower() or q in n.driver.lower()
            ]
            table = self.query_one(f"#{TableID.NETWORKS}", DataTable)
            table.clear(columns=False)
            self._populate_network_table(table, filtered)


class DockSurfApp(
    TableRenderer,
    SnapshotManager,
    ResourceFocusResolver,
    DetailPaneRenderer,
    ContainerActionHandler,
    ResourceDeletionHandler,
    ResourceSearchController,
    App,
):
    snapshot: DockerSnapshot | None = None

    docker: DockerClient

    BINDINGS = [
        ("?", "help", "Help"),
        ("r", "refresh", "Refresh"),
        ("/", "open_search", "Search"),
        ("q", "quit", "Quit"),
        ("s", "stop_container", "Stop"),
        ("S", "start_container", "Start"),
        ("x", "restart_container", "Restart"),
        ("e", "exec_container", "Exec"),
        ("d", "delete", "Delete"),
        ("l", "view_logs", "Logs"),
        ("L", "close_logs", "Close Logs"),
        ("f", "follow_logs", "Follow"),
        ("z", "toggle_log_expand", "Expand Logs"),
    ]
    CSS_PATH = "app.tcss"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id=MAIN_CONTAINER_ID):
            with TabbedContent():
                with TabPane("Containers", id=TabID.CONTAINERS):
                    yield ContainerTable(id=TableID.CONTAINERS)
                with TabPane("Images", id=TabID.IMAGES):
                    yield DataTable(id=TableID.IMAGES)
                with TabPane("Volumes", id=TabID.VOLUMES):
                    yield DataTable(id=TableID.VOLUMES)
                with TabPane("Networks", id=TabID.NETWORKS):
                    yield DataTable(id=TableID.NETWORKS)
            yield DetailPane(id=DETAIL_PANE_ID)
            yield LogPane(id=LOG_PANE_ID)
        yield LoadingIndicator(id=REFRESH_LOADING_ID)
        yield SearchBar(placeholder="🔍 Filter...", id=SEARCH_BAR_ID)
        yield StatusBar(id=STATUS_BAR_ID)
        yield Footer()

    def on_mount(self) -> None:
        self.docker = DockerClient()
        if not self.docker.is_connected:
            self.notify("Could not connect to docker daemon", severity="error")
        self.setup_tables()
        self.start_refresh()

    def action_refresh(self) -> None:
        self.start_refresh()

    @on(DataTable.RowHighlighted)
    def update_details(self, event: DataTable.RowHighlighted) -> None:
        if not self.snapshot:
            return
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        table_id = event.control.id
        try:
            if table_id == TableID.CONTAINERS:
                self._show_container_details(pane, event.cursor_row)
            elif table_id == TableID.IMAGES:
                self._show_image_details(pane, event.cursor_row)
            elif table_id == TableID.VOLUMES:
                self._show_volume_details(pane, event.cursor_row)
            elif table_id == TableID.NETWORKS:
                self._show_network_details(pane, event.cursor_row)
        except IndexError:
            pane.clear_details()

    @on(TabbedContent.TabActivated)
    def clear_on_tab_switch(self) -> None:
        self.query_one(f"#{DETAIL_PANE_ID}", DetailPane).clear_details()

    @on(Input.Changed, f"#{SEARCH_BAR_ID}")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    @on(Input.Submitted, f"#{SEARCH_BAR_ID}")
    def on_search_escape(self, event: Input.Submitted) -> None:
        self._close_search()

    def action_help(self) -> None:
        help_data = [
            ("q", "quit", "Quit"),
            ("r", "refresh", "Refresh"),
            ("/", "open_search", "Search"),
            ("?", "help", "Help"),
            ("d", "delete", "Delete selected resource"),
            ("s", "stop_container", "Stop container"),
            ("S", "start_container", "Start container"),
            ("x", "restart_container", "Restart container"),
            ("e", "exec_container", "Exec shell in container"),
            ("l", "view_logs", "Open log viewer"),
            ("L", "close_logs", "Close log viewer"),
            ("f", "follow_logs", "Toggle live log streaming"),
            ("z", "toggle_log_expand", "Expand / collapse log pane"),
        ]
        self.push_screen(HelpScreen(help_data))


def main():
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    DockSurfApp().run()


if __name__ == "__main__":
    main()
