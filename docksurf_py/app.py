from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, TabbedContent, TabPane

from docksurf_py.docker import fetch_snapshot


class DockSurfApp(App):
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh")]

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Containers", id="tab-containers"):
                yield DataTable(id="table-containers")
            with TabPane("Images", id="tab-images"):
                yield DataTable(id="table-images")
            with TabPane("Volumes", id="tab-volumes"):
                yield DataTable(id="table-volumes")
            with TabPane("Networks", id="tab-networks"):
                yield DataTable(id="table-networks")
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

        snap = fetch_snapshot()

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


def main():
    app = DockSurfApp()
    app.run()


if __name__ == "__main__":
    main()
