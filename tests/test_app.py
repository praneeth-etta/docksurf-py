import asyncio
import threading
import unittest
from dataclasses import replace
from typing import Callable
from unittest.mock import patch

from rich.console import Console
from rich.table import Table as RichTable
from rich.text import Text as RichText
from textual.app import App
from textual.widgets import (
    Checkbox,
    DataTable,
    Input,
    Label,
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
from docksurf_py.actions.clipboard import _yank_fields
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
    CONNECTION_INDICATOR_ID,
    DETAIL_PANE_ID,
    EMPTY_STATE_IDS,
    INSPECT_SEARCH_ID,
    INSPECT_VIEW_ID,
    LOG_PANE_HEADER_ID,
    LOG_PANE_ID,
    PULL_PROGRESS_VIEW_ID,
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
    ContainerDetail,
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
from docksurf_py.topology import _network_members, _network_summary, network_topology
from docksurf_py.widgets import (
    BuildProgressScreen,
    ConfirmDialog,
    ConnectionIndicator,
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
    SelectableRichLog,
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


class FakeComposeBuildStream:
    """A finite build stream yielding fixed lines then a fixed returncode."""

    def __init__(self, lines: list[str], returncode: int) -> None:
        self._lines = list(lines)
        self._returncode = returncode
        self.returncode: int | None = None
        self._active = True

    def __iter__(self):
        for line in self._lines:
            if not self._active:
                break
            yield line
        self.returncode = self._returncode

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
        # Per-container detail-pane-only fields returned by container_detail();
        # tests can override. Keyed by container id, default an "empty" detail
        # (no env, no health log, no start time, no restarts) for anything
        # unlisted.
        self.container_detail_map: dict[str, ContainerDetail] = {}
        # Rebuild controls: which services report a build section (None =
        # "couldn't determine"), and the lines/exit code a rebuild stream
        # yields. Tests can override before pressing `B`.
        self.buildable_services: set[str] | None = None
        self.rebuild_lines: list[str] = ["#1 building", "#2 exporting"]
        self.rebuild_returncode: int = 0

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

    def container_detail(self, container_id: str) -> ContainerDetail | None:
        self.calls.append(("container_detail", container_id))
        return self.container_detail_map.get(
            container_id,
            ContainerDetail(env=[], health_log=[], started_at="", restart_count=0),
        )

    def volume_sizes(self) -> dict[str, int]:
        self.calls.append(("volume_sizes",))
        return self.volume_size_map

    def compose_action(
        self, project, verb, config_files="", working_dir=""
    ) -> CommandResult:
        self.calls.append(("compose_action", project, verb))
        return CommandResult.success()

    def compose_buildable_services(
        self, project, config_files="", working_dir=""
    ) -> set[str] | None:
        self.calls.append(("compose_buildable_services", project))
        return self.buildable_services

    def stream_compose_rebuild(self, project, service, config_files="", working_dir=""):
        self.calls.append(("stream_compose_rebuild", project, service))
        return FakeComposeBuildStream(self.rebuild_lines, self.rebuild_returncode)

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


def _non_selection_calls(calls: list[tuple]) -> list[tuple]:
    """Filter out `container_detail` — selecting a container row lazily
    fetches its detail-pane-only fields (PATCH_WORK.md P-1) regardless of
    what action a test then takes, so it isn't part of what these
    "did the guarded/cancelled action call docker?" assertions care about."""
    return [c for c in calls if c[0] != "container_detail"]


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
            self.assertEqual(_non_selection_calls(app.docker.calls), [])

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
            self.assertEqual(_non_selection_calls(app.docker.calls), [])

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

    async def test_delete_on_project_header_routes_to_compose_down(self) -> None:
        """Regression (BF-1): `d` on a Compose project header used to crash
        the app — `_plan_container_delete` read `.running` off a
        `ComposeProject`, which doesn't have it. It must now route to
        `action_compose_down` instead."""
        svc = MockDockerService(_compose_snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.move_cursor(row=0)
            self.assertTrue(app._focused_is_project_header())

            app.action_delete()
            await wait_until(
                lambda: any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(True)
            await wait_until(lambda: ("compose_action", "myapp", "down") in svc.calls)
            self.assertNotIn(
                ("remove_container", "myapp"),
                [c[:2] for c in svc.calls],
            )


class BulkContainerActionTests(unittest.IsolatedAsyncioTestCase):
    """Regression (BF-4): restart now honors marks like stop/start do, and
    all three bulk verbs are gated to the Containers tab being active."""

    def _two_container_snapshot(self) -> DockerSnapshot:
        return DockerSnapshot(
            containers=[
                make_container("a", running=True),
                make_container("b", running=True),
            ],
            images=[],
            volumes=[],
            networks=[],
        )

    async def test_restart_honors_marked_containers(self) -> None:
        svc = MockDockerService(self._two_container_snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.focus()
            table.move_cursor(row=0)
            app.action_toggle_mark()
            table.move_cursor(row=1)
            app.action_toggle_mark()
            await pilot.pause()

            app.action_restart_container()
            await wait_until(lambda: ("restart_container", "a") in svc.calls)
            await wait_until(lambda: ("restart_container", "b") in svc.calls)

    async def test_bulk_verbs_ignore_marks_from_another_tab(self) -> None:
        svc = MockDockerService(self._two_container_snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.focus()
            table.move_cursor(row=0)
            app.action_toggle_mark()
            await pilot.pause()
            self.assertTrue(app._marked[TabID.CONTAINERS])

            app.query_one(TabbedContent).active = TabID.IMAGES
            await pilot.pause()

            with patch.object(app, "notify") as notify:
                app.action_stop_container()
                await pilot.pause()
                notify.assert_called_once()
            self.assertEqual(
                [c for c in svc.calls if c[0] == "stop_container"],
                [],
            )


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


class RebuildServiceTests(unittest.IsolatedAsyncioTestCase):
    """`B` on a Compose service container rebuilds its image + recreates it.

    `_compose_snapshot` groups the "myapp" project (header + sorted service
    rows `db`, `web`) then the standalone container; `_focus_web_service`
    resolves the "web" row by scanning `_current` rather than hardcoding an
    index the group sort could shift.
    """

    async def _focus_web_service(self, app: DockSurfApp, pilot) -> None:
        await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
        rows = app._current[TabID.CONTAINERS]
        row = next(
            i
            for i, item in enumerate(rows)
            if isinstance(item, Container) and item.compose_service == "web"
        )
        table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
        table.focus()
        table.move_cursor(row=row)

    async def test_rebuild_streams_then_opens_logs_on_success(self) -> None:
        svc = MockDockerService(_compose_snapshot)
        svc.buildable_services = {"web"}
        svc.rebuild_returncode = 0
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._focus_web_service(app, pilot)

            await pilot.press("B")
            await wait_until(
                lambda: any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss(True)

            await wait_until(
                lambda: ("stream_compose_rebuild", "myapp", "web") in svc.calls
            )
            # Success: the build modal is dismissed and the recreated
            # container's logs are opened — by its (stable) Compose name, so a
            # changed id doesn't matter.
            log_pane = app.query_one(f"#{LOG_PANE_ID}", LogPane)
            await wait_until(lambda: log_pane.display)
            self.assertFalse(
                any(isinstance(s, BuildProgressScreen) for s in app.screen_stack)
            )
            self.assertTrue(
                any(c[0] == "stream_logs" and c[1] == "myapp-web" for c in svc.calls)
            )

    async def test_rebuild_failure_keeps_modal_and_skips_logs(self) -> None:
        svc = MockDockerService(_compose_snapshot)
        svc.buildable_services = {"web"}
        svc.rebuild_returncode = 1
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._focus_web_service(app, pilot)

            with patch.object(app, "notify") as notify:
                await pilot.press("B")
                await wait_until(
                    lambda: any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
                )
                app.screen_stack[-1].dismiss(True)
                await wait_until(
                    lambda: any(
                        "Rebuild failed" in str(c.args[0])
                        for c in notify.call_args_list
                    )
                )
            # Modal stays up for the user to read the error; no logs opened.
            self.assertTrue(
                any(isinstance(s, BuildProgressScreen) for s in app.screen_stack)
            )
            self.assertFalse(app.query_one(f"#{LOG_PANE_ID}", LogPane).display)

    async def test_rebuild_skips_build_less_service(self) -> None:
        svc = MockDockerService(_compose_snapshot)
        svc.buildable_services = set()  # "web" not buildable (image-only)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._focus_web_service(app, pilot)

            with patch.object(app, "notify") as notify:
                await pilot.press("B")
                await wait_until(
                    lambda: any(
                        "No build defined" in str(c.args[0])
                        for c in notify.call_args_list
                    )
                )
            # No confirm, no rebuild stream.
            self.assertFalse(
                any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
            )
            self.assertFalse(any(c[0] == "stream_compose_rebuild" for c in svc.calls))

    async def test_rebuild_noops_on_project_header(self) -> None:
        svc = MockDockerService(_compose_snapshot)
        svc.buildable_services = {"web"}
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.focus()
            table.move_cursor(row=0)  # project header
            self.assertTrue(app._focused_is_project_header())

            with patch.object(app, "notify") as notify:
                await pilot.press("B")
                await wait_until(lambda: notify.called)
            self.assertFalse(any(c[0] == "stream_compose_rebuild" for c in svc.calls))


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
            self.assertEqual(app.theme, "docksurf-abyss")

    async def test_cycle_theme_key_cycles_and_persists(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc, persist_session=True)
        with patch("docksurf_py.app.save_session") as save:
            async with app.run_test() as pilot:
                await pilot.pause()
                # Key-press tests need explicit focus on the container table —
                # see TabNavigationTests/LogViewerTests._open_logs.
                app.query_one(f"#{TableID.CONTAINERS}", DataTable).focus()
                self.assertEqual(app.theme, "docksurf-abyss")
                await pilot.press("M")
                await pilot.pause()
                self.assertEqual(app.theme, "ansi-dark")
                self.assertEqual(app._session.theme, "ansi-dark")
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


class ExecContainerActionTests(unittest.IsolatedAsyncioTestCase):
    """Regression (BF-5): `e`'s shell probe used to run inline on the UI
    thread; it must now go through `asyncio.to_thread` (same pattern as `E`/
    `action_exec_custom`) so a slow daemon can't freeze the whole TUI."""

    _HAS_SHELL_PATCH_TARGET = (
        "docksurf_py.actions.ContainerActionHandler._container_has_shell"
    )
    _WHICH_PATCH_TARGET = "docksurf_py.actions.container.shutil.which"

    async def _select_first_row(self, app: DockSurfApp) -> None:
        await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
        table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
        table.move_cursor(row=0)

    async def test_exec_runs_shell_probe_via_to_thread_and_execs_detected_shell(
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
                patch(
                    "docksurf_py.actions.container.asyncio.to_thread",
                    wraps=asyncio.to_thread,
                ) as to_thread,
                patch.object(app, "_run_interactive_exec") as run_exec,
            ):
                app.action_exec_container()
                await wait_until(lambda: run_exec.called)
                to_thread.assert_called_once()
                argv = run_exec.call_args[0][1]
                self.assertEqual(argv, ["docker", "exec", "-it", "web", "bash"])

    async def test_exec_notifies_when_no_shell_found(self) -> None:
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._select_first_row(app)

            with (
                patch(self._HAS_SHELL_PATCH_TARGET, return_value=False),
                patch(self._WHICH_PATCH_TARGET, return_value="/usr/bin/docker"),
                patch.object(app, "notify") as notify,
                patch.object(app, "_run_interactive_exec") as run_exec,
            ):
                app.action_exec_container()
                await wait_until(lambda: notify.called)
                run_exec.assert_not_called()


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
            self.assertEqual(_non_selection_calls(svc.calls), [])

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
            self.assertEqual(_non_selection_calls(svc.calls), [])

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

    async def test_header_hints_lowercase_l_to_close(self) -> None:
        """Regression (BF-8): the footer said "L to close" while the actual
        binding is lowercase `l`."""
        snapshot = _standalone_snapshot(running=True)
        app = DockSurfApp(docker=MockDockerService(lambda: snapshot))
        async with app.run_test() as pilot:
            await pilot.pause()
            log_pane = await self._open_logs(app, pilot)
            header = log_pane.query_one(f"#{LOG_PANE_HEADER_ID}", Label)
            text = str(header.render())
            self.assertIn("l to close", text)
            self.assertNotIn("L to close", text)

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

    async def test_pull_completes_reports_success_and_refreshes(self) -> None:
        svc = MockDockerService(lambda: DockerSnapshot([], [_image()], [], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_images(app, pilot)
            with patch.object(app, "notify") as notify:
                app.action_pull_image()
                await wait_until(
                    lambda: any(isinstance(s, PromptScreen) for s in app.screen_stack)
                )
                app.screen_stack[-1].dismiss(["busybox"])
                await wait_until(
                    lambda: any(
                        str(c.args[0]).startswith("Pulled busybox:latest")
                        for c in notify.call_args_list
                    )
                )

    async def test_pull_abort_mid_stream_does_not_claim_success(self) -> None:
        """Regression (BF-6): if delivering a progress line to the screen
        fails partway through the stream (e.g. the screen was torn down),
        `_finish_pull` must not report "Pulled ..." — the pull's real
        outcome on the daemon side is unknown once we lose the display."""
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = PullProgressScreen("Pulling busybox:latest")
            with (
                patch.object(screen, "append", side_effect=RuntimeError("screen gone")),
                patch.object(app, "notify") as notify,
            ):
                app._execute_pull(screen, "busybox", "latest")
                await wait_until(lambda: notify.called)
            messages = [str(c.args[0]) for c in notify.call_args_list]
            self.assertTrue(any("Lost the progress display" in m for m in messages))
            self.assertFalse(any(m.startswith("Pulled ") for m in messages))

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
                {
                    ("image", "sha256:d1|<none>:<none>"),
                    ("image", "sha256:d2|<none>:<none>"),
                },
            )


class MultiTagImageTests(unittest.IsolatedAsyncioTestCase):
    """Regression (BF-3): `get_images` emits one row per tag, all sharing the
    same `id` — row identity now includes the tag so marking, selection
    restore, and delete are per-tag-row-accurate instead of colliding."""

    def _two_tag_snapshot(self) -> DockerSnapshot:
        return DockerSnapshot(
            containers=[],
            images=[
                _image(repo="myapp", tag="v1", image_id="sha256:shared"),
                _image(repo="myapp", tag="v2", image_id="sha256:shared"),
            ],
            volumes=[],
            networks=[],
        )

    async def test_marking_one_tag_row_does_not_mark_the_other(self) -> None:
        svc = MockDockerService(self._two_tag_snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.IMAGES
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.IMAGES)))
            table = app.query_one(f"#{TableID.IMAGES}", DataTable)
            table.focus()
            table.move_cursor(row=0)

            app.action_toggle_mark()
            await pilot.pause()
            self.assertEqual(len(app._marked[TabID.IMAGES]), 1)

    async def test_delete_removes_by_name_tag_not_shared_id(self) -> None:
        """Deleting one tag row must untag by `repo:tag`, not remove by the
        id shared with the other tag row."""
        svc = MockDockerService(self._two_tag_snapshot)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.IMAGES
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.IMAGES)))
            table = app.query_one(f"#{TableID.IMAGES}", DataTable)
            table.move_cursor(row=0)

            app.action_delete()
            await wait_until(
                lambda: any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss((True, False))
            await wait_until(lambda: any(c[0] == "remove_image" for c in svc.calls))
            remove_calls = [c for c in svc.calls if c[0] == "remove_image"]
            self.assertEqual(len(remove_calls), 1)
            self.assertEqual(remove_calls[0][1], "myapp:v1")

    async def test_dangling_image_still_removed_by_id(self) -> None:
        img = _image(repo="<none>", tag="<none>", dangling=True, image_id="sha256:d1")
        svc = MockDockerService(lambda: DockerSnapshot([], [img], [], []))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = TabID.IMAGES
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.IMAGES)))
            app.query_one(f"#{TableID.IMAGES}", DataTable).move_cursor(row=0)

            app.action_delete()
            await wait_until(
                lambda: any(isinstance(s, ConfirmDialog) for s in app.screen_stack)
            )
            app.screen_stack[-1].dismiss((True, False))
            await wait_until(lambda: any(c[0] == "remove_image" for c in svc.calls))
            remove_calls = [c for c in svc.calls if c[0] == "remove_image"]
            self.assertEqual(remove_calls[0][1], "sha256:d1")


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

    async def test_connect_picker_excludes_stopped_but_attached_container(
        self,
    ) -> None:
        """Regression (BF-7): `Network.endpoints` only lists running
        containers (Docker's network inspect omits stopped ones), so a
        stopped container already attached via its own `.networks` list used
        to show up as connectable and fail when picked."""
        running = make_container(name="web", running=True)
        stopped = replace(make_container(name="db", running=False), networks=["net1"])
        net = _network()  # no endpoints — "db" isn't running, so none listed
        svc = MockDockerService(
            lambda: DockerSnapshot([running, stopped], [], [], [net])
        )
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
            picker = app.screen_stack[-1]
            assert isinstance(picker, ContainerPickerScreen)
            offered_ids = {container_id for container_id, _ in picker._options}
            self.assertEqual(offered_ids, {"web"})

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

    async def test_network_detail_shows_topology_diagram(self) -> None:
        web = make_container("web")
        web.networks = ["net1", "proxy"]
        net = _network(
            endpoints=[NetworkEndpoint(container_name="web", ipv4="172.18.0.2")]
        )
        svc = MockDockerService(lambda: DockerSnapshot([web], [], [], [net]))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_networks(app, pilot)
            pane = app.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
            console = Console(width=120)
            with console.capture() as cap:
                console.print(pane._topology_panel.content)
            rendered = cap.get()
            self.assertIn("net1", rendered)
            self.assertIn("web", rendered)
            self.assertIn("172.18.0.2", rendered)
            self.assertIn("also on: proxy", rendered)

    async def test_yank_network_copies_summary_directly(self) -> None:
        web = make_container("web")
        web.networks = ["net1"]
        net = _network(
            endpoints=[NetworkEndpoint(container_name="web", ipv4="172.18.0.2")]
        )
        svc = MockDockerService(lambda: DockerSnapshot([web], [], [], [net]))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await self._on_networks(app, pilot)
            app.action_yank()
            await pilot.pause()
            self.assertFalse(
                any(isinstance(s, ContainerPickerScreen) for s in app.screen_stack)
            )
            self.assertIn("net1", app._clipboard)
            self.assertIn("web", app._clipboard)
            self.assertIn("172.18.0.2", app._clipboard)

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


class NetworkTopologyHelperTests(unittest.TestCase):
    """`_network_members` / `_topology_tree` / `_network_summary` / yank."""

    def _net_and_containers(self) -> tuple[Network, list[Container]]:
        web = make_container("web")
        web.networks = ["net1", "proxy"]
        web.ports = [PortBinding("80/tcp", "0.0.0.0", "8080")]
        db = make_container("db", running=False)
        db.networks = ["net1"]
        net = _network(
            endpoints=[NetworkEndpoint(container_name="web", ipv4="172.18.0.2")]
        )
        return net, [web, db]

    def test_members_union_endpoints_and_stopped_containers(self) -> None:
        net, containers = self._net_and_containers()
        db, web = _network_members(net, containers)
        self.assertEqual((db.name, web.name), ("db", "web"))
        self.assertFalse(db.running)
        self.assertEqual(db.ipv4, "")  # stopped: no endpoint, hence no IP
        self.assertTrue(web.running)
        self.assertEqual(web.ipv4, "172.18.0.2")
        self.assertEqual(web.ports, "0.0.0.0:8080->80/tcp")
        self.assertEqual(web.other_networks, ["proxy"])

    def test_endpoint_without_snapshot_container_still_listed(self) -> None:
        net = _network(
            endpoints=[NetworkEndpoint(container_name="ghost", ipv4="172.18.0.9")]
        )
        (member,) = _network_members(net, [])
        self.assertTrue(member.running)
        self.assertEqual(member.ipv4, "172.18.0.9")

    def _render(self, diagram, width: int = 120) -> str:
        console = Console(width=width)
        with console.capture() as cap:
            console.print(diagram)
        return cap.get()

    def test_network_diagram_marks_state_and_cross_network(self) -> None:
        net, containers = self._net_and_containers()
        out = self._render(network_topology(net, containers))
        self.assertIn("net1", out)
        self.assertIn("also on: proxy", out)
        self.assertIn("stopped", out)
        self.assertIn("172.18.0.2", out)

    def test_network_diagram_survives_narrow_width(self) -> None:
        net, containers = self._net_and_containers()
        out = self._render(network_topology(net, containers), width=30)
        self.assertIn("web", out)
        # Every rendered line fits the width — truncated, never wrapped.
        self.assertTrue(all(len(ln) <= 30 for ln in out.splitlines()))

    def test_radial_places_hub_between_members(self) -> None:
        net, containers = self._net_and_containers()
        lines = self._render(network_topology(net, containers), width=100)
        rows = lines.splitlines()
        hub = next(i for i, ln in enumerate(rows) if "net1" in ln)
        db = next(i for i, ln in enumerate(rows) if "db" in ln)
        web = next(i for i, ln in enumerate(rows) if "web" in ln)
        # Radial: the hub sits *between* its members, not above them.
        self.assertTrue(min(db, web) < hub < max(db, web))
        # Connectors puncture the hub's borders as T-junctions.
        self.assertIn("╧", lines)
        self.assertIn("╤", lines)

    def test_radial_falls_back_when_narrow(self) -> None:
        net, containers = self._net_and_containers()
        out = self._render(network_topology(net, containers), width=30)
        # Stacked fallback puts the hub box first.
        self.assertTrue(out.splitlines()[0].lstrip().startswith("╔"))

    def test_radial_falls_back_when_crowded(self) -> None:
        net = _network(
            endpoints=[
                NetworkEndpoint(container_name=f"c{i}", ipv4=f"172.18.0.{i}")
                for i in range(7)  # one over _MAX_RADIAL
            ]
        )
        out = self._render(network_topology(net, []), width=100)
        self.assertTrue(out.splitlines()[0].lstrip().startswith("╔"))

    def _rows_at_width(self, diagram, width: int) -> list[RichText]:
        """The diagram's yielded rows at an exact render width — unlike
        `_render`, console wrapping can't mask a too-wide row here."""
        console = Console(width=width)
        opts = console.options.update_width(width)
        rows: list[RichText] = []
        for item in diagram.__rich_console__(console, opts):
            if isinstance(item, RichText):
                rows.append(item)
            else:  # the stacked-fallback _HubDiagram renderable
                rows.extend(item.__rich_console__(console, opts))
        return rows

    def test_diagram_rows_fit_all_member_counts_and_widths(self) -> None:
        for n in range(1, 9):
            net = _network(
                endpoints=[
                    NetworkEndpoint(container_name=f"svc-{i}", ipv4=f"172.18.0.{i}")
                    for i in range(n)
                ]
            )
            diagram = network_topology(net, [])
            for width in range(24, 121, 8):
                rows = self._rows_at_width(diagram, width)
                self.assertTrue(
                    all(r.cell_len <= width for r in rows),
                    f"row overflow at members={n} width={width}",
                )

    def test_summary_has_header_and_one_line_per_member(self) -> None:
        net, containers = self._net_and_containers()
        lines = _network_summary(net, containers).splitlines()
        self.assertIn("net1", lines[0])
        self.assertIn("172.18.0.0/16", lines[0])
        self.assertIn("gateway 172.18.0.1", lines[0])
        self.assertTrue(
            any(
                "web" in ln and "running" in ln and "also on: proxy" in ln
                for ln in lines[1:]
            )
        )
        self.assertTrue(any("db" in ln and "stopped" in ln for ln in lines[1:]))

    def test_summary_for_empty_network(self) -> None:
        self.assertIn("(no containers attached)", _network_summary(_network(), []))

    def test_yank_fields_for_network_is_single_summary(self) -> None:
        # A single field means action_yank copies it directly, no picker.
        net, containers = self._net_and_containers()
        fields = _yank_fields(net, containers)
        self.assertEqual([label for label, _ in fields], ["Network summary"])
        self.assertIn("web", fields[0][1])


class ContainerTopologyPaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_selecting_container_leaves_topology_panel_empty(self) -> None:
        # The diagram is a Networks-tab feature; a container selection must
        # not draw one (and must clear any left over from the Networks tab).
        web = make_container("web")
        web.networks = ["net1"]
        svc = MockDockerService(lambda: DockerSnapshot([web], [], [], [_network()]))
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)
            await pilot.pause()
            pane = app.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
            self.assertEqual(str(pane._topology_panel.content), "")


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


