"""
app.py — The Application shell.

Assembles the seven mixin classes into DockSurfApp, defines layout and
key bindings, and wires the on_mount / action_refresh entry points.
"""

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from textual import on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    LoadingIndicator,
    Static,
    TabbedContent,
    TabPane,
)
from textual.worker import Worker

from docksurf_py.actions import (
    ClipboardHandler,
    ComposeActionHandler,
    ContainerActionHandler,
    ContextActionHandler,
    DeletePlan,
    ImageActionHandler,
    InspectHandler,
    NetworkActionHandler,
    PruneHandler,
    ResourceDeletionHandler,
    SelectionHandler,
    VolumeActionHandler,
)
from docksurf_py.config import DEFAULT_CONFIG_PATH, Config, load_config
from docksurf_py.constants import (
    CONNECTION_BANNER_ID,
    CONNECTION_INDICATOR_ID,
    DETAIL_PANE_ID,
    EMPTY_STATE_IDS,
    LOG_PANE_ID,
    MAIN_CONTAINER_ID,
    REFRESH_LOADING_ID,
    SEARCH_BAR_ID,
    STATUS_BAR_ID,
    LogOptions,
    TabID,
    TableID,
)
from docksurf_py.models import CommandResult, Container, DockerSnapshot
from docksurf_py.observability import LiveStatsController
from docksurf_py.paths import DATA_DIR
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
from docksurf_py.session import SessionState, load_session, save_session
from docksurf_py.themes import CUSTOM_THEMES, DEFAULT_THEME_NAME
from docksurf_py.widgets import (
    ConnectionIndicator,
    ContainerTable,
    DetailPane,
    HelpScreen,
    ImageTable,
    LogPane,
    NetworkTable,
    SearchBar,
    StatusBar,
    VolumeTable,
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
    sort_keys: dict[str, Callable[[Any], Any]]


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
    config: Config
    _session: SessionState
    _persist_session: bool
    _current: dict[TabID, list]
    _resource_registry: dict[TabID, ResourceEntry]
    _collapsed_projects: set[str]
    _marked: dict[TabID, set[tuple[str, str]]]
    _volume_sizes: dict[str, int]
    _image_architectures: dict[str, str]
    _reveal_secrets: bool
    _sort_state: dict[TabID, tuple[str, bool] | None]
    is_running: bool  # really a textual.app.App property

    def start_refresh(self) -> None: ...
    def _auto_select_first(self) -> None: ...
    def _apply_filter(self, query: str) -> None: ...
    def _rerender_containers(self) -> None: ...
    def _rerender_active_table(self) -> None: ...
    def _update_empty_state(
        self, tab_id: TabID, entry: ResourceEntry, items: list, query: str = ""
    ) -> None: ...
    def _sort_items(self, tab_id: TabID, entry: ResourceEntry, items: list) -> list: ...
    def _sync_stats(self) -> None: ...
    def _sync_top(self) -> None: ...
    def _sync_image_architecture(self) -> None: ...
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
    def action_compose_down(self) -> Worker[None]: ...
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
    def copy_to_clipboard(self, *args: Any, **kwargs: Any) -> Any: ...


class DockSurfApp(
    TableRenderer,
    SnapshotManager,
    ResourceFocusResolver,
    DetailPaneRenderer,
    ContainerActionHandler,
    ComposeActionHandler,
    ResourceDeletionHandler,
    ClipboardHandler,
    SelectionHandler,
    InspectHandler,
    PruneHandler,
    ContextActionHandler,
    ImageActionHandler,
    VolumeActionHandler,
    NetworkActionHandler,
    ResourceSearchController,
    LiveStatsController,
    App,
):
    snapshot: DockerSnapshot | None = None

    docker: DockerService
    config: Config

    _resource_registry: dict[TabID, ResourceEntry]

    BINDINGS = [
        ("?", "help", "Help"),
        ("r", "refresh", "Refresh"),
        ("/", "open_search", "Search"),
        ("q", "quit", "Quit"),
        # Container/log-viewer actions below are declared here so the Help screen
        # (`action_help`) and command palette (`get_system_commands`) both of which only
        # read `self.BINDINGS` still surface them everywhere. `show=False` keeps them
        # out of the Footer globally; `ContainerTable.BINDINGS` (widgets/tables.py)
        # re-declares the same actions with `show=True`, so the Footer only displays
        # them while the Containers tab's table actually has focus.
        Binding("s", "stop_container", "Stop", show=False),
        Binding("S", "start_container", "Start", show=False),
        Binding("x", "restart_container", "Restart", show=False),
        Binding("p", "pause_container", "Pause/Unpause", show=False),
        Binding("K", "kill_container", "Kill", show=False),
        Binding("e", "exec_container", "Exec", show=False),
        Binding("E", "exec_custom", "Exec (custom)", show=False),
        ("i", "inspect", "Inspect"),
        ("d", "delete", "Delete"),
        Binding("l", "view_logs", "Logs", show=False),
        Binding("f", "follow_logs", "Pause/Resume", show=False),
        Binding("c", "clear_logs", "Clear", show=False),
        Binding("C", "copy_files", "Copy files", show=False),
        Binding("z", "toggle_log_expand", "Expand Logs", show=False),
        Binding("o", "log_options", "Log options", show=False),
        Binding("T", "toggle_timestamps", "Toggle timestamps", show=False),
        Binding("W", "toggle_log_wrap", "Toggle wrap", show=False),
        Binding("n", "next_match", "Next log match", show=False),
        Binding("N", "prev_match", "Prev log match", show=False),
        Binding("g", "log_top", "Jump to log top", show=False),
        Binding("G", "log_bottom", "Jump to log bottom", show=False),
        Binding("X", "export_logs", "Export logs", show=False),
        Binding("ctrl+u", "compose_up", "Compose Up", show=False),
        Binding("ctrl+k", "compose_down", "Compose Down", show=False),
        Binding("t", "container_top", "Top", show=False),
        ("space", "toggle_mark", "Mark / Collapse"),
        ("w", "system_df", "Disk usage"),
        ("P", "prune", "Prune"),
        Binding("D", "switch_context", "Docker context", show=False),
        Binding("escape", "clear_marks", "Clear marks", show=False),
        Binding("Y", "yank", "Copy to clipboard", show=False),
        Binding("O", "open_port", "Open port in browser", show=False),
        Binding("R", "toggle_secrets", "Reveal/mask secret env vars", show=False),
        Binding("1", "switch_tab_1", "Containers tab", show=False),
        Binding("2", "switch_tab_2", "Images tab", show=False),
        Binding("3", "switch_tab_3", "Volumes tab", show=False),
        Binding("4", "switch_tab_4", "Networks tab", show=False),
        Binding("[", "prev_tab", "Previous tab", show=False),
        Binding("]", "next_tab", "Next tab", show=False),
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
        Binding("M", "cycle_theme", "Theme", show=True),
    ]
    CSS_PATH = "app.tcss"

    def __init__(
        self,
        docker: DockerService,
        config: Config | None = None,
        session: SessionState | None = None,
        persist_session: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._injected_docker = docker
        self.config = config or Config()
        self._session = session or SessionState()
        self._persist_session = persist_session
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
                sort_keys={
                    "Name": lambda c: c.name.lower(),
                    "Image": lambda c: c.image_name.lower(),
                    "Status": lambda c: (not c.running, c.status.lower()),
                    "Health": lambda c: c.health.lower(),
                    "Uptime": lambda c: c.started_at,
                },
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
                sort_keys={
                    "Repository": lambda i: i.repository.lower(),
                    "Tag": lambda i: i.tag.lower(),
                    "Size": lambda i: i.size_bytes,
                },
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
                sort_keys={
                    "Name": lambda v: v.name.lower(),
                    "Status": lambda v: bool(v.used_by),
                },
            ),
            TabID.NETWORKS: ResourceEntry(
                table_id=TableID.NETWORKS,
                columns=("Name", "Driver", "Scope"),
                label="network",
                snapshot_items=lambda snap: snap.networks,
                populate=self._populate_network_table,
                show_details=self._show_network_details,
                matches=_matches_network,
                sort_keys={
                    "Name": lambda n: n.name.lower(),
                    "Driver": lambda n: n.driver.lower(),
                    "Scope": lambda n: n.scope.lower(),
                },
                plan_delete=self._plan_network_delete,
            ),
        }

    def compose(self) -> ComposeResult:
        yield Header()
        yield ConnectionIndicator(id=CONNECTION_INDICATOR_ID)
        yield Static("", id=CONNECTION_BANNER_ID)
        with Horizontal(id=MAIN_CONTAINER_ID):
            with TabbedContent():
                with TabPane("Containers", id=TabID.CONTAINERS):
                    yield ContainerTable(id=TableID.CONTAINERS)
                    yield Static(
                        "", id=EMPTY_STATE_IDS[TabID.CONTAINERS], classes="empty-state"
                    )
                with TabPane("Images", id=TabID.IMAGES):
                    yield ImageTable(id=TableID.IMAGES)
                    yield Static(
                        "", id=EMPTY_STATE_IDS[TabID.IMAGES], classes="empty-state"
                    )
                with TabPane("Volumes", id=TabID.VOLUMES):
                    yield VolumeTable(id=TableID.VOLUMES)
                    yield Static(
                        "", id=EMPTY_STATE_IDS[TabID.VOLUMES], classes="empty-state"
                    )
                with TabPane("Networks", id=TabID.NETWORKS):
                    yield NetworkTable(id=TableID.NETWORKS)
                    yield Static(
                        "", id=EMPTY_STATE_IDS[TabID.NETWORKS], classes="empty-state"
                    )
            yield DetailPane(id=DETAIL_PANE_ID)
            yield LogPane(
                id=LOG_PANE_ID,
                default_options=LogOptions(
                    tail=self.config.default_log_tail,
                    since_seconds=self.config.default_log_since_seconds,
                ),
            )
        yield LoadingIndicator(id=REFRESH_LOADING_ID)
        yield SearchBar(placeholder="🔍 Filter...", id=SEARCH_BAR_ID)
        yield StatusBar(id=STATUS_BAR_ID)
        yield Footer()

    def on_mount(self) -> None:
        self.docker = self._injected_docker
        for theme in CUSTOM_THEMES:
            self.register_theme(theme)
        self.theme = (
            self._session.theme
            if self._session.theme in self.available_themes
            else DEFAULT_THEME_NAME
        )
        if self._session.active_tab:
            try:
                self.query_one(TabbedContent).active = TabID(self._session.active_tab)
            except ValueError:
                pass
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

    def _switch_tab(self, tab_id: TabID) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_switch_tab_1(self) -> None:
        self._switch_tab(TabID.CONTAINERS)

    def action_switch_tab_2(self) -> None:
        self._switch_tab(TabID.IMAGES)

    def action_switch_tab_3(self) -> None:
        self._switch_tab(TabID.VOLUMES)

    def action_switch_tab_4(self) -> None:
        self._switch_tab(TabID.NETWORKS)

    def action_next_tab(self) -> None:
        tabs = list(TabID)
        idx = tabs.index(TabID(self.query_one(TabbedContent).active))
        self._switch_tab(tabs[(idx + 1) % len(tabs)])

    def action_prev_tab(self) -> None:
        tabs = list(TabID)
        idx = tabs.index(TabID(self.query_one(TabbedContent).active))
        self._switch_tab(tabs[(idx - 1) % len(tabs)])

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
            self._sync_image_architecture()
            return
        table = self.query_one(f"#{entry.table_id}", DataTable)
        # Explicitly follow focus to the active tab's table (rather than
        # relying on Textual's implicit initial-focus behavior) so the
        # Footer's per-table BINDINGS scoping is deterministic.
        if not self.query_one(f"#{SEARCH_BAR_ID}", Input).display:
            table.focus()
        current = self._current.get(active, [])
        if not current:
            pane.clear_details()
            self._sync_stats()
            self._sync_top()
            self._sync_image_architecture()
            return
        table.move_cursor(row=0)
        try:
            entry.show_details(pane, 0)
        except IndexError:
            pane.clear_details()
        self._sync_stats()
        self._sync_top()
        self._sync_image_architecture()

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
        self._sync_image_architecture()

    @on(TabbedContent.TabActivated)
    def clear_on_tab_switch(self) -> None:
        self._auto_select_first()
        if self._persist_session:
            self._session.active_tab = self.query_one(TabbedContent).active
            save_session(self._session)

    @on(DataTable.HeaderSelected)
    def _on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        # Must live here, not on the TableRenderer mixin: `@on` only wires up
        # through Textual's metaclass for methods on a real message-pump
        # class, and the mixins run with a plain `object` base at runtime.
        self._on_header_selected(event)

    @on(Input.Changed, f"#{SEARCH_BAR_ID}")
    def _on_search_input_changed(self, event: Input.Changed) -> None:
        # Same reason as _on_data_table_header_selected above — the real
        # logic lives on ResourceSearchController (search.py), a mixin.
        self.on_search_changed(event)

    @on(Input.Submitted, f"#{SEARCH_BAR_ID}")
    def _on_search_input_submitted(self, event: Input.Submitted) -> None:
        self.on_search_escape(event)

    @on(LogPane.ToggleExpand)
    def _on_log_pane_toggle_expand(self) -> None:
        self.action_toggle_log_expand()

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

    def action_cycle_theme(self) -> None:
        """`M`: cycle through just the 3 curated DockSurf themes."""
        names = [t.name for t in CUSTOM_THEMES]
        idx = names.index(self.theme) if self.theme in names else -1
        next_name = names[(idx + 1) % len(names)]
        self.theme = next_name
        self._session.theme = next_name
        if self._persist_session:
            save_session(self._session)
        self.notify(f"Theme: {next_name}")

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Surface every bound action in the command palette (`ctrl+p`).

        Textual's default only offers Theme/Quit/Keys/Screenshot/Maximize.
        Reflecting over `BINDINGS` — the same source `?`'s help screen reads
        from — makes every DockSurf action discoverable, including the
        `show=False` ones hidden from the footer (that's the point: hidden
        from the footer, not from the palette).
        """
        yield from super().get_system_commands(screen)
        seen_actions: set[str] = set()
        for binding in self.BINDINGS:
            if isinstance(binding, Binding):
                key, action, description = (
                    binding.key,
                    binding.action,
                    binding.description,
                )
            elif len(binding) == 3:
                key, action, description = binding
            else:
                key, action = binding
                description = ""
            if not description or action in seen_actions:
                continue
            seen_actions.add(action)
            method = getattr(self, f"action_{action}", None)
            if method is None:
                continue
            yield SystemCommand(description, f"Key: {key}", method)

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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="docksurf", description="Terminal UI for Docker resources"
    )
    parser.add_argument(
        "--host", help="Docker daemon endpoint to connect to (e.g. tcp://host:2375)"
    )
    parser.add_argument(
        "--context", help="Docker context to connect through (this run only)"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=f"Path to config.toml (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser.parse_args(argv)


def main():
    from docksurf_py.docker import DockerClient

    args = _parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = DATA_DIR / "docksurf.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-30s  %(levelname)-8s  %(message)s",
        handlers=[logging.FileHandler(log_file)],
    )
    logger.info("DockSurf starting — log file: %s", log_file)
    DockSurfApp(
        docker=DockerClient(context_override=args.context, host_override=args.host),
        config=load_config(args.config),
        session=load_session(),
        persist_session=True,
    ).run()
    logger.info("DockSurf exiting")


if __name__ == "__main__":
    main()
