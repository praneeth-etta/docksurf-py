import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        with patch("docksurf_py.docker.context.docker.from_env") as from_env:
            client = DockerClient()

        from_env.assert_not_called()
        self.assertEqual(client.connection.status, ConnectionStatus.NOT_CONNECTED)
        self.assertFalse(client.is_connected)

    def test_fetch_snapshot_connects_lazily(self) -> None:
        fake_sdk = _fake_sdk()
        with patch("docksurf_py.docker.context.docker.from_env", return_value=fake_sdk):
            client = DockerClient()
            self.assertFalse(client.is_connected)

            snapshot = client.fetch_snapshot()

        self.assertTrue(client.is_connected)
        self.assertEqual(snapshot, DockerSnapshot([], [], [], []))
        fake_sdk.ping.assert_called_once()

    def test_fetch_snapshot_does_not_reping_once_connected(self) -> None:
        fake_sdk = _fake_sdk()
        with patch(
            "docksurf_py.docker.context.docker.from_env", return_value=fake_sdk
        ) as from_env:
            client = DockerClient()
            client.fetch_snapshot()
            client.fetch_snapshot()

        from_env.assert_called_once()
        fake_sdk.ping.assert_called_once()

    def test_fetch_snapshot_retries_after_daemon_recovers(self) -> None:
        fake_sdk = _fake_sdk()
        with patch("docksurf_py.docker.context.docker.from_env") as from_env:
            from_env.side_effect = [DockerException("daemon down"), fake_sdk]
            client = DockerClient()

            first = client.fetch_snapshot()
            self.assertFalse(client.is_connected)
            self.assertEqual(first, DockerSnapshot([], [], [], []))

            second = client.fetch_snapshot()

        self.assertTrue(client.is_connected)
        self.assertEqual(second, DockerSnapshot([], [], [], []))
        self.assertEqual(from_env.call_count, 2)


def _fake_context(name: str, host: str, tls=False) -> SimpleNamespace:
    return SimpleNamespace(Name=name, Host=host, TLSConfig=tls)


def _new_client() -> DockerClient:
    """A `DockerClient()` that never picks up a real persisted context
    override from this machine's `~/.local/share/docksurf-py/state.json`,
    so these tests stay hermetic regardless of host state."""
    with patch("docksurf_py.docker.client._load_last_context", return_value=None):
        return DockerClient()


class MarkDisconnectedTests(unittest.TestCase):
    def test_flips_state_and_resets_client(self) -> None:
        fake_sdk = _fake_sdk()
        with patch("docksurf_py.docker.context.docker.from_env", return_value=fake_sdk):
            client = _new_client()
            client.fetch_snapshot()  # connects
        self.assertTrue(client.is_connected)

        client.mark_disconnected(ConnectionError("connection refused"))

        self.assertFalse(client.is_connected)
        self.assertIsNone(client._sdk)
        self.assertIsNone(client._fetcher)

    def test_preserves_context_and_host_over_ambient(self) -> None:
        fake_sdk = _fake_sdk()
        with patch("docksurf_py.docker.context.docker.from_env", return_value=fake_sdk):
            client = _new_client()
            client.fetch_snapshot()
        client.connection.context = "custom-ctx"
        client.connection.host = "ssh://example.com"

        client.mark_disconnected(ConnectionError("boom"))

        self.assertEqual(client.connection.context, "custom-ctx")
        self.assertEqual(client.connection.host, "ssh://example.com")

    def test_noop_if_already_disconnected(self) -> None:
        client = _new_client()
        self.assertFalse(client.is_connected)
        client.mark_disconnected(ConnectionError("boom"))  # should not raise
        self.assertFalse(client.is_connected)

    def test_fetch_snapshot_marks_disconnected_on_daemon_death(self) -> None:
        fake_sdk = _fake_sdk()
        with patch("docksurf_py.docker.context.docker.from_env", return_value=fake_sdk):
            client = _new_client()
            client.fetch_snapshot()  # connects
            self.assertTrue(client.is_connected)

            fake_sdk.containers.list.side_effect = DockerException("daemon down")
            snapshot = client.fetch_snapshot()

        self.assertEqual(snapshot, DockerSnapshot([], [], [], []))
        self.assertFalse(client.is_connected)


