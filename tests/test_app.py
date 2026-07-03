import asyncio
import threading
import unittest
from typing import Callable

from rich.table import Table as RichTable
from textual.widgets import DataTable, LoadingIndicator, Static

from docksurf_py.actions import ContainerActionHandler
from docksurf_py.app import (
    DockSurfApp,
    _compose_actions,
    _container_only_actions,
)
from docksurf_py.connection import ConnectionState, ConnectionStatus
from docksurf_py.constants import TabID, TableID
from docksurf_py.docker import LogStream
from docksurf_py.models import (
    CommandResult,
    ComposeProject,
    Container,
    DockerSnapshot,
)
from docksurf_py.widgets import HelpScreen
from tests.test_compose import make_container

EMPTY_SNAPSHOT = DockerSnapshot([], [], [], [])

_CONNECTED_STATE = ConnectionState(
    status=ConnectionStatus.CONNECTED,
    message="Connected",
    hint="",
    context="default",
    host="unix:///var/run/docker.sock",
)


class MockDockerService:
    def __init__(self, fetch_fn: Callable[[], DockerSnapshot]) -> None:
        self._fetch_fn = fetch_fn
        self.connection = _CONNECTED_STATE

    @property
    def is_connected(self) -> bool:
        return True

    def fetch_snapshot(self) -> DockerSnapshot:
        return self._fetch_fn()

    def stream_logs(self, container_id: str) -> LogStream:
        return LogStream(container_id, None)

    def stream_project_logs(self, specs):
        return LogStream("", None)

    def compose_action(
        self, project, verb, config_files="", working_dir=""
    ) -> CommandResult:
        return CommandResult.success()

    def stop_container(self, container_id: str) -> CommandResult:
        return CommandResult.success()

    def start_container(self, container_id: str) -> CommandResult:
        return CommandResult.success()

    def restart_container(self, container_id: str) -> CommandResult:
        return CommandResult.success()

    def remove_container(self, container_id: str, force: bool = False) -> CommandResult:
        return CommandResult.success()

    def remove_image(self, image_id: str, force: bool = False) -> CommandResult:
        return CommandResult.success()

    def remove_volume(self, volume_name: str) -> CommandResult:
        return CommandResult.success()

    def remove_network(self, network_name: str) -> CommandResult:
        return CommandResult.success()


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

            for key, action, description in app.BINDINGS:
                if not description:
                    continue
                match = next(r for r in rows if r[1] == description)
                if action in _compose_actions():
                    expected_scope = "Compose project"
                elif action in _container_only_actions():
                    expected_scope = "Container only"
                else:
                    expected_scope = "Global"
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


if __name__ == "__main__":
    unittest.main()
