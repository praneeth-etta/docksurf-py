"""SelectionHandler — multi-select marking and the shared bulk-execution machinery."""

import logging
from typing import Any, Callable

from rich.markup import escape
from textual import work
from textual.coordinate import Coordinate
from textual.widgets import DataTable, TabbedContent

from docksurf_py.actions.common import _Base
from docksurf_py.constants import MARK_GLYPH, TabID
from docksurf_py.models import CommandResult

logger = logging.getLogger(__name__)


class SelectionHandler(_Base):
    """Multi-select marking and the shared bulk-execution machinery.

    Marks are keyed by `_row_key` (kind, id) tuples in `self._marked[tab]`
    (per-tab sets, initialized in `TableRenderer.setup_tables`) so they
    survive refresh/filter/collapse — `SnapshotManager._apply_snapshot` prunes
    vanished keys and every populate method re-renders the mark glyph on each
    repaint. `ContainerActionHandler`/`ResourceDeletionHandler` build the
    per-domain job lists (what to run, and its guard/plan logic); this mixin
    only knows how to toggle a mark and run a batch of jobs sequentially.
    """

    def action_toggle_mark(self) -> None:
        active = self.query_one(TabbedContent).active
        if self._resource_registry.get(active) is None:
            return
        # A project header has no mark of its own — space still collapses it.
        if self._focused_is_project_header():
            self.action_toggle_group()
            return
        item = self._get_focused_resource(active)
        if item is None:
            return
        key = self._row_key(item)
        if key is None:
            return

        table_id = self._resource_registry[active].table_id
        table = self.query_one(f"#{table_id}", DataTable)
        row = table.cursor_row
        if row is None:
            return
        marked = self._marked[active]
        if key in marked:
            marked.discard(key)
        else:
            marked.add(key)
        table.update_cell_at(Coordinate(row, 0), MARK_GLYPH if key in marked else "")

        # Advance the cursor — mark-and-move, k9s-style rapid selection.
        if row + 1 < table.row_count:
            table.move_cursor(row=row + 1)

    def action_clear_marks(self) -> None:
        active = self.query_one(TabbedContent).active
        if not self._marked.get(active):
            return
        self._marked[active].clear()
        self._rerender_active_table()

    def _marked_items(self, tab_id: TabID) -> list[Any]:
        """Resolve a tab's marked keys back to live objects from the snapshot."""
        if not self.snapshot:
            return []
        entry = self._resource_registry.get(tab_id)
        keys = self._marked.get(tab_id)
        if entry is None or not keys:
            return []
        return [
            item
            for item in entry.snapshot_items(self.snapshot)
            if self._row_key(item) in keys
        ]

    def _run_bulk(
        self,
        tab_id: TabID,
        verb: str,
        jobs: list[tuple[tuple[str, str], str, Callable[[], CommandResult]]],
    ) -> None:
        """Protocol-facing entry point for `ContainerActionHandler`/
        `ResourceDeletionHandler` — dispatches to the threaded worker below.

        Kept separate from `_execute_bulk` because `@work` gives a decorated
        method a Textual-generated wrapper signature at runtime, which mypy
        rejects as an incompatible override of the same name declared in
        `AppContext`. This thin, undecorated method is what the Protocol
        declares instead.
        """
        self._execute_bulk(tab_id, verb, jobs)

    @work(thread=True)
    def _execute_bulk(
        self,
        tab_id: TabID,
        verb: str,
        jobs: list[tuple[tuple[str, str], str, Callable[[], CommandResult]]],
    ) -> None:
        """Run each (key, name, command) job sequentially and summarize.

        Sequential, not parallel — these are direct Docker API/CLI calls, and
        running them one at a time keeps error attribution unambiguous (which
        job failed) without adding a thread pool for what's normally a
        handful of items.
        """
        ok_count = 0
        failures: list[tuple[str, str]] = []
        executed_keys: set[tuple[str, str]] = set()
        for key, name, command in jobs:
            result = command()
            executed_keys.add(key)
            if result.ok:
                ok_count += 1
            else:
                failures.append((name, result.message))
        self.call_from_thread(
            self._handle_bulk_result,
            tab_id,
            executed_keys,
            verb,
            ok_count,
            len(jobs),
            failures,
        )

    def _handle_bulk_result(
        self,
        tab_id: TabID,
        executed_keys: set[tuple[str, str]],
        verb: str,
        ok_count: int,
        total: int,
        failures: list[tuple[str, str]],
    ) -> None:
        if failures:
            shown = ", ".join(f"{escape(n)} ({m})" for n, m in failures[:3])
            more = f", +{len(failures) - 3} more" if len(failures) > 3 else ""
            logger.warning(
                "Bulk %s: %d/%d failed — %s%s", verb, len(failures), total, shown, more
            )
            self.notify(
                f"{verb} {ok_count}/{total} — failed: {shown}{more}",
                severity="error",
            )
        else:
            logger.info("Bulk %s: %d/%d succeeded", verb, ok_count, total)
            self.notify(f"{verb} {ok_count}/{total}")
        self._marked[tab_id] -= executed_keys
        self.start_refresh()
