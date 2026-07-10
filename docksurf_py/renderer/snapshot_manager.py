"""SnapshotManager — background snapshot fetch and event-driven auto-refresh."""

import logging
import threading
from typing import Any

from rich.markup import escape
from textual import work
from textual.timer import Timer
from textual.widgets import DataTable, Input, LoadingIndicator, Static, TabbedContent

from docksurf_py.connection import ConnectionState, ConnectionStatus
from docksurf_py.constants import (
    CONNECTION_BANNER_ID,
    CONNECTION_INDICATOR_ID,
    DETAIL_PANE_ID,
    REFRESH_LOADING_ID,
    SEARCH_BAR_ID,
    STATUS_BAR_ID,
    TabID,
)
from docksurf_py.docker import EventStream
from docksurf_py.models import (
    ComposeProject,
    Container,
    DockerSnapshot,
    Image,
    Network,
    Volume,
)
from docksurf_py.renderer.common import _Base
from docksurf_py.widgets import ConnectionIndicator, DetailPane, StatusBar

logger = logging.getLogger(__name__)


class SnapshotManager(_Base):
    """Fetches Docker state in a background thread and commits it to the UI."""

    _refresh_in_progress = False
    _refresh_pending = False
    _event_stream: EventStream | None = None
    _event_stop: threading.Event | None = None
    _refresh_debounce: Timer | None = None
    # Tracks the last connection state we've reported so notifications are shown
    # only when the connection actually changes.
    _last_conn_status: ConnectionStatus = ConnectionStatus.NOT_CONNECTED

    # Docker event prefixes that don't change the rendered state and would only
    # trigger unnecessary refreshes. Exec events include a command after ':', so
    # matching is done on the action prefix.
    _EVENT_NOISE = frozenset(
        {
            "exec_create",
            "exec_start",
            "exec_die",
            "exec_detach",
            "top",
            "health_status",
        }
    )

    def _is_noise_event(self, action: str) -> bool:
        return action.split(":", 1)[0].strip() in self._EVENT_NOISE

    def start_refresh(self) -> None:
        if self._refresh_in_progress:
            logger.debug("Refresh skipped — already in progress")
            return
        self._refresh_in_progress = True
        logger.info("Refresh started")
        self.query_one(f"#{REFRESH_LOADING_ID}", LoadingIndicator).display = True
        self.populate_tables()

    @work(thread=True)
    def populate_tables(self) -> None:
        try:
            snapshot = self.docker.fetch_snapshot()
        except Exception as exc:
            logger.exception("Snapshot fetch failed")
            self.call_from_thread(self._finish_refresh, None, str(exc))
        else:
            logger.info(
                "Snapshot fetched — containers=%d images=%d volumes=%d networks=%d",
                len(snapshot.containers),
                len(snapshot.images),
                len(snapshot.volumes),
                len(snapshot.networks),
            )
            self.call_from_thread(self._finish_refresh, snapshot, None)

    def _apply_snapshot(self, snapshot: DockerSnapshot) -> None:
        self.snapshot: DockerSnapshot | None = snapshot

        # A fetch worker can complete while the app is tearing down (e.g. the
        # user quit mid-refresh); the widgets are gone, so committing would
        # raise NoMatches. Skip — there's nothing left to paint.
        if not self.is_running:
            return

        if not self.docker.is_connected:
            state = self.docker.connection
            logger.error(
                "Docker unavailable — status=%s context=%s host=%s",
                state.status.value,
                state.context,
                state.host,
            )
        self._maybe_notify_connection_change(self.docker.connection)

        # Remember what the user had selected so a background refresh doesn't
        # yank the cursor back to the top (and needlessly restart the stats
        # stream) on every `docker events` tick.
        active = self.query_one(TabbedContent).active
        focus_key = self._focused_row_key(active)

        # Drop marks on resources that no longer exist — checked against the
        # full snapshot (not a search-filtered view), before any repopulate.
        for tab_id, entry in self._resource_registry.items():
            live_keys = {
                key
                for item in entry.snapshot_items(snapshot)
                if (key := self._row_key(item)) is not None
            }
            self._marked[tab_id] &= live_keys

        for tab_id, entry in self._resource_registry.items():
            table = self.query_one(f"#{entry.table_id}", DataTable)
            table.clear(columns=False)
            items = self._sort_items(tab_id, entry, entry.snapshot_items(snapshot))
            entry.populate(table, items)
            self._update_empty_state(tab_id, entry, items)

        status_bar = self.query_one(f"#{STATUS_BAR_ID}", StatusBar)
        status_bar.update_stats(
            snapshot.containers,
            snapshot.images,
            snapshot.volumes,
            context=self.docker.connection.context,
        )

        search_bar = self.query_one(f"#{SEARCH_BAR_ID}", Input)
        if search_bar.display and search_bar.value:
            self._apply_filter(search_bar.value)

        # Restore selection last — after any filter re-populate — so it wins.
        self._restore_selection(active, focus_key)

    @staticmethod
    def _row_key(item: Any) -> tuple[str, str] | None:
        """Stable identity for a row-backing object, for selection restore."""
        if isinstance(item, Container):
            return ("container", item.id)
        if isinstance(item, ComposeProject):
            return ("project", item.name)
        if isinstance(item, Image):
            return ("image", item.id)
        if isinstance(item, Volume):
            return ("volume", item.name)
        if isinstance(item, Network):
            return ("network", item.name)
        return None

    def _focused_row_key(self, tab_id: TabID) -> tuple[str, str] | None:
        item = self._get_focused_resource(tab_id)
        return self._row_key(item) if item is not None else None

    def _restore_selection(
        self, tab_id: TabID, focus_key: tuple[str, str] | None
    ) -> None:
        """Re-select the row matching `focus_key`, or fall back to the first row."""
        entry = self._resource_registry.get(tab_id)
        if focus_key is None or entry is None:
            self._auto_select_first()
            return
        for idx, item in enumerate(self._current.get(tab_id, [])):
            if self._row_key(item) == focus_key:
                self.query_one(f"#{entry.table_id}", DataTable).move_cursor(row=idx)
                pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
                try:
                    entry.show_details(pane, idx)
                except IndexError:
                    pane.clear_details()
                # Cursor may not have moved (same index) so RowHighlighted won't
                # fire — sync stats/top here in case the item's state changed.
                self._sync_stats()
                self._sync_top()
                return
        # The previously-focused resource is gone — select the first row.
        self._auto_select_first()

    def _maybe_notify_connection_change(self, state: ConnectionState) -> None:
        """UI-thread only — call via `call_from_thread` from a worker.

        No-ops unless `state.status` actually changed since the last call, so
        a poll loop retrying every couple seconds doesn't spam a toast on
        every attempt. Feeds the StatusBar's connection segment either way
        the first time, then only a real transition after that.
        """
        previous = self._last_conn_status
        if state.status == previous:
            return
        was_down = previous not in (
            ConnectionStatus.NOT_CONNECTED,
            ConnectionStatus.CONNECTED,
        )
        self._last_conn_status = state.status
        connected = state.status == ConnectionStatus.CONNECTED
        status_bar = self.query_one(f"#{STATUS_BAR_ID}", StatusBar)
        status_bar.set_connection_state(connected, "" if connected else state.message)
        indicator = self.query_one(f"#{CONNECTION_INDICATOR_ID}", ConnectionIndicator)
        indicator.set_connection_state(connected)
        banner = self.query_one(f"#{CONNECTION_BANNER_ID}", Static)
        if connected:
            banner.display = False
            if was_down:
                self.notify("Reconnected to Docker")
        else:
            banner_text = state.message
            if state.hint:
                banner_text += f"  —  {state.hint}"
            banner.update(escape(banner_text))
            banner.display = True
            self.notify(f"{state.message}\n{state.hint}", severity="error", timeout=12)

    def _finish_refresh(
        self, snapshot: DockerSnapshot | None, error: str | None
    ) -> None:
        try:
            if snapshot is not None:
                self._apply_snapshot(snapshot)
                logger.info("Refresh complete")
                failed = self.docker.last_fetch_errors
                if failed:
                    logger.warning("Partial fetch failure: %s", ", ".join(failed))
                    self.notify(
                        f"Could not refresh {', '.join(failed)} — "
                        "showing last known state",
                        severity="warning",
                    )
            elif error:
                logger.warning("Refresh failed: %s", error)
                self.notify(f"Refresh failed: {error}", severity="error")
        finally:
            self._refresh_in_progress = False
            self.query_one(f"#{REFRESH_LOADING_ID}", LoadingIndicator).display = False
            # An event arrived mid-refresh — run once more so the final state lands.
            if self._refresh_pending:
                self._refresh_pending = False
                self.start_refresh()

    # --- Event-driven auto-refresh ---

    # TODO: Refactor to reduce complexity.
    @work(thread=True)
    def start_event_listener(self) -> None:
        """Watch `docker events` and keep the UI in sync.

        Runs on a background thread. If the event stream ends because the daemon
        becomes unavailable, it also acts as the reconnect loop, retrying until the
        connection is restored and triggering an immediate refresh when it succeeds.
        """
        stop = threading.Event()
        self._event_stop = stop
        while not stop.is_set():
            stream = self.docker.stream_events()
            self._event_stream = stream
            try:
                for event in stream:
                    if stop.is_set():
                        break
                    if self._is_noise_event(event.get("Action", "")):
                        continue
                    self.call_from_thread(self._on_docker_event)
            except Exception:
                # Usually a `call_from_thread` race during app shutdown, not a Docker
                # failure. `EventStream` records daemon errors in `.error` instead of
                # propagating them.
                logger.exception("Event listener error")

            if stream.error is not None:
                self.docker.mark_disconnected(stream.error)
            self.call_from_thread(
                self._maybe_notify_connection_change, self.docker.connection
            )

            if not self.docker.is_connected:
                stop.wait(2.0)
                if stop.is_set():
                    break
                state = self.docker.ensure_connected()
                self.call_from_thread(self._maybe_notify_connection_change, state)
                if state.status == ConnectionStatus.CONNECTED:
                    self.call_from_thread(self.start_refresh)
                continue

            stop.wait(2.0)

    def _on_docker_event(self) -> None:
        # Coalesce bursts (e.g. `compose up` emits many events) into one refresh.
        if self._refresh_debounce is not None:
            self._refresh_debounce.stop()
        self._refresh_debounce = self.set_timer(0.4, self._debounced_refresh)

    def _debounced_refresh(self) -> None:
        self._refresh_debounce = None
        if self._refresh_in_progress:
            self._refresh_pending = True
        else:
            self.start_refresh()

    def stop_event_listener(self) -> None:
        if self._event_stop is not None:
            self._event_stop.set()
        if self._event_stream is not None:
            self._event_stream.stop()
