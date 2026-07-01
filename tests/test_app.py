import asyncio
import threading
import unittest
from typing import Callable

from textual.widgets import LoadingIndicator

from docksurf_py.app import DockSurfApp
from docksurf_py.connection import ConnectionState, ConnectionStatus
from docksurf_py.docker import LogStream
from docksurf_py.models import CommandResult, DockerSnapshot

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
        return True, "OK"

    def start_container(self, container_id: str) -> CommandResult:
        return True, "OK"

    def restart_container(self, container_id: str) -> CommandResult:
        return True, "OK"

    def remove_container(self, container_id: str, force: bool = False) -> CommandResult:
        return True, "OK"

    def remove_image(self, image_id: str, force: bool = False) -> CommandResult:
        return True, "OK"

    def remove_volume(self, volume_name: str) -> CommandResult:
        return True, "OK"

    def remove_network(self, network_name: str) -> CommandResult:
        return True, "OK"


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


if __name__ == "__main__":
    unittest.main()
