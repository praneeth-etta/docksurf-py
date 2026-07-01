"""
app.py — The Application shell.

Assembles the seven mixin classes into DockSurfApp, defines layout and
key bindings, and wires the on_mount / action_refresh entry points.
"""

import logging
import os

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    LoadingIndicator,
    TabbedContent,
    TabPane,
)

from docksurf_py.actions import ContainerActionHandler, ResourceDeletionHandler
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
from docksurf_py.models import DockerSnapshot
from docksurf_py.renderer import (
    DetailPaneRenderer,
    ResourceFocusResolver,
    SnapshotManager,
    TableRenderer,
)
from docksurf_py.search import ResourceSearchController
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


class DockSurfApp(
    TableRenderer,
    SnapshotManager,
    ResourceFocusResolver,
    DetailPaneRenderer,
    ContainerActionHandler,
    ResourceDeletionHandler,
    ResourceSearchController,
    App,
):
    snapshot: DockerSnapshot | None = None

    docker: DockerService

    BINDINGS = [
        ("?", "help", "Help"),
        ("r", "refresh", "Refresh"),
        ("/", "open_search", "Search"),
        ("q", "quit", "Quit"),
        ("s", "stop_container", "Stop"),
        ("S", "start_container", "Start"),
        ("x", "restart_container", "Restart"),
        ("e", "exec_container", "Exec"),
        ("d", "delete", "Delete"),
        ("l", "view_logs", "Logs"),
        ("f", "follow_logs", "Follow"),
        ("z", "toggle_log_expand", "Expand Logs"),
    ]
    CSS_PATH = "app.tcss"

    def __init__(self, docker: DockerService, **kwargs) -> None:
        super().__init__(**kwargs)
        self._injected_docker = docker

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
        state = self.docker.connection
        if not self.docker.is_connected:
            logger.error(
                "Docker unavailable — status=%s context=%s host=%s",
                state.status.value,
                state.context,
                state.host,
            )
            self.notify(
                f"{state.message}\n{state.hint}",
                severity="error",
                timeout=12,
            )
        self.setup_tables()
        self.start_refresh()

    def action_refresh(self) -> None:
        self.start_refresh()

    def _auto_select_first(self) -> None:
        if not self.snapshot:
            return
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        active = self.query_one(TabbedContent).active
        dispatch = {
            TabID.CONTAINERS: (
                TableID.CONTAINERS,
                self._show_container_details,
                "_current_containers",
            ),
            TabID.IMAGES: (TableID.IMAGES, self._show_image_details, "_current_images"),
            TabID.VOLUMES: (
                TableID.VOLUMES,
                self._show_volume_details,
                "_current_volumes",
            ),
            TabID.NETWORKS: (
                TableID.NETWORKS,
                self._show_network_details,
                "_current_networks",
            ),
        }
        if active not in dispatch:
            pane.clear_details()
            return
        table_id, show_fn, current_attr = dispatch[active]
        current = getattr(self, current_attr, [])
        if not current:
            pane.clear_details()
            return
        self.query_one(f"#{table_id}", DataTable).move_cursor(row=0)
        try:
            show_fn(pane, 0)
        except IndexError:
            pane.clear_details()

    @on(DataTable.RowHighlighted)
    def update_details(self, event: DataTable.RowHighlighted) -> None:
        if not self.snapshot:
            return
        active = self.query_one(TabbedContent).active
        table_id = event.control.id
        if table_id != f"table-{active.removeprefix('tab-')}":
            return
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        try:
            if table_id == TableID.CONTAINERS:
                self._show_container_details(pane, event.cursor_row)
            elif table_id == TableID.IMAGES:
                self._show_image_details(pane, event.cursor_row)
            elif table_id == TableID.VOLUMES:
                self._show_volume_details(pane, event.cursor_row)
            elif table_id == TableID.NETWORKS:
                self._show_network_details(pane, event.cursor_row)
        except IndexError:
            pane.clear_details()

    @on(TabbedContent.TabActivated)
    def clear_on_tab_switch(self) -> None:
        self._auto_select_first()

    def action_help(self) -> None:
        help_data = [
            ("q", "quit", "Quit"),
            ("r", "refresh", "Refresh"),
            ("/", "open_search", "Search"),
            ("?", "help", "Help"),
            ("d", "delete", "Delete selected resource"),
            ("s", "stop_container", "Stop container"),
            ("S", "start_container", "Start container"),
            ("x", "restart_container", "Restart container"),
            ("e", "exec_container", "Exec shell in container"),
            ("l", "view_logs", "Toggle log viewer"),
            ("f", "follow_logs", "Toggle live log streaming"),
            ("z", "toggle_log_expand", "Expand / collapse log pane"),
        ]
        self.push_screen(HelpScreen(help_data))


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
