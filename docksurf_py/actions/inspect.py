"""InspectHandler — the `docker inspect` escape hatch for any resource."""

import json

from rich.markup import escape
from textual import work
from textual.widgets import TabbedContent

from docksurf_py.actions.common import _Base, _display_name
from docksurf_py.models import ComposeProject
from docksurf_py.widgets import InspectScreen


class InspectHandler(_Base):
    """The `docker inspect` escape hatch — full raw JSON for any resource on
    any tab, in a scrollable/searchable modal (see `InspectScreen`)."""

    def action_inspect(self) -> None:
        active = self.query_one(TabbedContent).active
        item = self._get_focused_resource(active)
        if item is None:
            self.notify("Nothing selected to inspect", severity="warning")
            return
        if isinstance(item, ComposeProject):
            self.notify(
                "Select a container within the project to inspect",
                severity="warning",
            )
            return
        key = self._row_key(item)
        if key is None:
            self.notify("Nothing selected to inspect", severity="warning")
            return
        kind, ref = key
        self._execute_inspect(kind, ref, _display_name(item))

    @work(thread=True)
    def _execute_inspect(self, kind: str, ref: str, name: str) -> None:
        attrs = self.docker.inspect_resource(kind, ref)
        if attrs is None:
            self.call_from_thread(
                self.notify,
                f"Could not inspect {kind} {escape(name)}",
                severity="error",
            )
            return
        text = json.dumps(attrs, indent=2, default=str)
        self.call_from_thread(
            self.push_screen, InspectScreen(f"Inspect — {kind}: {name}", text)
        )
