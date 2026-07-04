import unittest
from unittest.mock import MagicMock, patch

from docker.errors import APIError, DockerException, NotFound

from docksurf_py.connection import ConnectionStatus
from docksurf_py.docker import DockerClient, format_size
from docksurf_py.models import CommandErrorKind, ContainerTop, DockerSnapshot


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


class PauseUnpauseKillTests(unittest.TestCase):
    def _client(self) -> DockerClient:
        client = DockerClient()
        client._sdk = MagicMock()
        return client

    def test_pause_calls_sdk_pause(self) -> None:
        client = self._client()
        result = client.pause_container("abc")
        client._sdk.containers.get.assert_called_with("abc")
        client._sdk.containers.get.return_value.pause.assert_called_once()
        self.assertTrue(result.ok)

    def test_unpause_calls_sdk_unpause(self) -> None:
        client = self._client()
        result = client.unpause_container("abc")
        client._sdk.containers.get.return_value.unpause.assert_called_once()
        self.assertTrue(result.ok)

    def test_kill_calls_sdk_kill(self) -> None:
        client = self._client()
        result = client.kill_container("abc")
        client._sdk.containers.get.return_value.kill.assert_called_once()
        self.assertTrue(result.ok)

    def test_pause_not_found_is_classified(self) -> None:
        client = self._client()
        client._sdk.containers.get.return_value.pause.side_effect = NotFound(
            "no such container"
        )
        result = client.pause_container("abc")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.NOT_FOUND)

    def test_uninitialized_client_is_classified_as_daemon_unreachable(self) -> None:
        client = DockerClient()
        result = client.kill_container("abc")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.DAEMON_UNREACHABLE)


class PruneTests(unittest.TestCase):
    def _client(self) -> DockerClient:
        client = DockerClient()
        client._sdk = MagicMock()
        return client

    def test_prune_containers_reports_count_and_reclaimed(self) -> None:
        client = self._client()
        client._sdk.containers.prune.return_value = {
            "ContainersDeleted": ["a", "b"],
            "SpaceReclaimed": 1536,
        }
        result = client.prune_containers()
        self.assertTrue(result.ok)
        self.assertIn("2", result.message)
        self.assertIn(format_size(1536), result.message)

    def test_prune_containers_handles_none_deleted(self) -> None:
        client = self._client()
        client._sdk.containers.prune.return_value = {
            "ContainersDeleted": None,
            "SpaceReclaimed": 0,
        }
        result = client.prune_containers()
        self.assertTrue(result.ok)
        self.assertIn("0", result.message)

    def test_prune_images_uses_dangling_filter(self) -> None:
        client = self._client()
        client._sdk.images.prune.return_value = {
            "ImagesDeleted": [{"Deleted": "sha256:x"}],
            "SpaceReclaimed": 2048,
        }
        result = client.prune_images()
        client._sdk.images.prune.assert_called_once_with(filters={"dangling": True})
        self.assertTrue(result.ok)
        self.assertIn("1", result.message)

    def test_prune_volumes_reports_count_and_reclaimed(self) -> None:
        client = self._client()
        client._sdk.volumes.prune.return_value = {
            "VolumesDeleted": ["v1"],
            "SpaceReclaimed": 100,
        }
        result = client.prune_volumes()
        self.assertTrue(result.ok)
        self.assertIn("1", result.message)

    def test_prune_networks_is_count_only(self) -> None:
        client = self._client()
        client._sdk.networks.prune.return_value = {"NetworksDeleted": ["net1", "net2"]}
        result = client.prune_networks()
        self.assertTrue(result.ok)
        self.assertIn("2", result.message)
        self.assertNotIn("reclaimed", result.message)

    def test_prune_system_sums_across_categories(self) -> None:
        client = self._client()
        client._sdk.containers.prune.return_value = {
            "ContainersDeleted": ["a"],
            "SpaceReclaimed": 100,
        }
        client._sdk.networks.prune.return_value = {"NetworksDeleted": ["n1", "n2"]}
        client._sdk.images.prune.return_value = {
            "ImagesDeleted": ["i1"],
            "SpaceReclaimed": 200,
        }
        client._sdk.api.prune_builds.return_value = {"SpaceReclaimed": 50}
        result = client.prune_system()
        self.assertTrue(result.ok)
        self.assertIn("4", result.message)  # 1 container + 2 networks + 1 image
        self.assertIn(format_size(350), result.message)

    def test_prune_system_tolerates_missing_prune_builds(self) -> None:
        client = self._client()
        client._sdk.containers.prune.return_value = {
            "ContainersDeleted": [],
            "SpaceReclaimed": 0,
        }
        client._sdk.networks.prune.return_value = {"NetworksDeleted": []}
        client._sdk.images.prune.return_value = {
            "ImagesDeleted": [],
            "SpaceReclaimed": 0,
        }
        client._sdk.api.prune_builds.side_effect = AttributeError("no such endpoint")
        result = client.prune_system()
        self.assertTrue(result.ok)

    def test_uninitialized_client_prune_is_daemon_unreachable(self) -> None:
        client = DockerClient()
        result = client.prune_containers()
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.DAEMON_UNREACHABLE)


