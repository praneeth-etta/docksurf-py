"""
docker.py — All system-level Docker execution lives here.
"""

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, TypeAlias

import docker
from docker.errors import APIError, DockerException, NotFound

logger = logging.getLogger(__name__)

CommandResult: TypeAlias = tuple[bool, str]

try:
    client = docker.from_env()
except DockerException as e:
    logger.exception(f"Failed to connect to Docker Daemon: {e}")
    client = None


@dataclass(slots=True)
class Container:
    id: str
    name: str
    image_id: str
    image_name: str
    status: str
    ports: str
    mounts: list[str]
    networks: list[str]
    created: str
    env: list[str]


@dataclass(slots=True)
class Image:
    id: str
    repository: str
    tag: str
    size: str
    is_dangling: bool
    used_by: list[str]
    created: str
    architecture: str


@dataclass(slots=True)
class Volume:
    name: str
    driver: str
    mountpoint: str
    used_by: list[str]
    labels: str


@dataclass(slots=True)
class Network:
    id: str
    name: str
    driver: str
    subnet: str
    gateway: str
    scope: str
    used_by: list[str]


@dataclass(slots=True)
class DockerSnapshot:
    containers: list[Container]
    images: list[Image]
    volumes: list[Volume]
    networks: list[Network]


class LogStream:
    """Wraps docker SDK log generator"""

    def __init__(self, container_id: str) -> None:
        self._container_id = container_id
        self._active = False
        self._generator = None

    def __iter__(self) -> Iterator[str]:
        if not client:
            return

        self._active = True
        try:
            container = client.containers.get(self._container_id)
            self._generator = container.logs(stream=True, follow=True, tail=500)

            for raw_line in self._generator:
                if not self._active:
                    break
                yield raw_line.decode("utf-8", errors="replace").rstrip()
        except NotFound:
            yield f"Container {self._container_id} not found"
        except Exception as e:
            yield f"Log stream error: {e}"
        finally:
            self.stop()

    def stop(self) -> None:
        self._active = False
        if self._generator and hasattr(self._generator, "close"):
            self._generator.close()


def format_relative_time(ts: str) -> str:
    """Convert a Docker timestamp string to a human-readable relative age."""
    if not ts:
        return "Unknown"

    # The SDK usually returns standard ISO formats
    ts_clean = ts.split(".")[0] if "." in ts else ts
    ts_clean = ts_clean.replace("Z", "")

    try:
        dt = datetime.fromisoformat(ts_clean)
    except ValueError:
        return ts

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    diff = int((datetime.now(timezone.utc) - dt).total_seconds())
    if diff < 0:
        return "just now"
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    if diff < 86400 * 30:
        return f"{diff // 86400}d ago"
    if diff < 86400 * 365:
        return f"{diff // (86400 * 30)}mo ago"
    return f"{diff // (86400 * 365)}y ago"


def format_size(size_in_bytes: int | None) -> str:
    if not size_in_bytes:
        return "0B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_in_bytes < 1024.0:
            return f"{size_in_bytes: .2f}{unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes: .2f}PB"


def get_containers() -> list[Container]:
    if not client:
        return []

    containers = []
    for c in client.containers.list(all=True):
        attrs = c.attrs

        ports_list = []
        port_bindings = attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        for port, bindings in port_bindings.items():
            if bindings:
                for binding in bindings:
                    host_ip = binding.get("HostIp", "")
                    host_port = binding.get("HostPort", "")
                    prefix = f"{host_ip}:" if host_ip else ""
                    ports_list.append(f"{prefix}{host_port}->{port}")
            else:
                ports_list.append(port)

        mounts = [
            m.get("Name") or m.get("Source", "")
            for m in attrs.get("Mounts", [])
            if m.get("Name") or m.get("Source")
        ]

        networks = list(attrs.get("NetworkSettings", {}).get("Networks", {}).keys())
        env_vars = attrs.get("Config", {}).get("Env", [])
        image_tags = (
            c.image.tags if c.image and c.image.tags else [attrs.get("Image", "")]
        )

        containers.append(
            Container(
                id=c.short_id,
                name=c.name,
                image_id=c.image.id if c.image else "",
                image_name=image_tags[0],
                status=c.status,
                ports=", ".join(ports_list),
                mounts=mounts,
                networks=networks,
                created=attrs.get("Created", ""),
                env=env_vars,
            )
        )
    return containers


