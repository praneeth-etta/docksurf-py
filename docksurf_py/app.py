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

from docksurf_py.docker import (
    Container,
    DockerSnapshot,
    Image,
    Network,
    Volume,
    fetch_logs,
    fetch_snapshot,
    format_relative_time,
    remove_container,
    remove_image,
    remove_network,
    remove_volume,
    restart_container,
    start_container,
    stop_container,
)
from docksurf_py.widgets import ConfirmDialog, DetailPane, LogPane, SearchBar

T = TypeVar("T")


def _status_markup(status: str) -> str:
    lower = status.lower()
    if "up" in lower or "running" in lower:
        return f"[green]{status}[/]"
    if "exited" in lower or "dead" in lower:
        return f"[red]{status}[/]"
    return f"[yellow]{status}[/]"


class DockSurfApp(App):
    snapshot: DockerSnapshot | None = None
    _refresh_in_progress = False
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("l", "view_logs", "Logs"),
        ("L", "close_logs", "Close Logs"),
        ("f", "follow_logs", "Follow"),
        ("z", "toggle_log_expand", "Expand Logs"),
        ("e", "exec_container", "Exec"),
        ("s", "stop_container", "Stop"),
        ("S", "start_container", "Start"),
        ("x", "restart_container", "Restart"),
        ("d", "delete", "Delete"),
        ("/", "open_search", "Search"),
    ]
    CSS_PATH = "app.tcss"
    TABLE_COLUMNS = {
        "table-containers": (
            "Name",
            "Image",
            "Status",
        ),
        "table-images": (
            "Repository",
            "Tag",
            "Size",
            "Status",
        ),
        "table-volumes": (
            "Name",
            "Driver",
            "Status",
        ),
        "table-networks": (
            "Name",
            "Driver",
            "Scope",
        ),
    }
    _TAB_RESOURCES = {
        "tab-containers": lambda s: s.containers,
        "tab-images": lambda s: s.images,
        "tab-volumes": lambda s: s.volumes,
        "tab-networks": lambda s: s.networks,
    }
    _CONTAINER_TAB_HINT = "Switch to the Containers tab and select a container"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-container"):
            with TabbedContent():
                with TabPane("Containers", id="tab-containers"):
                    yield DataTable(id="table-containers")
                with TabPane("Images", id="tab-images"):
                    yield DataTable(id="table-images")
                with TabPane("Volumes", id="tab-volumes"):
                    yield DataTable(id="table-volumes")
                with TabPane("Networks", id="tab-networks"):
                    yield DataTable(id="table-networks")
            yield DetailPane(
                "Select an item on the left to view details...", id="detail-pane"
            )
            yield LogPane(id="log-pane")
        yield LoadingIndicator(id="refresh-loading")
        yield SearchBar(placeholder="🔍 Filter...", id="search-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.setup_tables()
        self.start_refresh()

    def action_refresh(self) -> None:
        self.start_refresh()

    def start_refresh(self) -> None:
        """Start one snapshot refresh and expose its progress in the UI."""
        if self._refresh_in_progress:
            return

        self._refresh_in_progress = True
        self.query_one("#refresh-loading", LoadingIndicator).display = True
        self.populate_tables()

    def setup_tables(self) -> None:
        for table_id, columns in self.TABLE_COLUMNS.items():
            table = self.query_one(f"#{table_id}", DataTable)
            table.add_columns(*columns)
            table.cursor_type = "row"

    def _populate_container_table(self, table: DataTable) -> None:
        for c in self.snapshot.containers:
            table.add_row(c.name, c.image_name, _status_markup(c.status))

    def _populate_image_table(self, table: DataTable) -> None:
        for i in self.snapshot.images:
            if i.used_by:
                status = "[green]In Use[/]"
            elif i.is_dangling:
                status = "[red]Dangling[/]"
            else:
                status = "[yellow]Unused[/]"
            table.add_row(i.repository, i.tag, i.size, status)

    def _populate_volume_table(self, table: DataTable) -> None:
        for v in self.snapshot.volumes:
            status = "[green]In Use[/]" if v.used_by else "[yellow]Orphaned[/]"
            name = v.name[:20] + "..." if len(v.name) > 20 else v.name
            table.add_row(name, v.driver, status)

    def _populate_network_table(self, table: DataTable) -> None:
        for n in self.snapshot.networks:
            table.add_row(n.name, n.driver, n.scope)

    def _apply_snapshot(self, snapshot: DockerSnapshot) -> None:
        self.snapshot = snapshot

        for table_id in self.TABLE_COLUMNS:
            self.query_one(f"#{table_id}", DataTable).clear(columns=False)

        self._populate_container_table(self.query_one("#table-containers", DataTable))
        self._populate_image_table(self.query_one("#table-images", DataTable))
        self._populate_volume_table(self.query_one("#table-volumes", DataTable))
        self._populate_network_table(self.query_one("#table-networks", DataTable))

    @work(thread=True)
    def populate_tables(self) -> None:
        try:
            snapshot = fetch_snapshot()
        except Exception as exc:
            self.call_from_thread(self._finish_refresh, None, str(exc))
        else:
            self.call_from_thread(self._finish_refresh, snapshot, None)

    def _finish_refresh(
        self, snapshot: DockerSnapshot | None, error: str | None
    ) -> None:
        """Apply a worker result and restore the idle refresh state."""
        try:
            if snapshot is not None:
                self._apply_snapshot(snapshot)
            elif error:
                self.notify(f"Refresh failed: {error}", severity="error")
        finally:
            self._refresh_in_progress = False
            self.query_one("#refresh-loading", LoadingIndicator).display = False

    @on(TabbedContent.TabActivated)
    def clear_on_tab_switch(self) -> None:
        pane = self.query_one("#detail-pane", DetailPane)
        pane.clear_details()

    # Detail pane renderers

    def _show_container_details(self, pane: DetailPane, row: int) -> None:
        c = self.snapshot.containers[row]
        status_color = (
            "[green]" if "Up" in c.status or "running" in c.status else "[red]"
        )
        details = {
            "ID": c.id,
            "Image": c.image_name,
            "Image SHA": (c.image_id[:24] + "...")
            if len(c.image_id) > 24
            else c.image_id,
            "Status": f"{status_color}{c.status}[/]",
            "Created": format_relative_time(c.created),
            "Ports": c.ports if c.ports else "None",
            "Networks": "\n".join(c.networks) if c.networks else "None",
            "Mounts": "\n".join(c.mounts) if c.mounts else "None",
        }
        pane.update_details(f"Container: {c.name}", details)

    def _show_image_details(self, pane: DetailPane, row: int) -> None:
        image = self.snapshot.images[row]
        details = {
            "ID": (image.id[:24] + "...")
            if image.id and len(image.id) > 24
            else (image.id or "N/A"),
            "Size": image.size or "N/A",
            "Created": format_relative_time(image.created),
            "Architecture": image.architecture or "N/A",
            "Used By": "\n".join(image.used_by) if image.used_by else "None",
        }
        pane.update_details(f"Image: {image.repository}:{image.tag}", details)

    def _show_volume_details(self, pane: DetailPane, row: int) -> None:
        volume = self.snapshot.volumes[row]
        details = {
            "Mountpoint": volume.mountpoint,
            "Labels": volume.labels if volume.labels else "None",
            "Used By": (
                "\n".join(volume.used_by)
                if volume.used_by
                else "[yellow]Orphaned (safe to delete)[/yellow]"
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

        pane = self.query_one("#detail-pane", DetailPane)
        table_id = event.control.id

        try:
            if table_id == "table-containers":
                self._show_container_details(pane, event.cursor_row)
            elif table_id == "table-images":
                self._show_image_details(pane, event.cursor_row)
            elif table_id == "table-volumes":
                self._show_volume_details(pane, event.cursor_row)
            elif table_id == "table-networks":
                self._show_network_details(pane, event.cursor_row)
        except IndexError:
            pane.clear_details()

    # Focused-resource helpers

    def _get_focused_resource(self, tab_id: str, resources: list[T]) -> T | None:
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

    def _get_focused(self, tab_id: str):
        if not self.snapshot:
            return None
        resources = self._TAB_RESOURCES[tab_id](self.snapshot)
        return self._get_focused_resource(tab_id, resources)

    def _get_focused_container(self) -> Container | None:
        return self._get_focused("tab-containers")

    def _get_focused_image(self) -> Image | None:
        return self._get_focused("tab-images")

    def _get_focused_volume(self) -> Volume | None:
        return self._get_focused("tab-volumes")

    def _get_focused_network(self) -> Network | None:
        return self._get_focused("tab-networks")

    # Container actions

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
            command=stop_container,
            success_msg=lambda c: f"Stopped {c.name}",
            guard=lambda c: (
                f"{c.name} is not running" if "Up" not in c.status else None
            ),
        )

    def action_start_container(self) -> None:
        self._run_on_focused_container(
            command=start_container,
            success_msg=lambda c: f"Started {c.name}",
            guard=lambda c: (
                f"{c.name} is already running" if "Up" in c.status else None
            ),
        )

    def action_restart_container(self) -> None:
        self._run_on_focused_container(
            command=restart_container,
            success_msg=lambda c: f"Restarted {c.name}",
        )

    def action_view_logs(self) -> None:
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return
        log_pane = self.query_one("#log-pane", LogPane)
        log_pane.load(c.id, c.name, fetch_logs(c.id))
        self.query_one("#detail-pane", DetailPane).display = False
        log_pane.display = True

    def action_close_logs(self) -> None:
        log_pane = self.query_one("#log-pane", LogPane)
        if not log_pane.display:
            return
        log_pane.stop_follow()
        # Collapse before hiding so state is clean on next open
        if log_pane.has_class("expanded"):
            self._set_log_expanded(log_pane, False)
        log_pane.display = False
        self.query_one("#detail-pane", DetailPane).display = True

    def action_follow_logs(self) -> None:
        log_pane = self.query_one("#log-pane", LogPane)
        if not log_pane.display:
            return
        log_pane.toggle_follow()

    def action_toggle_log_expand(self) -> None:
        log_pane = self.query_one("#log-pane", LogPane)
        if not log_pane.display:
            return
        self._set_log_expanded(log_pane, not log_pane.has_class("expanded"))

    @on(LogPane.ToggleExpand)
    def on_log_pane_toggle_expand(self) -> None:
        self.action_toggle_log_expand()

    def _set_log_expanded(self, log_pane: LogPane, expanded: bool) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.display = not expanded
        log_pane.set_expanded(expanded)

    def action_exec_container(self) -> None:
        c = self._get_focused_container()
        if c is None:
            self.notify(
                self._CONTAINER_TAB_HINT,
                severity="warning",
            )
            return
        if "Up" not in c.status:
            self.notify(f"{c.name} is not running", severity="warning")
            return
        with self.suspend():
            subprocess.run(["docker", "exec", "-it", c.id, "sh"])

    # Delete action (context-sensitive across all tabs)

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

        tabbed = self.query_one(TabbedContent)
        active = tabbed.active

        if active == "tab-containers":
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
                lambda: remove_container(c.id, force=is_running),
                f"Removed container: {c.name}",
            )

        elif active == "tab-images":
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
                lambda: remove_image(img.id, force=in_use),
                f"Remove image {img.repository}:{img.tag}",
            )

        elif active == "tab-volumes":
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
                lambda: remove_volume(vol.name),
                f"Removed volume {vol.name}",
            )

        elif active == "tab-networks":
            net = self._get_focused_network()
            if net is None:
                self.notify("No network selected", severity="warning")
                return
            if net.name in ("bridge", "host", "none"):
                self.notify(
                    f"Cannot remove built-in network '{net.name}'", severity="warning"
                )
                return
            confirmed = await self.push_screen_wait(
                ConfirmDialog(f"Remove network '{net.name}'?")
            )
            self._apply_if_confirmed(
                confirmed,
                lambda: remove_network(net.name),
                f"Removed network {net.name}",
            )

    # Search Filters

    def action_open_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        search_bar.display = True
        search_bar.focus()

    @on(Input.Changed, "#search-bar")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    @on(Input.Submitted, "#search-bar")
    def on_search_escape(self, event: Input.Submitted) -> None:
        self._close_search()

    def _close_search(self) -> None:
        search_bar = self.query_one("#search-bar", Input)
        search_bar.display = False
        search_bar.value = ""
        self._apply_filter("")

    def _apply_filter(self, query: str) -> None:
        if not self.snapshot:
            return

        q = query.lower()
        tabbed = self.query_one(TabbedContent)
        active = tabbed.active

        if active == "tab-containers":
            table = self.query_one("#table-containers", DataTable)
            table.clear(columns=False)
            filtered = [
                c
                for c in self.snapshot.containers
                if q in c.name.lower()
                or q in c.image_name.lower()
                or q in c.status.lower()
            ]
            for c in filtered:
                table.add_row(c.name, c.image_name, _status_markup(c.status))

        elif active == "tab-images":
            table = self.query_one("#table-images", DataTable)
            table.clear(columns=False)
            filtered = [
                i
                for i in self.snapshot.images
                if q in (i.repository or "").lower() or q in (i.tag or "").lower()
            ]
            for i in filtered:
                status = (
                    "[green]In Use[/]"
                    if i.used_by
                    else "[red]Dangling[/]"
                    if i.is_dangling
                    else "[yellow]Unused[/]"
                )
                table.add_row(i.repository, i.tag, i.size, status)

        elif active == "tab-volumes":
            table = self.query_one("#table-volumes", DataTable)
            table.clear(columns=False)
            filtered = [
                v
                for v in self.snapshot.volumes
                if q in v.name.lower() or q in v.driver.lower()
            ]
            for v in filtered:
                status = "[green]In Use[/]" if v.used_by else "[yellow]Orphaned[/]"
                name = v.name[:20] + "..." if len(v.name) > 20 else v.name
                table.add_row(name, v.driver, status)

        elif active == "tab-networks":
            table = self.query_one("#table-networks", DataTable)
            table.clear(columns=False)
            filtered = [
                n
                for n in self.snapshot.networks
                if q in n.name.lower() or q in n.driver.lower()
            ]
            for n in filtered:
                table.add_row(n.name, n.driver, n.scope)


def main():
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    app = DockSurfApp()
    app.run()


if __name__ == "__main__":
    main()
