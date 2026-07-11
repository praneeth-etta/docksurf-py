"""Unit tests for DockerResourceFetcher's fetch-path shape: building
containers from the `/containers/json` list summary alone (no per-container
inspect — PATCH_WORK.md P-1), the images tab's inspect-free listing, and
`fetch_snapshot`'s single shared image listing (P-2)."""

import unittest
from unittest.mock import MagicMock

from docksurf_py.docker.fetcher import DockerResourceFetcher, _parse_summary_ports
from docksurf_py.docker.format import format_ports
from docksurf_py.models import PortBinding


def _container_summary(cid: str, name: str, image_id: str = "sha256:img1") -> dict:
    return {
        "Id": cid,
        "Names": [f"/{name}"],
        "ImageID": image_id,
        "Created": 1735689600,  # 2025-01-01T00:00:00Z
        "State": "running",
        "Status": "Up 5 minutes",
        "Labels": {},
        "NetworkSettings": {"Networks": {}},
        "Mounts": [],
        "Ports": [],
    }


class SummaryOnlyContainerTests(unittest.TestCase):
    def test_builds_every_container_without_inspecting_any(self) -> None:
        ids = [f"c{i}" * 8 for i in range(5)]
        sdk = MagicMock()
        sdk.api.containers.return_value = [
            _container_summary(cid, cid[:4]) for cid in ids
        ]
        sdk.api.images.return_value = []

        containers = DockerResourceFetcher(sdk).get_containers()

        sdk.api.inspect_container.assert_not_called()
        sdk.containers.list.assert_not_called()
        self.assertEqual(len(containers), len(ids))
        self.assertEqual({c.image_id for c in containers}, {"sha256:img1"})

    def test_no_containers_skips_image_lookup_entirely(self) -> None:
        sdk = MagicMock()
        sdk.api.containers.return_value = []

        containers = DockerResourceFetcher(sdk).get_containers()

        self.assertEqual(containers, [])
        sdk.api.inspect_container.assert_not_called()
        sdk.api.images.assert_not_called()

    def test_container_tags_resolved_from_image_summary(self) -> None:
        cid = "c1" * 16
        sdk = MagicMock()
        sdk.api.containers.return_value = [
            _container_summary(cid, "web", image_id="sha256:img1")
        ]
        sdk.api.images.return_value = [
            {"Id": "sha256:img1", "RepoTags": ["myapp:v1", "myapp:latest"]}
        ]

        containers = DockerResourceFetcher(sdk).get_containers()

        self.assertEqual(containers[0].image_name, "myapp:v1")
        sdk.images.get.assert_not_called()

    def test_shares_a_prefetched_image_summary_instead_of_fetching_its_own(
        self,
    ) -> None:
        cid = "d1" * 16
        sdk = MagicMock()
        sdk.api.containers.return_value = [
            _container_summary(cid, "web", image_id="sha256:img1")
        ]
        image_summaries = [
            {"Id": "sha256:img1", "RepoTags": ["myapp:v1"]},
        ]

        containers = DockerResourceFetcher(sdk).get_containers(image_summaries)

        self.assertEqual(containers[0].image_name, "myapp:v1")
        sdk.api.images.assert_not_called()

    def test_health_and_exit_code_parsed_from_status_text_not_inspected(self) -> None:
        sdk = MagicMock()
        healthy = _container_summary("a" * 64, "web")
        healthy["Status"] = "Up 2 hours (healthy)"
        exited = _container_summary("b" * 64, "db")
        exited["State"] = "exited"
        exited["Status"] = "Exited (137) 3 minutes ago"
        sdk.api.containers.return_value = [healthy, exited]
        sdk.api.images.return_value = []

        containers = DockerResourceFetcher(sdk).get_containers()

        by_name = {c.name: c for c in containers}
        self.assertEqual(by_name["web"].health, "healthy")
        self.assertEqual(by_name["web"].uptime_hint, "2 hours")
        self.assertEqual(by_name["db"].exit_code, 137)
        self.assertFalse(by_name["db"].running)
        sdk.api.inspect_container.assert_not_called()


