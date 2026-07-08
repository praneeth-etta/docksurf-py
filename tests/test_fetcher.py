"""Unit tests for DockerResourceFetcher's fetch-path shape: parallel
container inspects, tolerance for a container vanishing mid-fetch, and the
images tab's inspect-free listing. See ROBUSTNESS_PERF_P2_PLAN.md §1."""

import unittest
from unittest.mock import MagicMock

from docker.errors import APIError, NotFound

from docksurf_py.docker.fetcher import DockerResourceFetcher


def _container_attrs(cid: str, name: str, image_id: str = "sha256:img1") -> dict:
    return {
        "Id": cid,
        "Name": f"/{name}",
        "Image": image_id,
        "Created": "2026-01-01T00:00:00Z",
        "RestartCount": 0,
        "State": {"Status": "running", "Running": True, "ExitCode": 0},
        "Config": {"Env": [], "Labels": {}},
        "NetworkSettings": {"Ports": {}, "Networks": {}},
        "Mounts": [],
    }


class ParallelInspectTests(unittest.TestCase):
    def test_inspects_every_container_via_low_level_api(self) -> None:
        ids = [f"c{i}" * 8 for i in range(5)]
        sdk = MagicMock()
        sdk.api.containers.return_value = [{"Id": cid} for cid in ids]
        sdk.api.images.return_value = []
        sdk.api.inspect_container.side_effect = lambda cid: _container_attrs(
            cid, cid[:4]
        )

        containers = DockerResourceFetcher(sdk).get_containers()

        self.assertEqual(sdk.api.inspect_container.call_count, len(ids))
        sdk.containers.list.assert_not_called()
        self.assertEqual(len(containers), len(ids))
        self.assertEqual({c.image_id for c in containers}, {"sha256:img1"})

    def test_container_that_404s_mid_fetch_is_skipped_not_fatal(self) -> None:
        sdk = MagicMock()
        sdk.api.containers.return_value = [{"Id": "good1"}, {"Id": "vanished2"}]
        sdk.api.images.return_value = []

        def inspect(cid: str) -> dict:
            if cid == "vanished2":
                raise NotFound("no such container")
            return _container_attrs(cid, "good")

        sdk.api.inspect_container.side_effect = inspect

        containers = DockerResourceFetcher(sdk).get_containers()

        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0].id, "good1"[:12])

    def test_inspect_api_error_is_skipped_not_fatal(self) -> None:
        sdk = MagicMock()
        sdk.api.containers.return_value = [{"Id": "flaky1"}]
        sdk.api.images.return_value = []
        sdk.api.inspect_container.side_effect = APIError("boom")

        containers = DockerResourceFetcher(sdk).get_containers()

        self.assertEqual(containers, [])

    def test_no_containers_skips_inspect_and_image_lookup_entirely(self) -> None:
        sdk = MagicMock()
        sdk.api.containers.return_value = []

        containers = DockerResourceFetcher(sdk).get_containers()

        self.assertEqual(containers, [])
        sdk.api.inspect_container.assert_not_called()
        sdk.api.images.assert_not_called()

    def test_container_tags_resolved_from_image_summary_not_inspect(self) -> None:
        cid = "c1" * 16
        sdk = MagicMock()
        sdk.api.containers.return_value = [{"Id": cid}]
        sdk.api.images.return_value = [
            {"Id": "sha256:img1", "RepoTags": ["myapp:v1", "myapp:latest"]}
        ]
        sdk.api.inspect_container.return_value = _container_attrs(
            cid, "web", image_id="sha256:img1"
        )

        containers = DockerResourceFetcher(sdk).get_containers()

        self.assertEqual(containers[0].image_name, "myapp:v1")
        sdk.images.get.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
