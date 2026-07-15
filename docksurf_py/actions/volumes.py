"""VolumeActionHandler — create volume, on-demand per-volume size on disk."""

from textual import work
from textual.widgets import DataTable, TabbedContent

from docksurf_py.actions.common import _Base
from docksurf_py.constants import DETAIL_PANE_ID, TabID
from docksurf_py.widgets import DetailPane, PromptField, PromptScreen


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse a `k=v,k2=v2` prompt string into a labels dict (blank pairs skipped)."""
    labels: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        if key:
            labels[key] = value.strip()
    return labels


class VolumeActionHandler(_Base):
    """Volume-tab actions: create, and on-demand per-volume size on disk."""

    _VOLUME_TAB_HINT = "Switch to the Volumes tab"

    def _on_volumes_tab(self) -> bool:
        return self.query_one(TabbedContent).active == TabID.VOLUMES

    @work
    async def action_create_volume(self) -> None:
        if not self._on_volumes_tab():
            self.notify(self._VOLUME_TAB_HINT, severity="warning")
            return
        values = await self.push_screen_wait(
            PromptScreen(
                "Create volume",
                [
                    PromptField("Name", placeholder="leave blank for anonymous"),
                    PromptField("Driver", value="local"),
                    PromptField("Labels (k=v,k=v)"),
                ],
            )
        )
        if values is None:
            return
        name, driver, labels_raw = values
        self._execute_create_volume(
            name.strip(), driver.strip() or "local", _parse_labels(labels_raw)
        )

    @work(thread=True)
    def _execute_create_volume(
        self, name: str, driver: str, labels: dict[str, str]
    ) -> None:
        result = self.docker.create_volume(name, driver, labels)
        self.call_from_thread(self._handle_write_result, result)

    def action_volume_size(self) -> None:
        if not self._on_volumes_tab():
            self.notify(self._VOLUME_TAB_HINT, severity="warning")
            return
        self.notify("Computing volume sizes…")
        self._execute_volume_sizes()

    @work(thread=True)
    def _execute_volume_sizes(self) -> None:
        sizes = self.docker.volume_sizes()
        self.call_from_thread(self._apply_volume_sizes, sizes)

    def _apply_volume_sizes(self, sizes: dict[str, int]) -> None:
        self._volume_sizes = sizes
        if self.query_one(TabbedContent).active != TabID.VOLUMES:
            return
        # Re-render the table (Size column) and the focused volume's detail
        # pane (Size row) now that sizes are known.
        self._rerender_active_table()
        entry = self._resource_registry[TabID.VOLUMES]
        table = self.query_one(f"#{entry.table_id}", DataTable)
        row = table.cursor_row
        if row is not None:
            pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
            try:
                entry.show_details(pane, row)
            except IndexError:
                pass
        self.notify("Volume sizes updated")
