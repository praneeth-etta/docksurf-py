"""
docker.py — All system-level Docker execution lives here.
"""

import logging
import os
import queue
import shutil
import subprocess
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Iterator

import docker
import requests.exceptions
from docker.errors import APIError, DockerException, NotFound
from rich.markup import escape

from docksurf_py.connection import (
    ConnectionState,
    ConnectionStatus,
    _classify_docker_error,
    _get_docker_context,
    _get_docker_host,
)
from docksurf_py.constants import LOG_SERVICE_COLORS, SafeMarkup
from docksurf_py.models import (
    CommandErrorKind,
    CommandResult,
    Container,
    ContainerStats,
    DiskUsageEntry,
    DockerSnapshot,
    HealthProbe,
    Image,
    Network,
    PortBinding,
    SystemDf,
    Volume,
)

logger = logging.getLogger(__name__)


def _safe_close(generator) -> None:
    """Close a stream generator, tolerating a cross-thread mid-read close.

    `stop()` runs on the UI thread while the pump thread may be blocked inside
    the generator; CPython raises "generator already executing" in that race.
    The stream's `_active` flag already stops consumption, so closing (which
    just unblocks a pending read) is best-effort.
    """
    if generator is not None and hasattr(generator, "close"):
        try:
            generator.close()
        except Exception:
            pass


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
        _safe_close(self._generator)


def _assign_service_colors(services: list[str]) -> dict[str, str]:
    """Map each distinct service name to a colour, cycling the palette."""
    colors: dict[str, str] = {}
    for service in services:
        if service not in colors:
            colors[service] = LOG_SERVICE_COLORS[len(colors) % len(LOG_SERVICE_COLORS)]
    return colors


class MergedLogStream:
    """Interleaves several containers' logs into one line iterator.

    Each source line is prefixed with a colour-coded service label (emitted as
    `SafeMarkup` so `LogPane` renders the colour instead of escaping it), giving
    the combined stream a `docker compose logs -f` feel. Satisfies the
    `LogSource` structural protocol (`__iter__` + `stop()`) the same way
    `LogStream` does.
    """

    def __init__(self, specs: list[tuple[str, str]], sdk_client) -> None:
        # specs: list of (service_name, container_id)
        self._specs = specs
        self._client = sdk_client
        self._streams = [LogStream(cid, sdk_client) for _, cid in specs]
        self._active = False
        self._queue: queue.Queue = queue.Queue()

    def __iter__(self) -> Iterator[str]:
        if not self._client or not self._specs:
            return

        self._active = True
        logger.info("Merged log stream started for %d containers", len(self._specs))
        colors = _assign_service_colors([service for service, _ in self._specs])
        sentinel = object()

        def pump(service: str, stream: LogStream) -> None:
            color = colors[service]
            try:
                for line in stream:
                    if not self._active:
                        break
                    self._queue.put(
                        SafeMarkup(f"[{color}]{service:>14}[/] │ {escape(line)}")
                    )
            finally:
                self._queue.put(sentinel)

        for (service, _cid), stream in zip(self._specs, self._streams):
            threading.Thread(target=pump, args=(service, stream), daemon=True).start()

        remaining = len(self._specs)
        while remaining > 0:
            item = self._queue.get()
            if item is sentinel:
                remaining -= 1
                continue
            if not self._active:
                break
            yield item
        self.stop()

    def stop(self) -> None:
        self._active = False
        for stream in self._streams:
            stream.stop()


