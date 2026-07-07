"""ResourceFocusResolver — maps the active tab + cursor row to a resource object."""

from typing import Any

from textual.widgets import DataTable, TabbedContent

from docksurf_py.constants import TabID
from docksurf_py.models import ComposeProject, Container
from docksurf_py.renderer.common import _Base


class ResourceFocusResolver(_Base):
    """Maps the active tab + cursor row to the concrete resource object."""

    def _get_focused_resource(self, tab_id: TabID) -> Any | None:
        if not self.snapshot:
            return None
        tabbed = self.query_one(TabbedContent)
        if tabbed.active != tab_id:
            return None
        entry = self._resource_registry.get(tab_id)
        if entry is None:
            return None
        current_list = self._current.get(tab_id, [])
        table = self.query_one(f"#{entry.table_id}", DataTable)
        row = table.cursor_row
        if not current_list or row is None or row >= len(current_list):
            return None
        return current_list[row]

    def _get_focused_container(self) -> Container | None:
        item = self._get_focused_resource(TabID.CONTAINERS)
        return item if isinstance(item, Container) else None

    def _focused_is_project_header(self) -> bool:
        """True when the cursor sits on a Compose project header row."""
        item = self._get_focused_resource(TabID.CONTAINERS)
        return isinstance(item, ComposeProject)

    def _get_focused_project(self) -> ComposeProject | None:
        """Resolve the Compose project for the focused row.

        Works whether the cursor is on a project header or on one of its
        service rows. Members are rebuilt from the full snapshot (not the
        possibly-filtered/collapsed view) so project-wide actions cover every
        service, not just the visible ones.
        """
        item = self._get_focused_resource(TabID.CONTAINERS)
        if isinstance(item, ComposeProject):
            name = item.name
        elif isinstance(item, Container) and item.is_compose:
            name = item.compose_project
        else:
            return None

        if not self.snapshot:
            return None
        members = [c for c in self.snapshot.containers if c.compose_project == name]
        if not members:
            return None
        first = members[0]
        return ComposeProject(
            name=name,
            containers=members,
            config_files=first.compose_config_files,
            working_dir=first.compose_working_dir,
        )
