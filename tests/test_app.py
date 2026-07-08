import asyncio
import threading
import unittest
from typing import Callable
from unittest.mock import patch

from rich.console import Console
from rich.table import Table as RichTable
from textual.widgets import (
    Checkbox,
    DataTable,
    Input,
    LoadingIndicator,
    RichLog,
    Static,
    TabbedContent,
)

from docksurf_py.actions import (
    ContainerActionHandler,
    ImageActionHandler,
    NetworkActionHandler,
    VolumeActionHandler,
    _open_in_browser,
)
from docksurf_py.app import (
    DockSurfApp,
    _compose_actions,
    _container_only_actions,
    _tab_actions,
)
from docksurf_py.config import Config
from docksurf_py.connection import ConnectionState, ConnectionStatus
from docksurf_py.constants import (
    BTN_EXPAND_ID,
    CONFIRM_FORCE_CHECKBOX_ID,
    CONNECTION_BANNER_ID,
    DETAIL_PANE_ID,
    EMPTY_STATE_IDS,
    LOG_PANE_ID,
    SEARCH_BAR_ID,
    STATUS_BAR_ID,
    LogLine,
    LogOptions,
    TabID,
    TableID,
)
from docksurf_py.docker import EventStream, StatsStream
from docksurf_py.models import (
    CommandResult,
    ComposeProject,
    Container,
    ContainerTop,
    ContextInfo,
    DockerSnapshot,
    Image,
    ImageLayer,
    Network,
    NetworkEndpoint,
    PortBinding,
    SystemDf,
    Volume,
)
from docksurf_py.session import SessionState
from docksurf_py.widgets import (
    ConfirmDialog,
    ContainerPickerScreen,
    DetailPane,
    HelpScreen,
    InspectScreen,
    LayerHistoryScreen,
    LogOptionsScreen,
    LogPane,
    PromptScreen,
    PruneScreen,
    PullProgressScreen,
    StatusBar,
)
from tests.test_compose import make_container

EMPTY_SNAPSHOT = DockerSnapshot([], [], [], [])

_CONNECTED_STATE = ConnectionState(
    status=ConnectionStatus.CONNECTED,
    message="Connected",
    hint="",
    context="default",
    host="unix:///var/run/docker.sock",
)


class FakeLogStream:
    """A finite `LogSource` yielding fixed `LogLine`s, for driving `LogPane`."""

    def __init__(self, lines: list[LogLine]) -> None:
        self._lines = list(lines)
        self._active = True

    def __iter__(self):
        for line in self._lines:
            if not self._active:
                break
            yield line

    def stop(self) -> None:
        self._active = False


class FakePullStream:
    """A finite pull stream yielding fixed progress dicts, for driving pull UI."""

    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = list(chunks)
        self._active = True

    def __iter__(self):
        for chunk in self._chunks:
            if not self._active:
                break
            yield chunk

    def stop(self) -> None:
        self._active = False


