import asyncio
import threading
import unittest
from typing import Callable

from rich.table import Table as RichTable
from textual.widgets import LoadingIndicator, Static

from docksurf_py.actions import ContainerActionHandler
from docksurf_py.app import DockSurfApp, _container_only_actions
from docksurf_py.connection import ConnectionState, ConnectionStatus
from docksurf_py.docker import LogStream
from docksurf_py.models import CommandResult, DockerSnapshot
from docksurf_py.widgets import HelpScreen

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
                expected_scope = (
                    "Container only"
                    if action in _container_only_actions()
                    else "Global"
                )
                self.assertEqual(match[2], expected_scope, f"key={key} action={action}")

            # Regression: the old hand-maintained frozenset mislabeled
            # "delete" as container-only even though it applies to every tab.
            delete_row = next(r for r in rows if r[1] == "Delete")
            self.assertEqual(delete_row[2], "Global")


if __name__ == "__main__":
    unittest.main()