def _parse_stats(sample: dict) -> ContainerStats:
    """Turn one raw SDK stats sample into a typed `ContainerStats`.

    CPU% is derived from the in-sample `cpu_stats`/`precpu_stats` delta the same
    way `docker stats` computes it; memory subtracts the reclaimable page cache
    (`inactive_file`) to match the CLI's used figure.
    """
    cpu = sample.get("cpu_stats") or {}
    precpu = sample.get("precpu_stats") or {}
    cpu_usage = (cpu.get("cpu_usage") or {}).get("total_usage", 0)
    precpu_usage = (precpu.get("cpu_usage") or {}).get("total_usage", 0)
    cpu_delta = cpu_usage - precpu_usage
    system_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
    online = (
        cpu.get("online_cpus")
        or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or [])
        or 1
    )
    cpu_percent = (
        (cpu_delta / system_delta) * online * 100.0
        if system_delta > 0 and cpu_delta > 0
        else 0.0
    )

    mem = sample.get("memory_stats") or {}
    mem_limit = mem.get("limit", 0) or 0
    cache = (mem.get("stats") or {}).get("inactive_file", 0)
    mem_used = max((mem.get("usage", 0) or 0) - cache, 0)
    mem_percent = (mem_used / mem_limit * 100.0) if mem_limit else 0.0

    net_rx = net_tx = 0
    for iface in (sample.get("networks") or {}).values():
        net_rx += iface.get("rx_bytes", 0)
        net_tx += iface.get("tx_bytes", 0)

    blk_read = blk_write = 0
    for entry in (sample.get("blkio_stats") or {}).get(
        "io_service_bytes_recursive"
    ) or []:
        op = (entry.get("op") or "").lower()
        if op == "read":
            blk_read += entry.get("value", 0)
        elif op == "write":
            blk_write += entry.get("value", 0)

    return ContainerStats(
        cpu_percent=cpu_percent,
        mem_used=mem_used,
        mem_limit=mem_limit,
        mem_percent=mem_percent,
        net_rx=net_rx,
        net_tx=net_tx,
        blk_read=blk_read,
        blk_write=blk_write,
    )


class StatsStream:
    """Streams live `ContainerStats` for one container — mirrors `LogStream`.

    Wraps the SDK's `container.stats(stream=True)` generator; `__iter__` yields a
    parsed `ContainerStats` per sample (~1/sec) and `stop()` unblocks it.
    """

    def __init__(self, container_id: str, sdk_client) -> None:
        self._container_id = container_id
        self._client = sdk_client
        self._active = False
        self._generator: Iterator[dict] | None = None

    def __iter__(self) -> Iterator[ContainerStats]:
        if not self._client:
            return
        self._active = True
        logger.info("Stats stream started for container %s", self._container_id)
        try:
            container = self._client.containers.get(self._container_id)
            self._generator = container.stats(stream=True, decode=True)
            for sample in self._generator:
                if not self._active:
                    break
                yield _parse_stats(sample)
        except NotFound:
            logger.warning("Stats stream: container %s not found", self._container_id)
        except Exception as e:
            logger.exception("Stats stream error for %s: %s", self._container_id, e)
        finally:
            self.stop()

    def stop(self) -> None:
        if self._active:
            logger.info("Stats stream stopped for container %s", self._container_id)
        self._active = False
        _safe_close(self._generator)


class EventStream:
    """Streams decoded Docker daemon events — mirrors `LogStream`.

    Filtered to the resource types the UI renders; `__iter__` yields each event
    dict and `stop()` unblocks the (otherwise indefinitely blocking) generator.
    """

    _FILTERS = {"type": ["container", "image", "volume", "network"]}

    def __init__(self, sdk_client) -> None:
        self._client = sdk_client
        self._active = False
        self._generator: Iterator[dict] | None = None

    def __iter__(self) -> Iterator[dict]:
        if not self._client:
            return
        self._active = True
        logger.info("Event stream started")
        try:
            self._generator = self._client.events(decode=True, filters=self._FILTERS)
            for event in self._generator:
                if not self._active:
                    break
                yield event
        except Exception as e:
            logger.exception("Event stream error: %s", e)
        finally:
            self.stop()

    def stop(self) -> None:
        if self._active:
            logger.info("Event stream stopped")
        self._active = False
        _safe_close(self._generator)


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


