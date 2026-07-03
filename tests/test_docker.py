import unittest
from unittest.mock import MagicMock, patch

from docker.errors import APIError, DockerException, NotFound

from docksurf_py.connection import ConnectionStatus
from docksurf_py.docker import DockerClient
from docksurf_py.models import CommandErrorKind, DockerSnapshot


def _fake_sdk() -> MagicMock:
    sdk = MagicMock()
    sdk.containers.list.return_value = []
    sdk.images.list.return_value = []
    sdk.volumes.list.return_value = []
    sdk.networks.list.return_value = []
    return sdk


class LazyConnectTests(unittest.TestCase):
    def test_constructor_does_not_touch_the_sdk(self) -> None:
        with patch("docksurf_py.docker.docker.from_env") as from_env:
            client = DockerClient()

        from_env.assert_not_called()
        self.assertEqual(client.connection.status, ConnectionStatus.NOT_CONNECTED)
        self.assertFalse(client.is_connected)

    def test_fetch_snapshot_connects_lazily(self) -> None:
        fake_sdk = _fake_sdk()
        with patch("docksurf_py.docker.docker.from_env", return_value=fake_sdk):
            client = DockerClient()
            self.assertFalse(client.is_connected)

            snapshot = client.fetch_snapshot()

        self.assertTrue(client.is_connected)
        self.assertEqual(snapshot, DockerSnapshot([], [], [], []))
        fake_sdk.ping.assert_called_once()

    def test_fetch_snapshot_does_not_reping_once_connected(self) -> None:
        fake_sdk = _fake_sdk()
        with patch(
            "docksurf_py.docker.docker.from_env", return_value=fake_sdk
        ) as from_env:
            client = DockerClient()
            client.fetch_snapshot()
            client.fetch_snapshot()

        from_env.assert_called_once()
        fake_sdk.ping.assert_called_once()

    def test_fetch_snapshot_retries_after_daemon_recovers(self) -> None:
        fake_sdk = _fake_sdk()
        with patch("docksurf_py.docker.docker.from_env") as from_env:
            from_env.side_effect = [DockerException("daemon down"), fake_sdk]
            client = DockerClient()

            first = client.fetch_snapshot()
            self.assertFalse(client.is_connected)
            self.assertEqual(first, DockerSnapshot([], [], [], []))

            second = client.fetch_snapshot()

        self.assertTrue(client.is_connected)
        self.assertEqual(second, DockerSnapshot([], [], [], []))
        self.assertEqual(from_env.call_count, 2)


class ManagementCommandResultTests(unittest.TestCase):
    def _connected_client_with_stop_side_effect(self, side_effect) -> DockerClient:
        client = DockerClient()
        fake_sdk = MagicMock()
        fake_sdk.containers.get.return_value.stop.side_effect = side_effect
        client._sdk = fake_sdk
        return client

    def test_success_returns_ok_result_with_no_kind(self) -> None:
        client = self._connected_client_with_stop_side_effect(None)
        result = client.stop_container("abc")
        self.assertTrue(result.ok)
        self.assertIsNone(result.kind)

    def test_not_found_is_classified(self) -> None:
        client = self._connected_client_with_stop_side_effect(
            NotFound("no such container")
        )
        result = client.stop_container("abc")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.NOT_FOUND)

    def test_conflict_is_classified_as_in_use(self) -> None:
        response = MagicMock(status_code=409)
        client = self._connected_client_with_stop_side_effect(
            APIError("conflict", response=response)
        )
        result = client.stop_container("abc")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.IN_USE)

    def test_other_api_error_is_classified_as_unknown(self) -> None:
        response = MagicMock(status_code=500)
        client = self._connected_client_with_stop_side_effect(
            APIError("server error", response=response)
        )
        result = client.stop_container("abc")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.UNKNOWN)

    def test_daemon_exception_is_classified_as_daemon_unreachable(self) -> None:
        client = self._connected_client_with_stop_side_effect(
            DockerException("daemon gone")
        )
        result = client.stop_container("abc")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.DAEMON_UNREACHABLE)

    def test_uninitialized_client_is_classified_as_daemon_unreachable(self) -> None:
        client = DockerClient()  # never connected, self._sdk stays None
        result = client.stop_container("abc")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.DAEMON_UNREACHABLE)


if __name__ == "__main__":
    unittest.main()