class ContextPersistenceTests(unittest.TestCase):
    def test_round_trips_last_context(self) -> None:
        import docksurf_py.docker.context as dockmod

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            with patch.object(dockmod, "_STATE_FILE", state_file):
                self.assertIsNone(dockmod._load_last_context())

                dockmod._save_last_context("remote")
                self.assertEqual(dockmod._load_last_context(), "remote")
                self.assertEqual(
                    json.loads(state_file.read_text()), {"context": "remote"}
                )

                dockmod._clear_last_context()
                self.assertIsNone(dockmod._load_last_context())

    def test_load_tolerates_missing_or_corrupt_file(self) -> None:
        import docksurf_py.docker.context as dockmod

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "nested" / "state.json"
            with patch.object(dockmod, "_STATE_FILE", state_file):
                self.assertIsNone(dockmod._load_last_context())  # missing dir/file

                state_file.parent.mkdir(parents=True)
                state_file.write_text("not json")
                self.assertIsNone(dockmod._load_last_context())


class SwitchContextTests(unittest.TestCase):
    def test_switch_success_updates_connection_and_persists(self) -> None:
        fake_sdk = _fake_sdk()
        ctx = _fake_context("remote", "ssh://example.com")
        client = _new_client()
        with (
            patch("docker.context.ContextAPI.get_context", return_value=ctx),
            patch(
                "docksurf_py.docker.context.docker.DockerClient", return_value=fake_sdk
            ),
            patch("docksurf_py.docker.client._save_last_context") as save,
        ):
            result = client.switch_context("remote")

        self.assertTrue(result.ok)
        self.assertTrue(client.is_connected)
        self.assertEqual(client.connection.context, "remote")
        self.assertEqual(client.connection.host, "ssh://example.com")
        self.assertEqual(client._context_override, "remote")
        save.assert_called_once_with("remote")

    def test_switch_to_missing_context_fails(self) -> None:
        client = _new_client()
        with patch("docker.context.ContextAPI.get_context", return_value=None):
            result = client.switch_context("nope")

        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.NOT_FOUND)

    def test_switch_leaves_current_connection_untouched_on_ping_failure(self) -> None:
        fake_sdk = _fake_sdk()
        with patch("docksurf_py.docker.context.docker.from_env", return_value=fake_sdk):
            client = _new_client()
            client.fetch_snapshot()  # connects via ambient default
        self.assertTrue(client.is_connected)

        unreachable_ctx = _fake_context("remote", "ssh://unreachable.example")
        unreachable_sdk = MagicMock()
        unreachable_sdk.ping.side_effect = DockerException("no route to host")
        with (
            patch(
                "docker.context.ContextAPI.get_context", return_value=unreachable_ctx
            ),
            patch(
                "docksurf_py.docker.context.docker.DockerClient",
                return_value=unreachable_sdk,
            ),
        ):
            result = client.switch_context("remote")

        self.assertFalse(result.ok)
        # The working connection from before the failed switch is untouched.
        self.assertTrue(client.is_connected)
        self.assertIs(client._sdk, fake_sdk)

    def test_list_contexts_marks_current_by_active_connection(self) -> None:
        fake_sdk = _fake_sdk()
        with patch("docksurf_py.docker.context.docker.from_env", return_value=fake_sdk):
            client = _new_client()
            client.fetch_snapshot()  # connects, context becomes "default"

        contexts = [
            _fake_context("default", "unix:///var/run/docker.sock"),
            _fake_context("remote", "ssh://example.com"),
        ]
        with patch("docker.context.ContextAPI.contexts", return_value=contexts):
            infos = client.list_contexts()

        by_name = {c.name: c for c in infos}
        self.assertTrue(by_name["default"].is_current)
        self.assertFalse(by_name["remote"].is_current)

    def test_list_contexts_returns_empty_on_error(self) -> None:
        client = _new_client()
        with patch("docker.context.ContextAPI.contexts", side_effect=Exception("boom")):
            self.assertEqual(client.list_contexts(), [])


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
            patch(
                "docksurf_py.docker.client.shutil.which", return_value="/usr/bin/docker"
            ),
            patch("docksurf_py.docker.client.subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = client.container_cp("mycontainer:/etc/hosts", "./hosts")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd, ["docker", "cp", "mycontainer:/etc/hosts", "./hosts"])
        self.assertTrue(result.ok)

    def test_missing_docker_cli_is_daemon_unreachable(self) -> None:
        client = self._client()
        with patch("docksurf_py.docker.client.shutil.which", return_value=None):
            result = client.container_cp("a", "b")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.DAEMON_UNREACHABLE)

    def test_nonzero_exit_reports_failure_with_stderr(self) -> None:
        client = self._client()
        with (
            patch(
                "docksurf_py.docker.client.shutil.which", return_value="/usr/bin/docker"
            ),
            patch("docksurf_py.docker.client.subprocess.run") as run,
        ):
            run.return_value = MagicMock(
                returncode=1, stdout="", stderr="no such file or directory"
            )
            result = client.container_cp("a", "b")
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "no such file or directory")
        self.assertEqual(result.kind, CommandErrorKind.UNKNOWN)