def _parse_docker_ts(ts: str) -> datetime | None:
    """Parse a Docker RFC3339 timestamp to an aware datetime, or None."""
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
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_relative_time(ts: str) -> str:
    """Convert a Docker timestamp string to a human-readable relative age."""
    if not ts:
        return "Unknown"
    dt = _parse_docker_ts(ts)
    if dt is None:
        return ts
    diff = int((datetime.now(timezone.utc) - dt).total_seconds())
    return _format_age(diff)


def format_uptime(started_at: str) -> str:
    """How long a container has been running, e.g. "3h" — "—" if not started.

    Docker reports `StartedAt` as the zero time ("0001-01-01T…") for containers
    that have never run.
    """
    if not started_at or started_at.startswith("0001"):
        return "—"
    dt = _parse_docker_ts(started_at)
    if dt is None:
        return "—"
    diff = int((datetime.now(timezone.utc) - dt).total_seconds())
    return _format_age(diff).removesuffix(" ago")


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


def _parse_system_df(raw: dict) -> SystemDf:
    """Parse the raw `/system/df` payload into typed per-category entries.

    Reclaimable figures are approximate (image sizes include shared layers, so
    unused-image reclaimable can be slightly overstated) — enough to answer
    "what can I prune?" at a glance, matching `docker system df` closely.
    """
    entries: list[DiskUsageEntry] = []

    images = raw.get("Images") or []
    entries.append(
        DiskUsageEntry(
            kind="Images",
            total_count=len(images),
            active_count=sum(1 for i in images if i.get("Containers", 0)),
            size_bytes=sum(i.get("Size", 0) or 0 for i in images),
            reclaimable_bytes=sum(
                (i.get("Size", 0) or 0) for i in images if not i.get("Containers", 0)
            ),
        )
    )

    containers = raw.get("Containers") or []

    def _running(c: dict) -> bool:
        return (c.get("State") or "").lower() == "running"

    entries.append(
        DiskUsageEntry(
            kind="Containers",
            total_count=len(containers),
            active_count=sum(1 for c in containers if _running(c)),
            size_bytes=sum(c.get("SizeRw", 0) or 0 for c in containers),
            reclaimable_bytes=sum(
                (c.get("SizeRw", 0) or 0) for c in containers if not _running(c)
            ),
        )
    )

    volumes = raw.get("Volumes") or []

    def _vsize(v: dict) -> int:
        return (v.get("UsageData") or {}).get("Size", 0) or 0

    def _vactive(v: dict) -> bool:
        return ((v.get("UsageData") or {}).get("RefCount", 0) or 0) > 0

    entries.append(
        DiskUsageEntry(
            kind="Local Volumes",
            total_count=len(volumes),
            active_count=sum(1 for v in volumes if _vactive(v)),
            size_bytes=sum(_vsize(v) for v in volumes),
            reclaimable_bytes=sum(_vsize(v) for v in volumes if not _vactive(v)),
        )
    )

    cache = raw.get("BuildCache") or []
    entries.append(
        DiskUsageEntry(
            kind="Build Cache",
            total_count=len(cache),
            active_count=sum(1 for b in cache if b.get("InUse")),
            size_bytes=sum(b.get("Size", 0) or 0 for b in cache),
            reclaimable_bytes=sum(
                (b.get("Size", 0) or 0) for b in cache if not b.get("InUse")
            ),
        )
    )

    return SystemDf(
        entries=entries,
        total_size=sum(e.size_bytes for e in entries),
        total_reclaimable=sum(e.reclaimable_bytes for e in entries),
    )


_DEFAULT_DOCKER_SOCK = "unix:///var/run/docker.sock"


