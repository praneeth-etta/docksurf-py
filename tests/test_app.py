import asyncio
import threading
import unittest
from typing import Callable
from unittest.mock import patch

from rich.console import Console
from rich.table import Table as RichTable
from textual.widgets import (
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
)
from docksurf_py.app import (
    DockSurfApp,
    _compose_actions,
    _container_only_actions,
    _tab_actions,
)
from docksurf_py.connection import ConnectionState, ConnectionStatus
from docksurf_py.constants import (
    DETAIL_PANE_ID,
    LOG_PANE_ID,
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
    DockerSnapshot,
    Image,
    ImageLayer,
    Network,
    NetworkEndpoint,
    SystemDf,
    Volume,
)
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


class MockDockerService:
    def __init__(self, fetch_fn: Callable[[], DockerSnapshot]) -> None:
        self._fetch_fn = fetch_fn
        self.connection = _CONNECTED_STATE
        # Recorded (method_name, *args) tuples for every write call — lets
        # bulk/prune tests assert who was actually invoked.
        self.calls: list[tuple] = []
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

    @property
    def is_connected(self) -> bool:
        return True

    def fetch_snapshot(self) -> DockerSnapshot:
        return self._fetch_fn()

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
        self.calls.append(("remove_container", container_id))
        return CommandResult.success()

    def remove_image(self, image_id: str, force: bool = False) -> CommandResult:
        self.calls.append(("remove_image", image_id))
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
            architecture="amd64",
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


class ExecCustomActionTests(unittest.IsolatedAsyncioTestCase):
    """Exercises the guard/prompt/cancel paths of `E` without ever reaching
    the real `subprocess.run`/`self.suspend()` interactive-exec tail — the
    shell-detection probe (`_container_has_shell`) is patched out since it
    shells out to `docker exec ... which <shell>` for real."""

    _HAS_SHELL_PATCH_TARGET = (
        "docksurf_py.actions.ContainerActionHandler._container_has_shell"
    )

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

            with patch(self._HAS_SHELL_PATCH_TARGET, return_value=True):
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

            with patch(self._HAS_SHELL_PATCH_TARGET, return_value=False):
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

            with patch(self._HAS_SHELL_PATCH_TARGET, return_value=True):
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

            with patch(self._HAS_SHELL_PATCH_TARGET, return_value=True):
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
            with patch("docksurf_py.actions._write_log_export") as writer:
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


def _image(repo="alpine", tag="latest", dangling=False, image_id="sha256:img1"):
    return Image(
        id=image_id,
        repository=repo,
        tag=tag,
        size_bytes=100,
        is_dangling=dangling,
        used_by=[],
        created="",
        architecture="amd64",
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


if __name__ == "__main__":
    unittest.main()