class FakeEventStream:
    """A finite `EventStream`-shaped fake with a settable `.error`.

    Yields nothing and ends immediately; `.error` lets a test simulate the
    daemon dropping mid-stream (`start_event_listener` reads it to decide
    whether to call `mark_disconnected`).
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def __iter__(self):
        return iter(())

    def stop(self) -> None:
        pass


class MockDockerService:
    def __init__(self, fetch_fn: Callable[[], DockerSnapshot]) -> None:
        self._fetch_fn = fetch_fn
        self.connection = _CONNECTED_STATE
        # Tests can flip this (and `connection`) to simulate a daemon drop —
        # `ensure_connected()` below just returns whatever was set here.
        self._connected = True
        # Categories that failed on the last fetch_snapshot() call; tests can
        # override to exercise SnapshotManager's partial-failure notice.
        self.last_fetch_errors: list[str] = []
        # Recorded (method_name, *args) tuples for every write call — lets
        # bulk/prune tests assert who was actually invoked.
        self.calls: list[tuple] = []
        # Contexts returned by list_contexts(); tests can override.
        self.contexts: list[ContextInfo] = [
            ContextInfo(
                name="default", host="unix:///var/run/docker.sock", is_current=True
            )
        ]
        # Result returned by switch_context(); tests can override.
        self.switch_result: CommandResult = CommandResult.success("Switched")
        # Lines a log stream yields; tests can override before opening logs.
        self.log_lines: list[LogLine] = [
            LogLine(text="starting up", ts="2024-01-01T00:00:00Z"),
            LogLine(text="request handled", ts="2024-01-01T00:00:01Z"),
            LogLine(text="boom", ts="2024-01-01T00:00:02Z", stream="stderr"),
        ]
        # Progress dicts a pull stream yields; tests can override.
        self.pull_chunks: list[dict] = [
            {"status": "Pulling from library/alpine"},
            {"status": "Pull complete", "id": "abc123"},
            {"status": "Status: Downloaded newer image for alpine:latest"},
        ]
        # Per-volume sizes returned by volume_sizes(); tests can override.
        self.volume_size_map: dict[str, int] = {}
        # Layer history returned by image_history(); tests can override.
        self.history_layers: list[ImageLayer] = [
            ImageLayer(created_by="/bin/sh -c #(nop) CMD", size_bytes=0, created="0"),
            ImageLayer(
                created_by="/bin/sh -c apk add curl", size_bytes=1024, created="0"
            ),
        ]
        # Per-image Architecture returned by image_architecture(); tests can
        # override. Keyed by image id, default "amd64" for anything unlisted.
        self.architecture_map: dict[str, str] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    def ensure_connected(self) -> ConnectionState:
        self.calls.append(("ensure_connected",))
        return self.connection

    def mark_disconnected(self, exc: Exception) -> None:
        self.calls.append(("mark_disconnected", str(exc)))
        self._connected = False
        self.connection = ConnectionState(
            status=ConnectionStatus.DAEMON_UNAVAILABLE,
            message="Docker daemon is not running",
            hint="Start Docker Desktop, or run: sudo systemctl start docker",
            context=self.connection.context,
            host=self.connection.host,
        )

    def fetch_snapshot(self) -> DockerSnapshot:
        return self._fetch_fn()

    def list_contexts(self) -> list[ContextInfo]:
        self.calls.append(("list_contexts",))
        return self.contexts

    def switch_context(self, name: str) -> CommandResult:
        self.calls.append(("switch_context", name))
        return self.switch_result

    def stream_logs(self, container_id, options=None):
        self.calls.append(("stream_logs", container_id, options))
        return FakeLogStream(self.log_lines)

    def stream_project_logs(self, specs, options=None):
        self.calls.append(("stream_project_logs", tuple(specs), options))
        return FakeLogStream(self.log_lines)

    def stream_stats(self, container_id):
        return StatsStream(container_id, None)

    def stream_events(self):
        return EventStream(None)

    def stream_pull(self, repository: str, tag: str = "latest"):
        self.calls.append(("stream_pull", repository, tag))
        return FakePullStream(self.pull_chunks)

    def system_df(self) -> SystemDf:
        return SystemDf(entries=[], total_size=0, total_reclaimable=0)

    def image_history(self, image_id: str):
        self.calls.append(("image_history", image_id))
        return self.history_layers

    def image_architecture(self, image_id: str) -> str | None:
        self.calls.append(("image_architecture", image_id))
        return self.architecture_map.get(image_id, "amd64")

    def volume_sizes(self) -> dict[str, int]:
        self.calls.append(("volume_sizes",))
        return self.volume_size_map

    def compose_action(
        self, project, verb, config_files="", working_dir=""
    ) -> CommandResult:
        self.calls.append(("compose_action", project, verb))
        return CommandResult.success()

    def stop_container(self, container_id: str) -> CommandResult:
        self.calls.append(("stop_container", container_id))
        return CommandResult.success()

    def start_container(self, container_id: str) -> CommandResult:
        self.calls.append(("start_container", container_id))
        return CommandResult.success()

    def restart_container(self, container_id: str) -> CommandResult:
        self.calls.append(("restart_container", container_id))
        return CommandResult.success()

    def remove_container(self, container_id: str, force: bool = False) -> CommandResult:
        self.calls.append(("remove_container", container_id, force))
        return CommandResult.success()

    def remove_image(self, image_id: str, force: bool = False) -> CommandResult:
        self.calls.append(("remove_image", image_id, force))
        return CommandResult.success()

    def remove_volume(self, volume_name: str) -> CommandResult:
        self.calls.append(("remove_volume", volume_name))
        return CommandResult.success()

    def remove_network(self, network_name: str) -> CommandResult:
        self.calls.append(("remove_network", network_name))
        return CommandResult.success()

    def tag_image(
        self, image_id: str, repository: str, tag: str = "latest"
    ) -> CommandResult:
        self.calls.append(("tag_image", image_id, repository, tag))
        return CommandResult.success(f"Tagged {repository}:{tag}")

    def create_volume(
        self,
        name: str,
        driver: str = "local",
        labels: dict[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(("create_volume", name, driver, labels or {}))
        return CommandResult.success(f"Created volume {name}")

    def create_network(
        self, name: str, driver: str = "bridge", subnet: str = ""
    ) -> CommandResult:
        self.calls.append(("create_network", name, driver, subnet))
        return CommandResult.success(f"Created network {name}")

    def connect_container(self, network_name: str, container_id: str) -> CommandResult:
        self.calls.append(("connect_container", network_name, container_id))
        return CommandResult.success("Connected")

    def disconnect_container(
        self, network_name: str, container_id: str, force: bool = True
    ) -> CommandResult:
        self.calls.append(("disconnect_container", network_name, container_id))
        return CommandResult.success("Disconnected")

    def pause_container(self, container_id: str) -> CommandResult:
        self.calls.append(("pause_container", container_id))
        return CommandResult.success()

    def unpause_container(self, container_id: str) -> CommandResult:
        self.calls.append(("unpause_container", container_id))
        return CommandResult.success()

    def kill_container(self, container_id: str) -> CommandResult:
        self.calls.append(("kill_container", container_id))
        return CommandResult.success()

    def prune_containers(self) -> CommandResult:
        self.calls.append(("prune_containers",))
        return CommandResult.success("Pruned 0 container(s) — reclaimed 0B")

    def prune_images(self) -> CommandResult:
        self.calls.append(("prune_images",))
        return CommandResult.success("Pruned 0 image(s) — reclaimed 0B")

    def prune_volumes(self) -> CommandResult:
        self.calls.append(("prune_volumes",))
        return CommandResult.success("Pruned 0 volume(s) — reclaimed 0B")

    def prune_networks(self) -> CommandResult:
        self.calls.append(("prune_networks",))
        return CommandResult.success("Pruned 0 network(s)")

    def prune_system(self) -> CommandResult:
        self.calls.append(("prune_system",))
        return CommandResult.success("System prune: 0 item(s) removed — reclaimed 0B")

    def inspect_resource(self, kind: str, ref: str) -> dict | None:
        self.calls.append(("inspect_resource", kind, ref))
        return {}

    def container_top(self, container_id: str) -> ContainerTop | None:
        self.calls.append(("container_top", container_id))
        return ContainerTop(titles=[], processes=[])

    def container_cp(self, src: str, dst: str) -> CommandResult:
        self.calls.append(("container_cp", src, dst))
        return CommandResult.success(f"Copied {src} → {dst}")


async def wait_until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class RefreshLoadingIndicatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_request_is_ignored_while_refresh_is_active(self) -> None:
        started = threading.Event()
        release = threading.Event()
        call_count = 0
        call_count_lock = threading.Lock()

        def fetch() -> DockerSnapshot:
            nonlocal call_count
            with call_count_lock:
                call_count += 1
            started.set()
            release.wait(timeout=2)
            return EMPTY_SNAPSHOT

        app = DockSurfApp(docker=MockDockerService(fetch))
        async with app.run_test() as pilot:
            await asyncio.to_thread(started.wait, 1)
            app.action_refresh()
            await pilot.pause()

            with call_count_lock:
                self.assertEqual(call_count, 1)

            release.set()

    async def test_indicator_tracks_successful_refresh(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def fetch() -> DockerSnapshot:
            started.set()
            release.wait(timeout=2)
            return EMPTY_SNAPSHOT

        app = DockSurfApp(docker=MockDockerService(fetch))
        async with app.run_test() as pilot:
            await asyncio.to_thread(started.wait, 1)
            await pilot.pause()

            indicator = app.query_one("#refresh-loading", LoadingIndicator)
            self.assertTrue(indicator.display)
            self.assertIsNotNone(app.query_one("#main-container"))

            release.set()
            await wait_until(lambda: not indicator.display)

    async def test_indicator_hides_when_refresh_fails(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def fetch() -> DockerSnapshot:
            started.set()
            release.wait(timeout=2)
            raise RuntimeError("Docker is unavailable")

        app = DockSurfApp(docker=MockDockerService(fetch))
        async with app.run_test() as pilot:
            await asyncio.to_thread(started.wait, 1)
            await pilot.pause()

            indicator = app.query_one("#refresh-loading", LoadingIndicator)
            self.assertTrue(indicator.display)

            release.set()
            await wait_until(lambda: not indicator.display)


def _standalone_snapshot(running: bool, state: str | None = None) -> DockerSnapshot:
    c = make_container("web", running=running)
    if state is not None:
        c.state = state
    return DockerSnapshot(containers=[c], images=[], volumes=[], networks=[])


class PauseKillActionTests(unittest.IsolatedAsyncioTestCase):
    async def _select_first_row(self, app: DockSurfApp) -> None:
        await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
        table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
        table.move_cursor(row=0)

    async def test_pause_running_container(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            app.action_pause_container()
            await wait_until(lambda: ("pause_container", "web") in app.docker.calls)
            self.assertIn(("pause_container", "web"), app.docker.calls)

    async def test_unpause_paused_container(self) -> None:
        snapshot = _standalone_snapshot(running=False, state="paused")
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            app.action_pause_container()
            await wait_until(lambda: ("unpause_container", "web") in app.docker.calls)
            self.assertIn(("unpause_container", "web"), app.docker.calls)

    async def test_pause_stopped_container_is_noop(self) -> None:
        snapshot = _standalone_snapshot(running=False)  # state="exited"
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            app.action_pause_container()
            await pilot.pause()
            self.assertEqual(app.docker.calls, [])

    async def test_kill_running_container(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            app.action_kill_container()
            await wait_until(lambda: ("kill_container", "web") in app.docker.calls)
            self.assertIn(("kill_container", "web"), app.docker.calls)

    async def test_kill_stopped_container_is_guarded(self) -> None:
        snapshot = _standalone_snapshot(running=False)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            app.action_kill_container()
            await pilot.pause()
            self.assertEqual(app.docker.calls, [])

    async def test_pause_without_focused_container_does_not_call_docker(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_pause_container()
            await pilot.pause()
            self.assertEqual(app.docker.calls, [])

    async def test_kill_without_focused_container_does_not_call_docker(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_kill_container()
            await pilot.pause()
            self.assertEqual(app.docker.calls, [])


class HelpScreenTests(unittest.IsolatedAsyncioTestCase):
    def test_container_only_actions_matches_container_action_handler(self) -> None:
        expected = {
            name.removeprefix("action_")
            for name in vars(ContainerActionHandler)
            if name.startswith("action_")
        }
        self.assertEqual(_container_only_actions(), expected)
        # "delete" belongs to ResourceDeletionHandler and applies to every
        # resource tab -- it must never be classified as container-scoped.
        self.assertNotIn("delete", _container_only_actions())

    async def test_help_screen_rows_match_bindings_with_correct_scope(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_help()
            await pilot.pause()

            screen = app.screen_stack[-1]
            self.assertIsInstance(screen, HelpScreen)
            table = next(
                w.content
                for w in screen.query(Static)
                if isinstance(getattr(w, "content", None), RichTable)
            )
            rows = list(zip(*(list(c.cells) for c in table.columns)))

            tab_scopes = {
                "Images tab": _tab_actions(ImageActionHandler),
                "Volumes tab": _tab_actions(VolumeActionHandler),
                "Networks tab": _tab_actions(NetworkActionHandler),
            }
            for item in app.BINDINGS:
                if isinstance(item, tuple):
                    key, action, description = item
                else:
                    key, action, description = item.key, item.action, item.description
                if not description:
                    continue
                match = next(r for r in rows if r[1] == description)
                if action in _compose_actions():
                    expected_scope = "Compose project"
                elif action in _container_only_actions():
                    expected_scope = "Container only"
                else:
                    expected_scope = "Global"
                    for label, actions in tab_scopes.items():
                        if action in actions:
                            expected_scope = label
                            break
                self.assertEqual(match[2], expected_scope, f"key={key} action={action}")

            # Regression: the old hand-maintained frozenset mislabeled
            # "delete" as container-only even though it applies to every tab.
            delete_row = next(r for r in rows if r[1] == "Delete")
            self.assertEqual(delete_row[2], "Global")


class CommandPaletteTests(unittest.IsolatedAsyncioTestCase):
    async def test_includes_every_described_binding_action(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            commands = list(app.get_system_commands(app.screen))
            titles = {c.title for c in commands}
            self.assertIn("Refresh", titles)
            self.assertIn("Delete", titles)
            # `show=False` actions (hidden from the footer) must still surface.
            self.assertIn("Docker context", titles)
            self.assertIn("Log options", titles)
            # Base Textual commands (Theme, Quit, etc.) must still be present.
            self.assertIn("Theme", titles)
            self.assertIn("Quit", titles)

    async def test_command_callback_invokes_the_bound_action(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            commands = list(app.get_system_commands(app.screen))
            refresh_cmd = next(c for c in commands if c.title == "Refresh")
            self.assertEqual(refresh_cmd.callback, app.action_refresh)


def _compose_snapshot() -> DockerSnapshot:
    return DockerSnapshot(
        containers=[
            make_container("standalone"),
            make_container("myapp-web", project="myapp", service="web"),
            make_container("myapp-db", project="myapp", service="db", running=False),
        ],
        images=[],
        volumes=[],
        networks=[],
    )


class ComposeGroupingTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_list_interleaves_header_and_service_rows(self) -> None:
        app = DockSurfApp(docker=MockDockerService(_compose_snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))

            rows = app._current[TabID.CONTAINERS]
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)

            # Row-index invariant: _current length matches the DataTable rows.
            self.assertEqual(len(rows), table.row_count)

            # myapp (sorted) group header, then its two services, then standalone.
            self.assertIsInstance(rows[0], ComposeProject)
            self.assertEqual(rows[0].name, "myapp")
            self.assertIsInstance(rows[1], Container)
            self.assertIsInstance(rows[2], Container)
            self.assertIsInstance(rows[3], Container)
            self.assertEqual(rows[3].name, "standalone")

    async def test_focused_project_resolves_from_header_and_service_row(self) -> None:
        app = DockSurfApp(docker=MockDockerService(_compose_snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)

            # Header row -> project.
            table.move_cursor(row=0)
            self.assertTrue(app._focused_is_project_header())
            self.assertEqual(app._get_focused_project().name, "myapp")

            # A service row -> its parent project; not a header.
            table.move_cursor(row=1)
            self.assertFalse(app._focused_is_project_header())
            self.assertEqual(app._get_focused_project().name, "myapp")
            self.assertIsNotNone(app._get_focused_container())

    async def test_collapse_hides_service_rows(self) -> None:
        app = DockSurfApp(docker=MockDockerService(_compose_snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)

            full_rows = len(app._current[TabID.CONTAINERS])
            table.move_cursor(row=0)
            app.action_toggle_group()
            await pilot.pause()

            collapsed_rows = app._current[TabID.CONTAINERS]
            # Header + standalone remain; the two services are hidden.
            self.assertEqual(len(collapsed_rows), full_rows - 2)
            self.assertIn("myapp", app._collapsed_projects)


class TabNavigationTests(unittest.IsolatedAsyncioTestCase):
    """SearchBar (an Input) holds default focus on mount and would otherwise
    swallow digit/bracket keys as text — these tests explicitly focus the
    container table first, same as LogViewerTests._open_logs does.
    """

    async def test_digit_keys_switch_tabs(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).focus()
            await pilot.press("2")
            self.assertEqual(app.query_one(TabbedContent).active, TabID.IMAGES)
            await pilot.press("4")
            self.assertEqual(app.query_one(TabbedContent).active, TabID.NETWORKS)
            await pilot.press("1")
            self.assertEqual(app.query_one(TabbedContent).active, TabID.CONTAINERS)

    async def test_bracket_keys_cycle_tabs_with_wraparound(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).focus()
            self.assertEqual(app.query_one(TabbedContent).active, TabID.CONTAINERS)
            await pilot.press("[")
            self.assertEqual(app.query_one(TabbedContent).active, TabID.NETWORKS)
            await pilot.press("]")
            self.assertEqual(app.query_one(TabbedContent).active, TabID.CONTAINERS)
            await pilot.press("]")
            self.assertEqual(app.query_one(TabbedContent).active, TabID.IMAGES)

    async def test_ctrl_u_triggers_compose_up_on_focused_project(self) -> None:
        svc = MockDockerService(_compose_snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("ctrl+u")
            await wait_until(lambda: ("compose_action", "myapp", "up") in svc.calls)

    async def test_ctrl_k_triggers_compose_down_after_confirm(self) -> None:
        svc = MockDockerService(_compose_snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("ctrl+k")
            await wait_until(
                lambda: any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(True)
            await wait_until(lambda: ("compose_action", "myapp", "down") in svc.calls)


class InspectActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_container_opens_screen_with_json(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        svc = MockDockerService(lambda: snapshot)
        svc.inspect_resource = lambda kind, ref: {  # type: ignore[method-assign]
            "Id": ref,
            "Kind": kind,
        }
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.move_cursor(row=0)

            app.action_inspect()
            await wait_until(
                lambda: any(isinstance(s, InspectScreen) for s in app.screen_stack)
            )
            self.assertTrue(any(isinstance(s, InspectScreen) for s in app.screen_stack))

    async def test_inspect_project_header_notifies_without_opening_screen(
        self,
    ) -> None:
        app = DockSurfApp(docker=MockDockerService(_compose_snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.move_cursor(row=0)  # header row for "myapp"
            self.assertTrue(app._focused_is_project_header())

            app.action_inspect()
            await pilot.pause()
            self.assertFalse(
                any(isinstance(s, InspectScreen) for s in app.screen_stack)
            )

    async def test_inspect_with_nothing_selected_does_not_open_screen(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_inspect()
            await pilot.pause()
            self.assertFalse(
                any(isinstance(s, InspectScreen) for s in app.screen_stack)
            )

    async def test_inspect_none_result_does_not_open_screen(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        svc = MockDockerService(lambda: snapshot)

        def _none_inspect(kind: str, ref: str) -> None:
            svc.calls.append(("inspect_resource", kind, ref))
            return None

        svc.inspect_resource = _none_inspect  # type: ignore[method-assign]
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.move_cursor(row=0)

            app.action_inspect()
            await wait_until(
                lambda: ("inspect_resource", "container", "web") in svc.calls
            )
            await pilot.pause()
            self.assertFalse(
                any(isinstance(s, InspectScreen) for s in app.screen_stack)
            )

    async def test_inspect_on_images_tab_dispatches_image_kind(self) -> None:
        img = Image(
            id="sha256:abc",
            repository="nginx",
            tag="latest",
            size_bytes=100,
            is_dangling=False,
            used_by=[],
            created="",
        )
        snapshot = DockerSnapshot(containers=[], images=[img], volumes=[], networks=[])
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.IMAGES
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.IMAGES)))
            table = app.query_one(f"#{TableID.IMAGES}", DataTable)
            table.move_cursor(row=0)

            app.action_inspect()
            await wait_until(
                lambda: ("inspect_resource", "image", "sha256:abc") in svc.calls
            )
            await wait_until(
                lambda: any(isinstance(s, InspectScreen) for s in app.screen_stack)
            )


class PruneActionTests(unittest.IsolatedAsyncioTestCase):
    async def _open_prune_screen(self, app: DockSurfApp) -> "PruneScreen":
        app.action_prune()
        await wait_until(
            lambda: any(isinstance(s, PruneScreen) for s in app.screen_stack)
        )
        return app.screen_stack[-1]

    async def _open_confirm_dialog(self, app: DockSurfApp) -> "ConfirmDialog":
        await wait_until(
            lambda: any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
        )
        return app.screen_stack[-1]

    async def test_prune_containers_end_to_end(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()

            prune_screen = await self._open_prune_screen(app)
            prune_screen.dismiss("containers")
            await pilot.pause()

            confirm = await self._open_confirm_dialog(app)
            self.assertIn("STOPPED containers", confirm._message)
            confirm.dismiss(True)
            await pilot.pause()

            await wait_until(lambda: ("prune_containers",) in svc.calls)
            self.assertIn(("prune_containers",), svc.calls)

    async def test_prune_cancelled_at_target_picker_does_not_call_docker(
        self,
    ) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()

            prune_screen = await self._open_prune_screen(app)
            prune_screen.dismiss(None)
            await pilot.pause()
            self.assertEqual(svc.calls, [])

    async def test_prune_cancelled_at_confirm_does_not_call_docker(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()

            prune_screen = await self._open_prune_screen(app)
            prune_screen.dismiss("system")
            await pilot.pause()

            confirm = await self._open_confirm_dialog(app)
            confirm.dismiss(False)
            await pilot.pause()
            self.assertEqual(svc.calls, [])

    async def test_prune_volumes_confirm_mentions_anonymous(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()

            prune_screen = await self._open_prune_screen(app)
            prune_screen.dismiss("volumes")
            await pilot.pause()

            confirm = await self._open_confirm_dialog(app)
            self.assertIn("anonymous", confirm._message)

    async def test_prune_each_target_maps_to_matching_docker_method(self) -> None:
        targets_to_methods = [
            ("containers", "prune_containers"),
            ("images", "prune_images"),
            ("volumes", "prune_volumes"),
            ("networks", "prune_networks"),
            ("system", "prune_system"),
        ]
        for target, method_name in targets_to_methods:
            with self.subTest(target=target):
                svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
                app = DockSurfApp(docker=svc)
                async with app.run_test() as pilot:
                    await pilot.pause()

                    prune_screen = await self._open_prune_screen(app)
                    prune_screen.dismiss(target)
                    await pilot.pause()

                    confirm = await self._open_confirm_dialog(app)
                    confirm.dismiss(True)
                    await pilot.pause()

                    await wait_until(lambda: (method_name,) in svc.calls)
                    self.assertIn((method_name,), svc.calls)


class ConfirmToggleTests(unittest.IsolatedAsyncioTestCase):
    """`Config`'s confirm_* toggles skip their `ConfirmDialog` when False."""

    async def test_confirm_delete_false_skips_dialog(self) -> None:
        snapshot = _standalone_snapshot(running=False)
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc, config=Config(confirm_delete=False))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)

            app.action_delete()
            await wait_until(lambda: ("remove_container", "web", False) in svc.calls)
            self.assertFalse(
                any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
            )

    async def test_confirm_delete_false_skips_dialog_for_bulk_delete(self) -> None:
        snapshot = _standalone_snapshot(running=False)
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc, config=Config(confirm_delete=False))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("space")  # mark the row

            app.action_delete()
            await wait_until(lambda: ("remove_container", "web", False) in svc.calls)
            self.assertFalse(
                any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
            )

    async def test_confirm_compose_down_false_skips_dialog(self) -> None:
        svc = MockDockerService(_compose_snapshot)
        app = DockSurfApp(docker=svc, config=Config(confirm_compose_down=False))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.press("ctrl+k")
            await wait_until(lambda: ("compose_action", "myapp", "down") in svc.calls)
            self.assertFalse(
                any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
            )

    async def test_confirm_prune_false_skips_dialog(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc, config=Config(confirm_prune=False))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_prune()
            await wait_until(
                lambda: any(isinstance(s, PruneScreen) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss("containers")
            await wait_until(lambda: ("prune_containers",) in svc.calls)
            self.assertFalse(
                any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
            )


class SessionRestoreTests(unittest.IsolatedAsyncioTestCase):
    """`DockSurfApp` restores active tab / sort order from an injected
    `SessionState` — never touching the real filesystem (see `session.py`'s
    module docstring on why persistence is constructor-injected)."""

    async def test_restores_active_tab(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(
            docker=svc, session=SessionState(active_tab=TabID.IMAGES.value)
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one(TabbedContent).active, TabID.IMAGES)

    async def test_invalid_active_tab_falls_back_to_default(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc, session=SessionState(active_tab="not-a-tab"))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one(TabbedContent).active, TabID.CONTAINERS)

    async def test_restores_sort_state_and_renders_column_arrow(self) -> None:
        snapshot = _standalone_snapshot(running=False)
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(
            docker=svc,
            session=SessionState(sort_state={TabID.CONTAINERS.value: ("Name", True)}),
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app._sort_state[TabID.CONTAINERS], ("Name", True))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            labels = [str(col.label) for col in table.columns.values()]
            self.assertTrue(any("Name" in label and "▼" in label for label in labels))

    async def test_default_construction_does_not_persist(self) -> None:
        """Without `persist_session=True` (the default), switching tabs never
        touches disk — guards the test-hermeticity fix from the plan."""
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc)
        with patch("docksurf_py.app.save_session") as save:
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one(TabbedContent).active = TabID.IMAGES
                await pilot.pause()
        save.assert_not_called()

    async def test_persist_session_true_saves_on_tab_switch(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc, persist_session=True)
        with patch("docksurf_py.app.save_session") as save:
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one(TabbedContent).active = TabID.IMAGES
                await pilot.pause()
        save.assert_called()
        self.assertEqual(app._session.active_tab, TabID.IMAGES)

    async def test_restores_theme(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc, session=SessionState(theme="docksurf-nightcity"))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.theme, "docksurf-nightcity")

    async def test_invalid_theme_falls_back_to_default(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc, session=SessionState(theme="not-a-theme"))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.theme, "docksurf-ocean")

    async def test_cycle_theme_key_cycles_and_persists(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc, persist_session=True)
        with patch("docksurf_py.app.save_session") as save:
            async with app.run_test() as pilot:
                await pilot.pause()
                # Key-press tests need explicit focus on the container table —
                # see TabNavigationTests/LogViewerTests._open_logs.
                app.query_one(f"#{TableID.CONTAINERS}", DataTable).focus()
                self.assertEqual(app.theme, "docksurf-ocean")
                await pilot.press("M")
                await pilot.pause()
                self.assertEqual(app.theme, "docksurf-nightcity")
                self.assertEqual(app._session.theme, "docksurf-nightcity")
        save.assert_called()


