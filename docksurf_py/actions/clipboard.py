"""ClipboardHandler — copy the focused resource's ID/name/port-mapping."""

from typing import Any

from textual import work
from textual.widgets import TabbedContent

from docksurf_py.actions.common import _Base
from docksurf_py.docker import format_ports
from docksurf_py.models import ComposeProject, Container, Image, Network, Volume
from docksurf_py.widgets import ContainerPickerScreen


def _yank_fields(item: Any) -> list[tuple[str, str]]:
    """`(label, value)` pairs a resource can be copied to the clipboard as."""
    if isinstance(item, Container):
        fields = [("ID", item.id), ("Name", item.name)]
        if any(p.host_port for p in item.ports):
            fields.append(("Port mapping", format_ports(item.ports)))
        return fields
    if isinstance(item, ComposeProject):
        return [("Name", item.name)]
    if isinstance(item, Image):
        return [("ID", item.id), ("Repository:Tag", f"{item.repository}:{item.tag}")]
    if isinstance(item, Volume):
        return [("Name", item.name)]
    if isinstance(item, Network):
        return [("ID", item.id), ("Name", item.name)]
    return []


class ClipboardHandler(_Base):
    """`Y`: copy the focused resource's ID/name/port-mapping to the clipboard."""

    def action_yank(self) -> None:
        active = self.query_one(TabbedContent).active
        item = self._get_focused_resource(active)
        if item is None:
            self.notify("Nothing selected to copy", severity="warning")
            return
        fields = _yank_fields(item)
        if not fields:
            self.notify("Nothing to copy for this item", severity="warning")
            return
        if len(fields) == 1:
            self._yank(fields[0][1])
            return
        self._pick_yank_field(fields)

    @work
    async def _pick_yank_field(self, fields: list[tuple[str, str]]) -> None:
        # Keyed by label, not value — two fields (e.g. ID/Name) can share the
        # same value, and OptionList requires unique option ids.
        options = [(label, f"{label}: {value}") for label, value in fields]
        chosen_label = await self.push_screen_wait(
            ContainerPickerScreen("Copy to clipboard", options)
        )
        if chosen_label is None:
            return
        value = next((v for label, v in fields if label == chosen_label), None)
        if value is not None:
            self._yank(value)

    def _yank(self, value: str) -> None:
        self.copy_to_clipboard(value)
        self.notify(f"Copied: {value}")
