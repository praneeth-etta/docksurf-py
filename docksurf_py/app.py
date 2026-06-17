from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, TabbedContent, TabPane

from docksurf_py.docker import fetch_snapshot
from docksurf_py.widgets import DetailPane


class DockSurfApp(App):
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
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh")]
    snapshot = None

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
        table_con = self.query_one("#table-containers", DataTable)
        table_con.add_columns("Name", "Image", "Status", "ID", "Network", "Mounts")
        table_con.cursor_type = "row"

        table_img = self.query_one("#table-images", DataTable)
        table_img.add_columns("Repository", "Tag", "Size", "Dangling", "Used By")
        table_img.cursor_type = "row"

        table_vol = self.query_one("#table-volumes", DataTable)
        table_vol.add_columns("Name", "Driver", "Used By")
        table_vol.cursor_type = "row"

        table_net = self.query_one("#table-networks", DataTable)
        table_net.add_columns("Name", "Driver", "Scope", "Used By")
        table_net.cursor_type = "row"

    def populate_tables(self) -> None:
        table_con = self.query_one("#table-containers", DataTable)
        table_img = self.query_one("#table-images", DataTable)
        table_vol = self.query_one("#table-volumes", DataTable)
        table_net = self.query_one("#table-networks", DataTable)

        table_con.clear(columns=False)
        table_img.clear(columns=False)
        table_vol.clear(columns=False)
        table_net.clear(columns=False)

        self.snapshot = fetch_snapshot()
        snap = self.snapshot

        for c in snap.containers:
            table_con.add_row(
                c.name,
                c.image,
                c.status,
                c.id[:12],
                str(len(c.networks)),
                str(len(c.mounts)),
            )

        for i in snap.images:
            used_str = ", ".join(i.used_by) if i.used_by else "None"
            table_img.add_row(i.repository, i.tag, i.size, str(i.is_dangling), used_str)

        for v in snap.volumes:
            used_str = ", ".join(v.used_by) if v.used_by else "Orphaned"
            table_vol.add_row(
                v.name[:20] + "..." if len(v.name) > 20 else v.name, v.driver, used_str
            )

        for n in snap.networks:
            used_str = ", ".join(n.used_by) if n.used_by else "None"
            table_net.add_row(n.name, n.driver, n.scope, used_str)

    @on(TabbedContent.TabActivated)
    def clear_on_tab_switch(self) -> None:
        pane = self.query_one("#detail-pane", DetailPane)
        pane.clear_details()

    @on(DataTable.RowHighlighted)
    def update_details(self, event: DataTable.RowHighlighted) -> None:
        if not self.snapshot:
            return

        pane = self.query_one("#detail-pane", DetailPane)
        table_id = event.control.id
        row = event.cursor_row

        try:
            if table_id == "table-containers":
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

            elif table_id == "table-images":
                i = self.snapshot.images[row]
                details = {
                    "ID": i.id,
                    "Size": i.size,
                    "Dangling": "[red]True[/red]"
                    if i.is_dangling
                    else "[green]False[/green]",
                    "Used By": "\n".join(i.used_by) if i.used_by else "None",
                }
                pane.update_details(f"Image: {i.repository}:{i.tag}", details)

            elif table_id == "table-volumes":
                v = self.snapshot.volumes[row]
                details = {
                    "Driver": v.driver,
                    "Mountpoint": v.mountpoint,
                    "Used By": "\n".join(v.used_by)
                    if v.used_by
                    else "[yellow]Orphaned (Safe to delete)[/yellow]",
                }
                pane.update_details(f"Volume: {v.name}", details)

            elif table_id == "table-networks":
                n = self.snapshot.networks[row]
                details = {
                    "ID": n.id,
                    "Scope": n.scope,
                    "Driver": n.driver,
                    "Used By": "\n".join(n.used_by) if n.used_by else "None",
                }
                pane.update_details(f"Network: {n.name}", details)

        except IndexError:
            # Tell the widget to render the empty state
            pane.clear_details()


def main():
    app = DockSurfApp()
    app.run()


if __name__ == "__main__":
    main()