class ExecCustomActionTests(unittest.IsolatedAsyncioTestCase):
    """Exercises the guard/prompt/cancel paths of `E` without ever reaching
    the real `subprocess.run`/`self.suspend()` interactive-exec tail — the
    shell-detection probe (`_container_has_shell`) is patched out since it
    shells out to `docker exec ... which <shell>` for real. `shutil.which`
    is also patched out: `_exec_preflight`'s real PATH check would otherwise
    make these tests depend on the `docker` CLI being installed on the
    machine running them, which isn't true on the macOS/Windows CI runners."""

    _HAS_SHELL_PATCH_TARGET = (
        "docksurf_py.actions.ContainerActionHandler._container_has_shell"
    )
    _WHICH_PATCH_TARGET = "docksurf_py.actions.container.shutil.which"

    async def _select_first_row(self, app: DockSurfApp) -> None:
        await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
        table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
        table.move_cursor(row=0)

    async def test_exec_custom_prompts_with_detected_shell_prefilled(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            with (
                patch(self._HAS_SHELL_PATCH_TARGET, return_value=True),
                patch(self._WHICH_PATCH_TARGET, return_value="/usr/bin/docker"),
            ):
                app.action_exec_custom()
                await wait_until(
                    lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
                )
            screen = app.screen_stack[-1]
            inputs = list(screen.query(Input))
            self.assertEqual(len(inputs), 2)
            self.assertEqual(inputs[0].value, "bash")
            self.assertEqual(inputs[1].value, "")

    async def test_exec_custom_falls_back_to_sh_when_no_shell_detected(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            with (
                patch(self._HAS_SHELL_PATCH_TARGET, return_value=False),
                patch(self._WHICH_PATCH_TARGET, return_value="/usr/bin/docker"),
            ):
                app.action_exec_custom()
                await wait_until(
                    lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
                )
            screen = app.screen_stack[-1]
            inputs = list(screen.query(Input))
            self.assertEqual(inputs[0].value, "sh")

    async def test_exec_custom_guarded_when_not_running(self) -> None:
        snapshot = _standalone_snapshot(running=False)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            app.action_exec_custom()
            await pilot.pause()
            self.assertFalse(any(isinstance(s, PromptScreen) for s in app.screen_stack))

    async def test_exec_custom_cancel_closes_without_running_anything(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            with (
                patch(self._HAS_SHELL_PATCH_TARGET, return_value=True),
                patch(self._WHICH_PATCH_TARGET, return_value="/usr/bin/docker"),
            ):
                app.action_exec_custom()
                await wait_until(
                    lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
                )
                app.screen_stack[-1].dismiss(None)
                await pilot.pause()
            self.assertFalse(any(isinstance(s, PromptScreen) for s in app.screen_stack))

    async def test_exec_custom_empty_command_warns_without_running_anything(
        self,
    ) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            with (
                patch(self._HAS_SHELL_PATCH_TARGET, return_value=True),
                patch(self._WHICH_PATCH_TARGET, return_value="/usr/bin/docker"),
            ):
                app.action_exec_custom()
                await wait_until(
                    lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
                )
                app.screen_stack[-1].dismiss(["   ", ""])
                await pilot.pause()
            self.assertFalse(any(isinstance(s, PromptScreen) for s in app.screen_stack))


class CopyFilesActionTests(unittest.IsolatedAsyncioTestCase):
    async def _select_first_row(self, app: DockSurfApp) -> None:
        await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
        table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
        table.move_cursor(row=0)

    async def test_copy_files_prompts_with_prefilled_defaults(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            app.action_copy_files()
            await wait_until(
                lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
            )
            screen = app.screen_stack[-1]
            inputs = list(screen.query(Input))
            self.assertEqual(inputs[0].value, "web:")
            self.assertEqual(inputs[1].value, ".")

    async def test_copy_files_guarded_when_nothing_focused(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_copy_files()
            await pilot.pause()
            self.assertFalse(any(isinstance(s, PromptScreen) for s in app.screen_stack))

    async def test_copy_files_cancel_does_not_call_docker(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            app.action_copy_files()
            await wait_until(
                lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(None)
            await pilot.pause()
            self.assertEqual(svc.calls, [])

    async def test_copy_files_invalid_prefix_warns_without_calling_docker(
        self,
    ) -> None:
        snapshot = _standalone_snapshot(running=True)
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            app.action_copy_files()
            await wait_until(
                lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
            )
            # Neither side prefixed with "web:" -- invalid.
            app.screen_stack[-1].dismiss(["./a", "./b"])
            await pilot.pause()
            self.assertEqual(svc.calls, [])

    async def test_copy_files_valid_input_calls_docker_container_cp(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            app.action_copy_files()
            await wait_until(
                lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(["web:/etc/hosts", "./hosts"])
            await pilot.pause()
            await wait_until(lambda: bool(svc.calls))
            self.assertIn(("container_cp", "web:/etc/hosts", "./hosts"), svc.calls)


class LogViewerTests(unittest.IsolatedAsyncioTestCase):
    async def _open_logs(self, app: DockSurfApp, pilot) -> LogPane:
        await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
        # Focus the container table and open logs via the real keybinding, so
        # the test exercises binding routing (keys resolve through
        # ContainerTable.BINDINGS while the table keeps focus), not just the
        # action handlers.
        table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("l")
        log_pane = app.query_one(f"#{LOG_PANE_ID}", LogPane)
        # Wait for the daemon stream thread to marshal all lines into the buffer.
        await wait_until(lambda: len(log_pane._line_buffer) == 3)
        return log_pane

    async def test_open_logs_streams_lines_into_buffer(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            log_pane = await self._open_logs(app, pilot)
            self.assertTrue(log_pane.display)
            texts = [line.text for line in log_pane._line_buffer]
            self.assertEqual(texts, ["starting up", "request handled", "boom"])
            self.assertEqual(log_pane._line_buffer[2].stream, "stderr")

    async def test_toggle_timestamps_and_wrap_via_keys(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            log_pane = await self._open_logs(app, pilot)

            self.assertFalse(log_pane._show_timestamps)
            await pilot.press("T")
            self.assertTrue(log_pane._show_timestamps)

            self.assertFalse(log_pane._wrap)
            await pilot.press("W")
            self.assertTrue(log_pane._wrap)
            self.assertTrue(app.query_one(f"#{LOG_PANE_ID} RichLog", RichLog).wrap)

    async def test_filter_and_match_jump(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            log_pane = await self._open_logs(app, pilot)

            # Drive the filter directly (the "/" key needs the pane focused).
            log_pane._filter = "handled"
            log_pane._render_to_view()
            self.assertEqual(log_pane._match_count, 1)
            self.assertEqual(log_pane._match_cursor, -1)

            app.action_next_match()
            self.assertEqual(log_pane._match_cursor, 0)
            # Wraps around a single match.
            app.action_next_match()
            self.assertEqual(log_pane._match_cursor, 0)
            app.action_prev_match()
            self.assertEqual(log_pane._match_cursor, 0)

    async def test_export_writes_file(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._open_logs(app, pilot)
            with patch("docksurf_py.actions.container._write_log_export") as writer:
                writer.return_value = "/tmp/web-x.log"
                app.action_export_logs()
            self.assertTrue(writer.called)
            name, text = writer.call_args.args
            self.assertEqual(name, "web")
            self.assertIn("boom", text)
            self.assertIn("[stderr]", text)

    async def test_log_options_applies_new_options(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            log_pane = await self._open_logs(app, pilot)

            app.action_log_options()
            await wait_until(
                lambda: any(isinstance(s, LogOptionsScreen) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(LogOptions(tail=5000, since_seconds=300))
            await wait_until(lambda: log_pane.options.tail == 5000)
            self.assertEqual(log_pane.options.since_seconds, 300)
            # The re-subscribe passed the new options to the stream factory.
            self.assertTrue(
                any(
                    c[0] == "stream_logs" and c[2] == LogOptions(5000, 300)
                    for c in svc.calls
                )
            )

    async def test_expand_button_click_toggles_expanded(self) -> None:
        # Regression: LogPane.ToggleExpand used to be handled by a mixin
        # method (same class of dispatch bug as the header-click sort and
        # live-search regressions above) — a real button click did nothing.
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            log_pane = await self._open_logs(app, pilot)
            self.assertFalse(log_pane.has_class("expanded"))
            await pilot.click(f"#{LOG_PANE_ID} #{BTN_EXPAND_ID}")
            await pilot.pause()
            self.assertTrue(log_pane.has_class("expanded"))


def _image(repo="alpine", tag="latest", dangling=False, image_id="sha256:img1"):
    return Image(
        id=image_id,
        repository=repo,
        tag=tag,
        size_bytes=100,
        is_dangling=dangling,
        used_by=[],
        created="",
    )


def _volume(name="vol1"):
    return Volume(name=name, driver="local", mountpoint="/m", used_by=[], labels={})


def _network(name="net1", endpoints=None):
    return Network(
        id="n1",
        name=name,
        driver="bridge",
        subnet="172.18.0.0/16",
        gateway="172.18.0.1",
        scope="local",
        used_by=[],
        endpoints=endpoints or [],
    )


class ImageActionTests(unittest.IsolatedAsyncioTestCase):
    async def _on_images(self, app: DockSurfApp, pilot) -> None:
        app.query_one(TabbedContent).active = TabID.IMAGES
        await pilot.pause()
        await wait_until(lambda: bool(app._current.get(TabID.IMAGES)))
        app.query_one(f"#{TableID.IMAGES}", DataTable).move_cursor(row=0)

    async def test_pull_prompts_streams_and_pulls(self) -> None:
        svc = MockDockerService(lambda: DockerSnapshot([], [_image()], [], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_images(app, pilot)

            app.action_pull_image()
            await wait_until(
                lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(["redis:7"])
            await wait_until(
                lambda: any(isinstance(s, PullProgressScreen) for s in app.screen_stack)
            )
            await wait_until(lambda: ("stream_pull", "redis", "7") in svc.calls)

    async def test_pull_defaults_tag_to_latest(self) -> None:
        svc = MockDockerService(lambda: DockerSnapshot([], [_image()], [], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_images(app, pilot)
            app.action_pull_image()
            await wait_until(
                lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(["busybox"])
            await wait_until(lambda: ("stream_pull", "busybox", "latest") in svc.calls)

    async def test_pull_guarded_off_images_tab(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()  # Containers tab active
            app.action_pull_image()
            await pilot.pause()
            self.assertFalse(any(isinstance(s, PromptScreen) for s in app.screen_stack))

    async def test_tag_image_prompts_and_tags(self) -> None:
        svc = MockDockerService(lambda: DockerSnapshot([], [_image()], [], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_images(app, pilot)
            app.action_tag_image()
            await wait_until(
                lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(["myrepo", "v2"])
            await wait_until(
                lambda: ("tag_image", "sha256:img1", "myrepo", "v2") in svc.calls
            )

    async def test_image_history_opens_screen(self) -> None:
        svc = MockDockerService(lambda: DockerSnapshot([], [_image()], [], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_images(app, pilot)
            app.action_image_history()
            await wait_until(lambda: ("image_history", "sha256:img1") in svc.calls)
            await wait_until(
                lambda: any(isinstance(s, LayerHistoryScreen) for s in app.screen_stack)
            )

    async def test_mark_all_dangling_marks_only_dangling(self) -> None:
        images = [
            _image(repo="alpine", image_id="sha256:keep", dangling=False),
            _image(repo="<none>", tag="<none>", image_id="sha256:d1", dangling=True),
            _image(repo="<none>", tag="<none>", image_id="sha256:d2", dangling=True),
        ]
        svc = MockDockerService(lambda: DockerSnapshot([], images, [], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_images(app, pilot)
            app.action_mark_all_dangling()
            await pilot.pause()
            self.assertEqual(
                app._marked[TabID.IMAGES],
                {("image", "sha256:d1"), ("image", "sha256:d2")},
            )


class VolumeActionTests(unittest.IsolatedAsyncioTestCase):
    async def _on_volumes(self, app: DockSurfApp, pilot) -> None:
        app.query_one(TabbedContent).active = TabID.VOLUMES
        await pilot.pause()

    async def test_create_volume_prompts_and_creates(self) -> None:
        svc = MockDockerService(lambda: DockerSnapshot([], [], [_volume()], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_volumes(app, pilot)
            app.action_create_volume()
            await wait_until(
                lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(["data", "local", "env=test"])
            await wait_until(
                lambda: ("create_volume", "data", "local", {"env": "test"}) in svc.calls
            )

    async def test_new_resource_on_volumes_tab_creates_volume(self) -> None:
        svc = MockDockerService(lambda: DockerSnapshot([], [], [_volume()], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_volumes(app, pilot)
            app.action_new_resource()
            await wait_until(
                lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
            )

    async def test_volume_size_populates_detail(self) -> None:
        svc = MockDockerService(lambda: DockerSnapshot([], [], [_volume("vol1")], []))
        svc.volume_size_map = {"vol1": 4096}
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_volumes(app, pilot)
            await wait_until(lambda: bool(app._current.get(TabID.VOLUMES)))
            app.query_one(f"#{TableID.VOLUMES}", DataTable).move_cursor(row=0)
            app.action_volume_size()
            await wait_until(lambda: ("volume_sizes",) in svc.calls)
            await wait_until(lambda: app._volume_sizes == {"vol1": 4096})


class NetworkActionTests(unittest.IsolatedAsyncioTestCase):
    async def _on_networks(self, app: DockSurfApp, pilot) -> None:
        app.query_one(TabbedContent).active = TabID.NETWORKS
        await pilot.pause()
        await wait_until(lambda: bool(app._current.get(TabID.NETWORKS)))
        app.query_one(f"#{TableID.NETWORKS}", DataTable).move_cursor(row=0)

    async def test_create_network_prompts_and_creates(self) -> None:
        svc = MockDockerService(lambda: DockerSnapshot([], [], [], [_network()]))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_networks(app, pilot)
            app.action_create_network()
            await wait_until(
                lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(["mynet", "bridge", "10.5.0.0/16"])
            await wait_until(
                lambda: (
                    ("create_network", "mynet", "bridge", "10.5.0.0/16") in svc.calls
                )
            )

    async def test_connect_picker_connects_selected_container(self) -> None:
        container = make_container(name="web")  # make_container sets id == name
        net = _network()
        svc = MockDockerService(lambda: DockerSnapshot([container], [], [], [net]))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_networks(app, pilot)
            app.action_network_connect()
            await wait_until(
                lambda: any(
                    isinstance(s, ContainerPickerScreen) for s in app.screen_stack
                )
            )
            app.screen_stack[-1].dismiss("web")
            await wait_until(lambda: ("connect_container", "net1", "web") in svc.calls)

    async def test_disconnect_lists_attached_and_disconnects(self) -> None:
        net = _network(
            endpoints=[NetworkEndpoint(container_name="web", ipv4="1.2.3.4")]
        )
        svc = MockDockerService(lambda: DockerSnapshot([], [], [], [net]))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_networks(app, pilot)
            app.action_network_disconnect()
            await wait_until(
                lambda: any(
                    isinstance(s, ContainerPickerScreen) for s in app.screen_stack
                )
            )
            app.screen_stack[-1].dismiss("web")
            await wait_until(
                lambda: ("disconnect_container", "net1", "web") in svc.calls
            )

    async def test_disconnect_no_endpoints_does_nothing(self) -> None:
        net = _network(endpoints=[])
        svc = MockDockerService(lambda: DockerSnapshot([], [], [], [net]))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_networks(app, pilot)
            app.action_network_disconnect()
            await pilot.pause()
            self.assertFalse(
                any(isinstance(s, ContainerPickerScreen) for s in app.screen_stack)
            )

    async def test_network_detail_shows_attached_endpoint(self) -> None:
        net = _network(
            endpoints=[NetworkEndpoint(container_name="web", ipv4="172.18.0.2/16")]
        )
        svc = MockDockerService(lambda: DockerSnapshot([], [], [], [net]))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_networks(app, pilot)
            pane = app.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
            # Render the detail Panel to text and assert the endpoint shows up.
            console = Console(width=200)
            with console.capture() as cap:
                console.print(pane._panel.content)
            out = cap.get()
            self.assertIn("web", out)
            self.assertIn("172.18.0.2", out)


class ContextSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_context_picks_and_switches(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        svc.contexts = [
            ContextInfo(
                name="default", host="unix:///var/run/docker.sock", is_current=True
            ),
            ContextInfo(name="remote", host="ssh://example.com", is_current=False),
        ]
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_switch_context()
            await wait_until(
                lambda: any(
                    isinstance(s, ContainerPickerScreen) for s in app.screen_stack
                )
            )
            app.screen_stack[-1].dismiss("remote")
            await wait_until(lambda: ("switch_context", "remote") in svc.calls)

    async def test_switch_context_no_contexts_notifies(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        svc.contexts = []
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_switch_context()
            await pilot.pause()
            self.assertFalse(
                any(isinstance(s, ContainerPickerScreen) for s in app.screen_stack)
            )
            self.assertNotIn("switch_context", [c[0] for c in svc.calls])

    async def test_switch_context_cancel_does_nothing(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_switch_context()
            await wait_until(
                lambda: any(
                    isinstance(s, ContainerPickerScreen) for s in app.screen_stack
                )
            )
            app.screen_stack[-1].dismiss(None)
            await pilot.pause()
            self.assertNotIn("switch_context", [c[0] for c in svc.calls])


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_listener_recovers_after_daemon_drop(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)

        stream_calls = {"n": 0}

        def stream_events():
            stream_calls["n"] += 1
            if stream_calls["n"] == 1:
                return FakeEventStream(error=ConnectionError("daemon down"))
            return FakeEventStream()

        svc.stream_events = stream_events  # type: ignore[method-assign]

        def ensure_connected() -> ConnectionState:
            svc.calls.append(("ensure_connected",))
            svc._connected = True
            svc.connection = _CONNECTED_STATE
            return svc.connection

        svc.ensure_connected = ensure_connected  # type: ignore[method-assign]

        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            # The first stream_events() call errors out — mark_disconnected
            # fires and the StatusBar should show the disconnected segment.
            await wait_until(lambda: ("mark_disconnected", "daemon down") in svc.calls)
            status_bar = app.query_one(f"#{STATUS_BAR_ID}", StatusBar)
            await wait_until(lambda: bool(status_bar._conn_text))
            self.assertIn("Docker daemon is not running", status_bar._conn_text)

            # The 2s backoff then retries ensure_connected(), which our stub
            # flips back to connected — a refresh should follow automatically,
            # with no manual `r` press.
            await wait_until(lambda: svc.is_connected, timeout=3.0)
            await wait_until(lambda: not status_bar._conn_text, timeout=3.0)


class _FakeHeaderSelected:
    """Minimal stand-in for `DataTable.HeaderSelected` — `_on_header_selected`
    only reads `.data_table`/`.column_index`, so a real Textual message
    (which needs an internally-generated `ColumnKey`) isn't necessary."""

    def __init__(self, data_table: DataTable, column_index: int) -> None:
        self.data_table = data_table
        self.column_index = column_index


class ColumnSortTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_header_click_sorts(self) -> None:
        # Regression: `_on_header_selected` lives on the TableRenderer mixin,
        # whose runtime base is `object` (see the AppContext docstring in
        # app.py) — `@on(...)` decorators on such mixins are never wired into
        # Textual's dispatch table, so a real mouse click on the header used
        # to do nothing at all. This drives an actual `pilot.click`, not the
        # `_FakeHeaderSelected` bypass the other tests below use, so it would
        # catch that class of bug if the dispatch wiring broke again.
        images = [
            _image(repo="c", image_id="sha256:c"),
            _image(repo="a", image_id="sha256:a"),
        ]
        app = DockSurfApp(
            docker=MockDockerService(lambda: DockerSnapshot([], images, [], []))
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.IMAGES
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.IMAGES)))
            # x=5 lands past the 2-wide mark column, inside "Repository".
            await pilot.click(f"#{TableID.IMAGES}", offset=(5, 0))
            await pilot.pause()
            self.assertEqual(
                [i.repository for i in app._current[TabID.IMAGES]], ["a", "c"]
            )

    async def test_sort_images_by_size_toggles_direction(self) -> None:
        images = [
            _image(repo="c", image_id="sha256:c", tag="latest"),
            _image(repo="a", image_id="sha256:a", tag="latest"),
            _image(repo="b", image_id="sha256:b", tag="latest"),
        ]
        images[0].size_bytes = 300
        images[1].size_bytes = 100
        images[2].size_bytes = 200
        app = DockSurfApp(
            docker=MockDockerService(lambda: DockerSnapshot([], images, [], []))
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.IMAGES
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.IMAGES)))

            table = app.query_one(f"#{TableID.IMAGES}", DataTable)
            # Column 0 is the mark glyph; "Size" is entry.columns[2] -> index 3.
            app._on_header_selected(_FakeHeaderSelected(table, column_index=3))
            await pilot.pause()
            self.assertEqual(
                [i.size_bytes for i in app._current[TabID.IMAGES]], [100, 200, 300]
            )
            labels = [str(col.label) for col in table.ordered_columns]
            self.assertIn("Size ▲", labels)

            app._on_header_selected(_FakeHeaderSelected(table, column_index=3))
            await pilot.pause()
            self.assertEqual(
                [i.size_bytes for i in app._current[TabID.IMAGES]], [300, 200, 100]
            )
            labels = [str(col.label) for col in table.ordered_columns]
            self.assertIn("Size ▼", labels)

    async def test_sort_persists_across_refresh(self) -> None:
        images = [
            _image(repo="z", image_id="sha256:z"),
            _image(repo="a", image_id="sha256:a"),
        ]
        svc = MockDockerService(lambda: DockerSnapshot([], images, [], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.IMAGES
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.IMAGES)))

            table = app.query_one(f"#{TableID.IMAGES}", DataTable)
            # "Repository" is entry.columns[0] -> column index 1.
            app._on_header_selected(_FakeHeaderSelected(table, column_index=1))
            await pilot.pause()
            self.assertEqual(
                [i.repository for i in app._current[TabID.IMAGES]], ["a", "z"]
            )

            app.start_refresh()
            await pilot.pause()
            await wait_until(
                lambda: [i.repository for i in app._current[TabID.IMAGES]] == ["a", "z"]
            )

    async def test_sort_containers_preserves_compose_grouping(self) -> None:
        snapshot = DockerSnapshot(
            containers=[
                make_container("standalone"),
                make_container("myapp-zeta", project="myapp", service="zeta"),
                make_container("myapp-alpha", project="myapp", service="alpha"),
            ],
            images=[],
            volumes=[],
            networks=[],
        )
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))

            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            # "Name" is entry.columns[0] -> column index 1.
            app._on_header_selected(_FakeHeaderSelected(table, column_index=1))
            await pilot.pause()

            rows = app._current[TabID.CONTAINERS]
            self.assertIsInstance(rows[0], ComposeProject)
            self.assertEqual(rows[0].name, "myapp")
            service_names = [c.name for c in rows[1:3]]
            self.assertEqual(service_names, ["myapp-alpha", "myapp-zeta"])


class LiveSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_typing_into_search_bar_filters_the_table(self) -> None:
        # Regression: ResourceSearchController.on_search_changed lives on a
        # mixin (same class of bug as the header-click sort regression
        # above) — real typing into the search bar used to filter nothing.
        images = [
            _image(repo="alpine", image_id="sha256:a"),
            _image(repo="redis", image_id="sha256:r"),
        ]
        app = DockSurfApp(
            docker=MockDockerService(lambda: DockerSnapshot([], images, [], []))
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.IMAGES
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.IMAGES)))

            app.action_open_search()
            await pilot.pause()
            await pilot.click(f"#{SEARCH_BAR_ID}")
            for ch in "redis":
                await pilot.press(ch)
            await pilot.pause()
            self.assertEqual(
                [i.repository for i in app._current[TabID.IMAGES]], ["redis"]
            )


class DeleteForceTests(unittest.IsolatedAsyncioTestCase):
    async def _open_confirm_dialog(self, app: DockSurfApp) -> ConfirmDialog:
        await wait_until(
            lambda: any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
        )
        return app.screen_stack[-1]

    async def test_stopped_container_delete_defaults_force_unchecked(self) -> None:
        snapshot = _standalone_snapshot(running=False)
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)

            app.action_delete()
            dialog = await self._open_confirm_dialog(app)
            checkbox = dialog.query_one(f"#{CONFIRM_FORCE_CHECKBOX_ID}", Checkbox)
            self.assertFalse(checkbox.value)
            dialog.dismiss((True, checkbox.value))
            await wait_until(lambda: ("remove_container", "web", False) in svc.calls)

    async def test_running_container_delete_defaults_force_checked(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)

            app.action_delete()
            dialog = await self._open_confirm_dialog(app)
            checkbox = dialog.query_one(f"#{CONFIRM_FORCE_CHECKBOX_ID}", Checkbox)
            self.assertTrue(checkbox.value)
            dialog.dismiss((True, True))
            await wait_until(lambda: ("remove_container", "web", True) in svc.calls)

    async def test_running_container_delete_can_uncheck_force(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)

            app.action_delete()
            dialog = await self._open_confirm_dialog(app)
            dialog.dismiss((True, False))
            await wait_until(lambda: ("remove_container", "web", False) in svc.calls)

    async def test_in_use_volume_names_blocking_containers_without_a_dialog(
        self,
    ) -> None:
        vol = Volume(
            name="data", driver="local", mountpoint="/m", used_by=["web"], labels={}
        )
        svc = MockDockerService(lambda: DockerSnapshot([], [], [vol], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.VOLUMES
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.VOLUMES)))
            app.query_one(f"#{TableID.VOLUMES}", DataTable).move_cursor(row=0)

            with patch.object(app, "notify") as notify:
                app.action_delete()
                await pilot.pause()
                self.assertFalse(
                    any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
                )
                message = notify.call_args[0][0]
                self.assertIn("web", message)
            self.assertEqual(svc.calls, [])

    async def test_network_with_endpoints_force_disconnects_then_removes(self) -> None:
        net = _network(
            name="net1",
            endpoints=[
                NetworkEndpoint(container_name="web", ipv4="", ipv6="", mac=""),
                NetworkEndpoint(container_name="db", ipv4="", ipv6="", mac=""),
            ],
        )
        svc = MockDockerService(lambda: DockerSnapshot([], [], [], [net]))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.NETWORKS
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.NETWORKS)))
            app.query_one(f"#{TableID.NETWORKS}", DataTable).move_cursor(row=0)

            app.action_delete()
            dialog = await self._open_confirm_dialog(app)
            checkbox = dialog.query_one(f"#{CONFIRM_FORCE_CHECKBOX_ID}", Checkbox)
            self.assertTrue(checkbox.value)
            dialog.dismiss((True, True))
            await wait_until(lambda: ("remove_network", "net1") in svc.calls)
            self.assertIn(("disconnect_container", "net1", "web"), svc.calls)
            self.assertIn(("disconnect_container", "net1", "db"), svc.calls)
            disconnect_idx = svc.calls.index(("disconnect_container", "net1", "web"))
            remove_idx = svc.calls.index(("remove_network", "net1"))
            self.assertLess(disconnect_idx, remove_idx)

    async def test_network_without_endpoints_has_plain_dialog(self) -> None:
        net = _network(name="net1", endpoints=[])
        svc = MockDockerService(lambda: DockerSnapshot([], [], [], [net]))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.NETWORKS
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.NETWORKS)))
            app.query_one(f"#{TableID.NETWORKS}", DataTable).move_cursor(row=0)

            app.action_delete()
            dialog = await self._open_confirm_dialog(app)
            self.assertEqual(len(dialog.query(f"#{CONFIRM_FORCE_CHECKBOX_ID}")), 0)
            dialog.dismiss(True)
            await wait_until(lambda: ("remove_network", "net1") in svc.calls)
            self.assertNotIn(
                ("disconnect_container", "net1", "web"),
                svc.calls,
            )


class OpenInBrowserTests(unittest.TestCase):
    """`_open_in_browser` is plain sync logic — no App/event loop needed.

    Regression: `webbrowser.open()` can return True on Linux/WSL even when
    nothing actually opened (it shells to `gio`/`xdg-open` without checking
    exit status), and WSL has neither of those by default — "Opening ..."
    would show while nothing happened. WSL now shells to `explorer.exe`
    directly instead.
    """

    def test_wsl_with_explorer_shells_to_explorer_exe(self) -> None:
        with (
            patch("docksurf_py.actions.container._is_wsl", return_value=True),
            patch(
                "docksurf_py.actions.container.shutil.which",
                return_value="/mnt/c/Windows/explorer.exe",
            ),
            patch("docksurf_py.actions.container.subprocess.run") as run,
            patch("docksurf_py.actions.container.webbrowser.open") as browser_open,
        ):
            result = _open_in_browser("http://localhost:8080")
        self.assertTrue(result)
        run.assert_called_once_with(
            ["explorer.exe", "http://localhost:8080"], check=False
        )
        browser_open.assert_not_called()

    def test_non_wsl_falls_back_to_webbrowser(self) -> None:
        with (
            patch("docksurf_py.actions.container._is_wsl", return_value=False),
            patch("docksurf_py.actions.container.subprocess.run") as run,
            patch(
                "docksurf_py.actions.container.webbrowser.open", return_value=True
            ) as browser_open,
        ):
            result = _open_in_browser("http://localhost:8080")
        self.assertTrue(result)
        browser_open.assert_called_once_with("http://localhost:8080")
        run.assert_not_called()

    def test_wsl_without_explorer_falls_back_to_webbrowser(self) -> None:
        with (
            patch("docksurf_py.actions.container._is_wsl", return_value=True),
            patch("docksurf_py.actions.container.shutil.which", return_value=None),
            patch(
                "docksurf_py.actions.container.webbrowser.open", return_value=True
            ) as browser_open,
        ):
            result = _open_in_browser("http://localhost:8080")
        self.assertTrue(result)
        browser_open.assert_called_once_with("http://localhost:8080")

    def test_explorer_launch_failure_returns_false(self) -> None:
        with (
            patch("docksurf_py.actions.container._is_wsl", return_value=True),
            patch(
                "docksurf_py.actions.container.shutil.which",
                return_value="/mnt/c/Windows/explorer.exe",
            ),
            patch(
                "docksurf_py.actions.container.subprocess.run",
                side_effect=OSError("no such file"),
            ),
        ):
            result = _open_in_browser("http://localhost:8080")
        self.assertFalse(result)


class ClipboardAndPortTests(unittest.IsolatedAsyncioTestCase):
    async def test_yank_volume_copies_directly_without_a_picker(self) -> None:
        vol = _volume(name="data")
        app = DockSurfApp(
            docker=MockDockerService(lambda: DockerSnapshot([], [], [vol], []))
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.VOLUMES
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.VOLUMES)))
            app.query_one(f"#{TableID.VOLUMES}", DataTable).move_cursor(row=0)

            app.action_yank()
            await pilot.pause()
            self.assertFalse(
                any(isinstance(s, ContainerPickerScreen) for s in app.screen_stack)
            )
            self.assertEqual(app._clipboard, "data")

    async def test_yank_container_opens_picker_and_copies_chosen_field(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        snapshot.containers[0].ports = [
            PortBinding(container_port="80/tcp", host_ip="0.0.0.0", host_port="8080")
        ]
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)

            app.action_yank()
            await wait_until(
                lambda: any(
                    isinstance(s, ContainerPickerScreen) for s in app.screen_stack
                )
            )
            app.screen_stack[-1].dismiss("Name")
            await wait_until(lambda: app._clipboard == "web")

    async def test_yank_nothing_selected_warns(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            with patch.object(app, "notify") as notify:
                app.action_yank()
                await pilot.pause()
                notify.assert_called_once()
                self.assertEqual(notify.call_args.kwargs.get("severity"), "warning")

    async def test_open_port_single_published_port_opens_directly(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        snapshot.containers[0].ports = [
            PortBinding(container_port="80/tcp", host_ip="0.0.0.0", host_port="8080")
        ]
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)

            with patch(
                "docksurf_py.actions.container._open_in_browser", return_value=True
            ) as browser_open:
                app.action_open_port()
                await wait_until(lambda: browser_open.called)
                browser_open.assert_called_once_with("http://localhost:8080")

    async def test_open_port_multiple_ports_shows_picker(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        snapshot.containers[0].ports = [
            PortBinding(container_port="80/tcp", host_ip="0.0.0.0", host_port="8080"),
            PortBinding(container_port="443/tcp", host_ip="0.0.0.0", host_port="8443"),
        ]
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)

            with patch(
                "docksurf_py.actions.container._open_in_browser", return_value=True
            ) as browser_open:
                app.action_open_port()
                await wait_until(
                    lambda: any(
                        isinstance(s, ContainerPickerScreen) for s in app.screen_stack
                    )
                )
                # Picker options are keyed by index, not host_port (two ports
                # can share a host_port, e.g. one TCP one UDP) — "1" is 8443.
                app.screen_stack[-1].dismiss("1")
                await wait_until(lambda: browser_open.called)
                browser_open.assert_called_once_with("http://localhost:8443")

    async def test_open_port_duplicate_host_port_does_not_crash(self) -> None:
        # Regression: a container can publish the same host port twice (e.g.
        # one TCP, one UDP binding) — the picker used to key options by
        # host_port and raise DuplicateID in this case.
        snapshot = _standalone_snapshot(running=True)
        snapshot.containers[0].ports = [
            PortBinding(container_port="6379/tcp", host_ip="0.0.0.0", host_port="6379"),
            PortBinding(container_port="6379/udp", host_ip="0.0.0.0", host_port="6379"),
        ]
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)

            app.action_open_port()
            await wait_until(
                lambda: any(
                    isinstance(s, ContainerPickerScreen) for s in app.screen_stack
                )
            )

    async def test_open_port_no_published_ports_warns(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)

            with patch.object(app, "notify") as notify:
                app.action_open_port()
                await pilot.pause()
                notify.assert_called_once()
                self.assertEqual(notify.call_args.kwargs.get("severity"), "warning")


class EmptyStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_containers_shows_hint_and_hides_table(self) -> None:
        app = DockSurfApp(docker=MockDockerService(lambda: EMPTY_SNAPSHOT))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            empty = app.query_one(f"#{EMPTY_STATE_IDS[TabID.CONTAINERS]}", Static)
            self.assertFalse(table.display)
            self.assertTrue(empty.display)
            self.assertIn("No containers", str(empty.content))
            self.assertIn("docker run hello-world", str(empty.content))

    async def test_nonempty_tab_hides_empty_state(self) -> None:
        snapshot = DockerSnapshot([make_container("c1")], [], [], [])
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            empty = app.query_one(f"#{EMPTY_STATE_IDS[TabID.CONTAINERS]}", Static)
            self.assertTrue(table.display)
            self.assertFalse(empty.display)

    async def test_search_no_match_mentions_the_query(self) -> None:
        snapshot = DockerSnapshot([make_container("c1")], [], [], [])
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            app._apply_filter("nope-does-not-exist")
            await pilot.pause()
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            empty = app.query_one(f"#{EMPTY_STATE_IDS[TabID.CONTAINERS]}", Static)
            self.assertFalse(table.display)
            self.assertIn("nope-does-not-exist", str(empty.content))

    async def test_disconnected_shows_and_clears_persistent_banner(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = app.query_one(f"#{CONNECTION_BANNER_ID}", Static)
            self.assertFalse(banner.display)

            svc.mark_disconnected(RuntimeError("daemon down"))
            app.start_refresh()
            await wait_until(lambda: banner.display)
            self.assertIn("Docker daemon is not running", str(banner.content))

            svc._connected = True
            svc.connection = _CONNECTED_STATE
            app.start_refresh()
            await wait_until(lambda: not banner.display)


class PartialFetchFailureTests(unittest.IsolatedAsyncioTestCase):
    """DockerClient.fetch_snapshot degrading gracefully (ROBUSTNESS_PERF_P2_PLAN.md
    §2) should surface as one warning toast, not a wiped tab — see
    DockerService.last_fetch_errors."""

    async def test_notifies_without_wiping_other_tabs(self) -> None:
        snapshot = DockerSnapshot([make_container("c1")], [], [], [])
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))

            svc.last_fetch_errors = ["networks"]
            with patch.object(app, "notify") as notify:
                app.start_refresh()
                await wait_until(lambda: notify.called)
                notify.assert_called_once()
                self.assertEqual(notify.call_args.kwargs.get("severity"), "warning")
                self.assertIn("networks", notify.call_args[0][0])

            # A networks-only failure must not blank the tabs that fetched
            # fine — containers should still show its fresh data.
            self.assertEqual(len(app._current[TabID.CONTAINERS]), 1)

    async def test_no_notification_on_clean_fetch(self) -> None:
        snapshot = DockerSnapshot([make_container("c1")], [], [], [])
        svc = MockDockerService(lambda: snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))

            with patch.object(app, "notify") as notify:
                app.start_refresh()
                await wait_until(lambda: not app._refresh_in_progress)
                await pilot.pause()
                notify.assert_not_called()


class SecretMaskingTests(unittest.IsolatedAsyncioTestCase):
    """Env vars mask by default in the detail pane; `R` reveals/re-masks —
    see ROBUSTNESS_PERF_P2_PLAN.md §4."""

    @staticmethod
    def _env_text(pane: DetailPane) -> str:
        assert pane._env_collapsible is not None
        static = pane._env_collapsible.query_one("#env-content", Static)
        console = Console(width=200, no_color=True)
        with console.capture() as cap:
            console.print(static.content, highlight=False)
        return cap.get()

    async def test_reveal_toggle_is_idempotent_and_updates_the_title(self) -> None:
        c = make_container("web")
        c.env = ["DB_PASSWORD=supersecret", "PATH=/usr/bin"]
        snapshot = DockerSnapshot([c], [], [], [])
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)
            await pilot.pause()

            pane = app.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
            assert pane._env_collapsible is not None
            masked_text = self._env_text(pane)
            self.assertIn("DB_PASSWORD=••••••••", masked_text)
            self.assertIn("PATH=/usr/bin", masked_text)
            self.assertIn("masked", str(pane._env_collapsible.title))

            app.action_toggle_secrets()
            await pilot.pause()
            revealed_text = self._env_text(pane)
            self.assertIn("DB_PASSWORD=supersecret", revealed_text)
            self.assertIn("revealed", str(pane._env_collapsible.title))

            app.action_toggle_secrets()
            await pilot.pause()
            remasked_text = self._env_text(pane)
            self.assertEqual(remasked_text, masked_text)
            self.assertIn("masked", str(pane._env_collapsible.title))


if __name__ == "__main__":
    unittest.main()
