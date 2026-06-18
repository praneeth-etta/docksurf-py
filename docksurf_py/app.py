from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, TabbedContent, TabPane

from docksurf_py.docker import DockerSnapshot, fetch_snapshot
from docksurf_py.widgets import DetailPane


class DockSurfApp(App):
    snapshot: DockerSnapshot | None = None
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh")]
    CSS = """
        #main-container {
            height: 100%;
        }
        TabbedContent {
            width: 40%;
            border-right: solid $primary-background-lighten-2;
        }
        #detail-pane {
            width: 60%;
            height: 100%;
            padding: 1 2;
        }
    """
    TABLE_COLUMNS = {
        "table-containers": (
            "Name",
            "Image",
            "Status",
            "ID",
            "Network",
            "Mounts",
        ),
        "table-images": (
            "Repository",
            "Tag",
            "Size",
            "Dangling",
            "Used By",
        ),
        "table-volumes": (
            "Name",
            "Driver",
            "Used By",
        ),
        "table-networks": (
            "Name",
            "Driver",
            "Scope",
            "Used By",
        ),
    }

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
        yield Footer()

    def on_mount(self) -> None:
        self.setup_tables()
        self.populate_tables()

    def action_refresh(self) -> None:
        self.populate_tables()

    def setup_tables(self) -> None:
        for table_id, columns in self.TABLE_COLUMNS.items():
            table = self.query_one(f"#{table_id}", DataTable)
            table.add_columns(*columns)
            table.cursor_type = "row"

    def _populate_container_table(self, table: DataTable) -> None:
        for c in self.snapshot.containers:
            table.add_row(
                c.name,
                c.image,
                c.status,
                c.id[:12],
                str(len(c.networks)),
                str(len(c.mounts)),
            )

    def _populate_image_table(self, table: DataTable) -> None:
        for i in self.snapshot.images:
            used_str = ", ".join(i.used_by) if i.used_by else "None"
            table.add_row(i.repository, i.tag, i.size, str(i.is_dangling), used_str)

    def _populate_volume_table(self, table: DataTable) -> None:
        for v in self.snapshot.volumes:
            used_str = ", ".join(v.used_by) if v.used_by else "Orphaned"
            table.add_row(
                v.name[:20] + "..." if len(v.name) > 20 else v.name, v.driver, used_str
            )

    def _populate_network_table(self, table: DataTable) -> None:
        for n in self.snapshot.networks:
            used_str = ", ".join(n.used_by) if n.used_by else "None"
            table.add_row(n.name, n.driver, n.scope, used_str)

    def populate_tables(self) -> None:
        table_con = self.query_one("#table-containers", DataTable)
        table_img = self.query_one("#table-images", DataTable)
        table_vol = self.query_one("#table-volumes", DataTable)
        table_net = self.query_one("#table-networks", DataTable)

        for table_id in self.TABLE_COLUMNS:
            self.query_one(f"#{table_id}", DataTable).clear(columns=False)

        self.snapshot = fetch_snapshot()

        self._populate_container_table(table_con)
        self._populate_image_table(table_img)
        self._populate_volume_table(table_vol)
        self._populate_network_table(table_net)

    @on(TabbedContent.TabActivated)
    def clear_on_tab_switch(self) -> None:
        pane = self.query_one("#detail-pane", DetailPane)
        pane.clear_details()

    def _show_container_details(
        self,
        pane: DetailPane,
        row: int,
    ) -> None:
        c = self.snapshot.containers[row]

        status_color = (
            "[green]" if "Up" in c.status or "running" in c.status else "[red]"
        )

        details = {
            "ID": c.id,
            "Image": c.image,
            "Status": f"{status_color}{c.status}[/]",
            "Networks": "\n".join(c.networks) if c.networks else "None",
            "Mounts": "\n".join(c.mounts) if c.mounts else "None",
        }
        pane.update_details(f"Container: {c.name}", details)

    def _show_image_details(
        self,
        pane: DetailPane,
        row: int,
    ) -> None:
        image = self.snapshot.images[row]

        details = {
            "ID": image.id,
            "Size": image.size,
            "Dangling": "[red]True[/red]"
            if image.is_dangling
            else "[green]False[/green]",
            "Used By": "\n".join(image.used_by) if image.used_by else "None",
        }
        pane.update_details(f"Image: {image.repository}:{image.tag}", details)

    def _show_volume_details(
        self,
        pane: DetailPane,
        row: int,
    ) -> None:
        volume = self.snapshot.volumes[row]

        details = {
            "Driver": volume.driver,
            "Mountpoint": volume.mountpoint,
            "Used By": "\n".join(volume.used_by)
            if volume.used_by
            else "[yellow]Orphaned (Safe to delete)[/yellow]",
        }
        pane.update_details(f"Volume: {volume.name}", details)

    def _show_network_details(
        self,
        pane: DetailPane,
        row: int,
    ) -> None:
        network = self.snapshot.networks[row]

        details = {
            "ID": network.id,
            "Scope": network.scope,
            "Driver": network.driver,
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
            # Tell the widget to render the empty state
            pane.clear_details()


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
