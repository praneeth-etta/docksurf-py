"""
app.py — The Application shell.

Assembles the seven mixin classes into DockSurfApp, defines layout and
key bindings, and wires the on_mount / action_refresh entry points.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    LoadingIndicator,
    TabbedContent,
    TabPane,
)

from docksurf_py.actions import (
    ComposeActionHandler,
    ContainerActionHandler,
    DeletePlan,
    ImageActionHandler,
    InspectHandler,
    NetworkActionHandler,
    PruneHandler,
    ResourceDeletionHandler,
    SelectionHandler,
    VolumeActionHandler,
)
from docksurf_py.constants import (
    DETAIL_PANE_ID,
    LOG_PANE_ID,
    MAIN_CONTAINER_ID,
    REFRESH_LOADING_ID,
    SEARCH_BAR_ID,
    STATUS_BAR_ID,
    TabID,
    TableID,
)
from docksurf_py.models import CommandResult, Container, DockerSnapshot
from docksurf_py.observability import LiveStatsController
from docksurf_py.renderer import (
    DetailPaneRenderer,
    ResourceFocusResolver,
    SnapshotManager,
    TableRenderer,
)
from docksurf_py.search import (
    ResourceSearchController,
    _matches_container,
    _matches_image,
    _matches_network,
    _matches_volume,
)
from docksurf_py.service import DockerService
from docksurf_py.widgets import (
    ContainerTable,
    DetailPane,
    HelpScreen,
    LogPane,
    SearchBar,
    StatusBar,
)

logger = logging.getLogger(__name__)


def _container_only_actions() -> frozenset[str]:
    """Action names implemented by ContainerActionHandler.

    Used to scope the help screen to whatever that mixin actually owns, so
    the scope column can't drift out of sync the way a hand-copied set would.
    """
    return frozenset(
        name.removeprefix("action_")
        for name, member in vars(ContainerActionHandler).items()
        if name.startswith("action_") and callable(member)
    )


def _compose_actions() -> frozenset[str]:
    """Action names implemented by ComposeActionHandler.

    Lets the help screen tag project-wide actions with their own scope instead
    of mislabeling them Global — derived from the mixin so it can't drift.
    """
    return frozenset(
        name.removeprefix("action_")
        for name, member in vars(ComposeActionHandler).items()
        if name.startswith("action_") and callable(member)
    )


def _tab_actions(mixin: type) -> frozenset[str]:
    """Action names implemented directly by a tab-scoped handler mixin.

    Used to give the help screen a per-tab scope column for Image/Volume/
    Network actions — reflected from the mixin so it can't drift from what's
    actually bound (same trick as `_container_only_actions`).
    """
    return frozenset(
        name.removeprefix("action_")
        for name, member in vars(mixin).items()
        if name.startswith("action_") and callable(member)
    )


@dataclass(frozen=True)
class ResourceEntry:
    """Everything the app needs to treat one resource type generically.

    One instance per tab, keyed by `TabID` in `DockSurfApp._resource_registry`.
    Replaces the five places (auto-select, row-highlight, search filter,
    delete dispatch, plus the focus-resolver quartet) that used to hand-list
    each of the four resource types separately.
    """

    table_id: TableID
    columns: tuple[str, ...]
    label: str  # singular, human-readable — used in "No {label} selected"
    snapshot_items: Callable[[DockerSnapshot], list]
    populate: Callable[..., None]  # (table, items=None)
    show_details: Callable[[DetailPane, int], None]
    matches: Callable[[Any, str], bool]
    plan_delete: Callable[[Any], DeletePlan | None]


class AppContext(Protocol):
    """Structural contract each mixin's `self` is checked against by mypy.

    `TableRenderer`, `ContainerActionHandler`, etc. only ever run composed
    into `DockSurfApp` (which really does provide all of this), but mypy
    analyses each mixin class in isolation otherwise — reaching for
    `self.docker` in a bare `class ContainerActionHandler:` is an unchecked
    attribute error waiting to happen. `renderer.py`/`actions.py`/`search.py`
    give each mixin `self: AppContext` via
    `_Base = AppContext if TYPE_CHECKING else object`, so mypy checks method
    bodies against this instead. Never instantiated or inherited at runtime.
    """

    snapshot: DockerSnapshot | None
    docker: DockerService
    _current: dict[TabID, list]
    _resource_registry: dict[TabID, ResourceEntry]
    _collapsed_projects: set[str]
    _marked: dict[TabID, set[tuple[str, str]]]
    _volume_sizes: dict[str, int]
    is_running: bool  # really a textual.app.App property

    def start_refresh(self) -> None: ...
    def _auto_select_first(self) -> None: ...
    def _apply_filter(self, query: str) -> None: ...
    def _rerender_containers(self) -> None: ...
    def _rerender_active_table(self) -> None: ...
    def _sync_stats(self) -> None: ...
    def _sync_top(self) -> None: ...
    def _get_focused_container(self) -> Container | None: ...
    def _get_focused_resource(self, tab_id: TabID) -> Any: ...
    def _get_focused_project(self) -> Any: ...
    def _focused_is_project_header(self) -> bool: ...
    def _row_key(self, item: Any) -> tuple[str, str] | None: ...
    def _marked_items(self, tab_id: TabID) -> list[Any]: ...
    def _run_bulk(
        self,
        tab_id: TabID,
        verb: str,
        jobs: list[tuple[tuple[str, str], str, Callable[[], CommandResult]]],
    ) -> None: ...
    def action_compose_stop(self) -> None: ...
    def action_compose_start(self) -> None: ...
    def action_compose_restart(self) -> None: ...
    def action_toggle_group(self) -> None: ...
    def _handle_write_result(self, result: CommandResult) -> None: ...

    # These five are really `textual.app.App` methods. DockSurfApp's real
    # MRO includes both `App` and (fictitiously, TYPE_CHECKING-only) this
    # protocol, so mypy requires these stubs to be valid *overrides* of
    # App's real signatures — hence maximally loose rather than precise.
    def notify(self, *args: Any, **kwargs: Any) -> Any: ...
    def query_one(self, *args: Any, **kwargs: Any) -> Any: ...
    def call_from_thread(self, *args: Any, **kwargs: Any) -> Any: ...
    def push_screen_wait(self, *args: Any, **kwargs: Any) -> Any: ...
    def push_screen(self, *args: Any, **kwargs: Any) -> Any: ...
    def suspend(self, *args: Any, **kwargs: Any) -> Any: ...
    def set_timer(self, *args: Any, **kwargs: Any) -> Any: ...


class DockSurfApp(
    TableRenderer,
    SnapshotManager,
    ResourceFocusResolver,
    DetailPaneRenderer,
    ContainerActionHandler,
    ComposeActionHandler,
    ResourceDeletionHandler,
    SelectionHandler,
    InspectHandler,
    PruneHandler,
    ImageActionHandler,
    VolumeActionHandler,
    NetworkActionHandler,
    ResourceSearchController,
    LiveStatsController,
    App,
):
    snapshot: DockerSnapshot | None = None

    docker: DockerService

    _resource_registry: dict[TabID, ResourceEntry]

    BINDINGS = [
        ("?", "help", "Help"),
        ("r", "refresh", "Refresh"),
        ("/", "open_search", "Search"),
        ("q", "quit", "Quit"),
        ("s", "stop_container", "Stop"),
        ("S", "start_container", "Start"),
        ("x", "restart_container", "Restart"),
        ("p", "pause_container", "Pause/Unpause"),
        ("K", "kill_container", "Kill"),
        ("e", "exec_container", "Exec"),
        Binding("E", "exec_custom", "Exec (custom)", show=False),
        ("i", "inspect", "Inspect"),
        ("d", "delete", "Delete"),
        ("l", "view_logs", "Logs"),
        ("f", "follow_logs", "Pause/Resume"),
        ("c", "clear_logs", "Clear"),
        Binding("C", "copy_files", "Copy files", show=False),
        ("z", "toggle_log_expand", "Expand Logs"),
        ("o", "log_options", "Log options"),
        Binding("T", "toggle_timestamps", "Toggle timestamps", show=False),
        Binding("W", "toggle_log_wrap", "Toggle wrap", show=False),
        Binding("n", "next_match", "Next log match", show=False),
        Binding("N", "prev_match", "Prev log match", show=False),
        Binding("g", "log_top", "Jump to log top", show=False),
        Binding("G", "log_bottom", "Jump to log bottom", show=False),
        Binding("X", "export_logs", "Export logs", show=False),
        ("u", "compose_up", "Compose Up"),
        ("k", "compose_down", "Compose Down"),
        ("t", "container_top", "Top"),
        ("space", "toggle_mark", "Mark / Collapse"),
        ("w", "system_df", "Disk usage"),
        ("P", "prune", "Prune"),
        Binding("escape", "clear_marks", "Clear marks", show=False),
        # Image / Volume / Network tab actions (Roadmap §5). Tab-scoped: each
        # action guards its tab and notifies a hint elsewhere. show=False keeps
        # the footer uncluttered; all are documented in the `?` help screen.
        Binding("plus", "new_resource", "New / Pull", show=False),
        Binding("h", "image_history", "Image layer history", show=False),
        Binding("y", "tag_image", "Tag image", show=False),
        Binding("a", "mark_all_dangling", "Mark dangling images", show=False),
        Binding("b", "volume_size", "Volume size on disk", show=False),
        Binding("v", "network_connect", "Connect to network", show=False),
        Binding("m", "network_disconnect", "Disconnect from network", show=False),
    ]
    CSS_PATH = "app.tcss"

    def __init__(self, docker: DockerService, **kwargs) -> None:
        super().__init__(**kwargs)
        self._injected_docker = docker
        self._resource_registry = {
            TabID.CONTAINERS: ResourceEntry(
                table_id=TableID.CONTAINERS,
                columns=("Name", "Image", "Status", "Health", "Uptime"),
                label="container",
                snapshot_items=lambda snap: snap.containers,
                populate=self._populate_container_table,
                show_details=self._show_container_details,
                matches=_matches_container,
                plan_delete=self._plan_container_delete,
            ),
            TabID.IMAGES: ResourceEntry(
                table_id=TableID.IMAGES,
                columns=("Repository", "Tag", "Size"),
                label="image",
                snapshot_items=lambda snap: snap.images,
                populate=self._populate_image_table,
                show_details=self._show_image_details,
                matches=_matches_image,
                plan_delete=self._plan_image_delete,
            ),
            TabID.VOLUMES: ResourceEntry(
                table_id=TableID.VOLUMES,
                columns=("Name", "Status"),
                label="volume",
                snapshot_items=lambda snap: snap.volumes,
                populate=self._populate_volume_table,
                show_details=self._show_volume_details,
                matches=_matches_volume,
                plan_delete=self._plan_volume_delete,
            ),
            TabID.NETWORKS: ResourceEntry(
                table_id=TableID.NETWORKS,
                columns=("Name", "Driver", "Scope"),
                label="network",
                snapshot_items=lambda snap: snap.networks,
                populate=self._populate_network_table,
                show_details=self._show_network_details,
                matches=_matches_network,
                plan_delete=self._plan_network_delete,
            ),
        }

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id=MAIN_CONTAINER_ID):
            with TabbedContent():
                with TabPane("Containers", id=TabID.CONTAINERS):
                    yield ContainerTable(id=TableID.CONTAINERS)
                with TabPane("Images", id=TabID.IMAGES):
                    yield DataTable(id=TableID.IMAGES)
                with TabPane("Volumes", id=TabID.VOLUMES):
                    yield DataTable(id=TableID.VOLUMES)
                with TabPane("Networks", id=TabID.NETWORKS):
                    yield DataTable(id=TableID.NETWORKS)
            yield DetailPane(id=DETAIL_PANE_ID)
            yield LogPane(id=LOG_PANE_ID)
        yield LoadingIndicator(id=REFRESH_LOADING_ID)
        yield SearchBar(placeholder="🔍 Filter...", id=SEARCH_BAR_ID)
        yield StatusBar(id=STATUS_BAR_ID)
        yield Footer()

    def on_mount(self) -> None:
        self.docker = self._injected_docker
        # DockerClient connects lazily on the first fetch_snapshot() call
        # (inside start_refresh()'s background worker), not here — so the
        # UI never blocks on the daemon round-trip before its first paint.
        # Any connection failure is surfaced from SnapshotManager once that
        # first fetch comes back.
        self.setup_tables()
        self.start_refresh()
        # Live: react to `docker events` so the tables stay current without `r`.
        self.start_event_listener()

    def on_unmount(self) -> None:
        self.stop_event_listener()
        self.stop_stats()

    def action_refresh(self) -> None:
        self.start_refresh()

    def _auto_select_first(self) -> None:
        if not self.snapshot:
            return
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        active = self.query_one(TabbedContent).active
        entry = self._resource_registry.get(active)
        if entry is None:
            pane.clear_details()
            self._sync_stats()
            self._sync_top()
            return
        current = self._current.get(active, [])
        if not current:
            pane.clear_details()
            self._sync_stats()
            self._sync_top()
            return
        self.query_one(f"#{entry.table_id}", DataTable).move_cursor(row=0)
        try:
            entry.show_details(pane, 0)
        except IndexError:
            pane.clear_details()
        self._sync_stats()
        self._sync_top()

    @on(DataTable.RowHighlighted)
    def update_details(self, event: DataTable.RowHighlighted) -> None:
        # A queued RowHighlighted can dispatch during teardown (a write action's
        # refresh repopulates a table just as the app unmounts) — the widgets
        # are gone, so bail rather than raise NoMatches, matching the
        # is_running guard in SnapshotManager._apply_snapshot.
        if not self.is_running or not self.snapshot:
            return
        active = self.query_one(TabbedContent).active
        entry = self._resource_registry.get(active)
        if entry is None or event.control.id != entry.table_id:
            return
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        try:
            entry.show_details(pane, event.cursor_row)
        except IndexError:
            pane.clear_details()
        self._sync_stats()
        self._sync_top()

    @on(TabbedContent.TabActivated)
    def clear_on_tab_switch(self) -> None:
        self._auto_select_first()

    def action_new_resource(self) -> None:
        """`+`: create/pull on whichever non-container tab is active."""
        active = self.query_one(TabbedContent).active
        if active == TabID.IMAGES:
            self.action_pull_image()
        elif active == TabID.VOLUMES:
            self.action_create_volume()
        elif active == TabID.NETWORKS:
            self.action_create_network()
        else:
            self.notify("Nothing to create on this tab", severity="information")

    def action_help(self) -> None:
        self.push_screen(
            HelpScreen(
                self.BINDINGS,
                _container_only_actions(),
                _compose_actions(),
                tab_actions={
                    "Images tab": _tab_actions(ImageActionHandler),
                    "Volumes tab": _tab_actions(VolumeActionHandler),
                    "Networks tab": _tab_actions(NetworkActionHandler),
                },
            )
        )


def main():
    from docksurf_py.docker import DockerClient

    log_dir = os.path.expanduser("~/.local/share/docksurf-py")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "docksurf.log")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-30s  %(levelname)-8s  %(message)s",
        handlers=[logging.FileHandler(log_file)],
    )
    logger.info("DockSurf starting — log file: %s", log_file)
    DockSurfApp(docker=DockerClient()).run()
    logger.info("DockSurf exiting")


if __name__ == "__main__":
    main()
