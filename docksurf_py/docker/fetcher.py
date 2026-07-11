"""Read-only Docker state fetching, parsed into typed dataclasses."""

import logging
import re
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone

from docksurf_py.models import (
    Container,
    ContainerDetail,
    DockerSnapshot,
    HealthProbe,
    Image,
    Network,
    NetworkEndpoint,
    PortBinding,
    Volume,
)

logger = logging.getLogger(__name__)


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


def _parse_summary_ports(raw_ports: list[dict] | None) -> list[PortBinding]:
    """Parse the `/containers/json` list summary's `Ports` entries.

    Unlike `NetworkSettings.Ports` from a full inspect, the summary is already a
    flat list of bindings, with unpublished ports lacking host-binding fields.
    """
    bindings: list[PortBinding] = []
    for p in raw_ports or []:
        container_port = f"{p.get('PrivatePort', '')}/{p.get('Type', 'tcp')}"
        public_port = p.get("PublicPort")
        if public_port:
            bindings.append(
                PortBinding(
                    container_port=container_port,
                    host_ip=p.get("IP", ""),
                    host_port=str(public_port),
                )
            )
        else:
            bindings.append(PortBinding(container_port=container_port))
    return bindings


def _parse_health_from_status(status: str) -> str:
    """Extract health from the summary `Status` string.

    `/containers/json` omits `State.Health.Status`, instead embedding it as a
    suffix like "(healthy)" or "(health: starting)".
    """
    if "(healthy)" in status:
        return "healthy"
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(health: starting)" in status:
        return "starting"
    return ""


_EXIT_CODE_RE = re.compile(r"^Exited \((-?\d+)\)")


def _parse_exit_code_from_status(status: str) -> int:
    """Parses e.g. "Exited (137) 3 minutes ago" — 0 for anything else
    (including a bare "Dead", which carries no exit code in the summary)."""
    match = _EXIT_CODE_RE.match(status)
    return int(match.group(1)) if match else 0


_UPTIME_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _parse_uptime_hint(status: str, running: bool) -> str:
    """Extract the table's uptime text from the summary `Status` string.

    Unlike `format_uptime`, this uses Docker's human-readable duration because
    `StartedAt` is only available from a full inspect.
    """
    if not running or not status.startswith("Up "):
        return ""
    return _UPTIME_SUFFIX_RE.sub("", status[len("Up ") :]).strip()


def _build_container_from_summary(
    summary: dict, tags_by_image_id: dict[str, list[str]]
) -> Container:
    """Build a `Container` from a `/containers/json` summary.

    Everything needed for the table comes from the summary; detail-pane fields are
    fetched lazily via `DockerClient.container_detail`.
    """
    names = summary.get("Names") or []
    name = names[0].lstrip("/") if names else ""

    image_id = summary.get("ImageID", "")
    image_tags = tags_by_image_id.get(image_id) or [image_id]

    state = summary.get("State", "")
    status = summary.get("Status", "")
    running = state == "running"

    return Container(
        id=summary.get("Id", "")[:12],
        name=name,
        image_id=image_id,
        image_name=image_tags[0],
        status=state,
        state=state,
        running=running,
        exit_code=_parse_exit_code_from_status(status),
        health=_parse_health_from_status(status),
        ports=_parse_summary_ports(summary.get("Ports")),
        mounts=[
            m["Name"]
            for m in (summary.get("Mounts") or [])
            if m.get("Type") == "volume" and m.get("Name")
        ],
        networks=list((summary.get("NetworkSettings") or {}).get("Networks", {})),
        created=_unix_ts_to_iso(summary.get("Created")),
        env=[],
        labels=summary.get("Labels") or {},
        started_at="",
        restart_count=0,
        health_log=[],
        uptime_hint=_parse_uptime_hint(status, running),
    )


def _parse_container_detail(attrs: dict) -> ContainerDetail:
    """Parse the detail-pane-only fields from a full `inspect_container`
    result — used by `DockerClient.container_detail`'s lazy per-selection
    fetch (see `models.ContainerDetail`)."""
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
    config = attrs.get("Config", {}) or {}
    return ContainerDetail(
        env=config.get("Env", []),
        health_log=health_log,
        started_at=sdk_state.get("StartedAt", ""),
        restart_count=attrs.get("RestartCount", 0),
    )


class DockerResourceFetcher:
    """
    Fetches Docker state via the SDK and parses it into typed dataclasses.
    Knows nothing about management commands — read-only.
    """

    def __init__(self, sdk_client) -> None:
        self._client = sdk_client
        # Hoisted here rather than built-and-torn-down inside every
        # fetch_snapshot() call — a fresh ThreadPoolExecutor per refresh means
        # up to 5 threads spun up and joined on every ~0.4s event-driven tick.
        # This fetcher is itself recreated on reconnect/context switch (see
        # DockerClient._connect/switch_context/mark_disconnected), so the
        # pool's lifecycle matches the fetcher's — callers must call close()
        # before discarding a fetcher instance.
        self._pool = ThreadPoolExecutor(max_workers=5)

    def close(self) -> None:
        """Shut down the fetch pool.

        `wait=False` and no `cancel_futures` — a fetch already in flight on
        this pool (e.g. a background refresh racing a daemon disconnect) must
        run to completion rather than raise `CancelledError`, which subclasses
        `BaseException` and would slip past `fetch_snapshot`'s `except
        Exception` handling and kill the worker outright. Idle threads still
        exit once any in-flight work drains.
        """
        self._pool.shutdown(wait=False)

    def get_containers(
        self, image_summaries: list[dict] | None = None
    ) -> list[Container]:
        """Build `Container` rows from `/containers/json` summaries.

        Avoids per-container inspect; detail-pane fields are fetched lazily via
        `DockerClient.container_detail`.
        `image_summaries` lets snapshot refreshes share one `/images/json` listing;
        `None` fetches it on demand.
        """
        summaries = self._client.api.containers(all=True)
        if not summaries:
            return []

        if image_summaries is None:
            image_summaries = self._client.api.images(all=True)
        tags_by_image_id = _image_tags_by_id(image_summaries)

        return [_build_container_from_summary(s, tags_by_image_id) for s in summaries]

    def get_images(self, image_summaries: list[dict] | None = None) -> list[Image]:
        """Build `Image` rows from `/images/json` summaries.

        Avoids per-image inspect; `Architecture` is fetched lazily via
        `DockerClient.image_architecture`.
        `image_summaries` lets snapshot refreshes share one listing with
        `get_containers`; `None` fetches it on demand.
        """
        if image_summaries is None:
            image_summaries = self._client.api.images(all=True)
        images = []
        for s in image_summaries:
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
        # The `/images/json` listing is fetched once here and shared by both
        # get_containers (tag resolution) and get_images, instead of each
        # fetching it separately — halves the image-listing payload per
        # refresh. `image_summary_future` is submitted alongside the other
        # four so nothing serializes ahead of the pool; containers/images just
        # block on its `.result()` (raising through to their own category's
        # error handling below if the listing itself failed).
        pool = self._pool
        image_summary_future = pool.submit(self._client.api.images, all=True)

        def containers_task() -> list[Container]:
            return self.get_containers(image_summary_future.result())

        def images_task() -> list[Image]:
            return self.get_images(image_summary_future.result())

        futures: dict[str, Future] = {
            "containers": pool.submit(containers_task),
            "images": pool.submit(images_task),
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