class PortParsingTests(unittest.TestCase):
    """`_parse_summary_ports` parses a different shape than a full inspect's
    `NetworkSettings.Ports` — the list summary is already a flat list of
    individual bindings, one dict per container-port/host-port pair."""

    def test_published_and_unpublished_ports(self) -> None:
        raw = [
            {"PrivatePort": 80, "PublicPort": 8080, "IP": "0.0.0.0", "Type": "tcp"},
            {"PrivatePort": 443, "Type": "tcp"},
        ]

        bindings = _parse_summary_ports(raw)

        self.assertEqual(
            bindings,
            [
                PortBinding(
                    container_port="80/tcp", host_ip="0.0.0.0", host_port="8080"
                ),
                PortBinding(container_port="443/tcp"),
            ],
        )
        self.assertEqual(format_ports(bindings), "0.0.0.0:8080->80/tcp, 443/tcp")

    def test_no_ports_is_empty(self) -> None:
        self.assertEqual(_parse_summary_ports(None), [])
        self.assertEqual(_parse_summary_ports([]), [])

    def test_get_containers_carries_parsed_ports_through(self) -> None:
        summary = _container_summary("a" * 64, "web")
        summary["Ports"] = [
            {"PrivatePort": 80, "PublicPort": 8080, "IP": "0.0.0.0", "Type": "tcp"}
        ]
        sdk = MagicMock()
        sdk.api.containers.return_value = [summary]
        sdk.api.images.return_value = []

        got = DockerResourceFetcher(sdk).get_containers()[0]

        self.assertEqual(
            got.ports,
            [PortBinding(container_port="80/tcp", host_ip="0.0.0.0", host_port="8080")],
        )


class ImagesNoInspectTests(unittest.TestCase):
    def test_get_images_never_inspects(self) -> None:
        sdk = MagicMock()
        sdk.api.images.return_value = [
            {
                "Id": "sha256:img1",
                "RepoTags": ["alpine:latest"],
                "Size": 1024,
                "Created": 1735689600,  # 2025-01-01T00:00:00Z
            }
        ]

        images = DockerResourceFetcher(sdk).get_images()

        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].repository, "alpine")
        self.assertEqual(images[0].tag, "latest")
        self.assertEqual(images[0].size_bytes, 1024)
        self.assertTrue(images[0].created.startswith("2025-01-01"))
        sdk.images.list.assert_not_called()
        sdk.images.get.assert_not_called()

    def test_untagged_image_falls_back_to_none_sentinel(self) -> None:
        sdk = MagicMock()
        sdk.api.images.return_value = [
            {"Id": "sha256:dangling1", "RepoTags": None, "Size": 0, "Created": 0}
        ]

        images = DockerResourceFetcher(sdk).get_images()

        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].is_dangling)
        self.assertEqual(images[0].created, "")


class SharedImageListingTests(unittest.TestCase):
    """`fetch_snapshot` fetches `/images/json` once and shares it with both
    `get_containers` (tag resolution) and `get_images`, instead of each
    fetching it separately (PATCH_WORK.md P-2)."""

    def test_fetch_snapshot_lists_images_only_once(self) -> None:
        sdk = MagicMock()
        sdk.api.containers.return_value = [
            _container_summary("c1" * 16, "web", image_id="sha256:img1")
        ]
        sdk.api.images.return_value = [
            {"Id": "sha256:img1", "RepoTags": ["myapp:v1"], "Size": 10, "Created": 0}
        ]
        sdk.volumes.list.return_value = []
        sdk.networks.list.return_value = []

        snapshot, errors = DockerResourceFetcher(sdk).fetch_snapshot()

        self.assertEqual(errors, {})
        self.assertEqual(sdk.api.images.call_count, 1)
        self.assertEqual(snapshot.containers[0].image_name, "myapp:v1")
        self.assertEqual(snapshot.images[0].repository, "myapp")


class FetchPoolLifecycleTests(unittest.TestCase):
    """The fetch pool (PATCH_WORK.md P-3) is hoisted into __init__ and reused
    across calls instead of being built and torn down per fetch_snapshot()."""

    def _sdk(self) -> MagicMock:
        sdk = MagicMock()
        sdk.api.containers.return_value = []
        sdk.api.images.return_value = []
        sdk.volumes.list.return_value = []
        sdk.networks.list.return_value = []
        return sdk

    def test_same_pool_reused_across_fetch_snapshot_calls(self) -> None:
        fetcher = DockerResourceFetcher(self._sdk())
        pool_before = fetcher._pool
        fetcher.fetch_snapshot()
        fetcher.fetch_snapshot()
        self.assertIs(fetcher._pool, pool_before)
        self.assertFalse(pool_before._shutdown)

    def test_close_shuts_down_the_pool_without_cancelling_futures(self) -> None:
        fetcher = DockerResourceFetcher(self._sdk())
        fetcher.close()
        self.assertTrue(fetcher._pool._shutdown)

    def test_close_does_not_raise_cancelled_error_into_in_flight_fetch(self) -> None:
        # Regression: shutdown(cancel_futures=True) would raise
        # concurrent.futures.CancelledError (a BaseException, not Exception)
        # out of future.result() for any task still queued when close() runs
        # mid-fetch — exactly what a daemon-disconnect race looks like.
        fetcher = DockerResourceFetcher(self._sdk())
        snapshot, errors = fetcher.fetch_snapshot()
        fetcher.close()
        self.assertEqual(errors, {})
        self.assertEqual(snapshot.containers, [])


if __name__ == "__main__":
    unittest.main()
