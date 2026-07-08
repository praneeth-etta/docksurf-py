"""Read-only Docker state fetching, parsed into typed dataclasses."""

import logging
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone

from docker.errors import APIError, NotFound

from docksurf_py.models import (
    Container,
    DockerSnapshot,
    HealthProbe,
    Image,
    Network,
    NetworkEndpoint,
    PortBinding,
    Volume,
)

logger = logging.getLogger(__name__)

# Number of workers used to parallelize `inspect_container` calls in
# `get_containers()`. docker-py performs these inspections serially, making
# container discovery effectively one HTTP round-trip per container. A small
# worker pool reduces overall latency while avoiding excessive concurrent
# connections on large Docker hosts.
_INSPECT_WORKERS = 16


def _filter_real_tags(repo_tags: list[str] | None) -> list[str]:
    """Repo tags with Docker's `<none>:<none>` placeholder removed.

    Mirrors docker-py's `Image.tags` property — an untagged image reports
    `RepoTags: ["<none>:<none>"]` (or `None`), which is never a tag worth
    keeping.
    """
    return [t for t in (repo_tags or []) if t != "<none>:<none>"]


def _image_tags_by_id(image_summaries: list[dict]) -> dict[str, list[str]]:
    """Map each image ID to its real tags, from a raw `/images/json` listing.

    Reused by `get_containers()` to resolve a container's image name,
    replaces docker-py's `Container.image` property, which would otherwise
    trigger one *additional* full inspect per container just to read its
    image's tags.
    """
    return {s["Id"]: _filter_real_tags(s.get("RepoTags")) for s in image_summaries}