class ImageWriteTests(unittest.TestCase):
    def _client(self) -> DockerClient:
        client = DockerClient()
        client._sdk = MagicMock()
        return client

    def test_tag_image_calls_sdk_tag(self) -> None:
        client = self._client()
        client._sdk.images.get.return_value.tag.return_value = True
        result = client.tag_image("sha256:x", "myrepo", "v2")
        client._sdk.images.get.assert_called_once_with("sha256:x")
        client._sdk.images.get.return_value.tag.assert_called_once_with(
            "myrepo", tag="v2"
        )
        self.assertTrue(result.ok)
        self.assertIn("myrepo:v2", result.message)

    def test_tag_image_rejected_returns_failure(self) -> None:
        client = self._client()
        client._sdk.images.get.return_value.tag.return_value = False
        result = client.tag_image("sha256:x", "myrepo", "v2")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.UNKNOWN)

    def test_tag_image_not_found_is_classified(self) -> None:
        client = self._client()
        client._sdk.images.get.side_effect = NotFound("no such image")
        result = client.tag_image("sha256:x", "myrepo", "v2")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.NOT_FOUND)

    def test_image_history_maps_layers(self) -> None:
        client = self._client()
        client._sdk.images.get.return_value.history.return_value = [
            {"CreatedBy": "/bin/sh -c apk add curl", "Size": 2048, "Created": 1},
            {"CreatedBy": "/bin/sh -c #(nop) CMD", "Size": 0, "Created": 2},
        ]
        layers = client.image_history("sha256:x")
        assert layers is not None
        self.assertEqual(len(layers), 2)
        self.assertEqual(layers[0].size_bytes, 2048)
        self.assertEqual(layers[0].created_by, "/bin/sh -c apk add curl")

    def test_image_history_not_found_returns_none(self) -> None:
        client = self._client()
        client._sdk.images.get.side_effect = NotFound("no such image")
        self.assertIsNone(client.image_history("sha256:x"))

    def test_image_history_not_connected_returns_none(self) -> None:
        client = DockerClient()
        self.assertIsNone(client.image_history("sha256:x"))


class VolumeWriteTests(unittest.TestCase):
    def _client(self) -> DockerClient:
        client = DockerClient()
        client._sdk = MagicMock()
        return client

    def test_create_volume_calls_sdk_create(self) -> None:
        client = self._client()
        client._sdk.volumes.create.return_value.name = "myvol"
        result = client.create_volume("myvol", "local", {"env": "test"})
        client._sdk.volumes.create.assert_called_once_with(
            name="myvol", driver="local", labels={"env": "test"}
        )
        self.assertTrue(result.ok)
        self.assertIn("myvol", result.message)

    def test_create_volume_anonymous_passes_none_name(self) -> None:
        client = self._client()
        client._sdk.volumes.create.return_value.name = "auto"
        client.create_volume("", "local", {})
        _, kwargs = client._sdk.volumes.create.call_args
        self.assertIsNone(kwargs["name"])

    def test_volume_sizes_parses_df(self) -> None:
        client = self._client()
        client._sdk.df.return_value = {
            "Volumes": [
                {"Name": "a", "UsageData": {"Size": 100}},
                {"Name": "b", "UsageData": {"Size": 0}},
                {"Name": "c"},  # no UsageData
            ]
        }
        sizes = client.volume_sizes()
        self.assertEqual(sizes, {"a": 100, "b": 0, "c": 0})

    def test_volume_sizes_not_connected_returns_empty(self) -> None:
        client = DockerClient()
        self.assertEqual(client.volume_sizes(), {})