def _create_sdk_client() -> "docker.DockerClient":
    """Create the SDK client, honoring the active `docker context`.

    `docker.from_env()` only reads `DOCKER_HOST` (falling back to the default
    socket) — it ignores `docker context` entirely. Without this, DockSurf would
    silently talk to a *different daemon* than the user's `docker`/`docker
    compose` CLI whenever a non-default context is active (Docker Desktop
    alongside native docker, colima, a remote context, …), so its resource list
    wouldn't match theirs. Precedence matches the CLI: `DOCKER_HOST` >
    active context > default socket. The default-socket case still goes through
    `from_env()`, so existing setups (and its TLS-env handling) are unchanged.
    """
    if os.environ.get("DOCKER_HOST"):
        return docker.from_env()
    try:
        from docker.context import ContextAPI

        ctx = ContextAPI.get_current_context()
    except Exception:
        ctx = None
    if ctx is not None and ctx.Host and ctx.Host != _DEFAULT_DOCKER_SOCK:
        logger.info("Connecting via docker context %s → %s", ctx.Name, ctx.Host)
        kwargs: dict = {"base_url": ctx.Host, "tls": ctx.TLSConfig or False}
        if ctx.Host.startswith("ssh://"):
            kwargs["use_ssh_client"] = True
        return docker.DockerClient(**kwargs)
    return docker.from_env()


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
            sdk = _create_sdk_client()
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

    def stream_project_logs(self, specs: list[tuple[str, str]]) -> MergedLogStream:
        """Build a merged log stream over a project's (service, id) pairs."""
        return MergedLogStream(specs, self._sdk)

    def stream_stats(self, container_id: str) -> StatsStream:
        """Live resource-usage stream for one container."""
        return StatsStream(container_id, self._sdk)

    def stream_events(self) -> EventStream:
        """Live stream of Docker daemon events (for auto-refresh)."""
        return EventStream(self._sdk)

    def system_df(self) -> SystemDf:
        """Disk usage per resource category (`docker system df`).

        Slow on the daemon side (it sums layer/volume sizes), so callers should
        run this off the UI thread and never on the snapshot path.
        """
        if not self._sdk:
            return SystemDf(entries=[], total_size=0, total_reclaimable=0)
        raw = self._sdk.df()
        return _parse_system_df(raw)

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

    def compose_action(
        self,
        project: str,
        verb: str,
        config_files: str = "",
        working_dir: str = "",
    ) -> CommandResult:
        """Run `docker compose <verb>` for a whole project.

        Sanctioned subprocess exception (like exec-shell): docker-py has no
        Compose support, and `up` in particular is impossible via the SDK since
        it must recreate containers from the compose file. `up` therefore passes
        the project's `-f` config files (from labels) and runs from its
        working dir; `down`/`stop`/`start`/`restart` operate on the live project
        by `-p` name alone.
        """
        if shutil.which("docker") is None:
            logger.error("Compose %s aborted — docker CLI not found on PATH", verb)
            return CommandResult.failure(
                "docker CLI not found on PATH — cannot manage Compose projects",
                kind=CommandErrorKind.DAEMON_UNREACHABLE,
            )

        cwd: str | None = None
        if verb == "up":
            cmd = ["docker", "compose"]
            for config_file in filter(None, config_files.split(",")):
                cmd += ["-f", config_file.strip()]
            cmd += ["-p", project, "up", "-d"]
            cwd = working_dir or None
        else:
            cmd = ["docker", "compose", "-p", project, verb]

        logger.info("Running: %s (cwd=%s)", " ".join(cmd), cwd)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        except Exception as e:
            logger.exception("Compose %s failed for %s", verb, project)
            return CommandResult.failure(
                str(e), kind=CommandErrorKind.DAEMON_UNREACHABLE
            )

        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "").strip()
            msg = msg or f"docker compose {verb} exited {result.returncode}"
            logger.warning("Compose %s failed for %s: %s", verb, project, msg)
            return CommandResult.failure(msg, kind=CommandErrorKind.UNKNOWN)

        return CommandResult.success(f"Compose {verb} succeeded for {project}")

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