class InspectResourceTests(unittest.TestCase):
    def _client(self) -> DockerClient:
        client = DockerClient()
        client._sdk = MagicMock()
        return client

    def test_dispatches_container(self) -> None:
        client = self._client()
        client._sdk.containers.get.return_value.attrs = {"Id": "abc"}
        result = client.inspect_resource("container", "abc")
        client._sdk.containers.get.assert_called_once_with("abc")
        self.assertEqual(result, {"Id": "abc"})

    def test_dispatches_image(self) -> None:
        client = self._client()
        client._sdk.images.get.return_value.attrs = {"Id": "sha256:x"}
        result = client.inspect_resource("image", "sha256:x")
        self.assertEqual(result, {"Id": "sha256:x"})

    def test_dispatches_volume(self) -> None:
        client = self._client()
        client._sdk.volumes.get.return_value.attrs = {"Name": "vol"}
        result = client.inspect_resource("volume", "vol")
        self.assertEqual(result, {"Name": "vol"})

    def test_dispatches_network(self) -> None:
        client = self._client()
        client._sdk.networks.get.return_value.attrs = {"Name": "net"}
        result = client.inspect_resource("network", "net")
        self.assertEqual(result, {"Name": "net"})

    def test_unknown_kind_returns_none(self) -> None:
        client = self._client()
        result = client.inspect_resource("bogus", "x")
        self.assertIsNone(result)

    def test_not_found_returns_none(self) -> None:
        client = self._client()
        client._sdk.containers.get.side_effect = NotFound("no such container")
        result = client.inspect_resource("container", "abc")
        self.assertIsNone(result)

    def test_not_connected_returns_none(self) -> None:
        client = DockerClient()
        result = client.inspect_resource("container", "abc")
        self.assertIsNone(result)


class ContainerTopTests(unittest.TestCase):
    def _client(self) -> DockerClient:
        client = DockerClient()
        client._sdk = MagicMock()
        return client

    def test_maps_titles_and_processes(self) -> None:
        client = self._client()
        client._sdk.containers.get.return_value.top.return_value = {
            "Titles": ["PID", "CMD"],
            "Processes": [["1", "python"]],
        }
        result = client.container_top("abc")
        self.assertEqual(
            result, ContainerTop(titles=["PID", "CMD"], processes=[["1", "python"]])
        )

    def test_not_found_returns_none(self) -> None:
        client = self._client()
        client._sdk.containers.get.side_effect = NotFound("no such container")
        result = client.container_top("abc")
        self.assertIsNone(result)

    def test_api_error_returns_none(self) -> None:
        client = self._client()
        response = MagicMock(status_code=409)
        client._sdk.containers.get.return_value.top.side_effect = APIError(
            "container not running", response=response
        )
        result = client.container_top("abc")
        self.assertIsNone(result)

    def test_not_connected_returns_none(self) -> None:
        client = DockerClient()
        result = client.container_top("abc")
        self.assertIsNone(result)


class ContainerCpTests(unittest.TestCase):
    def _client(self) -> DockerClient:
        client = DockerClient()
        client._sdk = MagicMock()
        return client

    def test_builds_cp_argv_and_succeeds(self) -> None:
        client = self._client()
        with (
            patch("docksurf_py.docker.shutil.which", return_value="/usr/bin/docker"),
            patch("docksurf_py.docker.subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = client.container_cp("mycontainer:/etc/hosts", "./hosts")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd, ["docker", "cp", "mycontainer:/etc/hosts", "./hosts"])
        self.assertTrue(result.ok)

    def test_missing_docker_cli_is_daemon_unreachable(self) -> None:
        client = self._client()
        with patch("docksurf_py.docker.shutil.which", return_value=None):
            result = client.container_cp("a", "b")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.DAEMON_UNREACHABLE)

    def test_nonzero_exit_reports_failure_with_stderr(self) -> None:
        client = self._client()
        with (
            patch("docksurf_py.docker.shutil.which", return_value="/usr/bin/docker"),
            patch("docksurf_py.docker.subprocess.run") as run,
        ):
            run.return_value = MagicMock(
                returncode=1, stdout="", stderr="no such file or directory"
            )
            result = client.container_cp("a", "b")
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "no such file or directory")
        self.assertEqual(result.kind, CommandErrorKind.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
