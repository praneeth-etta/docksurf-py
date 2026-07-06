"""
docker.py — All system-level Docker execution lives here.
"""

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import docker
import requests.exceptions
from docker.errors import APIError, DockerException, NotFound
from docker.types import IPAMConfig, IPAMPool

from docksurf_py.connection import (
    ConnectionState,
    ConnectionStatus,
    _classify_docker_error,
    _get_docker_context,
    _get_docker_host,
)
from docksurf_py.constants import LOG_SERVICE_COLORS, LogLine, LogOptions
from docksurf_py.models import (
    CommandErrorKind,
    CommandResult,
    Container,
    ContainerStats,
    ContainerTop,
    ContextInfo,
    DiskUsageEntry,
    DockerSnapshot,
    HealthProbe,
    Image,
    ImageLayer,
    Network,
    NetworkEndpoint,
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


def _split_timestamp(raw: str) -> tuple[str, str]:
    """Split a docker `timestamps=True` log line into (timestamp, message).

    Docker prefixes each line with an RFC3339 timestamp and a single space,
    e.g. `2024-01-01T12:00:00.000000000Z hello`. The timestamp is stored
    separately so the view can show/hide it without a re-fetch and so the
    search filter matches on the message only. Lines without a recognisable
    timestamp (our own error strings) come back as `("", raw)`.
    """
    head, sep, rest = raw.partition(" ")
    looks_like_ts = "T" in head and (
        head.endswith("Z") or "+" in head or head[-6:-5] in "+-"
    )
    if sep and looks_like_ts:
        return head, rest
    return "", raw


class LogStream:
    """Wraps docker SDK log generator and exposes it as a `LogLine` iterator."""

    def __init__(
        self, container_id: str, sdk_client, options: LogOptions | None = None
    ) -> None:
        self._container_id = container_id
        self._client = sdk_client
        self._options = options or LogOptions()
        self._active = False
        self._generator: Iterator | None = None

    def _logs_kwargs(self, follow: bool) -> dict:
        # docker-py 7.x `logs()` has no `demux`, so stdout/stderr come back
        # combined (both default True) — there's no per-line origin to recover.
        # We request timestamps and honour the tail/since options.
        kwargs: dict = {
            "stream": True,
            "follow": follow,
            "timestamps": True,
            "tail": self._options.tail if self._options.tail is not None else "all",
        }
        if self._options.since_seconds > 0:
            kwargs["since"] = int(time.time()) - self._options.since_seconds
        return kwargs

    def __iter__(self) -> Iterator[LogLine]:
        if not self._client:
            return

        self._active = True
        logger.info("Log stream started for container %s", self._container_id)
        try:
            container = self._client.containers.get(self._container_id)
            follow = container.status == "running"
            self._generator = container.logs(**self._logs_kwargs(follow))

            for raw_line in self._generator:
                if not self._active:
                    break
                ts, text = _split_timestamp(
                    raw_line.decode("utf-8", errors="replace").rstrip()
                )
                yield LogLine(text=text, ts=ts)
        except NotFound:
            logger.warning("Log stream: container %s not found", self._container_id)
            yield LogLine(
                text=f"Container {self._container_id} not found", stream="stderr"
            )
        except Exception as e:
            logger.exception("Log stream error for %s: %s", self._container_id, e)
            yield LogLine(text=f"Log stream error: {e}", stream="stderr")
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
    """Interleaves several containers' logs into one `LogLine` iterator.

    Each child line is tagged with its service label and a cycled colour, so
    `LogPane` can render a `docker compose logs -f`-style colour-coded prefix.
    Presentation lives in the view — this class only sets `service`/`color` on
    the child `LogLine`s. Satisfies the `LogSource` structural protocol
    (`__iter__` + `stop()`) the same way `LogStream` does.
    """

    def __init__(
        self,
        specs: list[tuple[str, str]],
        sdk_client,
        options: LogOptions | None = None,
    ) -> None:
        # specs: list of (service_name, container_id)
        self._specs = specs
        self._client = sdk_client
        self._streams = [LogStream(cid, sdk_client, options) for _, cid in specs]
        self._active = False
        self._queue: queue.Queue = queue.Queue()

    def __iter__(self) -> Iterator[LogLine]:
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
                    self._queue.put(replace(line, service=service, color=color))
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
        # Populated if iteration ends because of an unexpected error rather than
        # a deliberate stop(), since this iterator never propagates exceptions.
        self.error: Exception | None = None

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
            self.error = e
        finally:
            self.stop()

    def stop(self) -> None:
        if self._active:
            logger.info("Event stream stopped")
        self._active = False
        _safe_close(self._generator)


class PullStream:
    """Streams `docker pull` progress for one image — mirrors `EventStream`.

    Wraps the low-level `api.pull(stream=True, decode=True)` generator; `__iter__`
    yields each raw progress dict (`{"status", "id", "progress", "error", ...}`)
    and `stop()` unblocks it. Consumers format the dicts (kept untyped like
    `EventStream`, since the shape is display-only). A pull that fails mid-stream
    yields a dict carrying an `"error"` key rather than raising.
    """

    def __init__(self, repository: str, tag: str, sdk_client) -> None:
        self._repository = repository
        self._tag = tag
        self._client = sdk_client
        self._active = False
        self._generator: Iterator[dict] | None = None

    def __iter__(self) -> Iterator[dict]:
        if not self._client:
            return
        self._active = True
        ref = f"{self._repository}:{self._tag}"
        logger.info("Pull stream started for %s", ref)
        try:
            self._generator = self._client.api.pull(
                self._repository, tag=self._tag, stream=True, decode=True
            )
            for chunk in self._generator:
                if not self._active:
                    break
                yield chunk
        except (APIError, DockerException, requests.exceptions.RequestException) as e:
            logger.warning("Pull stream error for %s: %s", ref, e)
            yield {"error": str(e)}
        except Exception as e:  # noqa: BLE001 - surface any unexpected failure
            logger.exception("Pull stream error for %s: %s", ref, e)
            yield {"error": str(e)}
        finally:
            self.stop()

    def stop(self) -> None:
        if self._active:
            logger.info("Pull stream stopped for %s:%s", self._repository, self._tag)
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

# Persist the context selected in DockSurf so it survives restarts.
# Keep this separate from ~/.docker/config.json, whose current-context is
# shared with the Docker CLI and other terminals.
_STATE_FILE = Path.home() / ".local/share/docksurf-py/state.json"


def _load_last_context() -> str | None:
    try:
        data = json.loads(_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("context")
    return name if isinstance(name, str) and name else None


def _save_last_context(name: str) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps({"context": name}))
    except OSError as e:
        logger.warning("Could not persist context selection: %s", e)


def _clear_last_context() -> None:
    try:
        _STATE_FILE.unlink()
    except OSError:
        pass


def _build_sdk_client_for_context(ctx) -> "docker.DockerClient":
    """Build an SDK client scoped to one `docker context` entry."""
    if not ctx.Host or ctx.Host == _DEFAULT_DOCKER_SOCK:
        return docker.from_env()
    kwargs: dict = {"base_url": ctx.Host, "tls": ctx.TLSConfig or False}
    if ctx.Host.startswith("ssh://"):
        kwargs["use_ssh_client"] = True
    return docker.DockerClient(**kwargs)


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
        return _build_sdk_client_for_context(ctx)
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


class DockerClient:
    """
    The only Docker-facing object the rest of the app should import.

    Owns the SDK client lifecycle, delegates reads to DockerResourceFetcher,
    and handles all write/management commands directly.
    """

    def __init__(self) -> None:
        self._sdk: docker.DockerClient | None = None
        self._fetcher: DockerResourceFetcher | None = None
        # Context picked via the in-app switcher (see `switch_context`),
        # persisted across restarts, takes precedence over the ambient
        # `docker context` on every (re)connect while set.
        self._context_override: str | None = _load_last_context()
        self.connection: ConnectionState = ConnectionState(
            status=ConnectionStatus.NOT_CONNECTED,
            message="Not yet connected",
            hint="",
            context=self._context_override or _get_docker_context(),
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
        ctx = None
        if self._context_override:
            try:
                from docker.context import ContextAPI

                ctx = ContextAPI.get_context(self._context_override)
            except Exception:
                ctx = None
            if ctx is None:
                logger.warning(
                    "Saved context %r no longer exists — using ambient default",
                    self._context_override,
                )
                self._context_override = None
                _clear_last_context()

        context = ctx.Name if ctx is not None else _get_docker_context()
        host = (
            (ctx.Host or _DEFAULT_DOCKER_SOCK)
            if ctx is not None
            else _get_docker_host()
        )
        try:
            sdk = (
                _build_sdk_client_for_context(ctx)
                if ctx is not None
                else _create_sdk_client()
            )
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
            return replace(_classify_docker_error(e), context=context, host=host)
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

    def mark_disconnected(self, exc: Exception) -> None:
        """Flip connection state to disconnected after a live call fails.

        `ensure_connected()` only reattempts `_connect()` while
        `not self.is_connected` — without this, a daemon that dies mid-session
        would leave `self.connection` stuck at `CONNECTED` forever (nothing
        else ever un-sets it), so every later `ensure_connected()` call would
        no-op and the app would never reconnect. Resetting `_sdk`/`_fetcher`
        also ensures the *next* connect attempt builds a fresh client rather
        than reusing a possibly-stale one. No-ops if already disconnected.
        """
        if not self.is_connected:
            return
        self._sdk = None
        self._fetcher = None
        classified = _classify_docker_error(exc)
        # Preserve the context/host we were actually using — `_classify_docker_error`
        # recomputes them from the ambient environment, which would be wrong
        # once an in-app context override (see switch_context) is active.
        self.connection = replace(
            classified, context=self.connection.context, host=self.connection.host
        )
        logger.warning("Docker connection lost: %s", exc)

    # --- Reads ---

    def fetch_snapshot(self) -> DockerSnapshot:
        self.ensure_connected()
        if not self._fetcher:
            return DockerSnapshot([], [], [], [])
        try:
            return self._fetcher.fetch_snapshot()
        except (DockerException, requests.exceptions.RequestException) as e:
            self.mark_disconnected(e)
            return DockerSnapshot([], [], [], [])

    def fetch_logs(self, container_id: str, tail: int = 500) -> str:
        if not self._sdk:
            return "Docker client not initialized"
        try:
            container = self._sdk.containers.get(container_id)
            logs = container.logs(tail=tail, stdout=True, stderr=True)
            return logs.decode("utf-8", errors="replace").strip()
        except Exception as e:
            return f"Error fetching logs: {e}"

    def stream_logs(
        self, container_id: str, options: LogOptions | None = None
    ) -> LogStream:
        return LogStream(container_id, self._sdk, options)

    def stream_project_logs(
        self, specs: list[tuple[str, str]], options: LogOptions | None = None
    ) -> MergedLogStream:
        """Build a merged log stream over a project's (service, id) pairs."""
        return MergedLogStream(specs, self._sdk, options)

    def stream_stats(self, container_id: str) -> StatsStream:
        """Live resource-usage stream for one container."""
        return StatsStream(container_id, self._sdk)

    def stream_events(self) -> EventStream:
        """Live stream of Docker daemon events (for auto-refresh)."""
        return EventStream(self._sdk)

    def stream_pull(self, repository: str, tag: str = "latest") -> PullStream:
        """Progress stream for pulling one image (`docker pull repo:tag`)."""
        return PullStream(repository, tag, self._sdk)

    def list_contexts(self) -> list[ContextInfo]:
        """All configured `docker context`s — for the in-app context switcher.

        `is_current` compares against `self.connection.context` (whatever
        DockSurf is actually connected through right now, override or
        ambient) rather than `ContextAPI.get_current_context()`, which only
        reflects the OS-level ambient context. Works even while disconnected,
        since listing contexts is local disk I/O, not a daemon round-trip.
        """
        try:
            from docker.context import ContextAPI

            contexts = ContextAPI.contexts()
        except Exception as e:
            logger.warning("Failed to list docker contexts: %s", e)
            return []
        return [
            ContextInfo(
                name=ctx.Name,
                host=ctx.Host or _DEFAULT_DOCKER_SOCK,
                is_current=(ctx.Name == self.connection.context),
            )
            for ctx in contexts
        ]

    def image_history(self, image_id: str) -> list[ImageLayer] | None:
        """Layer history for one image (`docker history`) — `None` on error.

        Read-only, so it mirrors `container_top`'s try/except shape rather than
        the write-path `_run_management_op`.
        """
        if not self._sdk:
            return None
        try:
            raw = self._sdk.images.get(image_id).history()
        except NotFound:
            logger.warning("History: image %s not found", image_id)
            return None
        except (APIError, DockerException, requests.exceptions.RequestException) as e:
            logger.warning("History failed for %s: %s", image_id, e)
            return None
        layers: list[ImageLayer] = []
        for entry in raw:
            layers.append(
                ImageLayer(
                    created_by=(entry.get("CreatedBy") or "").strip(),
                    size_bytes=entry.get("Size", 0) or 0,
                    created=str(entry.get("Created", "")),
                )
            )
        return layers

    def volume_sizes(self) -> dict[str, int]:
        """Per-volume on-disk size, keyed by volume name (`docker system df -v`).

        Slow (the daemon walks each volume), so callers run it off the UI thread
        and on-demand only — never on the snapshot path. Returns an empty dict
        if disconnected or the daemon reports no usage data.
        """
        if not self._sdk:
            return {}
        try:
            raw = self._sdk.df()
        except (APIError, DockerException, requests.exceptions.RequestException) as e:
            logger.warning("volume_sizes failed: %s", e)
            return {}
        sizes: dict[str, int] = {}
        for v in raw.get("Volumes") or []:
            name = v.get("Name")
            if name:
                sizes[name] = (v.get("UsageData") or {}).get("Size", 0) or 0
        return sizes

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

    def switch_context(self, name: str) -> CommandResult:
        """Switch DockSurf's own Docker connection to another context.

        In-app only: unlike `docker context use`, this never touches
        `~/.docker/config.json`, so every other terminal keeps whatever
        context it already had. Builds and pings a fresh client scoped to
        the target context *before* touching `self._sdk`/`self.connection`,
        so a failed switch (e.g. an unreachable remote host) leaves the
        current working connection untouched. On success the choice is
        persisted so it survives an app restart.
        """
        try:
            from docker.context import ContextAPI

            ctx = ContextAPI.get_context(name)
        except Exception as e:
            return CommandResult.failure(f"Could not load context '{name}': {e}")
        if ctx is None:
            return CommandResult.failure(
                f"Context '{name}' not found", kind=CommandErrorKind.NOT_FOUND
            )

        try:
            sdk = _build_sdk_client_for_context(ctx)
            sdk.ping()
        except Exception as e:
            logger.warning("Switch to context %s failed: %s", name, e)
            return CommandResult.failure(
                f"Could not connect via context '{name}': {e}",
                kind=CommandErrorKind.DAEMON_UNREACHABLE,
            )

        self._sdk = sdk
        self._fetcher = DockerResourceFetcher(sdk)
        self._context_override = name
        self.connection = ConnectionState(
            status=ConnectionStatus.CONNECTED,
            message="Connected",
            hint="",
            context=ctx.Name,
            host=ctx.Host or _DEFAULT_DOCKER_SOCK,
        )
        _save_last_context(name)
        logger.info("Switched Docker context to %s (%s)", name, ctx.Host)
        return CommandResult.success(f"Switched to context '{name}'")

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

    def tag_image(
        self, image_id: str, repository: str, tag: str = "latest"
    ) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            ok = sdk.images.get(image_id).tag(repository, tag=tag)
            if not ok:
                return CommandResult.failure(
                    f"Docker rejected tag {repository}:{tag}",
                    kind=CommandErrorKind.UNKNOWN,
                )
            return CommandResult.success(f"Tagged {repository}:{tag}")

        return self._run_management_op(op)

    def create_volume(
        self,
        name: str,
        driver: str = "local",
        labels: dict[str, str] | None = None,
    ) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            vol = sdk.volumes.create(
                name=name or None, driver=driver or "local", labels=labels or {}
            )
            return CommandResult.success(f"Created volume {vol.name}")

        return self._run_management_op(op)

    def create_network(
        self, name: str, driver: str = "bridge", subnet: str = ""
    ) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            ipam = None
            if subnet:
                pool = IPAMPool(subnet=subnet)
                ipam = IPAMConfig(pool_configs=[pool])
            net = sdk.networks.create(name=name, driver=driver or "bridge", ipam=ipam)
            return CommandResult.success(f"Created network {net.name}")

        return self._run_management_op(op)

    def connect_container(self, network_name: str, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.networks.get(network_name).connect(container_id),
            "Connected",
        )

    def disconnect_container(
        self, network_name: str, container_id: str, force: bool = True
    ) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.networks.get(network_name).disconnect(
                container_id, force=force
            ),
            "Disconnected",
        )

    def pause_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).pause(), "OK"
        )

    def unpause_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).unpause(), "OK"
        )

    def kill_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).kill(), "OK"
        )

    def prune_containers(self) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            report = sdk.containers.prune()
            deleted = report.get("ContainersDeleted") or []
            reclaimed = report.get("SpaceReclaimed", 0) or 0
            return CommandResult.success(
                f"Pruned {len(deleted)} container(s) — "
                f"reclaimed {format_size(reclaimed)}"
            )

        return self._run_management_op(op)

    def prune_images(self) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            report = sdk.images.prune(filters={"dangling": True})
            deleted = report.get("ImagesDeleted") or []
            reclaimed = report.get("SpaceReclaimed", 0) or 0
            return CommandResult.success(
                f"Pruned {len(deleted)} image(s) — reclaimed {format_size(reclaimed)}"
            )

        return self._run_management_op(op)

    def prune_volumes(self) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            report = sdk.volumes.prune()
            deleted = report.get("VolumesDeleted") or []
            reclaimed = report.get("SpaceReclaimed", 0) or 0
            return CommandResult.success(
                f"Pruned {len(deleted)} volume(s) — reclaimed {format_size(reclaimed)}"
            )

        return self._run_management_op(op)

    def prune_networks(self) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            report = sdk.networks.prune()
            deleted = report.get("NetworksDeleted") or []
            return CommandResult.success(f"Pruned {len(deleted)} network(s)")

        return self._run_management_op(op)

    def prune_system(self) -> CommandResult:
        """Sequential system-wide prune: containers, networks, dangling images,
        then build cache (best-effort — older docker-py/daemons may lack the
        build-cache prune endpoint).

        Matches `docker system prune` without `--volumes` (the CLI's default):
        volumes are left untouched since they can hold data the user wants to
        keep.
        """

        def op(sdk: "docker.DockerClient") -> CommandResult:
            total_deleted = 0
            total_reclaimed = 0

            containers_report = sdk.containers.prune()
            total_deleted += len(containers_report.get("ContainersDeleted") or [])
            total_reclaimed += containers_report.get("SpaceReclaimed", 0) or 0

            networks_report = sdk.networks.prune()
            total_deleted += len(networks_report.get("NetworksDeleted") or [])

            images_report = sdk.images.prune(filters={"dangling": True})
            total_deleted += len(images_report.get("ImagesDeleted") or [])
            total_reclaimed += images_report.get("SpaceReclaimed", 0) or 0

            try:
                build_report = sdk.api.prune_builds()
                total_reclaimed += build_report.get("SpaceReclaimed", 0) or 0
            except Exception:
                logger.debug("Build cache prune unavailable", exc_info=True)

            return CommandResult.success(
                f"System prune: {total_deleted} item(s) removed — "
                f"reclaimed {format_size(total_reclaimed)}"
            )

        return self._run_management_op(op)

    def inspect_resource(self, kind: str, ref: str) -> dict | None:
        """Full raw attrs for one resource — the `docker inspect` escape hatch.

        `kind` matches `_row_key()`'s first element (container/image/volume/
        network) so callers can pass a row key straight through.
        """
        sdk = self._sdk
        if not sdk:
            return None
        dispatch: dict[str, Callable[[], dict]] = {
            "container": lambda: sdk.containers.get(ref).attrs,
            "image": lambda: sdk.images.get(ref).attrs,
            "volume": lambda: sdk.volumes.get(ref).attrs,
            "network": lambda: sdk.networks.get(ref).attrs,
        }
        getter = dispatch.get(kind)
        if getter is None:
            return None
        try:
            return getter()
        except NotFound:
            logger.warning("Inspect: %s %s not found", kind, ref)
            return None
        except (DockerException, requests.exceptions.RequestException) as e:
            logger.warning("Inspect failed for %s %s: %s", kind, ref, e)
            return None

    def container_top(self, container_id: str) -> ContainerTop | None:
        """Running processes for one container (`docker top`) — `None` if the
        container isn't running or the daemon rejects the request."""
        if not self._sdk:
            return None
        try:
            raw = self._sdk.containers.get(container_id).top()
            return ContainerTop(
                titles=raw.get("Titles", []),
                processes=raw.get("Processes", []),
            )
        except NotFound:
            logger.warning("Top: container %s not found", container_id)
            return None
        except (APIError, DockerException, requests.exceptions.RequestException) as e:
            logger.warning("Top failed for %s: %s", container_id, e)
            return None

    def container_cp(self, src: str, dst: str) -> CommandResult:
        """Copy files in/out of a container (`docker cp <src> <dst>`).

        Third sanctioned subprocess exception (see CLAUDE.md): the SDK only
        exposes raw tar archives (`get_archive`/`put_archive`) — reproducing
        `docker cp`'s directory/trailing-slash semantics and safe tar
        extraction by hand is a large correctness/security surface for a
        convenience feature.
        """
        if shutil.which("docker") is None:
            logger.error("docker cp aborted — docker CLI not found on PATH")
            return CommandResult.failure(
                "docker CLI not found on PATH — cannot copy files",
                kind=CommandErrorKind.DAEMON_UNREACHABLE,
            )

        cmd = ["docker", "cp", src, dst]
        logger.info("Running: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except Exception as e:
            logger.exception("docker cp failed (%s -> %s)", src, dst)
            return CommandResult.failure(
                str(e), kind=CommandErrorKind.DAEMON_UNREACHABLE
            )

        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "").strip()
            msg = msg or f"docker cp exited {result.returncode}"
            logger.warning("docker cp failed (%s -> %s): %s", src, dst, msg)
            return CommandResult.failure(msg, kind=CommandErrorKind.UNKNOWN)

        return CommandResult.success(f"Copied {src} → {dst}")

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

    def _run_management_op(
        self, op: Callable[["docker.DockerClient"], CommandResult]
    ) -> CommandResult:
        """Run `op` against the SDK client, classifying any raised exception
        into a `CommandResult.failure`. `op` builds its own success result —
        this is the primitive `_run_management_command` and the prune methods
        (which need to report counts/space reclaimed) both sit on top of.
        """
        sdk = self._sdk
        if not sdk:
            logger.error("Management command skipped — Docker client not initialized")
            return CommandResult.failure(
                "Docker client not initialized",
                kind=CommandErrorKind.DAEMON_UNREACHABLE,
            )
        try:
            return op(sdk)
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
            self.mark_disconnected(e)
            return CommandResult.failure(
                str(e), kind=CommandErrorKind.DAEMON_UNREACHABLE
            )

    def _run_management_command(
        self,
        action_callable: Callable[["docker.DockerClient"], None],
        success_msg: str,
    ) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            action_callable(sdk)
            logger.debug("Management command succeeded")
            return CommandResult.success(success_msg)

        return self._run_management_op(op)


if __name__ == "__main__":
    dc = DockerClient()
    snapshot = dc.fetch_snapshot()  # triggers the lazy connect
    if dc.is_connected:
        print(snapshot)
    else:
        print("Could not connect to Docker daemon")