class LazyTabRenderTests(unittest.IsolatedAsyncioTestCase):
    """PATCH_WORK.md P-5: only the active tab's table is repopulated on a
    refresh; the other three are marked dirty and caught up lazily the
    moment they become active, instead of every refresh rebuilding all four."""

    async def test_inactive_tab_is_not_repopulated_until_switched_to(self) -> None:
        state = {"snapshot": DockerSnapshot([], [_image(repo="alpine")], [], [])}
        app = DockSurfApp(docker=MockDockerService(lambda: state["snapshot"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: TabID.IMAGES in app._dirty_tabs)

            # Containers tab is active by default — Images was never populated.
            images_table = app.query_one(f"#{TableID.IMAGES}", DataTable)
            self.assertEqual(images_table.row_count, 0)
            self.assertEqual(app._current[TabID.IMAGES], [])

            # A refresh with new data lands while Images stays inactive.
            state["snapshot"] = DockerSnapshot(
                [],
                [_image(repo="alpine"), _image(repo="redis", image_id="sha256:r")],
                [],
                [],
            )
            app.start_refresh()
            await wait_until(lambda: not app._refresh_in_progress)
            await pilot.pause()
            self.assertIn(TabID.IMAGES, app._dirty_tabs)
            self.assertEqual(images_table.row_count, 0)

            # Switching to it catches it up with the latest snapshot.
            app.query_one(TabbedContent).active = TabID.IMAGES
            await pilot.pause()
            await wait_until(lambda: TabID.IMAGES not in app._dirty_tabs)
            self.assertEqual(images_table.row_count, 2)
            self.assertEqual(
                [i.repository for i in app._current[TabID.IMAGES]], ["alpine", "redis"]
            )

    async def test_returning_to_a_dirty_tab_does_not_lose_the_active_tabs_repopulate(
        self,
    ) -> None:
        # The active (Containers) tab must still repopulate on every refresh
        # even though the other three are now lazily deferred.
        containers_v1 = [make_container(name="web", running=True)]
        containers_v2 = [
            make_container(name="web", running=True),
            make_container(name="db", running=True),
        ]
        state = {"snapshot": DockerSnapshot(containers_v1, [], [], [])}
        app = DockSurfApp(docker=MockDockerService(lambda: state["snapshot"]))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            self.assertEqual(len(app._current[TabID.CONTAINERS]), 1)

            state["snapshot"] = DockerSnapshot(containers_v2, [], [], [])
            app.start_refresh()
            await wait_until(lambda: len(app._current.get(TabID.CONTAINERS, [])) == 2)
            self.assertNotIn(TabID.CONTAINERS, app._dirty_tabs)


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

            # The filter is debounced 0.2s (PATCH_WORK.md P-7). Every prefix
            # of "redis" already narrows the table to just the "redis" row
            # (no other repo shares any of those letters), so waiting on the
            # rendered table alone can be satisfied by an intermediate
            # keystroke's debounced call rather than the final one — wait for
            # the settled call itself instead, or a pending timer for the
            # full query can still be armed when this test tears down the app.
            with patch.object(app, "_apply_filter", wraps=app._apply_filter) as spy:
                for ch in "redis":
                    await pilot.press(ch)
                await wait_until(
                    lambda: (
                        spy.call_args is not None and spy.call_args.args == ("redis",)
                    )
                )
            self.assertEqual(
                [i.repository for i in app._current[TabID.IMAGES]], ["redis"]
            )

    async def test_search_filter_is_debounced_not_applied_per_keystroke(self) -> None:
        # Real wall-clock timing around a burst of pilot.press() calls is too
        # fragile to assert on directly (keystroke dispatch overhead alone can
        # exceed the 0.2s debounce window on a loaded machine) — instead assert
        # the mechanism directly: 5 keystrokes collapse into one _apply_filter
        # call for the settled query, not five (PATCH_WORK.md P-7).
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

            with patch.object(app, "_apply_filter", wraps=app._apply_filter) as spy:
                # Batched into a single pilot.press() call — one
                # `_wait_for_screen()` round-trip for the whole burst instead
                # of one per character — to keep it well under the 0.2s
                # debounce window even on a loaded CI runner.
                await pilot.press(*"redis")
                # Every prefix of "redis" already narrows the table to just
                # the "redis" row (no other repo shares any of those letters),
                # so wait for the debounced call itself to settle on the full
                # query rather than inferring it from the rendered table.
                await wait_until(
                    lambda: (
                        spy.call_args is not None and spy.call_args.args == ("redis",)
                    )
                )
                # Collapsed into fewer calls than keystrokes typed — proves
                # the debounce coalesced at least one, not just that the
                # eventual query settled correctly. (A generous bound: under
                # heavy parallel test load the 0.2s window can occasionally
                # elapse mid-burst and split into more than one batch.)
                self.assertLess(spy.call_count, len("redis"))


class InspectScreenFilterDebounceTests(unittest.IsolatedAsyncioTestCase):
    async def test_filter_is_debounced_not_re_rendered_per_keystroke(self) -> None:
        # PATCH_WORK.md P-7: InspectScreen's filter re-rendered the whole
        # (potentially long) JSON dump on every keystroke — same fix as
        # ResourceSearchController's search bar, mirroring LogPane's existing
        # 0.2s debounce.
        text = "\n".join(f"line {i}" for i in range(5)) + "\nmatches-line"
        app = App()
        async with app.run_test() as pilot:
            screen = InspectScreen("Test", text)
            app.push_screen(screen)
            await pilot.pause()

            search_bar = screen.query_one(f"#{INSPECT_SEARCH_ID}", Input)
            search_bar.display = True
            await pilot.pause()
            await pilot.click(f"#{INSPECT_SEARCH_ID}")

            log_view = screen.query_one(f"#{INSPECT_VIEW_ID}", RichLog)
            with patch.object(
                screen, "_render_lines", wraps=screen._render_lines
            ) as spy:
                await pilot.press(*"matches")
                # `_filter` updates synchronously per keystroke; the debounced
                # re-render lags 0.2s behind it — wait for the render itself.
                await wait_until(lambda: len(log_view.lines) == 1)
                # Collapsed into fewer calls than keystrokes typed — same
                # generous bound as the search-bar debounce test above.
                self.assertLess(spy.call_count, len("matches"))


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


class SelectableRichLogSelectionTests(unittest.TestCase):
    """`get_selection` reconstructs the selected text from the line strips —
    upstream `RichLog` leaves it unimplemented. Pure logic, no app needed."""

    def _log(self, texts: list[str]) -> "SelectableRichLog":
        from rich.segment import Segment
        from textual.strip import Strip

        log = SelectableRichLog()
        log.lines = [Strip([Segment(t)]) for t in texts]
        return log

    def test_extracts_partial_single_line(self) -> None:
        from textual.geometry import Offset
        from textual.selection import Selection

        log = self._log(["hello world"])
        text, ending = log.get_selection(Selection(Offset(0, 0), Offset(5, 0)))
        self.assertEqual(text, "hello")
        self.assertEqual(ending, "\n")

    def test_extracts_whole_buffer_when_unbounded(self) -> None:
        from textual.selection import Selection

        log = self._log(["alpha", "beta", "gamma"])
        text, _ = log.get_selection(Selection(None, None))
        self.assertEqual(text, "alpha\nbeta\ngamma")


class SelectableRichLogRenderTests(unittest.IsolatedAsyncioTestCase):
    """The highlight path (`divide`/`join`/`apply_style` + offset baking) can't
    be driven headlessly by a real mouse, but this smoke-tests that rendering a
    selected line runs without error and that the selection extracts correctly
    once mounted."""

    async def test_selected_line_renders_and_extracts(self) -> None:
        from textual.app import App as _App
        from textual.geometry import Offset
        from textual.selection import Selection

        class _Host(_App):
            def compose(self):
                yield SelectableRichLog(id="log", markup=True, highlight=False)

        app = _Host()
        async with app.run_test() as pilot:
            log = app.query_one("#log", SelectableRichLog)
            log.write("hello world")
            log.write("second line")
            await pilot.pause()
            app.screen.selections = {log: Selection(Offset(0, 0), Offset(5, 0))}
            await pilot.pause()
            text, _ = log.get_selection(log.text_selection)
            self.assertIn("hello", text)
            # Rendering the selected line exercises the highlight span split.
            self.assertIsNotNone(log.render_line(0))

    async def test_wide_character_selection_highlights_and_extracts_correctly(
        self,
    ) -> None:
        """`get_span` returns *character* offsets, but `Strip.divide` cuts at
        *cell* positions — regression test for that mismatch dropping the
        highlight short of the actual selection on CJK/wide-character lines."""
        from textual.app import App as _App
        from textual.geometry import Offset
        from textual.selection import Selection

        class _Host(_App):
            def compose(self):
                yield SelectableRichLog(id="log", markup=True, highlight=False)

        app = _Host()
        async with app.run_test() as pilot:
            log = app.query_one("#log", SelectableRichLog)
            log.write("你好world")
            await pilot.pause()
            # Character offsets 0..2 select "你好" (4 screen cells).
            app.screen.selections = {log: Selection(Offset(0, 0), Offset(2, 0))}
            await pilot.pause()
            text, _ = log.get_selection(log.text_selection)
            self.assertEqual(text, "你好")
            highlight_bgcolor = log._selection_style.bgcolor
            strip = log.render_line(0)
            highlighted = "".join(
                segment.text
                for segment in strip._segments
                if segment.style is not None
                and segment.style.bgcolor == highlight_bgcolor
            )
            self.assertEqual(highlighted, "你好")


class PullProgressScreenBufferTests(unittest.IsolatedAsyncioTestCase):
    """Regression (BF-6): a pull chunk can arrive before the modal finishes
    mounting — `append` must buffer it instead of a bare `query_one` raising
    and killing the pump thread."""

    async def test_append_before_mount_does_not_raise(self) -> None:
        screen = PullProgressScreen("Pulling busybox:latest")
        screen.append("first line")  # not mounted yet — must not raise
        self.assertEqual(screen._pending_lines, ["first line"])

    async def test_buffered_lines_flush_on_mount(self) -> None:
        app = App()
        async with app.run_test() as pilot:
            screen = PullProgressScreen("Pulling busybox:latest")
            screen.append("buffered before mount")
            self.assertEqual(screen._pending_lines, ["buffered before mount"])

            await app.push_screen(screen)
            await pilot.pause()

            self.assertEqual(screen._pending_lines, [])
            log = screen.query_one(f"#{PULL_PROGRESS_VIEW_ID}", RichLog)
            rendered = "".join(strip.text for strip in log.lines)
            self.assertIn("buffered before mount", rendered)


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
            indicator = app.query_one(
                f"#{CONNECTION_INDICATOR_ID}", ConnectionIndicator
            )
            self.assertFalse(banner.display)

            svc.mark_disconnected(RuntimeError("daemon down"))
            app.start_refresh()
            await wait_until(lambda: banner.display)
            self.assertIn("Docker daemon is not running", str(banner.content))
            self.assertIn("Disconnected", str(indicator.content))

            svc._connected = True
            svc.connection = _CONNECTED_STATE
            app.start_refresh()
            await wait_until(lambda: not banner.display)
            # "Connected" is a substring of "Disconnected" too, so assert the
            # negative to actually prove the flip back happened.
            self.assertNotIn("Disconnected", str(indicator.content))


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
        svc = MockDockerService(lambda: snapshot)
        # env is detail-pane-only and fetched lazily via container_detail()
        # (PATCH_WORK.md P-1) — the plain Container built above no longer
        # carries it through to the pane on its own.
        svc.container_detail_map[c.id] = ContainerDetail(
            env=c.env, health_log=[], started_at="", restart_count=0
        )
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: bool(app._current.get(TabID.CONTAINERS)))
            app.query_one(f"#{TableID.CONTAINERS}", DataTable).move_cursor(row=0)
            await pilot.pause()

            pane = app.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
            await wait_until(lambda: pane._env_collapsible is not None)
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
