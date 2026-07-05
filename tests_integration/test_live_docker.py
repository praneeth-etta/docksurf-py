"""
tests_integration/test_live_docker.py — DockerClient against a REAL daemon.

Unlike tests/test_docker.py (mocked SDK), these hit an actual `dockerd`
through the real Docker SDK/CLI. They exist to catch what mocks structurally
can't: SDK/CLI behavior drift, real docker cp semantics, actual
container.top() output shapes, and similar. See README.md in this
directory for why this lives outside `tests/` and how to run it.

Every resource this suite creates is named with the docksurf-it- prefix
and removed in tearDown (even on failure) — if a run is interrupted, clean
up stragglers with:

    docker ps -a --filter "name=docksurf-it-" -q | xargs -r docker rm -f
    docker volume ls --filter "name=docksurf-it-" -q | xargs -r docker volume rm -f
    docker network ls --filter "name=docksurf-it-" -q | xargs -r docker network rm

Deliberately NOT covered: prune_*. Every prune method acts on the whole
daemon, not just resources this suite created — running one for real here
could delete a developer's unrelated stopped containers, dangling images, or
networks from other work. tests/test_docker.py (mocked) already covers
their message-formatting and SDK-call-shape logic; that's the right layer
for them.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

from docksurf_py.docker import DockerClient
from docksurf_py.models import Container

_PREFIX = "docksurf-it-"


def _unique_name(kind: str) -> str:
    return f"{_PREFIX}{kind}-{uuid.uuid4().hex[:8]}"


def _docker_available() -> bool:
    """True if the `docker` CLI is on PATH and a real daemon answers."""
    if shutil.which("docker") is None:
        return False
    try:
        client = DockerClient()
        client.fetch_snapshot()
        return client.is_connected
    except Exception:
        return False


_SKIP_REASON = "docker CLI/daemon not available — integration tests skipped"
_DOCKER_AVAILABLE = _docker_available()


def setUpModule() -> None:
    if not _DOCKER_AVAILABLE:
        return
    # Pulled once for the whole module — every fixture container is
    # busybox-based, and a shared pull avoids per-test network flakiness.
    subprocess.run(
        ["docker", "pull", "busybox:latest"], capture_output=True, check=False
    )


class _LiveDockerTestCase(unittest.TestCase):
    """Shared fixture-creation + guaranteed-cleanup helpers.

    `_run_container`/`_create_volume`/`_create_network` register what they
    make; tearDown removes all of it via the `docker` CLI directly (not
    through `DockerClient`, so cleanup doesn't depend on the code under test
    actually working).
    """

    def setUp(self) -> None:
        self.client = DockerClient()
        self.client.fetch_snapshot()  # trigger the lazy connect
        self._containers: list[str] = []
        self._volumes: list[str] = []
        self._networks: list[str] = []

    def tearDown(self) -> None:
        for name in self._containers:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        for name in self._volumes:
            subprocess.run(["docker", "volume", "rm", "-f", name], capture_output=True)
        for name in self._networks:
            subprocess.run(["docker", "network", "rm", name], capture_output=True)

    def _run_container(self, *args: str) -> str:
        name = _unique_name("c")
        subprocess.run(
            ["docker", "run", "-d", "--name", name, *args],
            check=True,
            capture_output=True,
            text=True,
        )
        self._containers.append(name)
        return name

    def _create_volume(self) -> str:
        name = _unique_name("vol")
        subprocess.run(
            ["docker", "volume", "create", name], check=True, capture_output=True
        )
        self._volumes.append(name)
        return name

    def _create_network(self) -> str:
        name = _unique_name("net")
        subprocess.run(
            ["docker", "network", "create", name], check=True, capture_output=True
        )
        self._networks.append(name)
        return name

    def _find_container(self, name: str) -> Container | None:
        snap = self.client.fetch_snapshot()
        return next((c for c in snap.containers if c.name == name), None)


@unittest.skipUnless(_DOCKER_AVAILABLE, _SKIP_REASON)
class ContainerLifecycleIntegrationTests(_LiveDockerTestCase):
    def test_fetch_snapshot_includes_created_container(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        self.assertIsNotNone(c)
        assert c is not None
        self.assertTrue(c.running)

    def test_stop_and_start_lifecycle(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        assert c is not None

        result = self.client.stop_container(c.id)
        self.assertTrue(result.ok)
        stopped = self._find_container(name)
        assert stopped is not None
        self.assertFalse(stopped.running)

        result = self.client.start_container(c.id)
        self.assertTrue(result.ok)
        restarted = self._find_container(name)
        assert restarted is not None
        self.assertTrue(restarted.running)

    def test_restart_container(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        assert c is not None

        result = self.client.restart_container(c.id)
        self.assertTrue(result.ok)
        restarted = self._find_container(name)
        assert restarted is not None
        self.assertTrue(restarted.running)

    def test_pause_and_unpause(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        assert c is not None

        result = self.client.pause_container(c.id)
        self.assertTrue(result.ok)
        paused = self._find_container(name)
        assert paused is not None
        self.assertEqual(paused.state, "paused")

        result = self.client.unpause_container(c.id)
        self.assertTrue(result.ok)
        resumed = self._find_container(name)
        assert resumed is not None
        self.assertTrue(resumed.running)

    def test_kill_sets_exit_code_137(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        assert c is not None

        result = self.client.kill_container(c.id)
        self.assertTrue(result.ok)
        killed = self._find_container(name)
        assert killed is not None
        self.assertFalse(killed.running)
        self.assertEqual(killed.exit_code, 137)

    def test_remove_stopped_container(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        assert c is not None
        self.client.stop_container(c.id)

        result = self.client.remove_container(c.id)
        self.assertTrue(result.ok)
        self._containers.remove(name)  # already gone; tearDown has nothing to do
        self.assertIsNone(self._find_container(name))

    def test_force_remove_running_container(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        assert c is not None

        result = self.client.remove_container(c.id, force=True)
        self.assertTrue(result.ok)
        self._containers.remove(name)
        self.assertIsNone(self._find_container(name))


@unittest.skipUnless(_DOCKER_AVAILABLE, _SKIP_REASON)
class ContainerInspectionIntegrationTests(_LiveDockerTestCase):
    def test_inspect_resource_container(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        assert c is not None

        attrs = self.client.inspect_resource("container", c.id)
        self.assertIsNotNone(attrs)
        assert attrs is not None
        self.assertEqual(attrs["Name"], f"/{name}")
        self.assertIn("Config", attrs)

    def test_inspect_resource_image(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        assert c is not None

        attrs = self.client.inspect_resource("image", c.image_id)
        self.assertIsNotNone(attrs)
        assert attrs is not None
        self.assertIn("RepoTags", attrs)

    def test_inspect_resource_missing_container_returns_none(self) -> None:
        attrs = self.client.inspect_resource("container", "no-such-container")
        self.assertIsNone(attrs)

    def test_container_top_returns_running_processes(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        assert c is not None

        top = self.client.container_top(c.id)
        self.assertIsNotNone(top)
        assert top is not None
        self.assertIn("CMD", top.titles)
        self.assertTrue(any("sleep" in " ".join(p) for p in top.processes))

    def test_container_top_on_stopped_container_returns_none(self) -> None:
        name = self._run_container("busybox", "true")
        for _ in range(50):
            c = self._find_container(name)
            if c is not None and not c.running:
                break
            time.sleep(0.1)

        top = self.client.container_top(name)
        self.assertIsNone(top)


@unittest.skipUnless(_DOCKER_AVAILABLE, _SKIP_REASON)
class ContainerCopyIntegrationTests(_LiveDockerTestCase):
    def test_copy_round_trip_both_directions(self) -> None:
        name = self._run_container("busybox", "sleep", "60")
        c = self._find_container(name)
        assert c is not None

        with tempfile.TemporaryDirectory() as tmpdir:
            src_path = os.path.join(tmpdir, "hello.txt")
            with open(src_path, "w") as f:
                f.write("hello from the integration suite\n")

            result = self.client.container_cp(src_path, f"{c.id}:/tmp/hello.txt")
            self.assertTrue(result.ok)

            out_path = os.path.join(tmpdir, "hello_out.txt")
            result = self.client.container_cp(f"{c.id}:/tmp/hello.txt", out_path)
            self.assertTrue(result.ok)

            with open(out_path) as f:
                self.assertEqual(f.read(), "hello from the integration suite\n")

    def test_copy_from_missing_container_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "out.txt")
            result = self.client.container_cp(
                "no-such-container:/etc/hostname", out_path
            )
            self.assertFalse(result.ok)


@unittest.skipUnless(_DOCKER_AVAILABLE, _SKIP_REASON)
class VolumeIntegrationTests(_LiveDockerTestCase):
    def test_remove_volume(self) -> None:
        name = self._create_volume()
        snap = self.client.fetch_snapshot()
        self.assertTrue(any(v.name == name for v in snap.volumes))

        result = self.client.remove_volume(name)
        self.assertTrue(result.ok)
        self._volumes.remove(name)

        snap = self.client.fetch_snapshot()
        self.assertFalse(any(v.name == name for v in snap.volumes))

    def test_create_volume_then_appears_in_snapshot(self) -> None:
        name = _unique_name("vol")
        self._volumes.append(name)  # register for cleanup before creating
        result = self.client.create_volume(name, "local", {"docksurf-it": "1"})
        self.assertTrue(result.ok)

        snap = self.client.fetch_snapshot()
        match = next((v for v in snap.volumes if v.name == name), None)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.labels.get("docksurf-it"), "1")

    def test_volume_sizes_includes_created_volume(self) -> None:
        name = self._create_volume()
        sizes = self.client.volume_sizes()
        self.assertIn(name, sizes)
        self.assertGreaterEqual(sizes[name], 0)


@unittest.skipUnless(_DOCKER_AVAILABLE, _SKIP_REASON)
class NetworkIntegrationTests(_LiveDockerTestCase):
    def test_remove_network(self) -> None:
        name = self._create_network()
        snap = self.client.fetch_snapshot()
        self.assertTrue(any(n.name == name for n in snap.networks))

        result = self.client.remove_network(name)
        self.assertTrue(result.ok)
        self._networks.remove(name)

        snap = self.client.fetch_snapshot()
        self.assertFalse(any(n.name == name for n in snap.networks))

    def test_create_network_with_subnet(self) -> None:
        name = _unique_name("net")
        self._networks.append(name)  # register for cleanup before creating
        result = self.client.create_network(name, "bridge", "172.31.251.0/24")
        self.assertTrue(result.ok)

        snap = self.client.fetch_snapshot()
        match = next((n for n in snap.networks if n.name == name), None)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.subnet, "172.31.251.0/24")

    def test_connect_disconnect_and_endpoint_detail(self) -> None:
        net = self._create_network()
        cname = self._run_container("--network", net, "busybox", "sleep", "60")
        c = self._find_container(cname)
        assert c is not None

        # The endpoint should show up in Network.endpoints (greedy list inspect).
        match = next(
            (n for n in self.client.fetch_snapshot().networks if n.name == net), None
        )
        assert match is not None
        self.assertTrue(any(ep.container_name == cname for ep in match.endpoints))
        attached = next(ep for ep in match.endpoints if ep.container_name == cname)
        self.assertTrue(attached.ipv4)  # has an IP within the network

        result = self.client.disconnect_container(net, c.id)
        self.assertTrue(result.ok)
        match = next(
            (n for n in self.client.fetch_snapshot().networks if n.name == net), None
        )
        assert match is not None
        self.assertFalse(any(ep.container_name == cname for ep in match.endpoints))

        result = self.client.connect_container(net, c.id)
        self.assertTrue(result.ok)
        match = next(
            (n for n in self.client.fetch_snapshot().networks if n.name == net), None
        )
        assert match is not None
        self.assertTrue(any(ep.container_name == cname for ep in match.endpoints))


@unittest.skipUnless(_DOCKER_AVAILABLE, _SKIP_REASON)
class ImageIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = DockerClient()
        self.client.fetch_snapshot()
        self.tag = _unique_name("image") + ":latest"

    def tearDown(self) -> None:
        subprocess.run(["docker", "rmi", "-f", self.tag], capture_output=True)

    def test_remove_image_tag(self) -> None:
        # Tagging an already-local image is instant (no re-pull) and safe to
        # remove afterward — it only drops this tag, not the shared layers
        # busybox:latest still references.
        subprocess.run(
            ["docker", "tag", "busybox:latest", self.tag],
            check=True,
            capture_output=True,
        )
        repo, _, tag = self.tag.partition(":")
        snap = self.client.fetch_snapshot()
        match = next(
            (i for i in snap.images if i.repository == repo and i.tag == tag), None
        )
        self.assertIsNotNone(match)
        assert match is not None

        result = self.client.remove_image(self.tag)
        self.assertTrue(result.ok)

        snap = self.client.fetch_snapshot()
        self.assertFalse(
            any(i.repository == repo and i.tag == tag for i in snap.images)
        )

    def test_tag_image_creates_new_ref(self) -> None:
        # busybox:latest is pulled by setUpModule; tag it under our unique repo.
        snap = self.client.fetch_snapshot()
        busybox = next((i for i in snap.images if i.repository == "busybox"), None)
        self.assertIsNotNone(busybox)
        assert busybox is not None

        repo, _, tag = self.tag.partition(":")
        result = self.client.tag_image(busybox.id, repo, tag)
        self.assertTrue(result.ok)

        snap = self.client.fetch_snapshot()
        self.assertTrue(any(i.repository == repo and i.tag == tag for i in snap.images))

    def test_image_history_returns_layers(self) -> None:
        snap = self.client.fetch_snapshot()
        busybox = next((i for i in snap.images if i.repository == "busybox"), None)
        self.assertIsNotNone(busybox)
        assert busybox is not None

        layers = self.client.image_history(busybox.id)
        self.assertIsNotNone(layers)
        assert layers is not None
        self.assertGreater(len(layers), 0)


if __name__ == "__main__":
    unittest.main()
