"""
search.py — Live filter bar mixin.

ResourceSearchController is a mixin class that composes into DockSurfApp.
"""

from typing import TYPE_CHECKING

from textual.timer import Timer
from textual.widgets import DataTable, Input, TabbedContent

from docksurf_py.constants import SEARCH_BAR_ID
from docksurf_py.models import Container, Image, Network, Volume

if TYPE_CHECKING:
    from docksurf_py.app import AppContext

    _Base = AppContext
else:
    # Real runtime base is `object` — `AppContext` only exists for mypy to
    # check these mixins' bodies against; see app.py's `AppContext` docstring.
    _Base = object


def _matches_container(c: Container, q: str) -> bool:
    return q in c.name.lower() or q in c.image_name.lower() or q in c.status.lower()


def _matches_image(i: Image, q: str) -> bool:
    return q in (i.repository or "").lower() or q in (i.tag or "").lower()


def _matches_volume(v: Volume, q: str) -> bool:
    return q in v.name.lower() or q in v.driver.lower() or q in v.mountpoint.lower()


def _matches_network(n: Network, q: str) -> bool:
    return q in n.name.lower() or q in n.driver.lower() or q in n.subnet.lower()


class ResourceSearchController(_Base):
    """Opens, closes, and applies the live filter bar across all resource tabs.

    `on_search_changed`/`on_search_escape` are plain methods, not `@on`-
    decorated, even though they look like Textual message handlers: this
    class runs with a plain `object` base at runtime, and `@on` only wires
    into Textual's dispatch table for methods defined on a real message-pump
    class. The real dispatch entry points are
    `DockSurfApp._on_search_input_changed`/`_on_search_input_submitted` in
    app.py, which just forward here.
    """

    _search_filter_timer: Timer | None = None

    def action_open_search(self) -> None:
        search_bar = self.query_one(f"#{SEARCH_BAR_ID}", Input)
        search_bar.display = True
        search_bar.focus()

    def on_search_changed(self, event: Input.Changed) -> None:
        # Debounced like LogPane's filter (log_pane.py): otherwise each keystroke
        # re-populates the active tab's table.
        query = event.value
        if self._search_filter_timer is not None:
            self._search_filter_timer.stop()
        self._search_filter_timer = self.set_timer(
            0.2, lambda: self._apply_filter(query)
        )

    def on_search_escape(self, event: Input.Submitted) -> None:
        self._close_search()

    def _close_search(self) -> None:
        if self._search_filter_timer is not None:
            self._search_filter_timer.stop()
            self._search_filter_timer = None
        search_bar = self.query_one(f"#{SEARCH_BAR_ID}", Input)
        search_bar.display = False
        search_bar.value = ""
        self._apply_filter("")

    def _apply_filter(self, query: str) -> None:
        if not self.snapshot:
            return

        q = query.lower()
        active = self.query_one(TabbedContent).active
        entry = self._resource_registry.get(active)
        if entry is None:
            return

        items = entry.snapshot_items(self.snapshot)
        filtered = [item for item in items if entry.matches(item, q)]
        filtered = self._sort_items(active, entry, filtered)
        table = self.query_one(f"#{entry.table_id}", DataTable)
        table.clear(columns=False)
        entry.populate(table, filtered)
        self._update_empty_state(active, entry, filtered, query)