def get_images() -> list[Image]:
    if not client:
        return []

    images = []
    for i in client.images.list(all=True):
        tags = i.tags if i.tags else ["<none>:<none>"]

        for tag_str in tags:
            repo, _, tag = tag_str.partition(":")
            if not tag:
                tag = "latest"

            images.append(
                Image(
                    id=i.short_id,
                    repository=repo,
                    tag=tag,
                    size=format_size(i.attrs.get("Size")),
                    is_dangling=(repo == "<none>" and tag == "<none>"),
                    used_by=[],
                    created=i.attrs.get("Created", ""),
                    architecture=i.attrs.get("Architecture", "unknown"),
                )
            )
    return images


def get_volumes() -> list[Volume]:
    if not client:
        return []

    volumes = []
    for v in client.volumes.list():
        labels = v.attrs.get("Labels", {})
        label_str = ", ".join(f"{k}={v}" for k, v in labels.items()) if labels else ""

        volumes.append(
            Volume(
                name=v.name,
                driver=v.attrs.get("Driver", ""),
                mountpoint=v.attrs.get("Mountpoint", ""),
                used_by=[],
                labels=label_str,
            )
        )
    return volumes


def get_networks() -> list[Network]:
    if not client:
        return []

    networks = []
    for n in client.networks.list():
        ipam_config = n.attrs.get("IPAM", {}).get("Config", [])
        subnet = "N/A"
        gateway = "N/A"

        if ipam_config and isinstance(ipam_config, list) and len(ipam_config) > 0:
            subnet = ipam_config[0].get("Subnet", "N/A")
            gateway = ipam_config[0].get("Gateway", "N/A")

        networks.append(
            Network(
                id=n.short_id,
                name=n.name,
                driver=n.attrs.get("Driver", ""),
                subnet=subnet,
                gateway=gateway,
                scope=n.attrs.get("Scope", ""),
                used_by=[],
            )
        )
    return networks


def fetch_snapshot() -> DockerSnapshot:
    if not client:
        return DockerSnapshot([], [], [], [])

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_containers = pool.submit(get_containers)
        f_images = pool.submit(get_images)
        f_volumes = pool.submit(get_volumes)
        f_networks = pool.submit(get_networks)

    containers, images, volumes, networks = (
        f_containers.result(),
        f_images.result(),
        f_volumes.result(),
        f_networks.result(),
    )

    image_usage = defaultdict(list)
    volume_usage = defaultdict(list)
    network_usage = defaultdict(list)

    for c in containers:
        image_usage[c.image_id].append(c.name)
        for mount in c.mounts:
            volume_usage[mount].append(c.name)
        for network in c.networks:
            network_usage[network].append(c.name)

    for image in images:
        image.used_by.extend(image_usage.get(image.id, []))
    for volume in volumes:
        volume.used_by.extend(volume_usage.get(volume.name, []))
    for network in networks:
        network.used_by.extend(network_usage.get(network.name, []))

    return DockerSnapshot(containers, images, volumes, networks)


# Management commands
def _run_management_command(action_callable, success_msg: str) -> CommandResult:
    """Run a docker management command; return (success, message)."""
    if not client:
        return False, "Docker client not initialized"
    try:
        action_callable()
        return True, success_msg
    except APIError as e:
        return False, str(e)


def stop_container(container_id: str) -> CommandResult:
    return _run_management_command(
        lambda: client.containers.get(container_id).stop(), "OK"
    )


def start_container(container_id: str) -> CommandResult:
    return _run_management_command(
        lambda: client.containers.get(container_id).start(), "OK"
    )


def restart_container(container_id: str) -> CommandResult:
    return _run_management_command(
        lambda: client.containers.get(container_id).restart(), "OK"
    )


def remove_container(container_id: str, force: bool = False) -> CommandResult:
    return _run_management_command(
        lambda: client.containers.get(container_id).remove(force=force), "OK"
    )


def remove_image(image_id: str, force: bool = False) -> CommandResult:
    return _run_management_command(
        lambda: client.images.remove(image=image_id, force=force), "OK"
    )


def remove_volume(volume_name: str) -> CommandResult:
    return _run_management_command(
        lambda: client.volumes.get(volume_name).remove(), "OK"
    )


def remove_network(network_name: str) -> CommandResult:
    return _run_management_command(
        lambda: client.networks.get(network_name).remove(), "OK"
    )


def fetch_logs(container_id: str, tail: int = 500) -> str:
    """Fetch recent log lines for a container."""
    if not client:
        return "Docker client not initialized"
    try:
        container = client.containers.get(container_id)
        logs = container.logs(tail=tail, stdout=True, stderr=True)
        return logs.decode("utf-8", errors="replace").strip()
    except Exception as e:
        return f"Error fetching logs: {e}"


if __name__ == "__main__":
    print(fetch_snapshot())
