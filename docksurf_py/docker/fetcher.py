"""Read-only Docker state fetching, parsed into typed dataclasses."""

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

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


class DockerResourceFetcher:
    """
    Fetches Docker state via the SDK and parses it into typed dataclasses.
    Knows nothing about management commands — read-only.
    """

    def __init__(self, sdk_client) -> None:
        self._client = sdk_client

    def get_containers(self) -> list[Container]:
        containers = []
        for c in self._client.containers.list(all=True):
            attrs = c.attrs

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
            config = attrs.get("Config", {})
            env_vars = config.get("Env", [])
            labels = config.get("Labels") or {}
            image_tags = (
                c.image.tags if c.image and c.image.tags else [attrs.get("Image", "")]
            )

            sdk_state = attrs.get("State", {})
            health_info = sdk_state.get("Health") or {}
            health_log = [
                HealthProbe(
                    start=probe.get("Start", ""),
                    exit_code=probe.get("ExitCode", 0),
                    output=(probe.get("Output") or "").strip(),
                )
                for probe in (health_info.get("Log") or [])
            ]

            containers.append(
                Container(
                    id=c.short_id,
                    name=c.name,
                    image_id=c.image.id if c.image else "",
                    image_name=image_tags[0],
                    status=c.status,
                    state=sdk_state.get("Status", c.status),
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
            )
        return containers

    def get_images(self) -> list[Image]:
        images = []
        for i in self._client.images.list(all=True):
            tags = i.tags if i.tags else ["<none>:<none>"]
            for tag_str in tags:
                repo, _, tag = tag_str.partition(":")
                if not tag:
                    tag = "latest"
                images.append(
                    Image(
                        id=i.id,  # full SHA256 — must match container.image_id format
                        repository=repo,
                        tag=tag,
                        size_bytes=i.attrs.get("Size") or 0,
                        is_dangling=(repo == "<none>" and tag == "<none>"),
                        used_by=[],
                        created=i.attrs.get("Created", ""),
                        architecture=i.attrs.get("Architecture", "unknown"),
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

    def fetch_snapshot(self) -> DockerSnapshot:
        logger.debug("Fetching Docker snapshot")
        with ThreadPoolExecutor(max_workers=4) as pool:
            f_containers = pool.submit(self.get_containers)
            f_images = pool.submit(self.get_images)
            f_volumes = pool.submit(self.get_volumes)
            f_networks = pool.submit(self.get_networks)

        containers = f_containers.result()
        images = f_images.result()
        volumes = f_volumes.result()
        networks = f_networks.result()

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

        return DockerSnapshot(containers, images, volumes, networks)