class NetworkWriteTests(unittest.TestCase):
    def _client(self) -> DockerClient:
        client = DockerClient()
        client._sdk = MagicMock()
        return client

    def test_create_network_without_subnet_passes_no_ipam(self) -> None:
        client = self._client()
        client._sdk.networks.create.return_value.name = "net1"
        result = client.create_network("net1", "bridge", "")
        _, kwargs = client._sdk.networks.create.call_args
        self.assertIsNone(kwargs["ipam"])
        self.assertTrue(result.ok)

    def test_create_network_with_subnet_builds_ipam(self) -> None:
        client = self._client()
        client._sdk.networks.create.return_value.name = "net1"
        client.create_network("net1", "bridge", "172.30.0.0/16")
        _, kwargs = client._sdk.networks.create.call_args
        self.assertIsNotNone(kwargs["ipam"])

    def test_connect_container_calls_sdk(self) -> None:
        client = self._client()
        result = client.connect_container("net1", "cid")
        client._sdk.networks.get.assert_called_with("net1")
        client._sdk.networks.get.return_value.connect.assert_called_once_with("cid")
        self.assertTrue(result.ok)

    def test_disconnect_container_calls_sdk_with_force(self) -> None:
        client = self._client()
        result = client.disconnect_container("net1", "cid")
        client._sdk.networks.get.return_value.disconnect.assert_called_once_with(
            "cid", force=True
        )
        self.assertTrue(result.ok)

    def test_create_network_not_connected_is_daemon_unreachable(self) -> None:
        client = DockerClient()
        result = client.create_network("net1")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.DAEMON_UNREACHABLE)


class GetNetworksEndpointTests(unittest.TestCase):
    def test_parses_attached_container_endpoints(self) -> None:
        from docksurf_py.docker import DockerResourceFetcher

        fake_net = MagicMock()
        fake_net.short_id = "abc123"
        fake_net.name = "mynet"
        fake_net.attrs = {
            "Driver": "bridge",
            "Scope": "local",
            "IPAM": {"Config": [{"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}]},
            "Containers": {
                "cid1": {
                    "Name": "web",
                    "IPv4Address": "172.18.0.2/16",
                    "IPv6Address": "",
                    "MacAddress": "02:42:ac:12:00:02",
                },
            },
        }
        sdk = MagicMock()
        sdk.networks.list.return_value = [fake_net]
        networks = DockerResourceFetcher(sdk).get_networks()
        self.assertEqual(len(networks), 1)
        self.assertEqual(len(networks[0].endpoints), 1)
        ep = networks[0].endpoints[0]
        self.assertEqual(ep.container_name, "web")
        self.assertEqual(ep.ipv4, "172.18.0.2/16")
        self.assertEqual(ep.mac, "02:42:ac:12:00:02")

    def test_no_containers_yields_empty_endpoints(self) -> None:
        from docksurf_py.docker import DockerResourceFetcher

        fake_net = MagicMock()
        fake_net.short_id = "abc123"
        fake_net.name = "mynet"
        fake_net.attrs = {"Driver": "bridge", "Scope": "local", "IPAM": {"Config": []}}
        sdk = MagicMock()
        sdk.networks.list.return_value = [fake_net]
        networks = DockerResourceFetcher(sdk).get_networks()
        self.assertEqual(networks[0].endpoints, [])


class PullStreamTests(unittest.TestCase):
    def test_yields_chunks_from_api_pull(self) -> None:
        from docksurf_py.docker import PullStream

        sdk = MagicMock()
        sdk.api.pull.return_value = iter(
            [{"status": "Pulling"}, {"status": "Pull complete", "id": "x"}]
        )
        chunks = list(PullStream("alpine", "latest", sdk))
        self.assertEqual(len(chunks), 2)
        sdk.api.pull.assert_called_once_with(
            "alpine", tag="latest", stream=True, decode=True
        )

    def test_api_error_yields_error_chunk(self) -> None:
        from docksurf_py.docker import PullStream

        sdk = MagicMock()
        sdk.api.pull.side_effect = DockerException("no such image")
        chunks = list(PullStream("nope", "latest", sdk))
        self.assertEqual(len(chunks), 1)
        self.assertIn("error", chunks[0])

    def test_no_client_yields_nothing(self) -> None:
        from docksurf_py.docker import PullStream

        self.assertEqual(list(PullStream("alpine", "latest", None)), [])


if __name__ == "__main__":
    unittest.main()
