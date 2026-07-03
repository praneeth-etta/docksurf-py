"""
docker.py — All system-level Docker execution lives here.
"""

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Iterator

import docker
import requests.exceptions
from docker.errors import APIError, DockerException, NotFound

from docksurf_py.connection import (
    ConnectionState,
    ConnectionStatus,
    _classify_docker_error,
    _get_docker_context,
    _get_docker_host,
)
from docksurf_py.models import (
    CommandErrorKind,
    CommandResult,
    Container,
    DockerSnapshot,
    Image,
    Network,
    PortBinding,
    Volume,
)

logger = logging.getLogger(__name__)


class LogStream:
    """Wraps docker SDK log generator and exposes it as a line iterator."""

    def __init__(self, container_id: str, sdk_client) -> None:
        self._container_id = container_id
        self._client = sdk_client
        self._active = False
        self._generator: Iterator[bytes] | None = None

    def __iter__(self) -> Iterator[str]:
        if not self._client:
            return

        self._active = True
        logger.info("Log stream started for container %s", self._container_id)
        try:
            container = self._client.containers.get(self._container_id)
            follow = container.status == "running"
            self._generator = container.logs(stream=True, follow=follow, tail=500)

            for raw_line in self._generator:
                if not self._active:
                    break
                yield raw_line.decode("utf-8", errors="replace").rstrip()
        except NotFound:
            logger.warning("Log stream: container %s not found", self._container_id)
            yield f"Container {self._container_id} not found"
        except Exception as e:
            logger.exception("Log stream error for %s: %s", self._container_id, e)
            yield f"Log stream error: {e}"
        finally:
            self.stop()

    def stop(self) -> None:
        if self._active:
            logger.info("Log stream stopped for container %s", self._container_id)
        self._active = False
        if self._generator and hasattr(self._generator, "close"):
            self._generator.close()


_AGE_UNITS = (
    (60, 1, "s"),
    (3600, 60, "m"),
    (86400, 3600, "h"),
    (86400 * 30, 86400, "d"),
    (86400 * 365, 86400 * 30, "mo"),
)


def _format_age(diff: int) -> str:
    """Format a second delta as a short relative-age string."""
    if diff < 0:
        return "just now"
    for threshold, unit, suffix in _AGE_UNITS:
        if diff < threshold:
            return f"{diff // unit}{suffix} ago"
    return f"{diff // (86400 * 365)}y ago"


def format_relative_time(ts: str) -> str:
    """Convert a Docker timestamp string to a human-readable relative age."""
    if not ts:
        return "Unknown"

    ts_clean = ts
    dot = ts_clean.find(".")
    if dot != -1:
        end = dot + 1
        while end < len(ts_clean) and ts_clean[end].isdigit():
            end += 1
        fraction = ts_clean[dot + 1 : end]
        if len(fraction) > 6:
            ts_clean = ts_clean[: dot + 1] + fraction[:6] + ts_clean[end:]

    if ts_clean.endswith("Z"):
        ts_clean = ts_clean[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(ts_clean)
    except ValueError:
        return ts

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    diff = int((datetime.now(timezone.utc) - dt).total_seconds())
    return _format_age(diff)


def format_size(size_in_bytes: int | None) -> str:
    if not size_in_bytes:
        return "0B"
    size: float = size_in_bytes
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f}{unit}"
        size /= 1024.0
    return f"{size:.2f}PB"


def format_ports(ports: list[PortBinding]) -> str:
    parts = []
    for p in ports:
        if p.host_port:
            prefix = f"{p.host_ip}:" if p.host_ip else ""
            parts.append(f"{prefix}{p.host_port}->{p.container_port}")
        else:
            parts.append(p.container_port)
    return ", ".join(parts)


def format_labels(labels: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in labels.items())


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
            env_vars = attrs.get("Config", {}).get("Env", [])
            image_tags = (
                c.image.tags if c.image and c.image.tags else [attrs.get("Image", "")]
            )

            sdk_state = attrs.get("State", {})
            health_info = sdk_state.get("Health") or {}

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
        for n in self._client.networks.list():
            ipam_config = n.attrs.get("IPAM", {}).get("Config", [])
            subnet = gateway = "N/A"
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