def _unix_ts_to_iso(ts: int | float | None) -> str:
    """Convert the image-list summary's Unix-seconds `Created` to the same
    RFC3339-ish shape a full inspect's `Created` field already has, so
    `format_relative_time` (which only ever parsed inspect timestamps before)
    doesn't need a second code path."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# TODO: Refactor to reduce complexity.
def _build_container(
    container_id: str, attrs: dict, tags_by_image_id: dict[str, list[str]]
) -> Container:
    """Assemble one `Container` from a full `inspect_container` attrs dict."""
    ports: list[PortBinding] = []
    port_bindings = attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    for port, bindings in port_bindings.items():
        if bindings:
            for binding in bindings:
                ports.append(
                    PortBinding(
                        container_port=port,
                        host_ip=binding.get("HostIp", ""),
                        host_port=binding.get("HostPort", ""),
                    )
                )
        else:
            ports.append(PortBinding(container_port=port))

    mounts = [
        m["Name"]
        for m in attrs.get("Mounts", [])
        if m.get("Type") == "volume" and m.get("Name")
    ]

    networks = list(attrs.get("NetworkSettings", {}).get("Networks", {}).keys())
    config = attrs.get("Config", {}) or {}
    env_vars = config.get("Env", [])
    labels = config.get("Labels") or {}

    image_id = attrs.get("Image", "")
    image_tags = tags_by_image_id.get(image_id) or [image_id]

    sdk_state = attrs.get("State") or {}
    health_info = sdk_state.get("Health") or {}
    health_log = [
        HealthProbe(
            start=probe.get("Start", ""),
            exit_code=probe.get("ExitCode", 0),
            output=(probe.get("Output") or "").strip(),
        )
        for probe in (health_info.get("Log") or [])
    ]
    status = sdk_state.get("Status", "")

    return Container(
        id=container_id[:12],
        name=(attrs.get("Name") or "").lstrip("/"),
        image_id=image_id,
        image_name=image_tags[0],
        status=status,
        state=status,
        running=sdk_state.get("Running", False),
        exit_code=sdk_state.get("ExitCode", 0),
        health=health_info.get("Status", ""),
        ports=ports,
        mounts=mounts,
        networks=networks,
        created=attrs.get("Created", ""),
        env=env_vars,
        labels=labels,
        started_at=sdk_state.get("StartedAt", ""),
        restart_count=attrs.get("RestartCount", 0),
        health_log=health_log,
    )


class DockerResourceFetcher:
    """
    Fetches Docker state via the SDK and parses it into typed dataclasses.
    Knows nothing about management commands — read-only.
    """

    def __init__(self, sdk_client) -> None:
        self._client = sdk_client

    def get_containers(self) -> list[Container]:
        summaries = self._client.api.containers(all=True)
        if not summaries:
            return []

        tags_by_image_id = _image_tags_by_id(self._client.api.images(all=True))

        ids = [s["Id"] for s in summaries]
        attrs_by_id = self._inspect_containers(ids)

        containers = []
        for container_id in ids:
            attrs = attrs_by_id.get(container_id)
            if attrs is None:
                continue  # vanished mid-fetch, or inspect failed — see below
            containers.append(_build_container(container_id, attrs, tags_by_image_id))
        return containers

    def _inspect_containers(self, ids: list[str]) -> dict[str, dict]:
        """Fan `inspect_container` out across a small thread pool.

        A container removed between the list call and its own inspect (a
        race, not a real error) or one that briefly rejects the request
        raises `NotFound`/`APIError` — skip just that container rather than
        failing the whole fetch.
        """
        attrs_by_id: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=_INSPECT_WORKERS) as pool:
            future_to_id = {
                pool.submit(self._client.api.inspect_container, cid): cid for cid in ids
            }
            for future, cid in future_to_id.items():
                try:
                    attrs_by_id[cid] = future.result()
                except NotFound:
                    logger.debug("Container %s vanished mid-fetch — skipping", cid)
                except APIError as e:
                    logger.warning("Inspect failed for container %s: %s", cid, e)
        return attrs_by_id

    def get_images(self) -> list[Image]:
        """Build every `Image` row straight from the `/images/json` listing.

        No per-image inspect — unlike docker-py's `images.list()`, which
        calls `.get(id)` (a full inspect) for every item, one at a time. The
        one field that requires a full inspect, `Architecture`, is detail-pane
        only (never shown in the table or matched by search) and is fetched
        lazily on row-select instead — see `DockerClient.image_architecture`.
        """
        images = []
        for s in self._client.api.images(all=True):
            tags = _filter_real_tags(s.get("RepoTags")) or ["<none>:<none>"]
            for tag_str in tags:
                repo, _, tag = tag_str.partition(":")
                if not tag:
                    tag = "latest"
                images.append(
                    Image(
                        id=s.get("Id", ""),  # full SHA256 — matches container.image_id
                        repository=repo,
                        tag=tag,
                        size_bytes=s.get("Size") or 0,
                        is_dangling=(repo == "<none>" and tag == "<none>"),
                        used_by=[],
                        created=_unix_ts_to_iso(s.get("Created")),
                    )
                )
        return images

    def get_volumes(self) -> list[Volume]:
        volumes = []
        for v in self._client.volumes.list():
            volumes.append(
                Volume(
                    name=v.name,
                    driver=v.attrs.get("Driver", ""),
                    mountpoint=v.attrs.get("Mountpoint", ""),
                    used_by=[],
                    labels=v.attrs.get("Labels") or {},
                )
            )
        return volumes

    def get_networks(self) -> list[Network]:
        networks = []
        # greedy=True inspects each network so `attrs["Containers"]` (the
        # attached endpoints with per-container IP/MAC) is populated — the plain
        # list endpoint leaves it empty. Networks are few, so the extra inspects
        # are cheap (unlike the container list's N+1 concern).
        for n in self._client.networks.list(greedy=True):
            ipam_config = n.attrs.get("IPAM", {}).get("Config", [])
            subnet = gateway = "N/A"
            if ipam_config and isinstance(ipam_config, list) and len(ipam_config) > 0:
                subnet = ipam_config[0].get("Subnet", "N/A")
                gateway = ipam_config[0].get("Gateway", "N/A")
            endpoints = []
            for ep in (n.attrs.get("Containers") or {}).values():
                endpoints.append(
                    NetworkEndpoint(
                        container_name=ep.get("Name", ""),
                        ipv4=ep.get("IPv4Address", ""),
                        ipv6=ep.get("IPv6Address", ""),
                        mac=ep.get("MacAddress", ""),
                    )
                )
            networks.append(
                Network(
                    id=n.short_id,
                    name=n.name,
                    driver=n.attrs.get("Driver", ""),
                    subnet=subnet,
                    gateway=gateway,
                    scope=n.attrs.get("Scope", ""),
                    used_by=[],
                    endpoints=endpoints,
                )
            )
        return networks

    def fetch_snapshot(self) -> tuple[DockerSnapshot, dict[str, Exception]]:
        """Fetch all four resource types, tolerating a category-scoped failure.

        Each of the four fetches runs independently — one failing (e.g. a
        transient error listing networks) must not blank out the other three,
        which were already fetched fine. `errors` names which categories
        failed (empty dict on a fully clean fetch); the caller
        (`DockerClient.fetch_snapshot`) decides what a failure means —
        substituting stale data for a single flaky category, or treating a
        failure across every category as the daemon itself being down.
        """
        logger.debug("Fetching Docker snapshot")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures: dict[str, Future] = {
                "containers": pool.submit(self.get_containers),
                "images": pool.submit(self.get_images),
                "volumes": pool.submit(self.get_volumes),
                "networks": pool.submit(self.get_networks),
            }

        results: dict[str, list] = {}
        errors: dict[str, Exception] = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as e:
                logger.warning("Fetching %s failed: %s", name, e)
                errors[name] = e
                results[name] = []

        containers = results["containers"]
        images = results["images"]
        volumes = results["volumes"]
        networks = results["networks"]

        image_usage: dict[str, list[str]] = defaultdict(list)
        volume_usage: dict[str, list[str]] = defaultdict(list)
        network_usage: dict[str, list[str]] = defaultdict(list)

        for c in containers:
            image_usage[c.image_id].append(c.name)
            for mount in c.mounts:
                volume_usage[mount].append(c.name)
            for network_name in c.networks:
                network_usage[network_name].append(c.name)

        for image in images:
            image.used_by.extend(image_usage.get(image.id, []))
        for volume in volumes:
            volume.used_by.extend(volume_usage.get(volume.name, []))
        for network in networks:
            network.used_by.extend(network_usage.get(network.name, []))

        return DockerSnapshot(containers, images, volumes, networks), errors
