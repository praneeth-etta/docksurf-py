"""
search.py — Live filter bar mixin.

ResourceSearchController is a mixin class that composes into DockSurfApp.
"""

from textual import on
from textual.widgets import DataTable, Input, TabbedContent

from docksurf_py.constants import SEARCH_BAR_ID, TabID, TableID


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