class DockerClient:
    """
    The only Docker-facing object the rest of the app should import.

    Owns the SDK client lifecycle, delegates reads to DockerResourceFetcher,
    and handles all write/management commands directly.
    """

    def __init__(self) -> None:
        self._sdk: docker.DockerClient | None = None
        self._fetcher: DockerResourceFetcher | None = None
        self.connection: ConnectionState = ConnectionState(
            status=ConnectionStatus.NOT_CONNECTED,
            message="Not yet connected",
            hint="",
            context=_get_docker_context(),
            host=_get_docker_host(),
        )

    def ensure_connected(self) -> ConnectionState:
        """(Re)attempt to connect if not already connected.

        Called lazily from the read/write methods below instead of blocking
        the constructor on the daemon round-trip. Safe to call on every
        operation — once connected it's a no-op, and while disconnected it
        naturally retries on the next call, which is what makes reconnecting
        after the daemon comes back up "just work".
        """
        if not self.is_connected:
            self.connection = self._connect()
        return self.connection

    def _connect(self) -> ConnectionState:
        context = _get_docker_context()
        host = _get_docker_host()
        try:
            sdk = docker.from_env()
            sdk.ping()  # real round-trip — surfaces connection errors at init time
            self._sdk = sdk
            self._fetcher = DockerResourceFetcher(sdk)
            logger.info("Connected to Docker — context=%s host=%s", context, host)
            return ConnectionState(
                status=ConnectionStatus.CONNECTED,
                message="Connected",
                hint="",
                context=context,
                host=host,
            )
        except DockerException as e:
            logger.exception("Docker connection failed: %s", e)
            return _classify_docker_error(e)
        except FileNotFoundError as e:
            logger.exception("Docker executable not found: %s", e)
            return ConnectionState(
                status=ConnectionStatus.NOT_INSTALLED,
                message="Docker is not installed on PATH",
                hint="Install docker: https://docs.docker.com/get-docker/",
                context=context,
                host=host,
            )
        except Exception as e:
            logger.exception("Unexpected error connecting to Docker: %s", e)
            return ConnectionState(
                status=ConnectionStatus.API_ERROR,
                message="Unexpected error connecting to Docker",
                hint="Install Docker: https://docs.docker.com/get-docker/",
                context=context,
                host=host,
            )

    @property
    def is_connected(self) -> bool:
        return self.connection.status == ConnectionStatus.CONNECTED

    # --- Reads ---

    def fetch_snapshot(self) -> DockerSnapshot:
        self.ensure_connected()
        if not self._fetcher:
            return DockerSnapshot([], [], [], [])
        return self._fetcher.fetch_snapshot()

    def fetch_logs(self, container_id: str, tail: int = 500) -> str:
        if not self._sdk:
            return "Docker client not initialized"
        try:
            container = self._sdk.containers.get(container_id)
            logs = container.logs(tail=tail, stdout=True, stderr=True)
            return logs.decode("utf-8", errors="replace").strip()
        except Exception as e:
            return f"Error fetching logs: {e}"

    def stream_logs(self, container_id: str) -> LogStream:
        return LogStream(container_id, self._sdk)

    # --- Writes ---

    def stop_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).stop(), "OK"
        )

    def start_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).start(), "OK"
        )

    def restart_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).restart(), "OK"
        )

    def remove_container(self, container_id: str, force: bool = False) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).remove(force=force), "OK"
        )

    def remove_image(self, image_id: str, force: bool = False) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.images.remove(image=image_id, force=force), "OK"
        )

    def remove_volume(self, volume_name: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.volumes.get(volume_name).remove(), "OK"
        )

    def remove_network(self, network_name: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.networks.get(network_name).remove(), "OK"
        )

    # --- Internal ---

    def _run_management_command(
        self,
        action_callable: Callable[["docker.DockerClient"], None],
        success_msg: str,
    ) -> CommandResult:
        sdk = self._sdk
        if not sdk:
            logger.error("Management command skipped — Docker client not initialized")
            return CommandResult.failure(
                "Docker client not initialized",
                kind=CommandErrorKind.DAEMON_UNREACHABLE,
            )
        try:
            action_callable(sdk)
            logger.debug("Management command succeeded")
            return CommandResult.success(success_msg)
        except NotFound as e:
            logger.warning("Docker resource not found: %s", e)
            return CommandResult.failure(str(e), kind=CommandErrorKind.NOT_FOUND)
        except APIError as e:
            kind = (
                CommandErrorKind.IN_USE
                if e.status_code == 409
                else CommandErrorKind.UNKNOWN
            )
            logger.warning("Docker API error: %s", e)
            return CommandResult.failure(str(e), kind=kind)
        except (DockerException, requests.exceptions.RequestException) as e:
            logger.warning("Docker daemon unreachable: %s", e)
            return CommandResult.failure(
                str(e), kind=CommandErrorKind.DAEMON_UNREACHABLE
            )


if __name__ == "__main__":
    dc = DockerClient()
    snapshot = dc.fetch_snapshot()  # triggers the lazy connect
    if dc.is_connected:
        print(snapshot)
    else:
        print("Could not connect to Docker daemon")
